"""The rate-limit file names no company id, and its mode is 0600.

Intuit asks that customer-identifying information not be exposed and that a
company id be stored encrypted. The token store does both. The rate limiter's
shared file, ``ratelimit.<env>.json``, is plaintext, so a full realm id cannot
go in it, and ``Path.write_text`` would let the umask choose its mode. Each
company is filed under its registry entity key; the file is written the way the
token store writes its document, 0600 from creation and atomically; and a file
keyed by realm id is discarded at first contact -- it is only recent call
history -- rather than carried forward. The registry refuses an
entity key that spells a company's id, so a key cannot bring the id back.

A file that cannot be read or written stops the call before a request: a call
it cannot record is invisible to every other process's windows.
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
import time
from pathlib import Path

import httpx
import pytest

from qbo_multicompany_mcp import limits
from qbo_multicompany_mcp.client import AccountingClient
from qbo_multicompany_mcp.errors import StartupError
from qbo_multicompany_mcp.limits import RateLimited, RateLimiter, UpstreamThrottled, Window
from qbo_multicompany_mcp.registry import load_registry

from .conftest import REALM_MAIN, REALM_OPS

#: A fixed wall clock for the limiter-level tests.
NOW = 1_800_000_000.0

QUERY = {"query": "SELECT * FROM Vendor"}

#: A file in another format: keyed by realm id.
REALM_KEYED = {
    REALM_MAIN: {"calls": [NOW, NOW], "blocked_until": NOW + 600},
    REALM_OPS: {"calls": [NOW]},
}


def _keys(value: object) -> list[str]:
    """Every mapping key in a JSON document, at any depth."""
    if isinstance(value, dict):
        return [k for key, item in value.items() for k in (key, *_keys(item))]
    if isinstance(value, list):
        return [k for item in value for k in _keys(item)]
    return []


def _names_no_company_id(path: Path) -> object:
    """The file's document, once it is shown to carry no company id."""
    raw = path.read_text(encoding="utf-8")
    assert REALM_MAIN not in raw and REALM_OPS not in raw, "the rate-limit file names a company id"
    document = json.loads(raw)
    assert [key for key in _keys(document) if re.search(r"[0-9]{5,}", key)] == [], (
        "a key in the rate-limit file carries more of an id than its last four digits"
    )
    return document


def _mode(path: Path) -> str:
    """The permission bits, in octal, so a failure reads 0o644 rather than 420."""
    return oct(stat.S_IMODE(path.stat().st_mode))


def _client(settings, store, status: int = 200) -> tuple[AccountingClient, list[httpx.Request]]:
    reached: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reached.append(request)
        return httpx.Response(status, json={"QueryResponse": {}}, request=request)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return AccountingClient(settings, store, http=http), reached


def _limiter(directory: Path, **kwargs) -> RateLimiter:
    """A limiter on a fake clock that a wait moves forward."""
    now = [NOW]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    return RateLimiter(
        directory, environment="sandbox", clock=lambda: now[0], sleep=sleep, **kwargs
    )


def _place(limiter: RateLimiter, document: object) -> None:
    """A state file as another process left it, at the umask's 0644."""
    limiter.directory.mkdir(parents=True, exist_ok=True)
    limiter.state_path.write_text(json.dumps(document), encoding="utf-8")
    os.chmod(limiter.state_path, 0o644)


# --------------------------------------------------------------------------
# What the file holds: entity keys, never a realm id
# --------------------------------------------------------------------------


def test_a_call_is_filed_under_its_entity_key_never_its_realm_id(
    settings, seeded_store, registry
) -> None:
    client, reached = _client(settings, seeded_store)
    with client:
        client.get(registry["acme_main"], "query", params=QUERY)
    assert len(reached) == 1
    document = _names_no_company_id(client.limiter.state_path)
    assert document["format"] == 2
    assert list(document["entities"]) == ["acme_main"]
    assert len(document["entities"]["acme_main"]["calls"]) == 1


def test_a_429_is_filed_under_the_entity_key_and_refuses_the_next_call_before_a_request(
    settings, seeded_store, registry
) -> None:
    """The 429 path writes the file too, and the block must be found where
    the next call looks for it."""
    client, reached = _client(settings, seeded_store, status=429)
    with client:
        for _ in range(2):
            with pytest.raises(UpstreamThrottled):
                client.get(registry["acme_main"], "query", params=QUERY)
    assert len(reached) == 1, "the recorded block must refuse the second call before a request"
    document = _names_no_company_id(client.limiter.state_path)
    assert list(document["entities"]) == ["acme_main"]
    assert document["entities"]["acme_main"]["blocked_until"] > time.time()


