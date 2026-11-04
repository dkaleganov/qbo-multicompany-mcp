"""Eight separate promises at the store boundary.

Kept separate here even where the implementation shares code, because each is
a different way a realm -- or every realm -- stops working:

a. one malformed row does not take the other realms down;
b. a consent ceremony begun before a revoke or a newer consent does not
   commit;
c. a persisted guard verdict is bound to the registry name it verified;
d. the lock and store files resist symlinks and lock replacement, and the
   store directory is private;
e. the journal survives faults after replacement, during cleanup and on
   cancellation, with the recovery outcome asserted;
f. two result records for one generation are a lost record, and quarantine;
g. ``auth verify`` is the one deliberate exit from a quarantine;
h. a 4xx from the token endpoint is a refusal, not an ambiguity.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anyio
import httpx
import pytest
from mcp.server import MCPServer

from qbo_multicompany_mcp import auth_cli, keepalive, oauth
from qbo_multicompany_mcp import store as store_module
from qbo_multicompany_mcp.crypto import seal, unseal
from qbo_multicompany_mcp.errors import (
    GuardMismatch,
    NeedsConsentError,
    RefreshUncertainError,
    StaleConsentError,
    StoreError,
)
from qbo_multicompany_mcp.limits import RateLimiter
from qbo_multicompany_mcp.registry import load_registry
from qbo_multicompany_mcp.session import QboSession
from qbo_multicompany_mcp.store import (
    AppCredentials,
    NameMismatch,
    TokenStore,
    canonical_lock,
    write_atomic,
)
from qbo_multicompany_mcp.tools import register_tools

from .conftest import REALM_MAIN, REALM_OPS, make_token_response
from .test_token_store import journal_records, make_record, reopen, write_journal


def seed(store: TokenStore, actor, entity_key: str, realm: str, **kwargs) -> None:
    store.commit_consent(
        entity=entity_key, realm_id=realm, response=make_token_response(**kwargs), actor=actor
    )


def call_tool(settings, registry, name: str, args: dict | None = None):
    server = MCPServer(name="t", version="0")
    register_tools(server, settings, registry)
    return anyio.run(lambda: server.call_tool(name, args or {}))


def _write_document(store: TokenStore, data: dict) -> None:
    payload = json.dumps(data, sort_keys=True).encode("utf-8")
    write_atomic(
        store.document_path,
        seal(store._key_material(), payload, environment=store.environment),
        mode=0o600,
    )


def _raw_document(store: TokenStore) -> dict:
    return json.loads(
        unseal(store._key_material(), store.document_path.read_bytes(), environment=store.environment)
    )


def _corrupt_row(store: TokenStore, realm: str) -> dict:
    """Break one row the way a partial write or a foreign tool would: a
    required key is gone. Returns what was written, for later comparison."""
    raw = _raw_document(store)
    del raw["rows"][realm]["refresh_token"]
    raw["rows"][realm]["access_token"] = "malformed-row-access"
    _write_document(store, raw)
    return raw["rows"][realm]


# --------------------------------------------------------------------------
# a. Malformed row isolation
# --------------------------------------------------------------------------


def test_a_malformed_row_blocks_its_own_realm_and_no_other(
    store: TokenStore, settings, credentials: AppCredentials, actor
) -> None:
    """A malformed row blocks its own entity; unreadable ciphertext is a
    whole-store failure. These are different failures."""
    seed(store, actor, "acme_main", REALM_MAIN)
    seed(store, actor, "acme_ops", REALM_OPS, access_token="ops-access")
    _corrupt_row(store, REALM_MAIN)

    fresh = reopen(settings, credentials)
    healthy = fresh.get_row(REALM_OPS)
    assert healthy is not None and healthy.access_token == "ops-access"

    with pytest.raises(StoreError, match="malformed"):
        fresh.get_row(REALM_MAIN)


def test_a_malformed_row_is_preserved_on_disk_across_another_realms_commit(
    store: TokenStore, settings, credentials: AppCredentials, actor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Isolation must not become deletion: the bytes of a row this build
    cannot read may be a credential a corrected build can."""
    seed(store, actor, "acme_main", REALM_MAIN)
    seed(store, actor, "acme_ops", REALM_OPS)
    broken = _corrupt_row(store, REALM_MAIN)

    monkeypatch.setattr(oauth, "refresh_tokens", lambda **kw: make_token_response(access_token="a2"))
    fresh = reopen(settings, credentials)
    refreshed = fresh.refresh(REALM_OPS, actor=actor, force=True)
    assert refreshed.access_token == "a2"

    on_disk = _raw_document(fresh)
    assert on_disk["rows"][REALM_MAIN] == broken, "the malformed row was rewritten or dropped"


