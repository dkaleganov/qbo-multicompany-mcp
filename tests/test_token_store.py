"""Proves the properties the refresh chain depends on.

A refresh may replace the refresh token, and once a new one has been issued
the previous one has to be treated as dead (Intuit's Authorization FAQ; the
rotation model, and Intuit's own disagreement about an overlap, are written up
at the top of store.py). So this package can rely on at most one credential
per realm at a time. Each test below names the way that single credential
could be lost, and shows that it is not.
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from qbo_multicompany_mcp import oauth, store as store_module
from qbo_multicompany_mcp.crypto import KeySource, seal, unseal
from qbo_multicompany_mcp.errors import (
    ConfigurationError,
    NeedsConsentError,
    PlaintextStoreRefused,
    RefreshUncertainError,
    StoreError,
    StoreLocationRefused,
    StoreLockTimeout,
)
from qbo_multicompany_mcp.store import (
    AppCredentials,
    JournalRecord,
    RefreshActor,
    TokenStore,
    canonical_lock,
    write_atomic,
)

from .conftest import REALM_MAIN, REALM_OPS, make_token_response


# --------------------------------------------------------------------------
# The canonical lock
# --------------------------------------------------------------------------


def test_every_mutation_takes_the_canonical_lock(
    seeded_store: TokenStore, actor: RefreshActor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refresh, consent, import, revoke and guard-state writes all lock.

    A "small" write that skipped the lock would read a document, be overtaken
    by a refresh, and write back the pre-refresh token pair.
    """
    taken: list[str] = []
    real_lock = store_module.canonical_lock

    def counting_lock(path, timeout=None):
        taken.append(str(path))
        return real_lock(path, timeout) if timeout is not None else real_lock(path)

    monkeypatch.setattr(store_module, "canonical_lock", counting_lock)

    monkeypatch.setattr(
        oauth, "refresh_tokens", lambda **kw: make_token_response(access_token="a2")
    )

    seeded_store.refresh(REALM_MAIN, actor=actor, force=True)
    seeded_store.update_row(REALM_MAIN, lambda row: replace(row, needs_consent=False))
    seeded_store.commit_consent(
        entity="acme_ops", realm_id=REALM_OPS, response=make_token_response(), actor=actor
    )
    seeded_store.revoke_row(REALM_OPS)
    seeded_store.read()

    assert len(taken) >= 5
    assert {os.path.basename(p) for p in taken} == {"tokens.lock"}


def test_lock_is_exclusive_across_threads(tmp_path: Path) -> None:
    lock_path = tmp_path / "tokens.lock"
    order: list[str] = []
    inside = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with canonical_lock(lock_path, timeout=5):
            order.append("holder-in")
            inside.set()
            release.wait(5)
            order.append("holder-out")

    thread = threading.Thread(target=holder)
    thread.start()
    inside.wait(5)

    with pytest.raises(StoreLockTimeout):
        with canonical_lock(lock_path, timeout=0.2):
            order.append("intruder")

    release.set()
    thread.join(5)
    assert order == ["holder-in", "holder-out"]


def test_lock_wait_outlasts_an_oauth_post_plus_the_write() -> None:
    """A caller must not time out on a lock a healthy refresh is about to
    release -- that would report a busy store as a credential failure."""
    from qbo_multicompany_mcp.constants import (
        OAUTH_POST_TIMEOUT_SECONDS,
        STORE_WRITE_BUDGET_SECONDS,
    )

    assert store_module.DEFAULT_LOCK_TIMEOUT >= (
        OAUTH_POST_TIMEOUT_SECONDS + STORE_WRITE_BUDGET_SECONDS
    )


def test_lock_timeout_is_transient_not_a_credential_failure() -> None:
    assert issubclass(StoreLockTimeout, StoreError)
    assert not issubclass(StoreLockTimeout, NeedsConsentError)


def test_lock_is_reentrant_within_one_call_stack(tmp_path: Path) -> None:
    lock_path = tmp_path / "tokens.lock"
    with canonical_lock(lock_path, timeout=2):
        with canonical_lock(lock_path, timeout=2):
            pass


def test_a_store_mutation_that_calls_another_does_not_wait_on_its_own_lock(
    seeded_store: TokenStore,
) -> None:
    """The reentrant case through the store itself, not the bare lock. A
    second ``flock`` from this process on a second descriptor conflicts with
    the first (canonical_lock's docstring), so without the reentrant mutex the
    inner mutation would wait on its own lock. A short lock timeout makes that
    a StoreLockTimeout, and the join timeout turns any other hang into a
    failure instead of a stuck suite. Afterwards the lock must be free for
    another thread."""
    seeded_store.lock_timeout = 2.0
    outcome: dict[str, object] = {}

    def outer_mutation() -> None:
        def mutate(row):
            # A second mutation from inside the first one's critical section.
            outcome["inner"] = seeded_store.update_row(REALM_MAIN, lambda inner: inner)
            return row

        try:
            seeded_store.update_row(REALM_MAIN, mutate)
            outcome["done"] = True
        except BaseException as exc:  # noqa: BLE001 -- reported below
            outcome["error"] = exc

    worker = threading.Thread(target=outer_mutation, daemon=True)
    worker.start()
    worker.join(timeout=20)
    assert not worker.is_alive(), "a store mutation that calls another hung on its own lock"
    assert outcome.get("done") is True, f"nested mutation failed: {outcome.get('error')!r}"
    assert outcome.get("inner") is not None

    other: dict[str, object] = {}

    def later_mutation() -> None:
        try:
            seeded_store.update_row(REALM_MAIN, lambda row: row)
            other["done"] = True
        except BaseException as exc:  # noqa: BLE001
            other["error"] = exc

    follower = threading.Thread(target=later_mutation, daemon=True)
    follower.start()
    follower.join(timeout=20)
    assert other.get("done") is True, f"the lock was not released: {other.get('error')!r}"


# --------------------------------------------------------------------------
# Write path and durability
# --------------------------------------------------------------------------


def test_write_creates_the_file_at_the_protection_mode(tmp_path: Path) -> None:
    target = tmp_path / "tokens.sandbox.enc"
    write_atomic(target, b"payload", mode=0o600)
    assert oct(target.stat().st_mode)[-3:] == "600"


def test_write_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    target = tmp_path / "tokens.sandbox.enc"
    write_atomic(target, b"payload", mode=0o600)
    assert [p.name for p in tmp_path.iterdir()] == ["tokens.sandbox.enc"]


def test_write_atomic_syncs_the_contents_before_replacing_and_the_directory_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ORDER is the durability guarantee, so the order is what is asserted.

    Checking the file's contents afterwards proves nothing about durability: a
    ``write_atomic`` with both ``fsync`` calls deleted produces byte-identical
    results on a machine that does not lose power. What distinguishes the two
    is the sequence of filesystem calls, so this test records them.

    Contents synced before the rename, or a crash can publish a directory entry
    pointing at an inode whose bytes never reached the disk. Directory synced
    after it, or the rename itself is not durable and the entry can still point
    at the old inode.
    """
    target = tmp_path / "tokens.sandbox.enc"
    events: list[tuple[str, str]] = []
    names: dict[int, str] = {}

    real_open, real_fsync, real_replace = os.open, os.fsync, os.replace

    def traced_open(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        fd = real_open(path, *args, **kwargs)
        name = "directory" if Path(path) == tmp_path else "temp"
        names[fd] = name
        return fd

    def traced_fsync(fd):  # noqa: ANN001
        events.append(("fsync", names.get(fd, "unknown")))
        return real_fsync(fd)

    def traced_replace(src, dst, **kwargs):  # noqa: ANN001, ANN003
        events.append(("replace", "temp->final"))
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "open", traced_open)
    monkeypatch.setattr(os, "fsync", traced_fsync)
    monkeypatch.setattr(os, "replace", traced_replace)
    try:
        write_atomic(target, b"payload", mode=0o600)
    finally:
        monkeypatch.undo()

    assert events == [
        ("fsync", "temp"),
        ("replace", "temp->final"),
        ("fsync", "directory"),
    ]
    assert target.read_bytes() == b"payload"


def test_a_store_commit_goes_through_the_same_durable_write(
    seeded_store: TokenStore, actor: RefreshActor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordering above is only worth anything if the store actually uses it."""
    calls: list[str] = []
    real_write = store_module.write_atomic

    def traced(path, data, *, mode):  # noqa: ANN001
        calls.append(Path(path).name)
        return real_write(path, data, mode=mode)

    monkeypatch.setattr(store_module, "write_atomic", traced)
    monkeypatch.setattr(
        oauth, "refresh_tokens", lambda **kw: make_token_response(access_token="a2")
    )
    seeded_store.refresh(REALM_MAIN, actor=actor, force=True)

    assert any(name.startswith("tokens.sandbox.journal") for name in calls)
    assert any(name == "tokens.sandbox.enc" for name in calls)