# --------------------------------------------------------------------------
# How it is written: 0600 from creation, atomically, as the store writes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("umask", [0o000, 0o002, 0o022], ids=["umask-000", "umask-002", "umask-022"])
def test_the_file_is_0600_whatever_the_umask(tmp_path: Path, umask: int) -> None:
    """Both writers: a call recorded, and a 429 recorded."""
    limiter = _limiter(tmp_path / "state")
    previous = os.umask(umask)
    try:
        limiter.acquire("acme_main")
        after_call = _mode(limiter.state_path)
        limiter.note_throttled("acme_main")
        after_block = _mode(limiter.state_path)
    finally:
        os.umask(previous)
    assert (after_call, after_block) == ("0o600", "0o600")


def test_the_file_is_written_the_way_the_store_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contents synced, then the rename, then the folder: the order the
    store's own document is held to (test_token_store.py), because it is the
    same routine. And no temp file is left behind."""
    directory = tmp_path / "state"
    limiter = _limiter(directory)
    events: list[tuple[str, str]] = []
    names: dict[int, str] = {}
    real_open, real_fsync, real_replace = os.open, os.fsync, os.replace

    def traced_open(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        fd = real_open(path, *args, **kwargs)
        target = Path(path)
        names[fd] = (
            "folder" if target == directory
            else "temp" if ".tmp." in target.name
            else target.name
        )
        return fd

    def traced_fsync(fd):  # noqa: ANN001
        events.append(("fsync", names.get(fd, "unknown")))
        return real_fsync(fd)

    def traced_replace(src, dst, **kwargs):  # noqa: ANN001, ANN003
        events.append(("replace", f"{Path(src).name.split('.tmp.')[0]} temp -> {Path(dst).name}"))
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "open", traced_open)
    monkeypatch.setattr(os, "fsync", traced_fsync)
    monkeypatch.setattr(os, "replace", traced_replace)
    limiter.acquire("acme_main")

    assert events == [
        ("fsync", "temp"),
        ("replace", "ratelimit.sandbox.json temp -> ratelimit.sandbox.json"),
        ("fsync", "folder"),
    ]
    assert sorted(p.name for p in directory.iterdir()) == ["ratelimit.lock", "ratelimit.sandbox.json"]


def test_a_symlink_at_the_state_path_is_replaced_never_followed(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("{}", encoding="utf-8")
    limiter = _limiter(directory)
    limiter.state_path.symlink_to(elsewhere)

    limiter.acquire("acme_main")

    assert elsewhere.read_text(encoding="utf-8") == "{}", "the write followed the link"
    assert not limiter.state_path.is_symlink()
    assert _mode(limiter.state_path) == "0o600"


# --------------------------------------------------------------------------
# A realm-keyed file: discarded, never carried forward
# --------------------------------------------------------------------------


def test_a_realm_keyed_file_is_discarded_at_the_first_call(tmp_path: Path) -> None:
    """Migrating would mean mapping each realm back to its entity, which the
    limiter cannot do and need not: the file is a minute of call history and
    any 429 block. Discarding costs at most one request that rediscovers a
    block; keeping it would keep the ids."""
    limiter = _limiter(tmp_path / "state", windows=(Window(60.0, 2),))
    _place(limiter, REALM_KEYED)

    limiter.acquire("acme_main")  # neither the old block nor the old window applies

    assert _names_no_company_id(limiter.state_path) == {
        "format": 2,
        "entities": {"acme_main": {"calls": [NOW]}},
    }
    assert _mode(limiter.state_path) == "0o600"


def test_a_first_contact_that_records_nothing_still_rewrites_a_realm_keyed_file(
    tmp_path: Path,
) -> None:
    """blocked_for only reads. The realm-keyed file does not survive it."""
    limiter = _limiter(tmp_path / "state")
    _place(limiter, REALM_KEYED)

    assert limiter.blocked_for("acme_main") == 0.0

    assert _names_no_company_id(limiter.state_path) == {"format": 2, "entities": {}}
    assert _mode(limiter.state_path) == "0o600"


def test_an_entry_beside_the_entities_is_dropped_at_the_next_contact(tmp_path: Path) -> None:
    """A top-level entry beside ``entities`` is outside the format. The next
    contact drops it -- even one that records nothing, because the company is
    blocked."""
    limiter = _limiter(tmp_path / "state")
    current = {"format": 2, "entities": {"acme_main": {"calls": [], "blocked_until": NOW + 60}}}
    _place(limiter, {**current, REALM_MAIN: {"calls": [NOW]}})

    with pytest.raises(UpstreamThrottled):
        limiter.acquire("acme_main")

    assert _names_no_company_id(limiter.state_path) == current


def test_a_file_in_any_other_format_is_discarded(tmp_path: Path) -> None:
    """Only the current format is read: a file this build cannot vouch for is
    history it does without, whatever it holds."""
    limiter = _limiter(tmp_path / "state")
    _place(limiter, {"format": 3, "entities": {"acme_main": {"calls": [], "blocked_until": NOW + 60}}})

    limiter.acquire("acme_main")

    assert _names_no_company_id(limiter.state_path) == {
        "format": 2,
        "entities": {"acme_main": {"calls": [NOW]}},
    }


# --------------------------------------------------------------------------
# A file that cannot be read or written: the call is not sent
# --------------------------------------------------------------------------


def _unwritable(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError(errno.EROFS, "read-only file system")

    monkeypatch.setattr(limits, "write_atomic", refuse)


def test_a_call_the_file_cannot_record_is_never_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a healthy file the eleventh call in a second has to wait. With a
    file that cannot be written, every call would go uncounted and the window
    would never fill, so none of them is sent."""
    limiter = _limiter(tmp_path / "state", windows=(Window(1.0, 10),))
    _unwritable(monkeypatch)
    for _ in range(11):
        with pytest.raises(RateLimited, match="could not be read or written"):
            limiter.acquire("acme_main")