def test_nothing_sends_a_credential_from_a_malformed_row(
    store: TokenStore, settings, credentials: AppCredentials, actor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(store, actor, "acme_main", REALM_MAIN)
    _corrupt_row(store, REALM_MAIN)
    submitted: list[str] = []
    monkeypatch.setattr(
        oauth, "refresh_tokens",
        lambda **kw: (submitted.append(str(kw["refresh_token"])), make_token_response())[1],
    )
    fresh = reopen(settings, credentials)

    with pytest.raises(StoreError, match="malformed"):
        fresh.usable_row(REALM_MAIN, entity_key="acme_main")
    with pytest.raises(StoreError, match="malformed"):
        fresh.refresh(REALM_MAIN, actor=actor, force=True)
    with pytest.raises(StoreError, match="malformed"):
        fresh.update_row(REALM_MAIN, lambda r: r)
    assert submitted == []


def test_a_consent_repairs_a_malformed_row(
    store: TokenStore, settings, credentials: AppCredentials, actor
) -> None:
    """Reconnecting is the documented repair, so it must be possible."""
    seed(store, actor, "acme_main", REALM_MAIN)
    seed(store, actor, "acme_ops", REALM_OPS)
    _corrupt_row(store, REALM_MAIN)

    repaired = reopen(settings, credentials).commit_consent(
        entity="acme_main", realm_id=REALM_MAIN,
        response=make_token_response(access_token="repaired"), actor=actor,
    )
    assert repaired.access_token == "repaired"
    after = reopen(settings, credentials)
    assert after.get_row(REALM_MAIN).access_token == "repaired"
    assert after.get_row(REALM_OPS) is not None
    assert after.read().malformed == {}


def test_diagnostics_name_a_malformed_row_rather_than_calling_it_never_authorized(
    store: TokenStore, settings, credentials: AppCredentials, actor, registry,
    monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    """'never authorized' invites a connect; 'malformed' tells the operator
    what actually happened. The keepalive must not read it as absent either."""
    seed(store, actor, "acme_main", REALM_MAIN)
    seed(store, actor, "acme_ops", REALM_OPS)
    _corrupt_row(store, REALM_MAIN)

    result = call_tool(settings, registry, "list_entities")
    states = {e["entity"]: e["auth_state"] for e in result.structured_content["entities"]}
    assert states == {"acme_main": "malformed", "acme_ops": "authorized"}

    monkeypatch.setenv("QBO_CLIENT_ID", credentials.client_id)
    monkeypatch.setenv("QBO_CLIENT_SECRET", credentials.client_secret)
    monkeypatch.setattr(oauth, "refresh_tokens", lambda **kw: make_token_response())
    code = keepalive.run(settings, registry, argparse.Namespace(force=True))
    out = capsys.readouterr().out
    assert code == 1
    assert "acme_main: malformed" in out
    assert "never authorized" not in out

    auth_cli.cmd_status(settings, registry, argparse.Namespace())
    status = capsys.readouterr().out
    assert "malformed" in status


# --------------------------------------------------------------------------
# b. Stale consent rejection
# --------------------------------------------------------------------------


def test_a_ceremony_begun_before_a_newer_consent_does_not_commit(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor
) -> None:
    """A consent captures the state it began from and commits only if that
    state still holds."""
    baseline = seeded_store.consent_baseline(REALM_MAIN)

    reopen(settings, credentials).commit_consent(
        entity="acme_main", realm_id=REALM_MAIN,
        response=make_token_response(refresh_token="newer-consent"), actor=actor,
    )

    with pytest.raises(StaleConsentError):
        seeded_store.commit_consent(
            entity="acme_main", realm_id=REALM_MAIN,
            response=make_token_response(refresh_token="stale-ceremony"), actor=actor,
            baseline=baseline,
        )
    assert reopen(settings, credentials).get_row(REALM_MAIN).refresh_token == "newer-consent"


def test_a_ceremony_begun_before_a_revoke_does_not_commit(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor
) -> None:
    baseline = seeded_store.consent_baseline(REALM_MAIN)
    reopen(settings, credentials).revoke_row(REALM_MAIN)

    with pytest.raises(StaleConsentError):
        seeded_store.commit_consent(
            entity="acme_main", realm_id=REALM_MAIN,
            response=make_token_response(refresh_token="resurrected"), actor=actor,
            baseline=baseline,
        )
    assert reopen(settings, credentials).get_row(REALM_MAIN) is None


def test_a_first_consent_is_stale_once_another_first_consent_has_landed(
    store: TokenStore, settings, credentials: AppCredentials, actor
) -> None:
    baseline = store.consent_baseline(REALM_MAIN)
    assert baseline.present is False
    reopen(settings, credentials).commit_consent(
        entity="acme_main", realm_id=REALM_MAIN,
        response=make_token_response(refresh_token="first"), actor=actor,
    )
    with pytest.raises(StaleConsentError):
        store.commit_consent(
            entity="acme_main", realm_id=REALM_MAIN,
            response=make_token_response(refresh_token="second"), actor=actor,
            baseline=baseline,
        )


def test_a_refresh_during_the_ceremony_is_not_staleness(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two hazards are a revoke and a newer consent. The keepalive
    refreshing the same grant mid-ceremony is neither: the ceremony is still
    the newest consent, and refusing it would revoke the only fresh grant."""
    baseline = seeded_store.consent_baseline(REALM_MAIN)
    monkeypatch.setattr(oauth, "refresh_tokens", lambda **kw: make_token_response(refresh_token="r2"))
    reopen(settings, credentials).refresh(REALM_MAIN, actor=actor, force=True)

    row = seeded_store.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN,
        response=make_token_response(refresh_token="from-ceremony"), actor=actor,
        baseline=baseline,
    )
    assert row.refresh_token == "from-ceremony"


def test_the_connect_command_stops_and_revokes_when_its_ceremony_went_stale(
    settings, registry, credentials: AppCredentials, actor, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Through the real command: while the human is at the consent screen,
    another consent for the same realm lands. The CLI must not overwrite it,
    and must not leave the grant it just received live and unstored."""
    monkeypatch.setenv("QBO_CLIENT_ID", credentials.client_id)
    monkeypatch.setenv("QBO_CLIENT_SECRET", credentials.client_secret)
    seed(settings.open_store(credentials=credentials), actor, "acme_main", REALM_MAIN)

    captured: dict[str, str] = {}
    real_authorize_url = auth_cli.authorize_url

    def spy_url(*, client_id: str, state: str, redirect_uri: str) -> str:
        captured["state"] = state
        return real_authorize_url(client_id=client_id, state=state, redirect_uri=redirect_uri)

    def fake_open(url: str) -> bool:
        def compete_then_deliver() -> None:
            settings.open_store(credentials=credentials).commit_consent(
                entity="acme_main", realm_id=REALM_MAIN,
                response=make_token_response(refresh_token="competing"), actor=actor,
            )
            httpx.get(
                f"http://127.0.0.1:{settings.callback_port}/callback",
                params={"code": "thecode", "state": captured["state"], "realmId": REALM_MAIN},
                timeout=5,
            )

        threading.Thread(target=compete_then_deliver, daemon=True).start()
        return True

    revoked: list[str] = []
    monkeypatch.setattr(auth_cli, "authorize_url", spy_url)
    monkeypatch.setattr(auth_cli.webbrowser, "open", fake_open)
    monkeypatch.setattr(
        auth_cli.oauth, "exchange_code",
        lambda **kw: make_token_response(refresh_token="from-stale-ceremony", realm_id=REALM_MAIN),
    )
    monkeypatch.setattr(auth_cli.oauth, "revoke_token", lambda **kw: revoked.append(kw["token"]))
    monkeypatch.setattr(auth_cli, "_probe_company_name", lambda *a, **k: "Acme Main")

    code = auth_cli.cmd_connect(settings, registry, argparse.Namespace(entity="acme_main", manual=False))
    out = capsys.readouterr().out

    assert code == 1
    assert "stale" in out.lower()
    assert revoked == ["from-stale-ceremony"], "the unstored grant was left live"
    assert settings.open_store(credentials=credentials).get_row(REALM_MAIN).refresh_token == "competing"


# --------------------------------------------------------------------------
# c. Guard invalidation
# --------------------------------------------------------------------------


def _mismatch(mock_settings, registry, credentials, actor, mock_intuit) -> TokenStore:
    store = mock_settings.open_store(credentials=credentials)
    seed(store, actor, "acme_main", REALM_MAIN)
    mock_intuit.company_name = "Renamed Elsewhere"
    with pytest.raises(GuardMismatch):
        QboSession(mock_settings, registry).ensure_ready(registry["acme_main"])
    row = store.get_row(REALM_MAIN)
    assert row is not None and row.name_mismatch is not None and row.name_mismatch.mismatch is True
    return store


def _corrected_registry(tmp_path: Path, display_name: str):
    path = tmp_path / "corrected.yaml"
    path.write_text(
        "entities:\n"
        "  acme_main:\n"
        f"    display_name: {display_name}\n"
        f'    realm_id: "{REALM_MAIN}"\n'
        "    environment: sandbox\n"
        "  acme_ops:\n"
        "    display_name: Acme Ops\n"
        f'    realm_id: "{REALM_OPS}"\n'
        "    environment: sandbox\n",
        encoding="utf-8",
    )
    return load_registry(path)


def test_a_corrected_display_name_retires_the_persisted_verdict_without_a_request(
    mock_settings, registry, credentials, actor, mock_intuit, tmp_path: Path
) -> None:
    """The operator fixes entities.yaml and restarts. Until a data call
    re-verifies, the request-free diagnostics must not repeat the old verdict
    against a name it never checked."""
    _mismatch(mock_settings, registry, credentials, actor, mock_intuit)
    corrected = _corrected_registry(tmp_path, "Renamed Elsewhere")
    requests_before = len(mock_intuit.requests)

    result = call_tool(mock_settings, corrected, "list_entities")
    entity = {e["entity"]: e for e in result.structured_content["entities"]}["acme_main"]

    assert entity["auth_state"] == "authorized"
    assert entity["company_name_matches_registry"] is None, "a verdict about another name was repeated"
    assert entity["name_mismatch_checked_at"] is None
    assert len(mock_intuit.requests) == requests_before


def test_an_unchanged_display_name_keeps_the_persisted_verdict(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    """The control: the verdict still applies to the name it was checked
    against, or the test above would pass on a server that reports nothing."""
    _mismatch(mock_settings, registry, credentials, actor, mock_intuit)
    result = call_tool(mock_settings, registry, "list_entities")
    entity = {e["entity"]: e for e in result.structured_content["entities"]}["acme_main"]
    assert entity["auth_state"] == "name_mismatch"
    assert entity["company_name_matches_registry"] is False


def test_the_verdict_records_the_registry_name_it_verified(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    store = _mismatch(mock_settings, registry, credentials, actor, mock_intuit)
    verdict = store.get_row(REALM_MAIN).name_mismatch
    assert verdict.registry_fingerprint == store_module.registry_fingerprint("Acme Main")
    assert verdict.applies_to("Acme Main") is True
    assert verdict.applies_to("Renamed Elsewhere") is False
    assert verdict.applies_to("Acme Main ") is False


def test_a_verdict_without_a_fingerprint_is_not_applied(actor) -> None:
    """A row with no fingerprint carries a verdict about an unknown name.
    Unknown is not 'matches this one'."""
    verdict = NameMismatch.from_json(
        {"mismatch": True, "checked_at": "2026-09-01T00:00:00Z", "auth_generation": 1}
    )
    assert verdict is not None
    assert verdict.applies_to("Acme Main") is False


def test_an_older_process_never_overwrites_a_newer_verdict(
    seeded_store: TokenStore,
) -> None:
    """An old process must never overwrite a newer observation. Both verdicts
    are bound to the row's own consent, so what separates them is their clock
    alone."""
    fingerprint = store_module.registry_fingerprint("Acme Main")
    epoch = seeded_store.get_row(REALM_MAIN).consent_epoch
    now = datetime.now(UTC)
    newer = NameMismatch(
        mismatch=False, checked_at=now, auth_generation=1, registry_fingerprint=fingerprint,
        consent_epoch=epoch,
    )
    older = NameMismatch(
        mismatch=True, checked_at=now - timedelta(minutes=5), auth_generation=1,
        registry_fingerprint=fingerprint, consent_epoch=epoch,
    )
    seeded_store.record_guard_verdict(REALM_MAIN, newer)
    seeded_store.record_guard_verdict(REALM_MAIN, older)
    persisted = seeded_store.get_row(REALM_MAIN).name_mismatch
    assert persisted == newer


def test_a_reconsent_clears_the_verdict(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    store = _mismatch(mock_settings, registry, credentials, actor, mock_intuit)
    seed(store, actor, "acme_main", REALM_MAIN)
    assert store.get_row(REALM_MAIN).name_mismatch is None


# --------------------------------------------------------------------------
# d. Filesystem hardening
# --------------------------------------------------------------------------


@pytest.fixture
def permissive_umask():
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def test_the_store_directory_is_created_private_by_the_lock(tmp_path: Path, permissive_umask) -> None:
    """A directory holding refresh tokens is 0700 whatever the umask says."""
    directory = tmp_path / "state" / "nested"
    with canonical_lock(directory / "tokens.lock", timeout=1):
        pass
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "state").stat().st_mode) == 0o700


def test_the_store_directory_is_created_private_by_a_write(tmp_path: Path, permissive_umask) -> None:
    directory = tmp_path / "state"
    write_atomic(directory / "tokens.sandbox.enc", b"x", mode=0o600)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def _open_a_lock(directory: Path) -> None:
    with canonical_lock(directory / "tokens.lock", timeout=1):
        pass


#: Every way the rate limiter or the store can be the first to reach the folder.
_FIRST_TO_THE_FOLDER = {
    "limiter-acquire": lambda d: RateLimiter(d, environment="sandbox").acquire("acme_main"),
    "limiter-429": lambda d: RateLimiter(d, environment="sandbox").note_throttled("acme_main"),
    "limiter-blocked-for": lambda d: RateLimiter(d, environment="sandbox").blocked_for("acme_main"),
    "store-lock": _open_a_lock,
    "store-write": lambda d: write_atomic(d / "tokens.sandbox.enc", b"x", mode=0o600),
}


@pytest.mark.parametrize("umask", [0o000, 0o022], ids=["umask-000", "umask-022"])
@pytest.mark.parametrize("first", list(_FIRST_TO_THE_FOLDER))
def test_a_new_store_folder_is_private_whoever_creates_it(
    tmp_path: Path, first: str, umask: int
) -> None:
    """The rate limiter keeps its file in the store's folder and can reach it
    first. A folder it created under the umask would stay that way, because
    the store's routine leaves a folder that exists alone. So both create it
    through that one routine, in either order."""
    directory = tmp_path / "state" / "nested"
    previous = os.umask(umask)
    try:
        _FIRST_TO_THE_FOLDER[first](directory)
        # Then the rest of a process's life: the limiter (another company, so
        # a block the first step recorded does not refuse it) and the store.
        RateLimiter(directory, environment="sandbox").acquire("acme_ops")
        _open_a_lock(directory)
    finally:
        os.umask(previous)
    modes = {path.name: oct(stat.S_IMODE(path.stat().st_mode)) for path in (directory, directory.parent)}
    assert modes == {"nested": "0o700", "state": "0o700"}


def test_an_existing_folder_keeps_the_mode_its_operator_gave_it(tmp_path: Path) -> None:
    """Like the store's routine, the limiter tightens only a folder it
    creates. Tightening one that already exists is the operator's one-time
    chmod, not something the package does behind their back."""
    directory = tmp_path / "state"
    directory.mkdir()
    os.chmod(directory, 0o755)
    RateLimiter(directory, environment="sandbox").acquire("acme_main")
    _open_a_lock(directory)
    assert oct(stat.S_IMODE(directory.stat().st_mode)) == "0o755"


def test_a_symlinked_lock_path_is_refused(tmp_path: Path) -> None:
    """A lock path that is a symlink is opened nowhere: not followed, not
    created behind."""
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    target = tmp_path / "elsewhere.lock"
    (directory / "tokens.lock").symlink_to(target)

    with pytest.raises(StoreError, match="symlink"):
        with canonical_lock(directory / "tokens.lock", timeout=1):
            pass
    assert not target.exists(), "the symlink was followed and its target created"


def test_a_symlinked_store_file_is_refused(
    seeded_store: TokenStore, settings, credentials: AppCredentials, tmp_path: Path
) -> None:
    moved = tmp_path / "moved.enc"
    seeded_store.document_path.rename(moved)
    seeded_store.document_path.symlink_to(moved)
    with pytest.raises(StoreError, match="symlink"):
        reopen(settings, credentials).read()


def test_a_lock_acquired_on_a_replaced_inode_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the lock file is replaced while a waiter holds a descriptor to the
    old inode, a later flock on that descriptor 'succeeds' on a file nothing
    else can see -- two processes then each hold a lock the other is not
    contending for."""
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    lock_path = directory / "tokens.lock"
    real_flock = store_module.fcntl.flock
    replaced = {"done": False}

    def replacing_flock(fd: int, operation: int):  # noqa: ANN202
        if not replaced["done"] and operation & store_module.fcntl.LOCK_EX:
            replaced["done"] = True
            # The file is replaced between open and flock.
            lock_path.unlink()
            os.close(os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600))
        return real_flock(fd, operation)

    monkeypatch.setattr(store_module.fcntl, "flock", replacing_flock)
    fd = store_module._flock_exclusive(lock_path, timeout=2)
    try:
        assert os.fstat(fd).st_ino == lock_path.stat().st_ino, (
            "the lock was taken on an inode that is no longer the lock file"
        )
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# e. The journal fault matrix: after replacement, during cleanup, on
#    cancellation, and disk-full on each durable write.
# --------------------------------------------------------------------------


def _arm_after_commit(monkeypatch, store: TokenStore, target: str, exc: BaseException, *, when=None):
    """Raise ``exc`` from ``target`` the first time it is called after the
    main store has committed -- the shape of every post-replacement fault."""
    armed = {"now": False}
    original_commit = store._commit_locked
    original_target = getattr(store, target)

    def commit(document):  # noqa: ANN001, ANN202
        result = original_commit(document)
        armed["now"] = True
        return result

    def failing(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        if armed["now"] and (when is None or when(*args, **kwargs)):
            armed["now"] = False
            raise exc
        return original_target(*args, **kwargs)

    monkeypatch.setattr(store, "_commit_locked", commit)
    monkeypatch.setattr(store, target, failing)


def _chain(submitted: list[str]):
    def refresh(**kwargs: object) -> oauth.TokenResponse:
        token = str(kwargs["refresh_token"])
        submitted.append(token)
        number = int(token.rsplit("-", 1)[1])
        return make_token_response(access_token=f"access-{number + 1}", refresh_token=f"refresh-{number + 1}")

    return refresh


def test_a_crash_after_replacement_and_before_cleanup_leaves_a_replay_not_a_second_advance(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After replacement: the store holds generation 2 and the journal still
    holds the result that produced it. Recovery must neither adopt it again
    nor read it as ambiguity."""
    submitted: list[str] = []
    monkeypatch.setattr(oauth, "refresh_tokens", _chain(submitted))
    _arm_after_commit(monkeypatch, seeded_store, "_clear_journal_locked", KeyboardInterrupt("killed after replace"))

    with pytest.raises(KeyboardInterrupt):
        seeded_store.refresh(REALM_MAIN, actor=actor, force=True)
    assert [r.stage for r in journal_records(seeded_store)] == ["result"]

    restarted = reopen(settings, credentials)
    row = restarted.get_row(REALM_MAIN)
    assert row.auth_generation == 2 and row.refresh_token == "refresh-2"
    assert row.refresh_uncertain is False
    assert journal_records(restarted) == []

    restarted.refresh(REALM_MAIN, actor=actor, force=True)
    assert submitted == ["refresh-1", "refresh-2"], "the replay was adopted or the realm stopped"


def test_a_failure_during_cleanup_does_not_fail_a_refresh_whose_pair_is_on_disk(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor,
    monkeypatch: pytest.MonkeyPatch, token_key: str,
) -> None:
    """During cleanup: the pair is durable in the main store; the journal
    clear hits a full disk. The caller gets its token -- and the surviving
    record is a replay the next recovery ignores."""
    monkeypatch.setattr(oauth, "refresh_tokens", lambda **kw: make_token_response(access_token="a2", refresh_token="r2"))
    _arm_after_commit(
        monkeypatch, seeded_store, "_write_journal_locked",
        StoreError("could not write the token store: No space left on device"),
        when=lambda records: records == [],
    )

    row = seeded_store.refresh(REALM_MAIN, actor=actor, force=True)
    assert row.access_token == "a2" and row.auth_generation == 2

    monkeypatch.undo()
    monkeypatch.setenv("QBO_TOKEN_KEY", token_key)
    after = reopen(settings, credentials).get_row(REALM_MAIN)
    assert after.auth_generation == 2 and after.refresh_token == "r2"
    assert after.refresh_uncertain is False


@pytest.mark.parametrize(
    ("point", "outcome"),
    [
        pytest.param("result-write", "quarantined", id="cancelled-writing-the-result"),
        pytest.param("before-commit", "adopted", id="cancelled-before-the-store-commit"),
        pytest.param("during-replace", "adopted", id="cancelled-during-the-store-replace"),
        pytest.param("during-cleanup", "committed", id="cancelled-during-cleanup"),
    ],
)
def test_cancellation_at_each_point_after_the_response_has_a_known_recovery(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor,
    monkeypatch: pytest.MonkeyPatch, token_key: str, point: str, outcome: str,
) -> None:
    """On cancellation: the response arrived and the process was cancelled
    before the pair was safe. Each point has one right recovery, asserted."""
    submitted: list[str] = []
    monkeypatch.setattr(oauth, "refresh_tokens", _chain(submitted))
    cancel = KeyboardInterrupt(f"cancelled at {point}")

    if point == "result-write":
        original = seeded_store._write_journal_locked

        def write(records):  # noqa: ANN001, ANN202
            if records and records[0].stage == "result":
                raise cancel
            return original(records)

        monkeypatch.setattr(seeded_store, "_write_journal_locked", write)
    elif point == "before-commit":
        original_commit = seeded_store._commit_locked
        original_write = seeded_store._write_journal_locked
        armed = {"now": False}

        def write(records):  # noqa: ANN001, ANN202
            original_write(records)
            if records and records[0].stage == "result":
                armed["now"] = True

        def commit(document):  # noqa: ANN001, ANN202
            if armed["now"]:
                raise cancel
            return original_commit(document)

        monkeypatch.setattr(seeded_store, "_write_journal_locked", write)
        monkeypatch.setattr(seeded_store, "_commit_locked", commit)
    elif point == "during-replace":
        real_replace = os.replace

        def replacing(src, dst, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
            if Path(dst) == seeded_store.document_path and submitted:
                raise cancel
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(store_module.os, "replace", replacing)
    else:
        _arm_after_commit(monkeypatch, seeded_store, "_clear_journal_locked", cancel)

    with pytest.raises(KeyboardInterrupt):
        seeded_store.refresh(REALM_MAIN, actor=actor, force=True)
    monkeypatch.undo()
    monkeypatch.setenv("QBO_TOKEN_KEY", token_key)
    monkeypatch.setattr(oauth, "refresh_tokens", _chain(submitted))

    leftovers = [p.name for p in seeded_store.directory.iterdir() if ".tmp." in p.name]
    assert leftovers == [], "a cancelled write left a credential-bearing temp file behind"

    restarted = reopen(settings, credentials)
    row = restarted.get_row(REALM_MAIN)
    assert row is not None

    if outcome == "quarantined":
        assert row.refresh_uncertain is True
        assert row.refresh_token == "refresh-1"
        with pytest.raises(RefreshUncertainError):
            restarted.refresh(REALM_MAIN, actor=actor, force=True)
        assert submitted == ["refresh-1"], "a possibly-spent token was resubmitted"
    else:
        assert row.refresh_uncertain is False
        assert row.auth_generation == 2 and row.refresh_token == "refresh-2"
        assert journal_records(restarted) == []
        restarted.refresh(REALM_MAIN, actor=actor, force=True)
        assert submitted == ["refresh-1", "refresh-2"]


def test_disk_full_on_the_intent_write_submits_nothing_and_stops_nothing(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor,
    monkeypatch: pytest.MonkeyPatch, token_key: str,
) -> None:
    """Before send: the intent cannot be written, so the token is not sent.
    Nothing was submitted, so nothing is ambiguous."""
    submitted: list[str] = []
    monkeypatch.setattr(oauth, "refresh_tokens", _chain(submitted))
    monkeypatch.setattr(
        seeded_store, "_write_journal_locked",
        lambda records: (_ for _ in ()).throw(StoreError("could not write the token store: No space left on device")),
    )
    with pytest.raises(StoreError):
        seeded_store.refresh(REALM_MAIN, actor=actor, force=True)
    assert submitted == []
    monkeypatch.undo()
    monkeypatch.setenv("QBO_TOKEN_KEY", token_key)
    row = reopen(settings, credentials).get_row(REALM_MAIN)
    assert row.refresh_uncertain is False and row.auth_generation == 1


def test_disk_full_on_the_result_write_loses_the_pair_and_says_so(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor,
    monkeypatch: pytest.MonkeyPatch, token_key: str,
) -> None:
    """After receipt, during the result write: the pair Intuit issued never
    reached the disk. The stored token may be spent, which is exactly what
    the surviving intent says on restart."""
    submitted: list[str] = []
    monkeypatch.setattr(oauth, "refresh_tokens", _chain(submitted))
    original = seeded_store._write_journal_locked

    def write(records):  # noqa: ANN001, ANN202
        if records and records[0].stage == "result":
            raise StoreError("could not write the token store: No space left on device")
        return original(records)

    monkeypatch.setattr(seeded_store, "_write_journal_locked", write)
    with pytest.raises(StoreError):
        seeded_store.refresh(REALM_MAIN, actor=actor, force=True)
    monkeypatch.undo()
    monkeypatch.setenv("QBO_TOKEN_KEY", token_key)
    monkeypatch.setattr(oauth, "refresh_tokens", _chain(submitted))

    restarted = reopen(settings, credentials)
    assert restarted.get_row(REALM_MAIN).refresh_uncertain is True
    with pytest.raises(RefreshUncertainError):
        restarted.refresh(REALM_MAIN, actor=actor, force=True)
    assert submitted == ["refresh-1"]


# --------------------------------------------------------------------------
# f. Two result records for one starting generation
# --------------------------------------------------------------------------


def test_two_results_for_one_generation_quarantine_the_realm(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refusing to pick one is right. Discarding both and leaving the row
    usable resubmits the stored token as if nothing happened -- the hazard of
    an unanswered intent, one case over. Two competing results mean the local
    record of which token is live has been lost, and that is what
    refresh_uncertain is for."""
    write_journal(
        seeded_store,
        [
            make_record(seeded_store, credentials, generation_from=1, access_token="one"),
            make_record(seeded_store, credentials, generation_from=1, access_token="two"),
        ],
    )
    restarted = reopen(settings, credentials)
    after = restarted.get_row(REALM_MAIN)
    assert after.access_token == "access-1", "one of two competing results was adopted"
    assert after.refresh_uncertain is True
    assert after.recovery_hint is not None and "journal" in after.recovery_hint

    submitted: list[str] = []
    monkeypatch.setattr(oauth, "refresh_tokens", _chain(submitted))
    with pytest.raises(RefreshUncertainError):
        restarted.refresh(REALM_MAIN, actor=actor, force=True)
    assert submitted == []


# --------------------------------------------------------------------------
# g. The one deliberate exit from a quarantine: auth verify
# --------------------------------------------------------------------------


def _quarantine(store: TokenStore, settings, credentials) -> TokenStore:
    from .test_token_store import make_intent

    write_journal(store, [make_intent(store, credentials, generation_from=1)])
    restarted = reopen(settings, credentials)
    assert restarted.get_row(REALM_MAIN).refresh_uncertain is True
    return restarted


def test_verify_submits_the_stored_token_once_and_clears_the_flag_only_on_success(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deliberate, operator-invoked single submission. Not automatic, so it
    is not the automatic retry the store forbids; not blind, because the token
    was never rejected -- it may merely have been spent."""
    restarted = _quarantine(seeded_store, settings, credentials)
    submitted: list[str] = []
    monkeypatch.setattr(oauth, "refresh_tokens", _chain(submitted))

    outcome = restarted.verify_uncertain(REALM_MAIN, actor=actor, entity_key="acme_main")

    assert outcome.submitted is True
    assert submitted == ["refresh-1"]
    assert outcome.row.refresh_uncertain is False
    assert outcome.row.recovery_hint is None
    assert outcome.row.auth_generation == 2 and outcome.row.refresh_token == "refresh-2"
    assert journal_records(restarted) == []
    assert reopen(settings, credentials).get_row(REALM_MAIN).refresh_uncertain is False


def test_verify_marks_needs_consent_on_invalid_grant_and_never_submits_twice(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restarted = _quarantine(seeded_store, settings, credentials)
    submitted: list[str] = []

    def rejected(**kwargs: object):  # noqa: ANN202
        submitted.append(str(kwargs["refresh_token"]))
        raise oauth.InvalidGrant("Intuit rejected the submitted grant (invalid_grant)")

    monkeypatch.setattr(oauth, "refresh_tokens", rejected)
    with pytest.raises(NeedsConsentError):
        restarted.verify_uncertain(REALM_MAIN, actor=actor, entity_key="acme_main")

    row = reopen(settings, credentials).get_row(REALM_MAIN)
    assert row.needs_consent is True
    assert row.refresh_uncertain is False
    assert submitted == ["refresh-1"]

    with pytest.raises(NeedsConsentError):
        restarted.verify_uncertain(REALM_MAIN, actor=actor, entity_key="acme_main")
    assert submitted == ["refresh-1"], "verify resubmitted a token Intuit already rejected"


def test_verify_leaves_the_quarantine_in_place_when_the_answer_is_lost_again(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restarted = _quarantine(seeded_store, settings, credentials)

    def lost(**kwargs: object):  # noqa: ANN202
        raise oauth.TokenResponseLost("the token request may have been accepted but no response arrived")

    monkeypatch.setattr(oauth, "refresh_tokens", lost)
    with pytest.raises(RefreshUncertainError):
        restarted.verify_uncertain(REALM_MAIN, actor=actor, entity_key="acme_main")
    row = reopen(settings, credentials).get_row(REALM_MAIN)
    assert row.refresh_uncertain is True
    assert row.auth_generation == 1


def test_verify_submits_nothing_for_a_realm_that_is_not_in_quarantine(
    seeded_store: TokenStore, actor, monkeypatch: pytest.MonkeyPatch
) -> None:
    submitted: list[str] = []
    monkeypatch.setattr(oauth, "refresh_tokens", _chain(submitted))
    outcome = seeded_store.verify_uncertain(REALM_MAIN, actor=actor, entity_key="acme_main")
    assert outcome.submitted is False
    assert submitted == []
    assert outcome.row.auth_generation == 1


def test_the_verify_command_asks_first_and_submits_nothing_without_a_yes(
    settings, registry, credentials: AppCredentials, actor, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Deliberately awkward: one entity, an explanation, an interactive
    confirmation. Never a bare 'clear this flag' switch."""
    monkeypatch.setenv("QBO_CLIENT_ID", credentials.client_id)
    monkeypatch.setenv("QBO_CLIENT_SECRET", credentials.client_secret)
    store = settings.open_store(credentials=credentials)
    seed(store, actor, "acme_main", REALM_MAIN)
    _quarantine(store, settings, credentials)

    submitted: list[str] = []
    monkeypatch.setattr(auth_cli.oauth, "refresh_tokens", _chain(submitted))
    monkeypatch.setattr(auth_cli, "_confirm", lambda question: False)

    code = auth_cli.cmd_verify(settings, registry, argparse.Namespace(entity="acme_main"))
    out = capsys.readouterr().out
    assert code == 1
    assert submitted == []
    assert "once" in out.lower()
    assert settings.open_store(credentials=credentials).get_row(REALM_MAIN).refresh_uncertain is True

    monkeypatch.setattr(auth_cli, "_confirm", lambda question: True)
    code = auth_cli.cmd_verify(settings, registry, argparse.Namespace(entity="acme_main"))
    out = capsys.readouterr().out
    assert code == 0
    assert submitted == ["refresh-1"]
    assert "authorized" in out
    assert settings.open_store(credentials=credentials).get_row(REALM_MAIN).refresh_uncertain is False


def test_the_verify_command_takes_exactly_one_entity() -> None:
    parser = auth_cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["verify"])
    with pytest.raises(SystemExit):
        parser.parse_args(["verify", "--all"])
    assert parser.parse_args(["verify", "acme_main"]).handler is auth_cli.cmd_verify


# --------------------------------------------------------------------------
# h. Which token-endpoint outcomes are ambiguous
# --------------------------------------------------------------------------


def _token_client(status: int, body: dict | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body if body is not None else {}, request=request)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("status", [400, 403, 429])
def test_a_4xx_from_the_token_endpoint_is_a_refusal_the_grant_survived(status: int) -> None:
    """ASSUMPTION, recorded in oauth.py: a 4xx is a request that was received
    and refused without the grant being consumed."""
    with _token_client(status) as client, pytest.raises(oauth.OAuthHttpError) as caught:
        oauth.refresh_tokens(
            refresh_token="r", client_id="c", client_secret="s", environment="sandbox", client=client
        )
    assert caught.value.status == status
    assert caught.value.grant_outcome_known is True


@pytest.mark.parametrize("status", [500, 502, 503, 302])
def test_a_5xx_or_other_status_from_the_token_endpoint_is_ambiguous(status: int) -> None:
    with _token_client(status) as client, pytest.raises(oauth.OAuthHttpError) as caught:
        oauth.refresh_tokens(
            refresh_token="r", client_id="c", client_secret="s", environment="sandbox", client=client
        )
    assert caught.value.grant_outcome_known is False


def test_a_429_does_not_quarantine_and_does_not_rotate(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A throttled token endpoint must not strand a realm that was never at
    risk. The stored token is still the live one: report the status, keep
    the token, and let the next refresh submit it."""
    submitted: list[str] = []

    def throttled(**kwargs: object):  # noqa: ANN202
        submitted.append(str(kwargs["refresh_token"]))
        raise oauth.OAuthHttpError(429)

    monkeypatch.setattr(oauth, "refresh_tokens", throttled)
    with pytest.raises(oauth.OAuthHttpError, match="429"):
        seeded_store.refresh(REALM_MAIN, actor=actor, force=True)

    assert journal_records(seeded_store) == [], "the intent survived a definite refusal"
    row = reopen(settings, credentials).get_row(REALM_MAIN)
    assert row.refresh_uncertain is False
    assert row.needs_consent is False
    assert row.auth_generation == 1 and row.refresh_token == "refresh-1"

    monkeypatch.setattr(oauth, "refresh_tokens", _chain(submitted))
    reopen(settings, credentials).refresh(REALM_MAIN, actor=actor, force=True)
    assert submitted == ["refresh-1", "refresh-1"]


def test_a_5xx_quarantines_with_a_hint_that_names_the_status(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted: list[str] = []

    def broken(**kwargs: object):  # noqa: ANN202
        submitted.append(str(kwargs["refresh_token"]))
        raise oauth.OAuthHttpError(503)

    monkeypatch.setattr(oauth, "refresh_tokens", broken)
    with pytest.raises(RefreshUncertainError):
        seeded_store.refresh(REALM_MAIN, actor=actor, force=True)

    row = reopen(settings, credentials).get_row(REALM_MAIN)
    assert row.refresh_uncertain is True
    assert row.recovery_hint is not None and "503" in row.recovery_hint
    assert journal_records(seeded_store) == []

    monkeypatch.setattr(oauth, "refresh_tokens", _chain(submitted))
    with pytest.raises(RefreshUncertainError):
        reopen(settings, credentials).refresh(REALM_MAIN, actor=actor, force=True)
    assert submitted == ["refresh-1"]


def test_a_refresh_whose_client_cant_be_built_is_not_quarantined(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Building the TLS context opens files, a key log the environment names
    among them. Failing there, nothing was sent, so the intent is dropped and
    the stored token stays usable."""

    def unbuildable(**kwargs: object):  # noqa: ANN202
        raise FileNotFoundError("the key log the environment names")

    monkeypatch.setattr(oauth, "isolated_client", unbuildable)
    with pytest.raises(oauth.OAuthError, match="could not build"):
        seeded_store.refresh(REALM_MAIN, actor=actor, force=True)

    assert journal_records(seeded_store) == [], "a refresh that never left was read as submitted"
    row = reopen(settings, credentials).get_row(REALM_MAIN)
    assert row.refresh_uncertain is False
    assert row.auth_generation == 1 and row.refresh_token == "refresh-1"


def test_a_refresh_the_policy_refuses_is_not_quarantined(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token post runs the policy itself before sending, so that refusal
    is a refresh that never left this machine."""
    from qbo_multicompany_mcp.errors import NetworkPolicyViolation

    def refuse(request, ctx):  # noqa: ANN001, ANN202
        raise NetworkPolicyViolation("refused for the test")

    monkeypatch.setattr(oauth, "enforce", refuse)
    with pytest.raises(NetworkPolicyViolation):
        seeded_store.refresh(REALM_MAIN, actor=actor, force=True)

    assert journal_records(seeded_store) == [], "a refused refresh was read as submitted"
    row = reopen(settings, credentials).get_row(REALM_MAIN)
    assert row.refresh_uncertain is False
    assert row.auth_generation == 1 and row.refresh_token == "refresh-1"


def test_a_policy_error_from_the_send_itself_keeps_the_intent(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the token post's own check, before sending, says nothing was sent.
    The store can't tell where any other policy error came from, so it takes
    the cautious reading, an unknown outcome: the company stops until verify."""
    from qbo_multicompany_mcp.errors import NetworkPolicyViolation

    def raises_from_send(*args: object, **kwargs: object):  # noqa: ANN202
        raise NetworkPolicyViolation("raised by send for the test")

    monkeypatch.setattr(oauth, "send", raises_from_send)
    with pytest.raises(NetworkPolicyViolation):
        seeded_store.refresh(REALM_MAIN, actor=actor, force=True)

    assert journal_records(seeded_store) != [], "the intent was cleared on an error that may follow a send"
    row = reopen(settings, credentials).get_row(REALM_MAIN)
    assert row.refresh_uncertain is True


# --------------------------------------------------------------------------
# The credential store: revoke, recovery and the consent epoch
# --------------------------------------------------------------------------


def test_revoking_one_realm_adopts_another_realms_pending_result_first(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor
) -> None:
    """A refresh of acme_main durably journalled its result and died before
    the main store was committed. Revoking acme_ops must not discard that
    result: the journal is recovered under the same lock before the revoke
    commits, so acme_main keeps the pair Intuit issued rather than its old,
    possibly spent one -- with refresh_uncertain False and truthfully so."""
    seed(seeded_store, actor, "acme_ops", REALM_OPS, refresh_token="ops-refresh")
    write_journal(
        seeded_store,
        [
            make_record(
                seeded_store, credentials, generation_from=1, realm_id=REALM_MAIN,
                access_token="issued-access", refresh_token="issued-refresh",
            )
        ],
    )

    revoked = reopen(settings, credentials).revoke_row(REALM_OPS)

    assert revoked is not None and revoked.refresh_token == "ops-refresh"
    survivor = reopen(settings, credentials).get_row(REALM_MAIN)
    assert survivor is not None
    assert survivor.auth_generation == 2
    assert survivor.refresh_token == "issued-refresh"
    assert survivor.refresh_uncertain is False
    assert reopen(settings, credentials).get_row(REALM_OPS) is None


def test_revoking_one_realm_quarantines_another_realms_unanswered_intent(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor
) -> None:
    """An intent with no result for acme_main is the ambiguous outcome.
    Revoking acme_ops must not clear it, which would leave acme_main usable
    with a token that may already have been spent."""
    seed(seeded_store, actor, "acme_ops", REALM_OPS)
    write_journal(
        seeded_store,
        [make_record(seeded_store, credentials, generation_from=1, stage="intent")],
    )

    reopen(settings, credentials).revoke_row(REALM_OPS)

    survivor = reopen(settings, credentials).get_row(REALM_MAIN)
    assert survivor is not None
    assert survivor.refresh_uncertain is True
    assert survivor.auth_generation == 1


def test_a_revoke_drops_only_its_own_realms_journal_records(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor
) -> None:
    """What a revoke owns in the journal is its own realm's records. Recovery
    empties the journal before the revoke runs, so the ownership rule is
    asserted on the helper directly: another realm's record survives it."""
    seed(seeded_store, actor, "acme_ops", REALM_OPS)
    write_journal(
        seeded_store,
        [
            make_record(seeded_store, credentials, generation_from=7, realm_id=REALM_MAIN),
            make_record(seeded_store, credentials, generation_from=7, realm_id=REALM_OPS),
        ],
    )
    with seeded_store.lock():
        seeded_store._drop_journal_records_locked(REALM_OPS)
        remaining = seeded_store._read_journal_locked()
    assert [record.realm_id for record in remaining] == [REALM_MAIN]


def test_a_first_ceremony_is_stale_after_a_consent_and_a_revoke_of_the_same_realm(
    store: TokenStore, settings, credentials: AppCredentials, actor
) -> None:
    """Ceremony A begins with no row. Ceremony B finishes; B is revoked; A
    finishes. Presence plus consented_at cannot see the two intervening
    events, because the store looks exactly as A found it. The consent epoch
    can: every consent and every revoke of a realm advances it, and a ceremony
    commits only against the epoch it began from."""
    baseline = store.consent_baseline(REALM_MAIN)
    other = reopen(settings, credentials)
    other.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN,
        response=make_token_response(refresh_token="ceremony-b"), actor=actor,
    )
    other.revoke_row(REALM_MAIN)
    assert other.get_row(REALM_MAIN) is None

    with pytest.raises(StaleConsentError):
        store.commit_consent(
            entity="acme_main", realm_id=REALM_MAIN,
            response=make_token_response(refresh_token="ceremony-a"), actor=actor,
            baseline=baseline,
        )
    assert reopen(settings, credentials).get_row(REALM_MAIN) is None


def test_the_consent_epoch_advances_on_consent_and_on_revoke_and_not_on_refresh(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The epoch is the consent's identity. A refresh advances the token
    generation and leaves the epoch alone; a consent or a revoke advances
    the epoch, and the epoch outlives the row."""
    first = seeded_store.consent_baseline(REALM_MAIN).epoch
    assert first == seeded_store.get_row(REALM_MAIN).consent_epoch

    monkeypatch.setattr(oauth, "refresh_tokens", lambda **kw: make_token_response(refresh_token="r2"))
    seeded_store.refresh(REALM_MAIN, actor=actor, force=True)
    assert seeded_store.consent_baseline(REALM_MAIN).epoch == first
    assert seeded_store.get_row(REALM_MAIN).auth_generation == 2

    seed(seeded_store, actor, "acme_main", REALM_MAIN)
    assert seeded_store.consent_baseline(REALM_MAIN).epoch == first + 1

    seeded_store.revoke_row(REALM_MAIN)
    assert seeded_store.get_row(REALM_MAIN) is None
    assert reopen(settings, credentials).consent_baseline(REALM_MAIN).epoch == first + 2


def test_a_revoke_against_an_absent_row_advances_the_epoch_and_stops_a_pending_ceremony(
    store: TokenStore, settings, credentials: AppCredentials, actor
) -> None:
    """Every revoke advances the epoch, including a revoke against an absent
    row. Start a ceremony with no row, revoke before any row exists, finish
    the ceremony: it must stop, and the epoch must be on disk so a fresh
    process sees it too."""
    baseline = store.consent_baseline(REALM_MAIN)
    assert store.get_row(REALM_MAIN) is None
    assert store.revoke_row(REALM_MAIN) is None
    assert reopen(settings, credentials).consent_baseline(REALM_MAIN).epoch == baseline.epoch + 1, (
        "a revoke against an absent row did not advance the persisted epoch"
    )
    with pytest.raises(StaleConsentError):
        store.commit_consent(
            entity="acme_main", realm_id=REALM_MAIN,
            response=make_token_response(refresh_token="ceremony-late"), actor=actor,
            baseline=baseline,
        )
    assert reopen(settings, credentials).get_row(REALM_MAIN) is None


def test_a_guard_verdict_from_the_previous_consent_is_not_written_after_a_reconsent(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor
) -> None:
    """A guard pass that began under consent 1 finishes after consent 2
    landed. Its verdict is about a company file the row may no longer denote,
    so it is bound to the consent it checked and refused; timestamp order
    alone (it is the newest verdict) would accept it, and auth_state would
    report a mismatch nobody observed against this consent."""
    before = seeded_store.get_row(REALM_MAIN)
    assert before is not None
    seed(seeded_store, actor, "acme_main", REALM_MAIN, refresh_token="consent-2")
    after = seeded_store.get_row(REALM_MAIN)
    assert after is not None and after.consent_epoch != before.consent_epoch

    late = NameMismatch(
        mismatch=True,
        checked_at=datetime.now(UTC) + timedelta(seconds=1),
        auth_generation=before.auth_generation,
        registry_fingerprint=store_module.registry_fingerprint("Acme Main"),
        consent_epoch=before.consent_epoch,
    )
    seeded_store.record_guard_verdict(REALM_MAIN, late)

    row = reopen(settings, credentials).get_row(REALM_MAIN)
    assert row is not None
    assert row.name_mismatch is None, "a verdict from another consent was written"
    assert row.auth_state(registry_display_name="Acme Main") == "authorized"


def test_a_guard_verdict_from_the_current_consent_is_written(
    seeded_store: TokenStore, settings, credentials: AppCredentials
) -> None:
    """The control for the test above: the same verdict bound to the row's
    own consent is accepted, or the refusal could be a write that never
    works."""
    row = seeded_store.get_row(REALM_MAIN)
    assert row is not None
    verdict = NameMismatch(
        mismatch=True, checked_at=datetime.now(UTC), auth_generation=row.auth_generation,
        registry_fingerprint=store_module.registry_fingerprint("Acme Main"),
        consent_epoch=row.consent_epoch,
    )
    seeded_store.record_guard_verdict(REALM_MAIN, verdict)
    persisted = reopen(settings, credentials).get_row(REALM_MAIN)
    assert persisted is not None and persisted.name_mismatch == verdict
    assert persisted.auth_state(registry_display_name="Acme Main") == "name_mismatch"


def test_a_verdict_bound_to_another_consent_is_never_applied() -> None:
    """For a row written by hand: a persisted verdict whose epoch differs from
    the row's applies to no name."""
    from qbo_multicompany_mcp.store import TokenRow

    expires = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    row = TokenRow.from_json(
        {
            "entity": "acme_main", "environment": "sandbox", "client_id_fingerprint": "f",
            "refresh_token": "r", "access_token": "a",
            "access_token_expires_at": expires,
            "refresh_token_expires_at": expires,
            "consented_at": "2026-01-01T00:00:00Z", "auth_generation": 1,
            "synthetic": True, "consent_epoch": 2,
            "name_mismatch": {
                "mismatch": True, "checked_at": "2026-01-02T00:00:00Z", "auth_generation": 1,
                "registry_fingerprint": store_module.registry_fingerprint("Acme Main"),
                "consent_epoch": 1,
            },
        }
    )
    assert row.guard_verdict_for("Acme Main") is None
    assert row.auth_state(registry_display_name="Acme Main") == "authorized"


def test_a_revoke_returns_the_row_recovery_adopted_not_the_one_it_superseded(
    seeded_store: TokenStore, settings, credentials: AppCredentials
) -> None:
    """A result journalled for the realm being revoked is adopted under the
    lock first, so what revoke_row hands back -- the token the caller then
    revokes at Intuit -- is the pair Intuit issued last, not the one that pair
    superseded. Without the recovery the old row is returned and the record
    dropped, and the newest pair is gone with nobody having revoked it."""
    write_journal(
        seeded_store,
        [make_record(seeded_store, credentials, generation_from=1, refresh_token="issued-last")],
    )
    revoked = reopen(settings, credentials).revoke_row(REALM_MAIN)
    assert revoked is not None
    assert revoked.auth_generation == 2
    assert revoked.refresh_token == "issued-last"
    assert journal_records(reopen(settings, credentials)) == []