def test_disk_full_discards_the_token_and_keeps_the_old_file(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor: RefreshActor,
    token_key: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persist-before-use: a failed write must raise, not return a token.

    A token used but not persisted vanishes on the next crash and takes the
    chain with it, because its predecessor has already been spent.
    """
    before = seeded_store.get_row(REALM_MAIN)
    assert before is not None

    monkeypatch.setattr(
        oauth, "refresh_tokens", lambda **kw: make_token_response(access_token="never-used")
    )

    real_write = os.write
    state = {"armed": False}

    def failing_write(fd: int, data):  # noqa: ANN001
        if state["armed"]:
            raise OSError(28, "No space left on device")
        return real_write(fd, data)

    # Let the journal write succeed; fail the main store commit.
    original_commit = seeded_store._commit_locked

    def commit(document):
        state["armed"] = True
        try:
            return original_commit(document)
        finally:
            state["armed"] = False

    monkeypatch.setattr(os, "write", failing_write)
    monkeypatch.setattr(seeded_store, "_commit_locked", commit)

    with pytest.raises(StoreError):
        seeded_store.refresh(REALM_MAIN, actor=actor, force=True)

    monkeypatch.undo()
    # `undo` also drops the key the fixture put in the environment, and the
    # reopened store below needs it.
    monkeypatch.setenv("QBO_TOKEN_KEY", token_key)

    # The file on disk is untouched: the failed write published nothing. Read
    # it directly rather than through the store, because a store read runs
    # journal recovery, and recovery is the next assertion rather than this one.
    with seeded_store.lock():
        on_disk = seeded_store._load_locked().rows[REALM_MAIN]
    assert on_disk.access_token == before.access_token
    assert on_disk.auth_generation == before.auth_generation

    # And the pair is not lost: the journal holds it, which is the entire point
    # of writing the journal first, so the next read adopts it.
    recovered = reopen(settings, credentials).get_row(REALM_MAIN)
    assert recovered is not None
    assert recovered.access_token == "never-used"
    assert recovered.auth_generation == before.auth_generation + 1


def test_temp_file_is_removed_when_the_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "tokens.sandbox.enc"
    target.write_bytes(b"old")

    def failing_write(fd: int, data):  # noqa: ANN001
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "write", failing_write)
    with pytest.raises(StoreError):
        write_atomic(target, b"new", mode=0o600)
    monkeypatch.undo()

    assert target.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["tokens.sandbox.enc"]


# --------------------------------------------------------------------------
# Generation adoption
# --------------------------------------------------------------------------


def journal_records(store: TokenStore) -> list[JournalRecord]:
    with store.lock():
        return store._read_journal_locked()


def write_journal(store: TokenStore, records: list[JournalRecord]) -> None:
    with store.lock():
        store._write_journal_locked(records)


def make_record(
    store: TokenStore,
    credentials: AppCredentials,
    *,
    generation_from: int,
    realm_id: str = REALM_MAIN,
    environment: str = "sandbox",
    fingerprint: str | None = None,
    access_token: str = "adopted-access",
    refresh_token: str = "adopted-refresh",
    stage: str = "result",
) -> JournalRecord:
    now = datetime.now(UTC)
    return JournalRecord(
        op_id="0" * 32,
        stage=stage,
        realm_id=realm_id,
        environment=environment,
        client_id_fingerprint=fingerprint or credentials.fingerprint,
        generation_from=generation_from,
        old_refresh_digest=oauth.token_digest("refresh-1"),
        written_at=now,
        tokens={
            "access_token": access_token,
            "refresh_token": refresh_token,
            "access_token_expires_at": (now + timedelta(hours=1)).isoformat(),
            "refresh_token_expires_at": (now + timedelta(days=100)).isoformat(),
        },
    )


def make_intent(
    store: TokenStore,
    credentials: AppCredentials,
    *,
    generation_from: int,
    realm_id: str = REALM_MAIN,
    submitted_refresh_token: str = "refresh-1",
) -> JournalRecord:
    """A real intent: written before the token call, carrying no tokens.

    ``make_record(stage="intent")`` still attaches a token pair, which no real
    intent ever has. The distinction matters for recovery: an intent is the
    record that a refresh was *submitted*, and its whole content is that fact.
    """
    return JournalRecord(
        op_id="1" * 32,
        stage="intent",
        realm_id=realm_id,
        environment="sandbox",
        client_id_fingerprint=credentials.fingerprint,
        generation_from=generation_from,
        old_refresh_digest=oauth.token_digest(submitted_refresh_token),
        written_at=datetime.now(UTC),
        tokens=None,
    )


def reopen(settings, credentials: AppCredentials) -> TokenStore:
    """A fresh store object, as a new process would have."""
    return settings.open_store(credentials=credentials)


def test_journal_is_adopted_for_the_immediately_applicable_generation(
    seeded_store: TokenStore, settings, credentials: AppCredentials
) -> None:
    row = seeded_store.get_row(REALM_MAIN)
    assert row is not None and row.auth_generation == 1

    write_journal(seeded_store, [make_record(seeded_store, credentials, generation_from=1)])

    adopted = reopen(settings, credentials).get_row(REALM_MAIN)
    assert adopted is not None
    assert adopted.access_token == "adopted-access"
    assert adopted.auth_generation == 2


def test_adoption_accepts_an_unchanged_refresh_token(
    seeded_store: TokenStore, settings, credentials: AppCredentials
) -> None:
    """Adoption is decided by generation, never by comparing token strings.

    An unchanged refresh token can legitimately accompany a new access token
    and a new expiry. Code that adopted only when the refresh token differed
    would silently drop exactly those responses.
    """
    write_journal(
        seeded_store,
        [
            make_record(
                seeded_store,
                credentials,
                generation_from=1,
                access_token="fresh-access",
                refresh_token="refresh-1",  # identical to what is stored
            )
        ],
    )

    adopted = reopen(settings, credentials).get_row(REALM_MAIN)
    assert adopted is not None
    assert adopted.refresh_token == "refresh-1"
    assert adopted.access_token == "fresh-access"
    assert adopted.auth_generation == 2


def test_stale_journal_replay_is_rejected(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor: RefreshActor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A record whose generation is behind the row has already been applied."""
    monkeypatch.setattr(
        oauth,
        "refresh_tokens",
        lambda **kw: make_token_response(access_token="live", refresh_token="refresh-2"),
    )
    seeded_store.refresh(REALM_MAIN, actor=actor, force=True)
    current = seeded_store.get_row(REALM_MAIN)
    assert current is not None and current.auth_generation == 2

    write_journal(
        seeded_store,
        [make_record(seeded_store, credentials, generation_from=1, access_token="stale")],
    )

    after = reopen(settings, credentials).get_row(REALM_MAIN)
    assert after is not None
    assert after.access_token == "live"
    assert after.auth_generation == 2


def test_ambiguous_journal_records_are_rejected(
    seeded_store: TokenStore, settings, credentials: AppCredentials
) -> None:
    """Two records claiming the same starting generation: nothing here can say
    which one Intuit honoured, so neither is adopted.

    What happens next -- the realm stops, because the local record of which
    token is live has been lost -- is asserted in
    ``tests/test_store_lifecycle.py``."""
    write_journal(
        seeded_store,
        [
            make_record(seeded_store, credentials, generation_from=1, access_token="one"),
            make_record(seeded_store, credentials, generation_from=1, access_token="two"),
        ],
    )
    after = reopen(settings, credentials).get_row(REALM_MAIN)
    assert after is not None
    assert after.access_token == "access-1"
    assert after.refresh_uncertain is True


def test_cross_realm_journal_record_is_rejected(
    seeded_store: TokenStore, settings, credentials: AppCredentials
) -> None:
    write_journal(
        seeded_store,
        [make_record(seeded_store, credentials, generation_from=1, realm_id=REALM_OPS)],
    )
    after = reopen(settings, credentials).get_row(REALM_MAIN)
    assert after is not None
    assert after.access_token == "access-1"


def test_journal_record_for_another_app_is_rejected(
    seeded_store: TokenStore, settings, credentials: AppCredentials
) -> None:
    write_journal(
        seeded_store,
        [make_record(seeded_store, credentials, generation_from=1, fingerprint="f" * 64)],
    )
    after = reopen(settings, credentials).get_row(REALM_MAIN)
    assert after is not None
    assert after.access_token == "access-1"


def test_journal_record_from_another_environment_is_rejected(
    seeded_store: TokenStore, settings, credentials: AppCredentials
) -> None:
    write_journal(
        seeded_store,
        [
            make_record(
                seeded_store, credentials, generation_from=1, environment="production"
            )
        ],
    )
    after = reopen(settings, credentials).get_row(REALM_MAIN)
    assert after is not None
    assert after.access_token == "access-1"


def test_an_unresolved_intent_quarantines_the_realm_rather_than_resubmitting(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor: RefreshActor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An intent with no result is an ambiguous outcome, and must stop.

    The intent records that a refresh was *submitted*. It cannot distinguish
    "the request never left" from "Intuit accepted it and the response was
    lost" -- and it must not resolve that ambiguity by assuming the cheaper one.
    Resubmitting a token that was in fact spent draws ``invalid_grant``, which
    reads as a dead credential and sends a human through the consent screen.

    Asserting only that an intent does not replace the access token would
    distinguish nothing: that is true of the safe behaviour and of the unsafe
    one alike.
    """
    write_journal(seeded_store, [make_intent(seeded_store, credentials, generation_from=1)])

    restarted = reopen(settings, credentials)
    after = restarted.get_row(REALM_MAIN)
    assert after is not None
    # The intent carried no tokens, so of course nothing was adopted...
    assert after.access_token == "access-1"
    assert after.refresh_token == "refresh-1"
    # ...but the ambiguity itself was preserved, which is the point.
    assert after.refresh_uncertain is True
    assert after.recovery_hint is not None and "connect" in after.recovery_hint

    submitted: list[str] = []

    def must_not_be_called(**kwargs: object):  # noqa: ANN202
        submitted.append(str(kwargs["refresh_token"]))
        return make_token_response()

    monkeypatch.setattr(oauth, "refresh_tokens", must_not_be_called)
    with pytest.raises(RefreshUncertainError):
        restarted.refresh(REALM_MAIN, actor=actor, force=True)
    assert submitted == [], "a possibly-spent refresh token was resubmitted"


def test_a_crash_between_the_intent_and_the_response_quarantines_the_realm(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor: RefreshActor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same thing, produced the way it actually happens.

    The process writes its intent, hands the token to Intuit, and dies before it
    can record the answer -- so nothing local says whether the token was spent.
    """

    def die_mid_flight(**kwargs: object):  # noqa: ANN202
        raise KeyboardInterrupt("the process was killed with the request in flight")

    monkeypatch.setattr(oauth, "refresh_tokens", die_mid_flight)
    with pytest.raises(KeyboardInterrupt):
        seeded_store.refresh(REALM_MAIN, actor=actor, force=True)

    # What the dead process left behind: an intent, and an untouched row.
    records = journal_records(seeded_store)
    assert [r.stage for r in records] == ["intent"]

    restarted = reopen(settings, credentials)
    after = restarted.get_row(REALM_MAIN)
    assert after is not None
    assert after.refresh_uncertain is True

    submitted: list[str] = []

    def must_not_be_called(**kwargs: object):  # noqa: ANN202
        submitted.append(str(kwargs["refresh_token"]))
        return make_token_response()

    monkeypatch.setattr(oauth, "refresh_tokens", must_not_be_called)
    with pytest.raises(RefreshUncertainError):
        restarted.refresh(REALM_MAIN, actor=actor, force=True)
    assert submitted == []


def test_a_consent_clears_a_quarantine(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor: RefreshActor,
) -> None:
    """Re-consent is a documented way out of `refresh_uncertain`, and since
    recovery can set that flag, it is worth asserting rather than assuming.

    The other way out is ``auth verify``, asserted in
    ``tests/test_store_lifecycle.py``.
    """
    write_journal(seeded_store, [make_intent(seeded_store, credentials, generation_from=1)])
    quarantined = reopen(settings, credentials).get_row(REALM_MAIN)
    assert quarantined is not None and quarantined.refresh_uncertain is True

    after_consent = reopen(settings, credentials).commit_consent(
        entity="acme_main",
        realm_id=REALM_MAIN,
        response=make_token_response(access_token="access-9", refresh_token="refresh-9"),
        actor=actor,
    )
    assert after_consent.refresh_uncertain is False
    assert after_consent.recovery_hint is None
    assert after_consent.refresh_token == "refresh-9"

    # And the journal that caused the quarantine is gone, so reopening does not
    # quarantine the brand-new credential.
    reopened = reopen(settings, credentials).get_row(REALM_MAIN)
    assert reopened is not None
    assert reopened.refresh_uncertain is False


def test_an_intent_from_a_superseded_generation_is_dropped_not_quarantined(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor: RefreshActor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quarantine is for live ambiguity, not for every leftover record.

    An intent whose starting generation the row has already moved past belongs
    to a refresh whose outcome is no longer in question: a later generation
    exists, so the chain continued. Treating that as uncertain would strand a
    healthy realm.
    """
    monkeypatch.setattr(
        oauth,
        "refresh_tokens",
        lambda **kw: make_token_response(access_token="access-2", refresh_token="refresh-2"),
    )
    seeded_store.refresh(REALM_MAIN, actor=actor, force=True)

    write_journal(seeded_store, [make_intent(seeded_store, credentials, generation_from=1)])

    after = reopen(settings, credentials).get_row(REALM_MAIN)
    assert after is not None
    assert after.auth_generation == 2
    assert after.refresh_uncertain is False


def test_a_warm_store_adopts_a_result_another_process_left_in_the_journal(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor: RefreshActor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery is not a startup step. It is a precondition of every read.

    Two store objects have both read generation 1. One refreshes, receives pair
    2, durably journals it, and then fails to commit to the main store -- so the
    journal is the only persisted copy of the live credential. If the second
    object treats "I recovered when I opened" as valid for the life of the
    process, it never looks at the journal again: it resubmits the spent token
    and its own intent overwrites the only copy of pair 2.

    A startup-only recovery flag is not valid for the life of a process.
    """
    warm = settings.open_store(credentials=credentials)
    # Warm it exactly as a long-lived server process is warm: it has read once.
    assert warm.get_row(REALM_MAIN) is not None

    monkeypatch.setattr(
        oauth,
        "refresh_tokens",
        lambda **kw: make_token_response(access_token="access-2", refresh_token="refresh-2"),
    )
    original_commit = seeded_store._commit_locked
    armed = {"now": False}

    def commit(document):  # noqa: ANN001, ANN202
        if armed["now"]:
            raise StoreError("simulated failure between the journal and the store")
        return original_commit(document)

    monkeypatch.setattr(seeded_store, "_commit_locked", commit)
    armed["now"] = True
    with pytest.raises(StoreError):
        seeded_store.refresh(REALM_MAIN, actor=actor, force=True)
    armed["now"] = False

    journalled = journal_records(warm)
    assert [r.stage for r in journalled] == ["result"]
    assert journalled[0].tokens is not None
    assert journalled[0].tokens["refresh_token"] == "refresh-2"

    submitted: list[str] = []

    def chain(**kwargs: object):  # noqa: ANN202
        submitted.append(str(kwargs["refresh_token"]))
        return make_token_response(access_token="access-3", refresh_token="refresh-3")

    monkeypatch.setattr(oauth, "refresh_tokens", chain)
    warm.refresh(REALM_MAIN, actor=actor, force=True)

    assert submitted == ["refresh-2"], (
        "the warm object resubmitted a spent token and overwrote the only "
        "persisted copy of the live pair"
    )
    final = reopen(settings, credentials).get_row(REALM_MAIN)
    assert final is not None
    assert final.refresh_token == "refresh-3"
    assert final.auth_generation == 3


def _aside(store: TokenStore) -> list[Path]:
    return list(store.journal_path.parent.glob(f"{store.journal_path.name}.unreadable-*"))


def _sealed(store: TokenStore, payload: bytes) -> bytes:
    return seal(store._key_material(), payload, environment=store.environment)


def test_an_unreadable_journal_stops_refreshes_and_keeps_the_tokens(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor: RefreshActor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It may be the only record that a refresh was accepted, and nothing can say
    for which company. So nothing is submitted, no row is lost, and the file is
    kept for whoever looks into it."""
    monkeypatch.setattr(oauth, "refresh_tokens", lambda **kw: pytest.fail("a stored token was resubmitted"))
    seeded_store.journal_path.write_bytes(b"not even encrypted")

    store = reopen(settings, credentials)
    with pytest.raises(RefreshUncertainError):
        store.refresh(REALM_MAIN, actor=actor, force=True)

    after = store.get_row(REALM_MAIN)
    assert after is not None and after.refresh_uncertain
    assert after.access_token == "access-1"
    assert "could not be read" in (after.recovery_hint or "")
    assert not store.journal_path.exists()
    assert [path.read_bytes() for path in _aside(store)] == [b"not even encrypted"]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"not json", id="not-json"),
        pytest.param(b"[]", id="not-a-document"),
        pytest.param(b'{"records": {}}', id="records-not-a-list"),
        pytest.param(b'{"records": [{"stage": "intent"}]}', id="an-invalid-record"),
    ],
)
def test_a_journal_that_decrypts_to_nonsense_is_unreadable_too(
    seeded_store: TokenStore, settings, credentials: AppCredentials, payload: bytes
) -> None:
    seeded_store.journal_path.write_bytes(_sealed(seeded_store, payload))
    after = reopen(settings, credentials).get_row(REALM_MAIN)
    assert after is not None and after.refresh_uncertain


def test_an_empty_journal_is_not_unreadable(
    seeded_store: TokenStore, settings, credentials: AppCredentials
) -> None:
    """The ordinary state after a refresh. Only a journal that can't be read stops
    anything."""
    seeded_store.journal_path.write_bytes(_sealed(seeded_store, b'{"records": []}'))
    after = reopen(settings, credentials).get_row(REALM_MAIN)
    assert after is not None and not after.refresh_uncertain


def test_an_unreadable_journal_stops_every_usable_row_and_leaves_the_rest(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor: RefreshActor
) -> None:
    """Nothing says which company it concerned. A row already stopped for a
    stronger reason keeps that reason."""
    third = "0000000000000003"
    seeded_store.commit_consent(entity="acme_third", realm_id=third, response=make_token_response(), actor=actor)
    seeded_store.commit_consent(entity="acme_ops", realm_id=REALM_OPS, response=make_token_response(), actor=actor)
    seeded_store.update_row(REALM_OPS, lambda row: replace(row, needs_consent=True))
    seeded_store.journal_path.write_bytes(b"not even encrypted")

    rows = reopen(settings, credentials).rows()
    assert rows[REALM_MAIN].refresh_uncertain and rows[third].refresh_uncertain
    assert rows[REALM_OPS].needs_consent and not rows[REALM_OPS].refresh_uncertain


def test_a_read_that_failed_changes_nothing_and_the_next_call_recovers(
    seeded_store: TokenStore, settings, credentials: AppCredentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed open says nothing about the contents, and here the journal holds
    the only copy of a new pair. Treating that as damage would lose it."""
    row = seeded_store.get_row(REALM_MAIN)
    assert row is not None
    write_journal(seeded_store, [make_record(seeded_store, credentials, generation_from=row.auth_generation)])
    store = reopen(settings, credentials)
    journal, real = store.journal_path, store_module._read_nofollow

    def failing(path: Path) -> bytes:
        if path == journal:
            raise OSError(errno.EMFILE, "synthetic")
        return real(path)

    monkeypatch.setattr(store_module, "_read_nofollow", failing)
    with pytest.raises(StoreError) as refused:
        store.read()
    assert journal.name in str(refused.value), "the operator has to know which file"
    assert journal.exists() and _aside(store) == []

    monkeypatch.setattr(store_module, "_read_nofollow", real)
    after = store.get_row(REALM_MAIN)
    assert after is not None and not after.refresh_uncertain
    assert after.access_token == "adopted-access"


def test_a_symlink_in_the_journals_place_is_damage_not_a_passing_failure(
    seeded_store: TokenStore, settings, credentials: AppCredentials, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(b"synthetic")
    seeded_store.journal_path.symlink_to(elsewhere)

    after = reopen(settings, credentials).get_row(REALM_MAIN)
    assert after is not None and after.refresh_uncertain
    assert not seeded_store.journal_path.is_symlink()


def test_a_directory_in_the_journals_place_is_damage_not_a_passing_failure(
    seeded_store: TokenStore, settings, credentials: AppCredentials
) -> None:
    seeded_store.journal_path.mkdir()

    after = reopen(settings, credentials).get_row(REALM_MAIN)
    assert after is not None and after.refresh_uncertain
    assert not seeded_store.journal_path.exists()


def test_a_second_unreadable_journal_keeps_the_first(
    seeded_store: TokenStore, settings, credentials: AppCredentials
) -> None:
    """Each one is evidence, and two can land inside one second."""
    for contents in (b"first", b"second"):
        seeded_store.journal_path.write_bytes(contents)
        reopen(settings, credentials).rows()
    assert sorted(path.read_bytes() for path in _aside(seeded_store)) == [b"first", b"second"]


def test_a_failed_move_still_stops_the_rows_and_breaks_nothing(
    seeded_store: TokenStore, settings, credentials: AppCredentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rows are stopped and durable. A raised error here would stop every
    call on the store, status and verify included."""
    seeded_store.journal_path.write_bytes(b"not even encrypted")
    store = reopen(settings, credentials)
    real, journal = Path.replace, store.journal_path

    def refusing(self: Path, target: object) -> Path:
        if self == journal:
            raise OSError(errno.EACCES, "synthetic")
        return real(self, target)

    monkeypatch.setattr(Path, "replace", refusing)
    assert store.rows()[REALM_MAIN].refresh_uncertain
    assert store.rows()[REALM_MAIN].refresh_uncertain, "every later call still works"
    assert journal.exists()


@pytest.mark.parametrize(
    "failure", [StoreError("synthetic"), store_module._JournalUnreadable()], ids=["failed-read", "damage"]
)
def test_a_revoke_finishes_when_the_journal_fails_to_read_after_recovery(
    seeded_store: TokenStore, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """The row is gone by then, and a raised error would end a revoke --all with
    companies still to go."""
    real = TokenStore._read_journal_locked
    calls: list[int] = []

    def fails_the_second_read(self: TokenStore) -> list[JournalRecord]:
        calls.append(1)
        if len(calls) == 2:
            raise failure
        return real(self)

    monkeypatch.setattr(TokenStore, "_read_journal_locked", fails_the_second_read)
    assert seeded_store.revoke_row(REALM_MAIN) is not None
    assert len(calls) >= 2


@pytest.mark.parametrize(
    "record",
    [
        pytest.param({"stage": "result", "tokens": None}, id="a-result-with-no-tokens"),
        pytest.param({"stage": "result", "tokens": {"access_token": "half"}}, id="a-result-with-half-its-tokens"),
        pytest.param({"stage": "something-else"}, id="an-unknown-stage"),
        pytest.param({"generation_from": float("inf")}, id="a-generation-json-allows-and-int-refuses"),
    ],
)
def test_a_record_that_cant_be_acted_on_stops_the_row(
    seeded_store: TokenStore, settings, credentials: AppCredentials, record: dict
) -> None:
    row = seeded_store.get_row(REALM_MAIN)
    assert row is not None
    good = make_record(seeded_store, credentials, generation_from=row.auth_generation).to_json()
    payload = json.dumps({"records": [{**good, **record}]}).encode()
    seeded_store.journal_path.write_bytes(_sealed(seeded_store, payload))

    after = reopen(settings, credentials).get_row(REALM_MAIN)
    assert after is not None and after.refresh_uncertain


def test_verify_clears_what_an_unreadable_journal_stopped(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor: RefreshActor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify is the way out, and a verified row stays verified."""
    monkeypatch.setattr(oauth, "refresh_tokens", lambda **kw: make_token_response(access_token="a2"))
    seeded_store.journal_path.write_bytes(b"not even encrypted")

    store = reopen(settings, credentials)
    assert store.rows()[REALM_MAIN].refresh_uncertain

    outcome = store.verify_uncertain(REALM_MAIN, actor=actor)
    assert outcome.submitted is True

    after = reopen(settings, credentials).get_row(REALM_MAIN)
    assert after is not None and not after.refresh_uncertain
    assert after.access_token == "a2"


def test_journal_is_cleared_after_the_store_commits(
    seeded_store: TokenStore, actor: RefreshActor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        oauth, "refresh_tokens", lambda **kw: make_token_response(access_token="a2")
    )
    seeded_store.refresh(REALM_MAIN, actor=actor, force=True)
    assert journal_records(seeded_store) == []


def test_journal_is_encrypted_like_the_store(
    seeded_store: TokenStore, credentials: AppCredentials
) -> None:
    write_journal(
        seeded_store,
        [make_record(seeded_store, credentials, generation_from=1, access_token="secret-token")],
    )
    raw = seeded_store.journal_path.read_bytes()
    assert b"secret-token" not in raw
    assert raw.startswith(b"QBOMC1")


# --------------------------------------------------------------------------
# Refresh outcomes
# --------------------------------------------------------------------------


def test_refresh_advances_the_generation_even_with_an_identical_token(
    seeded_store: TokenStore, actor: RefreshActor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        oauth,
        "refresh_tokens",
        lambda **kw: make_token_response(access_token="a2", refresh_token="refresh-1"),
    )
    row = seeded_store.refresh(REALM_MAIN, actor=actor, force=True)
    assert row.auth_generation == 2
    assert row.refresh_token == "refresh-1"


def test_response_loss_marks_uncertain_and_never_retries(
    seeded_store: TokenStore, actor: RefreshActor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost response leaves the local token possibly spent. Retrying would
    resubmit it, and Intuit's invalid_grant would read as a dead credential."""
    calls: list[int] = []

    def lost(**kwargs):
        calls.append(1)
        raise oauth.TokenResponseLost("no response")

    monkeypatch.setattr(oauth, "refresh_tokens", lost)

    with pytest.raises(RefreshUncertainError):
        seeded_store.refresh(REALM_MAIN, actor=actor, force=True)

    row = seeded_store.get_row(REALM_MAIN)
    assert row is not None
    assert row.refresh_uncertain is True
    assert row.recovery_hint and "connect acme_main" in row.recovery_hint
    assert row.auth_state(registry_display_name="Acme Main") == "refresh_uncertain"

    with pytest.raises(RefreshUncertainError):
        seeded_store.refresh(REALM_MAIN, actor=actor, force=True)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "tokens",
    [
        pytest.param({"access_token": "", "refresh_token": ""}, id="both-empty"),
        pytest.param({"access_token": "fresh-access", "refresh_token": ""}, id="refresh-empty"),
        pytest.param({"access_token": "", "refresh_token": "fresh-refresh"}, id="access-empty"),
        pytest.param({"access_token": "   ", "refresh_token": "fresh-refresh"}, id="access-blank"),
        pytest.param({"access_token": "fresh-access", "refresh_token": " \t"}, id="refresh-blank"),
        pytest.param({"access_token": "fresh-access", "refresh_token": None}, id="refresh-null"),
        pytest.param({"access_token": "fresh\naccess", "refresh_token": "fresh-refresh"}, id="control-character"),
        pytest.param({"access_token": "fresh-access", "refresh_token": " fresh-refresh"}, id="leading-space"),
    ],
)
def test_a_success_with_unusable_tokens_is_a_lost_response(
    seeded_store: TokenStore, actor: RefreshActor, monkeypatch: pytest.MonkeyPatch, tokens: dict
) -> None:
    """Through the real parser: a 200 whose tokens can't be sent can't replace
    the stored pair, and the refresh may still have been accepted. So the realm
    is quarantined, the old pair is kept, and nothing is retried."""
    before = seeded_store.get_row(REALM_MAIN)
    assert before is not None
    sent: list[httpx.Request] = []

    def answering(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={**tokens, "expires_in": 3600, "x_refresh_token_expires_in": 8_640_000})

    monkeypatch.setattr(
        oauth, "isolated_client", lambda **kw: httpx.Client(transport=httpx.MockTransport(answering), **kw)
    )
    for _ in range(2):
        with pytest.raises(RefreshUncertainError):
            seeded_store.refresh(REALM_MAIN, actor=actor, force=True)
    after = seeded_store.get_row(REALM_MAIN)
    assert after is not None
    assert len(sent) == 1, "the refresh went out again"
    assert after.refresh_uncertain is True
    assert after.refresh_token == before.refresh_token
    assert after.auth_generation == before.auth_generation


def test_invalid_client_is_configuration_and_never_needs_consent(
    seeded_store: TokenStore, actor: RefreshActor, monkeypatch: pytest.MonkeyPatch
) -> None:
    def bad_client(**kwargs):
        raise oauth.InvalidClient("invalid_client")

    monkeypatch.setattr(oauth, "refresh_tokens", bad_client)

    with pytest.raises(ConfigurationError):
        seeded_store.refresh(REALM_MAIN, actor=actor, force=True)

    row = seeded_store.get_row(REALM_MAIN)
    assert row is not None
    assert row.needs_consent is False


def test_invalid_grant_never_resubmits_the_rejected_token(
    seeded_store: TokenStore, actor: RefreshActor, monkeypatch: pytest.MonkeyPatch
) -> None:
    submitted: list[str] = []

    def rejected(**kwargs):
        submitted.append(kwargs["refresh_token"])
        raise oauth.InvalidGrant("invalid_grant")

    monkeypatch.setattr(oauth, "refresh_tokens", rejected)

    with pytest.raises(NeedsConsentError):
        seeded_store.refresh(REALM_MAIN, actor=actor, force=True)

    assert submitted == ["refresh-1"]
    row = seeded_store.get_row(REALM_MAIN)
    assert row is not None and row.needs_consent is True


def test_invalid_grant_retries_only_with_an_independently_newer_credential(
    seeded_store: TokenStore, actor: RefreshActor, monkeypatch: pytest.MonkeyPatch,
    settings, credentials: AppCredentials,
) -> None:
    """Another process persisted a newer generation while this refresh was in
    flight, so this one was simply working from a stale read."""
    submitted: list[str] = []

    def behaviour(**kwargs):
        token = kwargs["refresh_token"]
        submitted.append(token)
        if token == "refresh-1":
            # Simulate the concurrent writer landing a newer generation.
            with seeded_store.lock():
                document = seeded_store._load_locked()
                document.rows[REALM_MAIN] = replace(
                    document.rows[REALM_MAIN],
                    refresh_token="refresh-2",
                    auth_generation=2,
                )
                seeded_store._commit_locked(document)
            raise oauth.InvalidGrant("invalid_grant")
        return make_token_response(access_token="a3", refresh_token="refresh-3")

    monkeypatch.setattr(oauth, "refresh_tokens", behaviour)

    row = seeded_store.refresh(REALM_MAIN, actor=actor, force=True)
    assert submitted == ["refresh-1", "refresh-2"]
    assert row.refresh_token == "refresh-3"
    assert row.auth_generation == 3


def test_invalid_grant_retry_happens_at_most_once(
    seeded_store: TokenStore, actor: RefreshActor, monkeypatch: pytest.MonkeyPatch
) -> None:
    submitted: list[str] = []

    def always_rejected(**kwargs):
        token = kwargs["refresh_token"]
        submitted.append(token)
        if token == "refresh-1":
            with seeded_store.lock():
                document = seeded_store._load_locked()
                document.rows[REALM_MAIN] = replace(
                    document.rows[REALM_MAIN], refresh_token="refresh-2", auth_generation=2
                )
                seeded_store._commit_locked(document)
        raise oauth.InvalidGrant("invalid_grant")

    monkeypatch.setattr(oauth, "refresh_tokens", always_rejected)

    with pytest.raises(NeedsConsentError):
        seeded_store.refresh(REALM_MAIN, actor=actor, force=True)
    assert submitted == ["refresh-1", "refresh-2"]


def test_min_age_is_rechecked_inside_the_lock(
    seeded_store: TokenStore, actor: RefreshActor, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        oauth,
        "refresh_tokens",
        lambda **kw: (calls.append(1), make_token_response(access_token="a2"))[1],
    )
    seeded_store.refresh(REALM_MAIN, actor=actor, min_age_seconds=3600)
    assert calls == []


# --------------------------------------------------------------------------
# 401
# --------------------------------------------------------------------------


def test_401_uses_a_newer_stored_generation_without_a_request(
    seeded_store: TokenStore, actor: RefreshActor, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        oauth,
        "refresh_tokens",
        lambda **kw: (calls.append(1), make_token_response())[1],
    )
    with seeded_store.lock():
        document = seeded_store._load_locked()
        document.rows[REALM_MAIN] = replace(
            document.rows[REALM_MAIN], auth_generation=5, access_token="newer"
        )
        seeded_store._commit_locked(document)

    row = seeded_store.on_unauthorized(REALM_MAIN, rejected_generation=1, actor=actor)
    assert row.access_token == "newer"
    assert calls == []


def test_401_forces_exactly_one_refresh_though_the_token_looks_valid(
    seeded_store: TokenStore, actor: RefreshActor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 401 is not proof of expiry, so the timestamp does not get a vote."""
    calls: list[int] = []
    monkeypatch.setattr(
        oauth,
        "refresh_tokens",
        lambda **kw: (calls.append(1), make_token_response(access_token="a2"))[1],
    )
    before = seeded_store.get_row(REALM_MAIN)
    assert before is not None and before.access_token_valid()

    row = seeded_store.on_unauthorized(
        REALM_MAIN, rejected_generation=before.auth_generation, actor=actor
    )
    assert calls == [1]
    assert row.access_token == "a2"


# --------------------------------------------------------------------------
# Configuration vs consent
# --------------------------------------------------------------------------


def test_row_from_the_wrong_environment_is_configuration(
    seeded_store: TokenStore, actor: RefreshActor
) -> None:
    with seeded_store.lock():
        document = seeded_store._load_locked()
        document.rows[REALM_MAIN] = replace(
            document.rows[REALM_MAIN], environment="production"
        )
        seeded_store._commit_locked(document)

    with pytest.raises(ConfigurationError, match="configuration problem"):
        seeded_store.refresh(REALM_MAIN, actor=actor, force=True)
    row = seeded_store.get_row(REALM_MAIN)
    assert row is not None and row.needs_consent is False


def test_row_from_another_client_id_is_configuration(
    seeded_store: TokenStore, actor: RefreshActor
) -> None:
    with seeded_store.lock():
        document = seeded_store._load_locked()
        document.rows[REALM_MAIN] = replace(
            document.rows[REALM_MAIN], client_id_fingerprint="f" * 64
        )
        seeded_store._commit_locked(document)

    with pytest.raises(ConfigurationError, match="client id"):
        seeded_store.refresh(REALM_MAIN, actor=actor, force=True)
    row = seeded_store.get_row(REALM_MAIN)
    assert row is not None and row.needs_consent is False


def test_document_from_the_wrong_environment_is_configuration(
    seeded_store: TokenStore, settings, credentials: AppCredentials
) -> None:
    from dataclasses import replace as dc_replace

    production = dc_replace(settings, environment="production")
    other = production.open_store(credentials=credentials)
    # Same directory, different environment: separate document, so this is a
    # clean miss rather than a mismatch.
    assert other.document_path != seeded_store.document_path


# --------------------------------------------------------------------------
# Two threads, and -- separately -- two processes
# --------------------------------------------------------------------------


def test_two_threads_refreshing_one_realm_do_not_break_the_chain(
    seeded_store: TokenStore, settings, credentials: AppCredentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two threads in ONE interpreter: this covers the in-process mutex only.

    Both workers below are ``threading.Thread``, so the mutex in
    ``canonical_lock`` serialises them and the ``flock`` is never contended.
    The file lock has its own test, which spawns real interpreters:
    ``test_two_processes_refreshing_one_realm_do_not_break_the_chain``.
    """
    server = settings.open_store(credentials=credentials)
    keepalive = settings.open_store(credentials=credentials)

    submitted: list[str] = []
    counter = {"n": 1}
    barrier = threading.Barrier(2, timeout=10)

    def behaviour(**kwargs):
        submitted.append(kwargs["refresh_token"])
        time.sleep(0.05)
        counter["n"] += 1
        return make_token_response(
            access_token=f"a{counter['n']}", refresh_token=f"refresh-{counter['n']}"
        )

    monkeypatch.setattr(oauth, "refresh_tokens", behaviour)

    results: list[object] = []

    def worker(target: TokenStore, role: str) -> None:
        barrier.wait()
        try:
            results.append(target.refresh(REALM_MAIN, actor=RefreshActor.here(role), force=True))
        except Exception as exc:  # pragma: no cover - recorded for the assertion
            results.append(exc)

    threads = [
        threading.Thread(target=worker, args=(server, "server")),
        threading.Thread(target=worker, args=(keepalive, "keepalive")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)

    assert all(not isinstance(r, Exception) for r in results), results
    # Each refresh submitted the token the previous one produced: no fork.
    assert submitted == ["refresh-1", "refresh-2"]
    final = settings.open_store(credentials=credentials).get_row(REALM_MAIN)
    assert final is not None
    assert final.auth_generation == 3
    assert final.refresh_token == "refresh-3"


def _spawn_refresh_child(
    *, store_dir: Path, realm: str, role: str, workspace: Path, hold_seconds: float
) -> tuple[subprocess.Popen[bytes], Path, Path]:
    """One real interpreter, refreshing one realm. Returns (process, out, ready)."""
    out = workspace / f"{role}.json"
    ready = workspace / f"{role}.ready"
    process = subprocess.Popen(
        [
            sys.executable, "-m", "tests.refresh_child",
            "--store-dir", str(store_dir),
            "--realm", realm,
            "--role", role,
            "--out", str(out),
            "--ready", str(ready),
            "--go", str(workspace / "go"),
            "--hold-seconds", str(hold_seconds),
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=refresh_child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return process, out, ready


def refresh_child_env() -> dict[str, str]:
    """The child's environment: the active source root first, so it imports
    the same tree the parent did -- which is what makes the two-process test
    usable as a mutation detector -- and the store key."""
    from .conftest import with_source_root

    return with_source_root(
        {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "QBO_TOKEN_KEY": os.environ["QBO_TOKEN_KEY"],
            "QBO_TOKEN_KEY_SOURCE": "env",
        }
    )


def test_two_processes_refreshing_one_realm_do_not_break_the_chain(
    seeded_store: TokenStore, settings, credentials: AppCredentials, tmp_path: Path
) -> None:
    """Genuinely separate interpreters, contending for one realm.

    This is the test that exercises the ``flock``. Each child holds the lock for
    a quarter of a second while its token call is "in flight"; without the file
    lock both would read generation 1, both would submit ``refresh-1``, and the
    chain would fork -- one of the two resulting pairs being lost, which under
    rotation costs a trip through the consent screen.

    Three independent assertions, because a lock can fail in three ways: the
    submitted tokens must chain, the critical sections must not overlap, and the
    surviving row must have advanced by exactly two generations.
    """
    workspace = tmp_path / "children"
    workspace.mkdir()

    children = [
        _spawn_refresh_child(
            store_dir=seeded_store.directory, realm=REALM_MAIN, role=role,
            workspace=workspace, hold_seconds=0.25,
        )
        for role in ("server", "keepalive")
    ]

    try:
        deadline = time.monotonic() + 60
        while not all(ready.exists() for _, _, ready in children):
            assert time.monotonic() < deadline, "a child never reported ready"
            assert all(p.poll() is None for p, _, _ in children), [
                p.communicate() for p, _, _ in children
            ]
            time.sleep(0.01)

        # Both are parked on the same line; release them together.
        (workspace / "go").write_text("go", encoding="utf-8")

        for process, _, _ in children:
            stdout, stderr = process.communicate(timeout=60)
            assert process.returncode == 0, stderr.decode("utf-8", "replace")
    finally:
        for process, _, _ in children:
            if process.poll() is None:  # pragma: no cover - only on a hang
                process.kill()

    records = [json.loads(out.read_text(encoding="utf-8")) for _, out, _ in children]
    assert [r.get("error") for r in records] == [None, None], records
    # Both children imported the tree under test, not an installed package.
    from .conftest import source_root

    for record in records:
        assert Path(record["module_file"]).resolve().is_relative_to(source_root()), record

    submitted = sorted(token for record in records for token in record["submitted"])
    assert submitted == ["refresh-1", "refresh-2"], (
        "both processes submitted the same token: the chain forked and one of "
        f"the two issued pairs is lost -- {records}"
    )

    first, second = sorted(records, key=lambda r: r["in_section"])
    assert first["out_of_section"] <= second["in_section"], (
        "the two critical sections overlapped, so the file lock excluded nothing"
    )

    final = settings.open_store(credentials=credentials).get_row(REALM_MAIN)
    assert final is not None
    assert final.auth_generation == 3
    assert final.refresh_token == "refresh-3"


def test_connect_import_and_revoke_race_leaves_one_consistent_row(
    store: TokenStore, settings, credentials: AppCredentials, actor: RefreshActor
) -> None:
    """Two ceremonies for one realm begin from the same state; one commits.

    A consent captures the state it began from and a stale ceremony stops, so
    the second to arrive is refused and the row is the first's -- never a
    blend, and never a silent overwrite.
    """
    from qbo_multicompany_mcp.errors import StaleConsentError

    baselines = {
        "connect": store.consent_baseline(REALM_MAIN),
        "import": settings.open_store(credentials=credentials).consent_baseline(REALM_MAIN),
    }
    barrier = threading.Barrier(3, timeout=10)
    errors: list[Exception] = []
    stale: list[str] = []

    def ceremony(name: str, target: TokenStore) -> None:
        barrier.wait()
        try:
            target.commit_consent(
                entity="acme_main",
                realm_id=REALM_MAIN,
                response=make_token_response(refresh_token=f"from-{name}"),
                actor=actor,
                baseline=baselines[name],
            )
        except StaleConsentError:
            stale.append(name)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def revoke() -> None:
        barrier.wait()
        try:
            settings.open_store(credentials=credentials).revoke_row(REALM_OPS)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=ceremony, args=("connect", store)),
        threading.Thread(
            target=ceremony, args=("import", settings.open_store(credentials=credentials))
        ),
        threading.Thread(target=revoke),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)

    assert errors == []
    assert len(stale) == 1, f"exactly one ceremony must be refused as stale, got {stale}"
    row = settings.open_store(credentials=credentials).get_row(REALM_MAIN)
    assert row is not None
    winner = ({"connect", "import"} - set(stale)).pop()
    assert row.refresh_token == f"from-{winner}"
    assert row.auth_generation == 1


# --------------------------------------------------------------------------
# Location, protection and plaintext
# --------------------------------------------------------------------------


def test_store_inside_a_git_working_copy_is_refused(tmp_path: Path, settings) -> None:
    from dataclasses import replace as dc_replace

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(StoreLocationRefused, match="git working copy"):
        dc_replace(settings, token_store_dir=repo / "state").open_store()


def test_store_inside_a_known_cloud_root_is_refused(
    tmp_path: Path, settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace as dc_replace

    home = tmp_path / "home"
    target = home / "Dropbox" / "creds"
    target.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    with pytest.raises(StoreLocationRefused, match="cloud-sync"):
        dc_replace(settings, token_store_dir=target).open_store()


def test_plaintext_store_is_refused_without_the_flag(settings) -> None:
    from dataclasses import replace as dc_replace

    plain = dc_replace(settings, key_source=KeySource.NONE, allow_plaintext_store=False)
    with pytest.raises(PlaintextStoreRefused, match="--allow-plaintext-store"):
        plain.open_store()


def test_plaintext_store_works_with_the_flag(settings, credentials, actor) -> None:
    from dataclasses import replace as dc_replace

    plain = dc_replace(settings, key_source=KeySource.NONE, allow_plaintext_store=True)
    target = plain.open_store(credentials=credentials)
    target.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN, response=make_token_response(), actor=actor
    )
    assert target.document_path.name.endswith(".json")
    assert json.loads(target.document_path.read_text())["environment"] == "sandbox"


def test_encrypted_store_holds_no_token_in_clear(seeded_store: TokenStore) -> None:
    raw = seeded_store.document_path.read_bytes()
    assert b"refresh-1" not in raw
    assert b"access-1" not in raw
    assert REALM_MAIN.encode() not in raw


def test_store_is_bound_to_its_environment(token_key: str) -> None:
    import base64

    key = base64.b64decode(token_key)
    blob = seal(key, b'{"a":1}', environment="sandbox")
    with pytest.raises(StoreError, match="failed authentication"):
        unseal(key, blob, environment="production")


def test_opening_the_store_creates_nothing(settings, credentials) -> None:
    target = settings.open_store(credentials=credentials)
    assert not target.directory.exists()
    assert not target.document_path.exists()


def test_no_backup_files_are_written(seeded_store: TokenStore) -> None:
    """Under this rotation model a backup older than one rotation holds a dead
    token, so restoring one cannot help."""
    names = sorted(p.name for p in seeded_store.directory.iterdir())
    assert names == ["tokens.lock", "tokens.sandbox.enc"]


# --------------------------------------------------------------------------
# Key material
# --------------------------------------------------------------------------
#
# ``QBO_TOKEN_KEY`` is documented as "base64 or hex". Every 64-character hex
# string is also valid base64, so a decoder that tries base64 first gets 48
# bytes and the length check rejects the key before hex is tried: an operator
# following the documented option could not open their store. So the string's
# shape decides, and the tests below pin that.


@pytest.mark.parametrize(
    "key",
    [
        pytest.param(bytes(range(32)), id="every-byte-value"),
        pytest.param(b"\xff" * 32, id="all-ff"),
        pytest.param(b"\x00" * 32, id="all-zero"),
    ],
)
def test_a_hex_key_decodes_to_the_bytes_it_spells(key: bytes) -> None:
    from qbo_multicompany_mcp.crypto import _decode_key

    assert _decode_key(key.hex(), where="QBO_TOKEN_KEY") == key
    assert _decode_key(key.hex().upper(), where="QBO_TOKEN_KEY") == key


def test_a_base64_key_still_decodes_to_the_bytes_it_spells() -> None:
    import base64 as b64

    from qbo_multicompany_mcp.crypto import _decode_key

    key = bytes(range(32))
    assert _decode_key(b64.b64encode(key).decode(), where="QBO_TOKEN_KEY") == key


def test_a_key_in_neither_encoding_says_so() -> None:
    from qbo_multicompany_mcp.crypto import _decode_key

    with pytest.raises(ConfigurationError, match="not valid base64 or hex"):
        _decode_key("not a key at all!", where="QBO_TOKEN_KEY")


def test_a_short_hex_key_is_a_length_failure_not_an_encoding_one() -> None:
    from qbo_multicompany_mcp.crypto import _decode_key

    with pytest.raises(ConfigurationError, match="must decode to 32 bytes"):
        _decode_key("ab" * 16, where="QBO_TOKEN_KEY")


def test_a_store_opens_under_a_hex_key(
    settings, credentials, actor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator-facing half: the documented hex option opens the store."""
    from qbo_multicompany_mcp.crypto import generate_key

    monkeypatch.setenv("QBO_TOKEN_KEY", generate_key().hex())
    target = settings.open_store(credentials=credentials)
    target.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN, response=make_token_response(), actor=actor
    )
    row = settings.open_store(credentials=credentials).get_row(REALM_MAIN)
    assert row is not None
    assert row.access_token == "access-1"


# --------------------------------------------------------------------------
# The configuration binding is checked before every credential use
# --------------------------------------------------------------------------
#
# Every call is bound to an immutable entity/environment/realm tuple. A check
# on the refresh paths alone is not enough: while a cached access token is
# still valid no refresh runs, so a store carried over from another configured
# app, or a row left inconsistent by a registry change, would send its
# credential to Intuit instead of raising a configuration error.


def _tamper(store: TokenStore, realm: str, **fields) -> None:
    with store.lock():
        document = store._load_locked()
        document.rows[realm] = replace(document.rows[realm], **fields)
        store._commit_locked(document)


def _serving(mock_settings, registry, credentials, actor, monkeypatch=None):
    import anyio
    from mcp.server import MCPServer

    from qbo_multicompany_mcp.tools import register_tools

    if monkeypatch is not None:
        # A deployment that can refresh has an app configured, which is what
        # makes "a store carried over from a different app" a thing the server
        # can recognise at all.
        monkeypatch.setenv("QBO_CLIENT_ID", credentials.client_id)
        monkeypatch.setenv("QBO_CLIENT_SECRET", credentials.client_secret)

    store = mock_settings.open_store(credentials=credentials)
    store.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN,
        response=make_token_response(), actor=actor,
    )
    server = MCPServer(name="t", version="0")
    register_tools(server, mock_settings, registry)
    return store, (lambda name, args: anyio.run(lambda: server.call_tool(name, args)))


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        pytest.param({"environment": "production"}, "environment", id="environment"),
        pytest.param({"client_id_fingerprint": "f" * 64}, "client id", id="client-id"),
        pytest.param({"entity": "acme_ops"}, "entity", id="entity"),
    ],
)
def test_a_still_valid_cached_token_is_not_sent_under_a_mismatched_binding(
    mock_settings, registry, credentials, actor, mock_intuit, fields, expected,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The access token has an hour left, so no refresh path runs.

    That is the whole point: a check on the refresh paths alone never fires
    here.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    store, call = _serving(mock_settings, registry, credentials, actor, monkeypatch)
    _tamper(store, REALM_MAIN, **fields)
    mock_intuit.requests.clear()

    with pytest.raises(ToolError) as caught:
        call("get_company_info", {"entity": "acme_main"})

    message = str(caught.value)
    assert "configuration problem" in message
    assert expected in message
    assert mock_intuit.requests == [], "the credential was sent anyway"


def test_a_matching_binding_still_serves(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    """The control: the same path with nothing tampered with must still work,
    or the test above would pass on a server that refuses everything."""
    _store, call = _serving(mock_settings, registry, credentials, actor)
    result = call("get_company_info", {"entity": "acme_main"})
    assert result.is_error is False
    assert mock_intuit.requests, "a real request was made"


# --------------------------------------------------------------------------
# The 401 fast path, and the critical-section boundary of update_row
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flag", "error"),
    [
        pytest.param("refresh_uncertain", RefreshUncertainError, id="refresh-uncertain"),
        pytest.param("needs_consent", NeedsConsentError, id="needs-consent"),
    ],
)
def test_a_401_never_releases_a_newer_generation_that_is_blocked(
    seeded_store: TokenStore, actor: RefreshActor, flag: str, error: type[Exception]
) -> None:
    """A request used generation 1 and got a 401. Meanwhile another process
    refreshed the realm to generation 2 and then stopped it (a lost response,
    or invalid_grant). A 401 path that handed the generation-2 token back
    before looking at either flag would have the retry send it, so the blocked
    states are checked before any generation is returned."""
    _tamper(
        seeded_store, REALM_MAIN, auth_generation=2, access_token="blocked-access",
        **{flag: True},
    )
    with pytest.raises(error):
        seeded_store.on_unauthorized(
            REALM_MAIN, rejected_generation=1, actor=actor, entity_key="acme_main"
        )


def test_a_401_retry_does_not_send_a_quarantined_newer_generation(
    settings, credentials: AppCredentials, registry, actor: RefreshActor
) -> None:
    """Through the client, with a transport that quarantines the row while
    the first request is in flight: exactly one request reaches the
    transport, and the call ends with the quarantine, not with a retry."""
    from qbo_multicompany_mcp.client import AccountingClient

    store = settings.open_store(credentials=credentials)
    store.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN, response=make_token_response(), actor=actor
    )
    reached: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        reached.append(request.headers.get("authorization", ""))
        if len(reached) == 1:
            _tamper(
                store, REALM_MAIN, auth_generation=2, access_token="quarantined-2",
                refresh_uncertain=True,
            )
            return httpx.Response(401, json={"fault": "AuthenticationFailed"}, request=request)
        return httpx.Response(200, json={}, request=request)

    pointed = replace(settings, api_base="http://127.0.0.1:9")
    http = httpx.Client(transport=httpx.MockTransport(answer))
    with AccountingClient(pointed, store, http=http) as client:
        with pytest.raises(RefreshUncertainError):
            client.get(registry["acme_main"], "preferences")
    assert reached == ["Bearer access-1"], "the retry carried the quarantined token"


def test_a_capability_write_never_rolls_back_a_refresh_that_landed_during_it(
    seeded_store: TokenStore, settings, credentials: AppCredentials, actor: RefreshActor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One process is inside update_row, recording a capability from a
    generation-1 document, while a second refreshes the realm. update_row
    holds the canonical lock through its commit, so the refresh waits and
    lands on top of the capability write. Moving that commit outside the lock
    survives every other test in this suite and loses the generation-2 pair
    here; the gate entry store-update-row-commit-under-lock makes that edit."""
    updater = seeded_store
    refresher = reopen(settings, credentials)
    monkeypatch.setattr(
        oauth, "refresh_tokens",
        lambda **kw: make_token_response(access_token="a2", refresh_token="refresh-2"),
    )

    commit_entered = threading.Event()
    release_commit = threading.Event()
    real_commit = updater._commit_locked

    def delayed_commit(document) -> None:
        commit_entered.set()
        assert release_commit.wait(10), "the delayed commit was never released"
        real_commit(document)

    monkeypatch.setattr(updater, "_commit_locked", delayed_commit)

    errors: list[str] = []

    def update() -> None:
        try:
            updater.update_row(
                REALM_MAIN, lambda row: replace(row, capabilities={"fixture": "written"})
            )
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(repr(exc))

    def refresh() -> None:
        try:
            refresher.refresh(REALM_MAIN, actor=actor, force=True, entity_key="acme_main")
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(repr(exc))

    first = threading.Thread(target=update)
    second = threading.Thread(target=refresh)
    first.start()
    assert commit_entered.wait(5), "update_row never reached its commit"
    second.start()
    # Long enough for the refresh to reach the lock -- or, with the commit
    # outside the lock, to run to completion before the stale commit lands.
    time.sleep(0.3)
    release_commit.set()
    first.join(10)
    second.join(10)

    assert errors == []
    final = reopen(settings, credentials).get_row(REALM_MAIN)
    assert final is not None
    assert final.auth_generation == 2, "the refreshed pair was rolled back"
    assert final.refresh_token == "refresh-2"
    assert final.capabilities == {"fixture": "written"}
    assert final.refresh_uncertain is False