def test_a_file_that_cannot_be_read_is_not_taken_as_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty state is permission to send. A file that cannot be read refuses
    the call, and the history in it is left as it was."""
    limiter = _limiter(tmp_path / "state")
    history = {"format": 2, "entities": {"acme_main": {"calls": [NOW]}}}
    _place(limiter, history)
    real_read_text = Path.read_text

    def unreadable(self: Path, *args, **kwargs):  # noqa: ANN002, ANN003
        if self == limiter.state_path:
            raise PermissionError(errno.EACCES, "permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(RateLimited, match="could not be read or written"):
        limiter.acquire("acme_main")
    assert json.loads(real_read_text(limiter.state_path, encoding="utf-8")) == history


def test_a_429_the_file_cannot_record_still_stops_the_next_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The block cannot be shared through a file that cannot be written. The
    429 itself is still reported, and no call is sent past it, because one the
    file cannot record is not sent either."""
    limiter = _limiter(tmp_path / "state")
    _unwritable(monkeypatch)
    assert limiter.note_throttled("acme_main") >= 60.0
    with pytest.raises(RateLimited, match="could not be read or written"):
        limiter.acquire("acme_main")


@pytest.mark.parametrize("content", [b"{not json", b"\xff\xfe"], ids=["not JSON", "not text"])
def test_a_corrupt_file_is_rebuilt_not_refused(tmp_path: Path, content: bytes) -> None:
    """Corrupt is not unreadable. The file is rebuilt in the current format and
    the call goes ahead, which forgets the last minute's calls and any 429
    block it recorded."""
    limiter = _limiter(tmp_path / "state")
    limiter.directory.mkdir(parents=True)
    limiter.state_path.write_bytes(content)

    limiter.acquire("acme_main")

    assert _names_no_company_id(limiter.state_path) == {
        "format": 2,
        "entities": {"acme_main": {"calls": [NOW]}},
    }


# --------------------------------------------------------------------------
# The registry: an entity key may not carry a company id
# --------------------------------------------------------------------------


def _registry_file(tmp_path: Path, main_key: str) -> Path:
    path = tmp_path / "entities.yaml"
    path.write_text(
        "entities:\n"
        f'  "{main_key}":\n'
        "    display_name: Acme Main\n"
        f'    realm_id: "{REALM_MAIN}"\n'
        "    environment: sandbox\n"
        '  "acme_ops":\n'
        "    display_name: Acme Ops\n"
        f'    realm_id: "{REALM_OPS}"\n'
        "    environment: sandbox\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    "key",
    [REALM_MAIN, f"acme_{REALM_MAIN}", f"main_{REALM_OPS}_copy"],
    ids=["the-id-itself", "its-own-id-inside", "another-entitys-id-inside"],
)
def test_an_entity_key_that_carries_a_company_id_is_refused(tmp_path: Path, key: str) -> None:
    """The key goes wherever the company is named -- every result, every
    error, the rate-limit file -- so a key that spells an id would take the id
    with it. The refusal names the key with the id cut to its last four."""
    with pytest.raises(StartupError, match="realm_id") as caught:
        load_registry(_registry_file(tmp_path, key))
    text = str(caught.value)
    assert REALM_MAIN not in text and REALM_OPS not in text


def test_an_entity_key_may_carry_the_last_four_digits(tmp_path: Path) -> None:
    """The last four are what every result prints anyway."""
    registry = load_registry(_registry_file(tmp_path, "acme_0001"))
    assert registry["acme_0001"].realm_id == REALM_MAIN
