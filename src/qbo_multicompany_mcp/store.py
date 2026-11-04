"""The token store: one encrypted document per environment, keyed by realm.

Everything difficult about this package is in this file, and all of it follows
from one fact: **a refresh may replace the refresh token, and once a new one
has been issued the previous one has to be treated as dead.** There are two
readings of when the old token dies. Intuit's Authorization FAQ says the value
is updated periodically -- every 24 hours, or on the first refresh after 24
hours -- that the previous value expires automatically when a new one is
issued, and that an old token yields ``invalid_grant``. Intuit's oauth-jsclient
README says instead that previous refresh tokens expire 24 hours after a new
one arrives, which would mean an overlap.
Nothing here depends on either reading: adoption is decided by generation,
never by comparing token strings, and recovery never assumes an overlap
exists. What holds under both readings is that this package can rely on at
most one credential per realm, it lives in one file, and losing it costs a
human trip through a consent screen.

The rules below follow from that.

*One canonical lock.* Every mutation -- refresh, consent commit, import,
revoke, guard-state write -- takes the same ``flock`` on a
``tokens.lock`` sidecar, plus an in-process mutex so two threads in one process
serialise as well as two processes do. The lock is never held across human
interaction: a browser consent screen can sit open for minutes, and a lock held
that long would look exactly like a deadlock to every other process.

*Persist before use.* A new access token is written to disk -- temp file at the
store's protection mode, ``fsync`` the contents, ``os.replace``, ``fsync`` the
parent directory -- before a single request is made with it. If the write
fails, the token is discarded and the failure is raised. A token used but not
persisted is a token that vanishes on the next crash, taking the whole chain
with it, because its predecessor has already been spent.

*A write-ahead journal, encrypted like the store.* Before a refresh, an intent
is persisted; after a valid response, the new pair is persisted in the same
binding. A journal record is adopted only for the expected realm, app and
environment, and only for an immediately applicable generation.

Recovery runs before **every** locked read, not once when a process starts. A
store object that recovered at startup and then ran for a week cannot see a
result some other process journalled in the meantime, so it would keep working
from a generation that has already been superseded -- and its next refresh
would submit a token a newer response has already replaced, and overwrite the
only persisted copy of the newer pair.

*An intent that outlives its refresh stops the realm.* An intent records that a
token was submitted, and nothing more. It cannot distinguish "the request never
left this machine" from "Intuit accepted it and the response was lost", and the
difference decides whether the stored token is still usable. So recovery marks
such a realm ``refresh_uncertain`` rather than discarding the record and
resubmitting. Every path that does learn the answer -- success, ``invalid_grant``,
``invalid_client``, a connection that was never established, a client that
couldn't be built, a request the network policy refused -- clears the journal,
so ambiguity is what is left over rather than what is assumed. A journal whose
contents make no sense is ambiguity about every realm, and stops them all the
same way.

*Adoption is decided by generation, never by comparing token strings.* An
unchanged refresh token can legitimately accompany a new access token and a new
expiry. Code that adopted only when the refresh token differed would silently
drop exactly those responses.

*Ambiguous outcomes stop.* If a refresh may have been accepted but the response
was lost, the realm is marked ``refresh_uncertain`` and is not retried
automatically. Retrying resubmits a token that may already be spent, and Intuit
answers that with ``invalid_grant`` (its Authorization FAQ) -- which reads as a
dead credential when the credential is in fact fine.

There are no backups here, on purpose. A backup taken before a rotation holds
a token this package must treat as dead (whether Intuit keeps it alive for a
while is the unverified overlap above), so restoring one cannot be relied on
and will usually make the situation harder to reason about.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import logging
import os
import platform
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from . import deadline as call_deadline
from . import live_values, oauth
from .constants import (
    OAUTH_POST_TIMEOUT_SECONDS,
    REFRESH_HARD_EXPIRY_WARN_DAYS,
    REFRESH_INACTIVITY_WARN_DAYS,
    REFRESH_TOKEN_HARD_LIMIT_SECONDS,
    STALE_REFRESH_SECONDS,
    STORE_SCHEMA_VERSION,
    STORE_WRITE_BUDGET_SECONDS,
)
from .crypto import KeySource, load_key, seal, store_file_mode, unseal
from .errors import (
    LOCK_TIMEOUT_TEXT,
    NO_APP_CREDENTIALS_TEXT,
    REFRESH_UNCERTAIN_TEXT,
    AppCredentialsRejected,
    ConfigurationError,
    MalformedRowError,
    NeedsConsentError,
    PlaintextStoreRefused,
    RefreshUncertainError,
    StaleConsentError,
    StoreError,
    StoreLocationRefused,
    StoreLockTimeout,
    config_mismatch_text,
    malformed_row_text,
    needs_consent_text,
    stale_consent_text,
)

log = logging.getLogger(__name__)

LOCK_FILENAME: Final = "tokens.lock"

#: A lock wait must outlast an in-flight token POST plus the write that follows
#: it, or a caller would time out on a lock that is about to be released by a
#: perfectly healthy refresh -- and would report a busy store as a failure.
DEFAULT_LOCK_TIMEOUT: Final = OAUTH_POST_TIMEOUT_SECONDS + STORE_WRITE_BUDGET_SECONDS

#: What a refresh is expected to need once begun: one token POST timeout, then
#: the durable write. A refresh is not submitted with less than this left. It is
#: an estimate, not a bound: the token response is read without the call's
#: deadline, and its read timeout restarts on every socket read.
REFRESH_BUDGET_SECONDS: Final = OAUTH_POST_TIMEOUT_SECONDS + STORE_WRITE_BUDGET_SECONDS

_LOCK_POLL_SECONDS: Final = 0.02

#: Directories whose contents are copied to other machines by software that
#: cannot be asked to stop. A rotating credential replicated to a second
#: machine produces two processes that each believe they hold the live token.
_CLOUD_SYNC_ROOTS: Final[tuple[str, ...]] = (
    "Library/Mobile Documents",
    "Library/CloudStorage",
    "Dropbox",
    "Google Drive",
    "My Drive",
    "OneDrive",
    "Box",
    "Box Sync",
    "Nextcloud",
    "ownCloud",
    "pCloud Drive",
    "Sync",
    "MEGA",
    "Creative Cloud Files",
    "Yandex.Disk",
)


# --------------------------------------------------------------------------
# Row and document
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AppCredentials:
    client_id: str
    client_secret: str

    def __post_init__(self) -> None:
        # The secret is registered the moment it exists in this process, so
        # every later log line, error and test-origin request is checked
        # against it by value rather than by shape.
        live_values.register(self.client_secret)

    @property
    def fingerprint(self) -> str:
        return oauth.client_id_fingerprint(self.client_id)


@dataclass(frozen=True, slots=True)
class RefreshActor:
    """Who performed the last refresh. Diagnostic; never a decision input."""

    role: str
    pid: int
    hostname: str

    @classmethod
    def here(cls, role: str) -> RefreshActor:
        return cls(role=role, pid=os.getpid(), hostname=platform.node())

    def to_json(self) -> dict[str, Any]:
        return {"role": self.role, "pid": self.pid, "hostname": self.hostname}

    @classmethod
    def from_json(cls, data: Any) -> RefreshActor | None:
        if not isinstance(data, dict):
            return None
        return cls(
            role=str(data.get("role", "unknown")),
            pid=int(data.get("pid", 0) or 0),
            hostname=str(data.get("hostname", "")),
        )


def registry_fingerprint(display_name: str) -> str:
    """The full SHA-256 digest of a registry display name.

    A persisted guard verdict is bound to the name it was checked against.
    The digest rather than the name, so the store never carries the
    registry's text; full rather than truncated, for the same reason the
    client-id fingerprint is full.
    """
    return hashlib.sha256(display_name.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class NameMismatch:
    """The guard's persisted verdict for a realm.

    Persisted so ``list_entities`` can report a mismatch without making a
    request. It is bound to what it verified: ``registry_fingerprint`` is the
    digest of the registry ``display_name`` the Intuit name was compared
    with, and the verdict is applied only against that same name. An
    operator who corrects ``entities.yaml`` and restarts therefore stops
    seeing the old verdict at once, rather than until the next data call
    re-checks.

    The consent it verified is bound by ``consent_epoch``: the epoch the row
    carried when the guard read the company name.
    :meth:`TokenStore.commit_consent` clears the verdict outright, and
    :meth:`TokenStore.record_guard_verdict` refuses a verdict whose epoch is
    not the row's, so a guard pass that began under one consent and finished
    after the next cannot write an observation about a company file the row
    may no longer denote. ``auth_generation`` is recorded for diagnosis and
    is not a binding: a refresh advances the generation too, and a verdict
    about a company name does not go stale when a token rotates.
    """

    mismatch: bool
    checked_at: datetime
    auth_generation: int
    #: Empty when no binding was recorded. A verdict about an unknown name
    #: applies to no name.
    registry_fingerprint: str = ""
    #: The consent this verdict was checked under. Zero when none was recorded,
    #: which is also the epoch of a row with none recorded, so such a verdict
    #: still applies to such a row.
    consent_epoch: int = 0

    def applies_to(self, display_name: str) -> bool:
        return bool(self.registry_fingerprint) and (
            self.registry_fingerprint == registry_fingerprint(display_name)
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "mismatch": self.mismatch,
            "checked_at": _iso(self.checked_at),
            "auth_generation": self.auth_generation,
            "registry_fingerprint": self.registry_fingerprint,
            "consent_epoch": self.consent_epoch,
        }

    @classmethod
    def from_json(cls, data: Any) -> NameMismatch | None:
        if not isinstance(data, dict):
            return None
        checked_at = _parse_iso(data.get("checked_at"))
        if checked_at is None:
            return None
        fingerprint = data.get("registry_fingerprint")
        return cls(
            mismatch=bool(data.get("mismatch", False)),
            checked_at=checked_at,
            auth_generation=int(data.get("auth_generation", 0) or 0),
            registry_fingerprint=fingerprint if isinstance(fingerprint, str) else "",
            consent_epoch=_epoch(data.get("consent_epoch")),
        )


@dataclass(frozen=True, slots=True)
class HardExpiry:
    """What is known of a refresh token's five-year clock.

    ``observed``: a token response carried ``x_refresh_token_hard_expires_in``
    and ``at`` is that timestamp. ``upper_bound_from_consent``: no response
    has carried it; ``at`` is the documented five-year maximum counted from a
    connect's consent date, which is at most seconds after the grant, so the
    true hard expiry is no later than this. ``unknown``: neither -- an
    imported grant's age is not knowable here, and no date is invented.
    """

    basis: str
    at: datetime | None

    def days(self, *, now: datetime | None = None) -> float | None:
        if self.at is None:
            return None
        return (self.at - (now or datetime.now(UTC))).total_seconds() / 86400

    def describe(self, *, now: datetime | None = None) -> str:
        days = self.days(now=now)
        if self.basis == "observed":
            return f"in {days:.1f} days (reported by Intuit; only a re-consent restarts it)"
        if self.basis == "upper_bound_from_consent":
            return (
                f"unknown; no later than {days:.1f} days (the five-year maximum "
                f"counted from this consent)"
            )
        return "unknown (Intuit did not report it and the grant's age is not recorded)"


@dataclass(frozen=True, slots=True)
class TokenRow:
    entity: str
    environment: str
    client_id_fingerprint: str
    refresh_token: str
    access_token: str
    access_token_expires_at: datetime
    #: The INACTIVITY clock: 100 days, restarted when Intuit issues a new
    #: refresh-token value, at most once per 24 hours (observed 2026-09-26;
    #: see :meth:`days_to_inactivity_expiry`). Not the hard limit; see
    #: :meth:`hard_expiry`.
    refresh_token_expires_at: datetime
    consented_at: datetime
    auth_generation: int
    last_refresh_at: datetime | None = None
    last_refresh_by: RefreshActor | None = None
    needs_consent: bool = False
    refresh_uncertain: bool = False
    recovery_hint: str | None = None
    name_mismatch: NameMismatch | None = None
    capabilities: dict[str, Any] | None = None
    #: Carried from the :class:`oauth.TokenResponse` that produced this pair.
    #: A row of unknown provenance is live.
    synthetic: bool = False
    #: The HARD clock, when a token response carried the opt-in
    #: ``x_refresh_token_hard_expires_in``. ``None`` is unknown and stays
    #: unknown: no date is invented.
    refresh_token_hard_expires_at: datetime | None = None
    #: How the consent this row runs on was obtained: ``connect`` (the grant
    #: was issued seconds before ``consented_at``), ``import`` (the grant is
    #: older than this store's record of it by an unknown amount) or
    #: ``unknown`` (no record of how).
    grant_origin: str = "unknown"
    #: The identity of the consent this row runs on, as an epoch. Copied from
    #: the document's per-realm counter by :meth:`TokenStore.commit_consent`;
    #: a refresh carries it forward unchanged. The document keeps the counter
    #: itself, because it has to outlive the row: a revoke advances it with no
    #: row to write to.
    consent_epoch: int = 0

    def __post_init__(self) -> None:
        if not self.synthetic:
            live_values.register(self.access_token, self.refresh_token)

    # -- derived -----------------------------------------------------------

    def access_token_valid(self, *, now: datetime | None = None, skew: float = 60.0) -> bool:
        moment = now or datetime.now(UTC)
        return self.access_token_expires_at - timedelta(seconds=skew) > moment

    def refresh_age_seconds(self, *, now: datetime | None = None) -> float | None:
        if self.last_refresh_at is None:
            return None
        return ((now or datetime.now(UTC)) - self.last_refresh_at).total_seconds()

    def is_stale(self, *, now: datetime | None = None) -> bool:
        """Staleness is computed per realm, and it is an observation.

        A realm is stale when its own ``last_refresh_at`` is old, independent of
        every other realm -- a healthy realm must never mask another's age. A
        realm that has never been refreshed is stale by definition, since the
        consent it still runs on is the oldest credential state there is.
        """
        age = self.refresh_age_seconds(now=now)
        return age is None or age > STALE_REFRESH_SECONDS

    def days_to_inactivity_expiry(self, *, now: datetime | None = None) -> float:
        """Days left on the 100-day inactivity clock, read from the instant stored.

        The clock restarts when Intuit issues a new refresh-token VALUE, which
        happens at most once per 24 hours (Intuit's Authorization FAQ); a
        refresh inside those 24 hours returns the same value and the same
        expiry. Observed 2026-09-26 on both sandbox companies: each stored
        instant sat 101 days after the refresh that last issued a new value,
        and two refreshes a minute apart on 2026-09-24 left it where it was.
        No refresh moves the hard clock.
        """
        return (self.refresh_token_expires_at - (now or datetime.now(UTC))).total_seconds() / 86400

    def hard_expiry(self) -> HardExpiry:
        """What is known of the five-year clock: observed, bounded, or unknown."""
        if self.refresh_token_hard_expires_at is not None:
            return HardExpiry("observed", self.refresh_token_hard_expires_at)
        if self.grant_origin == "connect":
            return HardExpiry(
                "upper_bound_from_consent",
                self.consented_at + timedelta(seconds=REFRESH_TOKEN_HARD_LIMIT_SECONDS),
            )
        return HardExpiry("unknown", None)

    def days_to_access_expiry(self, *, now: datetime | None = None) -> float:
        return (self.access_token_expires_at - (now or datetime.now(UTC))).total_seconds() / 86400

    def auth_state(
        self, *, now: datetime | None = None, registry_display_name: str | None
    ) -> str:
        """The state a diagnostic reports.

        ``registry_display_name`` is the name the caller's registry holds for
        this realm. The persisted guard verdict is applied only if it was
        checked against that exact name. With no name to bind to (``None``)
        no verdict is applied: "mismatch" is a statement about one pair of
        names, not a property of the row.
        """
        if self.needs_consent:
            return "needs_consent"
        if self.refresh_uncertain:
            return "refresh_uncertain"
        if self.days_to_inactivity_expiry(now=now) <= 0:
            return "expired"
        # A passed upper bound is a certainty too: the grant is at least as
        # old as this store's record of its consent.
        hard_days = self.hard_expiry().days(now=now)
        if hard_days is not None and hard_days <= 0:
            return "expired"
        verdict = self.guard_verdict_for(registry_display_name)
        if verdict is not None and verdict.mismatch:
            return "name_mismatch"
        return "authorized"

    def guard_verdict_for(self, registry_display_name: str | None) -> NameMismatch | None:
        """The persisted verdict, if it was checked against this name, under
        this row's consent."""
        verdict = self.name_mismatch
        if verdict is None or registry_display_name is None:
            return None
        if verdict.consent_epoch != self.consent_epoch:
            return None
        return verdict if verdict.applies_to(registry_display_name) else None

    # -- transitions -------------------------------------------------------

    def with_tokens(
        self,
        response: oauth.TokenResponse,
        actor: RefreshActor,
        *,
        now: datetime | None = None,
    ) -> TokenRow:
        """Advance to the next generation.

        The generation, not the token string, is the identity of a credential
        here. Two refreshes that happen to return the same refresh token are
        still two distinct generations.
        """
        return replace(
            self,
            refresh_token=response.refresh_token,
            access_token=response.access_token,
            access_token_expires_at=response.access_token_expires_at,
            refresh_token_expires_at=response.refresh_token_expires_at,
            # An earlier observation stands when the new response omits the
            # field: the value is an absolute timestamp, and no refresh has
            # been observed to move the five-year clock.
            refresh_token_hard_expires_at=(
                response.refresh_token_hard_expires_at or self.refresh_token_hard_expires_at
            ),
            last_refresh_at=now or datetime.now(UTC),
            last_refresh_by=actor,
            auth_generation=self.auth_generation + 1,
            needs_consent=False,
            refresh_uncertain=False,
            recovery_hint=None,
            synthetic=response.synthetic,
        )

    # -- serialisation -----------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "environment": self.environment,
            "client_id_fingerprint": self.client_id_fingerprint,
            "refresh_token": self.refresh_token,
            "access_token": self.access_token,
            "access_token_expires_at": _iso(self.access_token_expires_at),
            "refresh_token_expires_at": _iso(self.refresh_token_expires_at),
            "consented_at": _iso(self.consented_at),
            "auth_generation": self.auth_generation,
            "last_refresh_at": _iso(self.last_refresh_at) if self.last_refresh_at else None,
            "last_refresh_by": self.last_refresh_by.to_json() if self.last_refresh_by else None,
            "needs_consent": self.needs_consent,
            "refresh_uncertain": self.refresh_uncertain,
            "recovery_hint": self.recovery_hint,
            "name_mismatch": self.name_mismatch.to_json() if self.name_mismatch else None,
            "capabilities": self.capabilities,
            "synthetic": self.synthetic,
            "refresh_token_hard_expires_at": (
                _iso(self.refresh_token_hard_expires_at)
                if self.refresh_token_hard_expires_at
                else None
            ),
            "grant_origin": self.grant_origin,
            "consent_epoch": self.consent_epoch,
        }

    @classmethod
    def from_json(cls, data: Any) -> TokenRow:
        if not isinstance(data, dict):
            raise StoreError("token store row is malformed")
        try:
            return cls(
                entity=str(data["entity"]),
                environment=str(data["environment"]),
                client_id_fingerprint=str(data["client_id_fingerprint"]),
                refresh_token=str(data["refresh_token"]),
                access_token=str(data["access_token"]),
                access_token_expires_at=_require_iso(data["access_token_expires_at"]),
                refresh_token_expires_at=_require_iso(data["refresh_token_expires_at"]),
                consented_at=_require_iso(data["consented_at"]),
                auth_generation=int(data["auth_generation"]),
                last_refresh_at=_parse_iso(data.get("last_refresh_at")),
                last_refresh_by=RefreshActor.from_json(data.get("last_refresh_by")),
                needs_consent=bool(data.get("needs_consent", False)),
                refresh_uncertain=bool(data.get("refresh_uncertain", False)),
                recovery_hint=data.get("recovery_hint"),
                name_mismatch=NameMismatch.from_json(data.get("name_mismatch")),
                capabilities=data.get("capabilities"),
                # Only an explicit `true` exempts a row; anything else is live.
                synthetic=data.get("synthetic") is True,
                refresh_token_hard_expires_at=_parse_iso(data.get("refresh_token_hard_expires_at")),
                grant_origin=_grant_origin(data.get("grant_origin")),
                consent_epoch=_epoch(data.get("consent_epoch")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StoreError("token store row is malformed") from exc


@dataclass(frozen=True, slots=True)
class MalformedRow:
    """A row this build could not decode, kept exactly as it was found.

    A malformed row blocks its own entity, where unreadable whole-store
    ciphertext is a whole-store failure. Isolation is not deletion: the raw
    row is written back unchanged on every commit, because its bytes may be a
    credential a corrected build can read. Only a consent for the same realm
    replaces it.
    """

    raw: Any
    reason: str


@dataclass
class StoreDocument:
    environment: str
    rows: dict[str, TokenRow] = field(default_factory=dict)
    malformed: dict[str, MalformedRow] = field(default_factory=dict)
    schema_version: int = STORE_SCHEMA_VERSION
    #: Per realm, the number of consents and revokes the realm has seen: the
    #: consent's identity. Kept on the document rather than only on the row
    #: because it must survive the row's absence -- a ceremony that began with
    #: no row must still see a consent and a revoke that both happened while
    #: it ran. A document without the key reads as zero for every realm.
    consent_epochs: dict[str, int] = field(default_factory=dict)

    def epoch_of(self, realm_id: str) -> int:
        return self.consent_epochs.get(realm_id, 0)

    def advance_epoch(self, realm_id: str) -> int:
        """Advance this realm's consent epoch and return the new value."""
        self.consent_epochs[realm_id] = self.epoch_of(realm_id) + 1
        return self.consent_epochs[realm_id]

    def to_json(self) -> dict[str, Any]:
        rows: dict[str, Any] = {realm: row.to_json() for realm, row in self.rows.items()}
        for realm, broken in self.malformed.items():
            rows.setdefault(realm, broken.raw)
        return {
            "schema_version": self.schema_version,
            "environment": self.environment,
            "rows": rows,
            "consent_epochs": dict(self.consent_epochs),
        }

    @classmethod
    def from_json(cls, data: Any, *, environment: str) -> StoreDocument:
        if not isinstance(data, dict):
            raise StoreError("token store is malformed")
        version = data.get("schema_version")
        if version != STORE_SCHEMA_VERSION:
            raise StoreError(
                f"token store schema version {version!r} is not supported by this build"
            )
        stored_env = data.get("environment")
        if stored_env != environment:
            raise ConfigurationError(
                f"token store holds environment {stored_env!r} but QBO_ENV is "
                f"{environment!r}. This is a configuration problem, not an expired "
                f"credential."
            )
        raw_rows = data.get("rows")
        if not isinstance(raw_rows, dict):
            raise StoreError("token store is malformed")
        rows: dict[str, TokenRow] = {}
        malformed: dict[str, MalformedRow] = {}
        for realm, raw in raw_rows.items():
            try:
                rows[str(realm)] = TokenRow.from_json(raw)
            except StoreError as exc:
                # One bad realm must not take the other realms down: the row
                # is set aside, and every access to *its* realm raises.
                malformed[str(realm)] = MalformedRow(raw=raw, reason=str(exc))
        raw_epochs = data.get("consent_epochs", {})
        if not isinstance(raw_epochs, dict):
            raise StoreError("token store is malformed")
        epochs: dict[str, int] = {}
        for realm, value in raw_epochs.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise StoreError("token store is malformed")
            epochs[str(realm)] = value
        return cls(
            environment=environment, rows=rows, malformed=malformed, consent_epochs=epochs
        )


# --------------------------------------------------------------------------
# Journal
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JournalRecord:
    """A write-ahead record for one refresh.

    Recovery adopts a record only for the row it was written against: the same
    realm, environment, full client-id digest and starting generation
    (``_recover_locked``). A record that differs in any of them is passed over.
    The unique id pairs an intent with its result. The digest of the submitted
    token is recorded, not compared: the generation is what tells a stale
    record from a live one.
    """

    op_id: str
    stage: str  # "intent" | "result"
    realm_id: str
    environment: str
    client_id_fingerprint: str
    generation_from: int
    old_refresh_digest: str
    written_at: datetime
    tokens: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "op_id": self.op_id,
            "stage": self.stage,
            "realm_id": self.realm_id,
            "environment": self.environment,
            "client_id_fingerprint": self.client_id_fingerprint,
            "generation_from": self.generation_from,
            "old_refresh_digest": self.old_refresh_digest,
            "written_at": _iso(self.written_at),
            "tokens": self.tokens,
        }

    @classmethod
    def from_json(cls, data: Any) -> JournalRecord | None:
        """Returns ``None`` for anything malformed. A corrupt record is not an
        error to propagate here: the journal's reader decides what it means."""
        if not isinstance(data, dict):
            return None
        try:
            written_at = _parse_iso(data.get("written_at"))
            if written_at is None:
                return None
            return cls(
                op_id=str(data["op_id"]),
                stage=str(data["stage"]),
                realm_id=str(data["realm_id"]),
                environment=str(data["environment"]),
                client_id_fingerprint=str(data["client_id_fingerprint"]),
                generation_from=int(data["generation_from"]),
                old_refresh_digest=str(data["old_refresh_digest"]),
                written_at=written_at,
                tokens=data.get("tokens"),
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            return None


# --------------------------------------------------------------------------
# The canonical lock
# --------------------------------------------------------------------------


@dataclass
class _ProcessLock:
    mutex: threading.RLock
    depth: int = 0
    fd: int | None = None


_PROCESS_LOCKS: dict[str, _ProcessLock] = {}
_REGISTRY_MUTEX = threading.Lock()


def _process_lock_for(path: Path) -> _ProcessLock:
    key = str(path)
    with _REGISTRY_MUTEX:
        existing = _PROCESS_LOCKS.get(key)
        if existing is None:
            existing = _ProcessLock(mutex=threading.RLock())
            _PROCESS_LOCKS[key] = existing
        return existing


@contextmanager
def canonical_lock(lock_path: Path, timeout: float = DEFAULT_LOCK_TIMEOUT) -> Iterator[None]:
    """The one lock every store mutation takes.

    ``flock`` alone is not enough, for a reason observed rather than assumed:
    a second ``flock`` from the *same* process on a second descriptor does NOT
    succeed, it conflicts with the first (observed on macOS:
    ``LOCK_EX | LOCK_NB`` on a second ``open`` of the lock file answers
    ``EAGAIN``; the Linux ``flock(2)`` page says the same, because descriptors
    from separate ``open`` calls are independent). So a mutation that calls
    another mutation would open a second descriptor and wait on its own lock
    until the timeout. The in-process mutex is reentrant and counts depth, so
    the file lock is taken once per thread's outermost mutation, and it gives
    same-process threads a bounded wait, which the OS lock has no notion of.

    ``timeout`` covers both waits together: the file lock gets what the mutex
    left of it, so a caller that bounded it by its budget is not kept twice as
    long.
    """
    state = _process_lock_for(lock_path)
    expires = time.monotonic() + timeout
    if not state.mutex.acquire(timeout=timeout):
        raise StoreLockTimeout(LOCK_TIMEOUT_TEXT)
    try:
        if state.depth == 0:
            state.fd = _flock_exclusive(lock_path, max(0.0, expires - time.monotonic()))
        state.depth += 1
        try:
            yield
        finally:
            state.depth -= 1
            if state.depth == 0 and state.fd is not None:
                try:
                    fcntl.flock(state.fd, fcntl.LOCK_UN)
                finally:
                    os.close(state.fd)
                    state.fd = None
    finally:
        state.mutex.release()


def _mkdir_private(directory: Path) -> None:
    """Create the store directory 0700, whatever the umask says.

    ``mkdir(mode=0o700)`` is masked by the umask, so the mode is set explicitly
    on every directory this call creates -- and only on those: an existing
    directory's mode is the operator's decision.
    """
    for candidate in reversed([directory, *directory.parents]):
        if candidate.exists():
            continue
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            continue
        os.chmod(candidate, 0o700)


def _open_nofollow(path: Path, flags: int, mode: int = 0o600) -> int:
    """``os.open`` that refuses a symlink at ``path``.

    A symlink in the store directory would redirect the lock or the store
    file somewhere this process does not control. ``O_NOFOLLOW`` makes the
    open fail on a symlink instead of following it: ``ELOOP`` on Linux and
    macOS, ``EMLINK`` on some BSDs.
    """
    try:
        return os.open(path, flags | os.O_NOFOLLOW, mode)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise StoreError(
                f"{path.name} is a symlink; the token store refuses symlinks"
            ) from exc
        raise


def _read_nofollow(path: Path) -> bytes:
    fd = _open_nofollow(path, os.O_RDONLY)
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def _same_inode(fd: int, path: Path) -> bool:
    try:
        current = os.stat(path)
    except FileNotFoundError:
        return False
    held = os.fstat(fd)
    return (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino)


def _flock_exclusive(lock_path: Path, timeout: float) -> int:
    """Take the file lock, on the inode that is the lock file when it is taken.

    Two protections. The path is opened ``O_NOFOLLOW``, so a symlink planted
    at the lock path is refused rather than followed. And after ``flock``
    succeeds, the descriptor's inode is compared with the path's: if the file
    was replaced while this caller waited, the lock it holds is on an orphaned
    inode that no other process will ever contend for -- two holders, each
    believing it is alone. The open is retried on the current file until the
    two agree.
    """
    _mkdir_private(lock_path.parent)
    deadline = time.monotonic() + timeout
    while True:
        fd = _open_nofollow(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise StoreError("could not take the token store lock") from exc
                    if time.monotonic() >= deadline:
                        raise StoreLockTimeout(LOCK_TIMEOUT_TEXT) from exc
                    time.sleep(_LOCK_POLL_SECONDS)
            if _same_inode(fd, lock_path):
                return fd
            # Replaced while waiting. Release the orphan and take the current file.
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)
        if time.monotonic() >= deadline:
            raise StoreLockTimeout(LOCK_TIMEOUT_TEXT)


# --------------------------------------------------------------------------
# Durable write
# --------------------------------------------------------------------------


def write_atomic(path: Path, data: bytes, *, mode: int) -> None:
    """Temp file at ``mode`` -> fsync contents -> replace -> fsync directory.

    The temp file is created *at* the final protection mode rather than chmod'd
    afterwards, so the token pair is never momentarily world-readable. The
    directory fsync is what makes the rename itself durable; without it a crash
    can leave the directory entry pointing at the old inode even though the new
    file's contents were flushed.
    """
    _mkdir_private(path.parent)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        # O_EXCL: a symlink planted at the temp path is a failure, not a target.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise StoreError(f"could not write the token store: {exc.strerror}") from exc
    except BaseException:
        # A cancellation mid-write must not leave a credential-bearing temp
        # file behind.
        tmp.unlink(missing_ok=True)
        raise

    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


# --------------------------------------------------------------------------
# Location
# --------------------------------------------------------------------------


def assert_store_location_allowed(directory: Path) -> None:
    """Refuse a store directory inside a git working copy or a sync root.

    A rotating credential in a git working copy gets committed sooner or later,
    and a credential in a sync root gets copied to another machine that then
    refreshes it independently -- which breaks the chain for both.

    This check is necessarily incomplete, and saying so is part of its job:
    arbitrary sync software cannot be detected. A backup agent, an rsync cron
    entry, a network home directory or a filesystem snapshot will all copy this
    file without leaving any marker to look for. Treat the list below as a
    guard against the common accidents, not as a guarantee.
    """
    resolved = directory.expanduser().resolve()

    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            raise StoreLocationRefused(
                f"refusing a token store inside a git working copy ({candidate}). "
                f"Point --token-store somewhere outside the repository."
            )

    home = Path.home().resolve()
    try:
        relative = resolved.relative_to(home)
    except ValueError:
        return
    parts = relative.parts
    for root in _CLOUD_SYNC_ROOTS:
        root_parts = Path(root).parts
        if parts[: len(root_parts)] == root_parts:
            raise StoreLocationRefused(
                f"refusing a token store inside a cloud-sync directory (~/{root}). "
                f"A rotating credential must not be replicated to another machine."
            )


def default_store_directory() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "qbo-multicompany-mcp"


# --------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------


class TokenStore:
    """One store, one environment, one canonical lock.

    Opened lazily, inside a tool call. ``tools/list`` must answer with only
    ``QBO_ENTITIES_FILE`` set, creating no store and no directory -- a client
    that is merely enumerating tools has not asked this server to touch a
    credential, and creating a directory on enumeration would make a
    read-only inspection leave traces.
    """

    def __init__(
        self,
        directory: Path,
        *,
        environment: str,
        key_source: KeySource,
        allow_plaintext: bool,
        credentials: AppCredentials | None = None,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    ) -> None:
        self.directory = Path(directory).expanduser()
        self.environment = environment
        self.key_source = key_source
        self.allow_plaintext = allow_plaintext
        self.credentials = credentials
        self.lock_timeout = lock_timeout
        self._key: bytes | None = None
        self._key_loaded = False

        assert_store_location_allowed(self.directory)
        if key_source is KeySource.NONE and not allow_plaintext:
            raise PlaintextStoreRefused()

    # -- paths -------------------------------------------------------------

    @property
    def suffix(self) -> str:
        return "json" if self.key_source is KeySource.NONE else "enc"

    @property
    def document_path(self) -> Path:
        return self.directory / f"tokens.{self.environment}.{self.suffix}"

    @property
    def journal_path(self) -> Path:
        return self.directory / f"tokens.{self.environment}.journal.{self.suffix}"

    @property
    def lock_path(self) -> Path:
        return self.directory / LOCK_FILENAME

    def exists(self) -> bool:
        return self.document_path.exists()

    # -- lock --------------------------------------------------------------

    @contextmanager
    def lock(self, timeout: float | None = None) -> Iterator[None]:
        """The canonical lock, never waited for past a tool call's budget.

        Only the wait is bounded. Once the lock is held, the deadline is
        consulted once more, as the last step before a refresh is submitted
        (``_refresh_locked``), and never after it, so a refresh that has begun
        runs to its durable write.
        """
        wait = timeout if timeout is not None else self.lock_timeout
        with canonical_lock(self.lock_path, call_deadline.bounded(wait)):
            yield

    # -- crypto ------------------------------------------------------------

    def _key_material(self) -> bytes | None:
        if not self._key_loaded:
            self._key = load_key(self.key_source, self.environment)
            self._key_loaded = True
        return self._key

    def _encode(self, document: StoreDocument) -> bytes:
        payload = json.dumps(document.to_json(), indent=2, sort_keys=True).encode("utf-8")
        return seal(self._key_material(), payload, environment=self.environment)

    def _decode(self, blob: bytes) -> StoreDocument:
        payload = unseal(self._key_material(), blob, environment=self.environment)
        try:
            data = json.loads(payload)
        except ValueError as exc:
            raise StoreError("token store is not valid JSON") from exc
        return StoreDocument.from_json(data, environment=self.environment)

    # -- read / write ------------------------------------------------------

    def _load_locked(self) -> StoreDocument:
        if not self.document_path.exists():
            return StoreDocument(environment=self.environment)
        try:
            blob = _read_nofollow(self.document_path)
        except OSError as exc:
            raise StoreError(f"could not read the token store: {exc.strerror}") from exc
        return self._decode(blob)

    def _commit_locked(self, document: StoreDocument) -> None:
        write_atomic(
            self.document_path,
            self._encode(document),
            mode=store_file_mode(self.key_source),
        )

    def read(self) -> StoreDocument:
        """Read the store, running journal recovery first -- every time.

        Not once per process. A long-lived object that recovered when it opened
        cannot see a result another process journalled afterwards, so it works
        from a generation that has already been superseded and its next refresh
        overwrites the only persisted copy of the live pair. Recovery costs one
        stat on a store with no journal, and one read once it has one.
        """
        with self.lock():
            document = self._load_locked()
            self._recover_locked(document)
            return document

    def rows(self) -> dict[str, TokenRow]:
        return self.read().rows

    def get_row(self, realm_id: str) -> TokenRow | None:
        """A row, for inspection. Not for use as a credential.

        ``status``, the keepalive report and the tests read rows to describe
        them. Anything that is about to *send* the token in a row goes through
        :meth:`usable_row`, which checks the configuration binding first.

        A malformed row raises rather than reading as absent: "absent" invites
        a connect, and the operator needs to know what actually happened.
        """
        return self._row_of(self.read(), realm_id)

    @staticmethod
    def _row_of(document: StoreDocument, realm_id: str) -> TokenRow | None:
        """A row from a loaded document, or the error a malformed one is."""
        broken = document.malformed.get(realm_id)
        if broken is not None:
            raise MalformedRowError(malformed_row_text(realm_id, broken.reason))
        return document.rows.get(realm_id)

    def usable_row(self, realm_id: str, *, entity_key: str) -> TokenRow | None:
        """A row whose configuration binding has been checked.

        Every call is bound to an immutable entity/environment/realm tuple.
        Enforced on the refresh paths alone, for as long as a cached access
        token stayed valid, a store carried over from a different configured
        app, or a row left inconsistent by a registry change, would have its
        credential sent to Intuit instead of raising a configuration error.
        The check belongs before every use, not only before the uses that
        happen to need the network.

        With no app configured, nothing is released at all. The app leg of the
        binding compares the row's client fingerprint with the running app,
        and with no app it would compare nothing: a cached token from a row
        written by any app would go out. The refusal sits here, on the read
        that precedes a send, and not on :meth:`get_row`, because inspection
        must keep working without secrets.
        """
        if self.credentials is None:
            raise ConfigurationError(NO_APP_CREDENTIALS_TEXT)
        row = self._row_of(self.read(), realm_id)
        if row is not None:
            self._assert_configuration(row, expected_entity=entity_key)
        return row

    def check_binding(self, row: TokenRow, *, entity_key: str) -> None:
        """The check :meth:`usable_row` makes, for a row obtained another way.

        Raises :class:`ConfigurationError` when the row belongs to another
        environment, app or entity, so its token is never sent under this one.
        """
        if self.credentials is None:
            raise ConfigurationError(NO_APP_CREDENTIALS_TEXT)
        self._assert_configuration(row, expected_entity=entity_key)

    def update_row(self, realm_id: str, mutate: Callable[[TokenRow], TokenRow]) -> TokenRow:
        """Apply ``mutate`` to one row under the canonical lock.

        Guard verdicts and the capability cache come through here, so they take
        the same lock as a refresh does. A "small" write that skipped the lock
        would read a document, be overtaken by a refresh, and write back the
        pre-refresh token pair -- destroying a live credential to record a
        boolean.
        """
        with self.lock():
            document = self._load_locked()
            self._recover_locked(document)
            row = self._row_of(document, realm_id)
            if row is None:
                raise NeedsConsentError("no stored credential for this realm")
            updated = mutate(row)
            document.rows[realm_id] = updated
            self._commit_locked(document)
            return updated

    def record_guard_verdict(self, realm_id: str, verdict: NameMismatch) -> TokenRow:
        """Persist a guard verdict, unless it is stale.

        Stale in either of two ways. An old process must never overwrite a
        newer observation -- two processes can verify the same realm, and the
        one that finishes second may have started first. And a verdict checked
        under a consent the row no longer runs on is about a company file the
        row may no longer denote: the row's consent epoch is compared before
        its clock is, because the newest timestamp can belong to the older
        consent.
        """

        def keep_current(row: TokenRow) -> TokenRow:
            if verdict.consent_epoch != row.consent_epoch:
                return row
            current = row.name_mismatch
            if current is not None and current.checked_at > verdict.checked_at:
                return row
            return replace(row, name_mismatch=verdict)

        return self.update_row(realm_id, keep_current)

    # -- journal -----------------------------------------------------------

    def _write_journal_locked(self, records: list[JournalRecord]) -> None:
        payload = json.dumps(
            {"schema_version": STORE_SCHEMA_VERSION, "records": [r.to_json() for r in records]},
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        write_atomic(
            self.journal_path,
            seal(self._key_material(), payload, environment=self.environment),
            mode=store_file_mode(self.key_source),
        )

    def _read_journal_locked(self) -> list[JournalRecord]:
        """The journal's records, none if there is no journal.

        Two failures, which mean opposite things. Contents that were read and
        make no sense raise :class:`_JournalUnreadable`: the journal may be the
        only record of an accepted refresh, so it never reads as empty. A read
        that failed says nothing about the contents, which may be the only copy
        of a new pair, so it raises :class:`StoreError`, changes nothing, and
        the next call reads again.
        """
        if not self.journal_path.exists():
            return []
        try:
            blob = _read_nofollow(self.journal_path)
        except (StoreError, IsADirectoryError):
            # A symlink refused, or a directory, in the journal's place: damage,
            # and it won't pass.
            raise _JournalUnreadable from None
        except OSError as exc:
            raise StoreError(
                f"could not read {self.journal_path.name}: {exc.strerror}. Nothing was changed. "
                f"If this lasts, fix the file's owner or mode, and don't move it: it may be "
                f"the only record of a refresh"
            ) from None
        try:
            payload = unseal(self._key_material(), blob, environment=self.environment)
            data = json.loads(payload)
        except (ValueError, StoreError):
            raise _JournalUnreadable from None
        raw = data.get("records") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            raise _JournalUnreadable
        parsed = [JournalRecord.from_json(item) for item in raw]
        if any(record is None or not _can_act_on(record) for record in parsed):
            raise _JournalUnreadable
        return [record for record in parsed if record is not None]

    def _clear_journal_locked(self) -> None:
        """Clear durably, and only after the main store has committed.

        Clearing first would create a window in which the new pair exists
        nowhere: not yet in the store, no longer in the journal.
        """
        if self.journal_path.exists():
            self._write_journal_locked([])

    def _drop_journal_records_locked(self, realm_id: str) -> None:
        """Drop one realm's journal records and nobody else's.

        What a revoke owns is its own realm. The journal is shared by every
        realm in the store, so a revoke that cleared it whole would discard
        another realm's durable result. Recovery has already run under this
        lock, so this is the ownership rule made explicit rather than a second
        recovery.

        A read that fails here leaves the records where they are: the row is
        already gone, and recovery ignores a record for a realm with no row.
        """
        try:
            records = self._read_journal_locked()
        except (_JournalUnreadable, StoreError):
            return
        remaining = [record for record in records if record.realm_id != realm_id]
        if len(remaining) != len(records):
            self._write_journal_locked(remaining)

    def _drop_journal_after_a_definite_outcome(self) -> None:
        """Clear the journal on a path where Intuit's answer is known.

        An intent that outlives its refresh is read by recovery as an ambiguous
        outcome, and stops the realm. That is the right reading when the outcome
        really is unknown and the wrong one when the endpoint answered -- so
        every path that learns the answer says so, here.

        A failure to clear is swallowed on purpose: the caller is already
        raising the real error, and a surviving intent fails in the safe
        direction.
        """
        try:
            self._clear_journal_locked()
        except StoreError:
            pass

    def _quarantine_locked(
        self, document: StoreDocument, realm_id: str, row: TokenRow, *, hint: str
    ) -> None:
        """Record an ambiguous outcome on the row, durably, then drop the intent.

        The row now records the ambiguity, so the intent has nothing left to
        say. Clearing only after the commit: the other order leaves a window
        in which neither copy exists.
        """
        document.rows[realm_id] = replace(row, refresh_uncertain=True, recovery_hint=hint)
        self._commit_locked(document)
        self._drop_journal_after_a_definite_outcome()

    def _stop_after_an_unreadable_journal_locked(self, document: StoreDocument) -> bool:
        """Nothing can say which company an unreadable journal concerned, so
        every usable row stops, as after an unanswered intent, until ``verify``
        or a new consent settles it. Tools refuse a stopped row, so this costs
        each company a command.

        The file moves aside rather than away: it's the evidence, and left in
        place the next refresh or consent would overwrite it. A failed move is
        left for the next recovery, since the rows are already stopped and
        durable, and a raised error here would stop every call on the store.
        """
        changed = False
        for realm_id, row in document.rows.items():
            if row.needs_consent or row.refresh_uncertain:
                continue
            document.rows[realm_id] = replace(
                row, refresh_uncertain=True, recovery_hint=_unreadable_journal_hint(row.entity)
            )
            changed = True
        if changed:
            self._commit_locked(document)
        stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
        try:
            self.journal_path.replace(self.journal_path.with_name(f"{self.journal_path.name}.unreadable-{stamp}"))
        except OSError:
            pass
        return changed

    def _recover_locked(self, document: StoreDocument) -> bool:
        """Adopt what the journal proves, quarantine what it leaves in doubt.

        Two kinds of record survive a refresh that did not finish, and they mean
        opposite things. A *result* is a validated pair that reached the disk
        before the main store did: adopting it is the whole reason the journal
        exists. An *intent* with no matching result says only that a token was
        submitted -- it cannot distinguish "the request never left" from
        "Intuit accepted it and the answer was lost". Discarding one of those
        silently picks the cheaper reading, and picking wrong resubmits a token
        that may be spent.
        """
        try:
            records = self._read_journal_locked()
        except _JournalUnreadable:
            return self._stop_after_an_unreadable_journal_locked(document)
        if not records:
            return False

        answered = {record.op_id for record in records if record.stage == "result"}

        applicable: dict[str, list[JournalRecord]] = {}
        unanswered: dict[str, list[JournalRecord]] = {}
        for record in records:
            if record.environment != self.environment:
                continue
            row = document.rows.get(record.realm_id)
            if row is None:
                # Cross-realm, or a realm that has since been revoked.
                continue
            if record.client_id_fingerprint != row.client_id_fingerprint:
                continue
            # Adoption is decided by generation alone. A record whose
            # generation is behind the row has already been applied (replay);
            # one that is ahead refers to a state this store never reached. The
            # same test retires an intent: a row that has moved on is evidence
            # the chain continued past it.
            if record.generation_from != row.auth_generation:
                continue
            if record.stage == "result" and isinstance(record.tokens, dict):
                applicable.setdefault(record.realm_id, []).append(record)
            elif record.stage == "intent" and record.op_id not in answered:
                unanswered.setdefault(record.realm_id, []).append(record)

        changed = False
        adopted_realms: set[str] = set()
        quarantine: dict[str, str] = {}
        for realm_id, candidates in applicable.items():
            if len(candidates) != 1:
                # Two records claim the same starting generation for one realm,
                # and nothing here can say which one Intuit honoured. Refusing
                # to pick is right; leaving the row usable would not be -- the
                # next refresh would submit the stored token as if nothing had
                # happened. This package writes the journal whole, one record
                # at a time, so this is a corrupt or foreign journal; still,
                # the local record of which token is live is lost, and that
                # is what refresh_uncertain is for.
                quarantine[realm_id] = _competing_results_hint(document.rows[realm_id].entity)
                continue
            record = candidates[0]
            row = document.rows[realm_id]
            adopted = _row_from_journal_tokens(row, record)
            if adopted is None:
                continue
            document.rows[realm_id] = adopted
            adopted_realms.add(realm_id)
            changed = True

        for realm_id in unanswered:
            if realm_id in adopted_realms:
                # The outcome is known after all: this realm's result landed.
                continue
            quarantine.setdefault(realm_id, _unanswered_intent_hint(document.rows[realm_id].entity))

        for realm_id, hint in quarantine.items():
            row = document.rows[realm_id]
            if row.needs_consent or row.refresh_uncertain:
                # Already stopped, for this reason or a stronger one.
                continue
            document.rows[realm_id] = replace(row, refresh_uncertain=True, recovery_hint=hint)
            changed = True

        if changed:
            self._commit_locked(document)
        self._clear_journal_locked()
        return changed

    # -- configuration checks ---------------------------------------------

    def _assert_configuration(
        self, row: TokenRow, *, expected_entity: str | None = None
    ) -> None:
        """A store row from the wrong environment, app or entity is a
        configuration error, and never ``needs_consent``.

        Setting ``needs_consent`` here would send an operator through an Intuit
        consent screen to fix a typo in ``QBO_ENV`` -- and the re-consent would
        replace a perfectly good credential on the way.

        ``expected_entity`` is passed wherever the registry entry is in hand.
        The store alone cannot know which entity key a realm is supposed to
        belong to, so the refresh paths check the two legs they can and the
        caller supplies the third.
        """
        if row.environment != self.environment:
            raise ConfigurationError(config_mismatch_text(row.entity, "environment"))
        if (
            self.credentials is not None
            and row.client_id_fingerprint != self.credentials.fingerprint
        ):
            raise ConfigurationError(config_mismatch_text(row.entity, "client id"))
        if expected_entity is not None and row.entity != expected_entity:
            raise ConfigurationError(config_mismatch_text(expected_entity, "entity"))

    def _require_credentials(self) -> AppCredentials:
        if self.credentials is None:
            raise ConfigurationError(
                "no Intuit app credentials are configured. Set QBO_CLIENT_ID and "
                "QBO_CLIENT_SECRET in the environment."
            )
        return self.credentials

    # -- consent / import / revoke ----------------------------------------

    def consent_baseline(self, realm_id: str) -> ConsentBaseline:
        """Capture the consent state a ceremony begins from.

        Taken under the lock, briefly, before any human interaction, and
        handed back to :meth:`commit_consent` when the ceremony ends.
        """
        with self.lock():
            document = self._load_locked()
            self._recover_locked(document)
            row = document.rows.get(realm_id)
            epoch = document.epoch_of(realm_id)
        return ConsentBaseline(
            realm_id=realm_id,
            epoch=epoch,
            present=row is not None,
            consented_at=row.consented_at if row else None,
            auth_generation=row.auth_generation if row else None,
        )

    def commit_consent(
        self,
        *,
        entity: str,
        realm_id: str,
        response: oauth.TokenResponse,
        actor: RefreshActor,
        baseline: ConsentBaseline | None = None,
        origin: str = "unknown",
    ) -> TokenRow:
        """Persist a freshly consented pair.

        Takes the canonical lock, and takes it *after* the human interaction is
        over. The consent flow itself -- browser, callback, exchange -- runs
        unlocked, because a lock held across a consent screen would block every
        other process for as long as someone takes to find their password.

        ``baseline`` is what the ceremony saw before that interaction began.
        If the realm was revoked or a newer consent landed in the meantime,
        the ceremony is stale and stops here, without writing: the CLI then
        revokes the pair it received. ``None`` skips the check; the CLI always
        passes one, and test seeding does not.

        ``origin`` is how the grant was obtained -- ``connect`` or ``import``
        -- and decides whether the consent date bounds the hard expiry.
        """
        credentials = self._require_credentials()
        now = datetime.now(UTC)
        with self.lock():
            document = self._load_locked()
            self._recover_locked(document)
            previous = document.rows.get(realm_id)
            if baseline is not None and not baseline.still_holds(document):
                raise StaleConsentError(stale_consent_text(entity))
            generation = (previous.auth_generation + 1) if previous else 1
            # A new consent is a new identity for this realm, whether or not
            # a row was there before.
            epoch = document.advance_epoch(realm_id)
            row = TokenRow(
                entity=entity,
                environment=self.environment,
                client_id_fingerprint=credentials.fingerprint,
                refresh_token=response.refresh_token,
                access_token=response.access_token,
                access_token_expires_at=response.access_token_expires_at,
                refresh_token_expires_at=response.refresh_token_expires_at,
                refresh_token_hard_expires_at=response.refresh_token_hard_expires_at,
                grant_origin=_grant_origin(origin),
                consented_at=now,
                auth_generation=generation,
                last_refresh_at=now,
                last_refresh_by=actor,
                needs_consent=False,
                refresh_uncertain=False,
                recovery_hint=None,
                # A re-consent invalidates the guard verdict and the capability
                # cache: a new grant is re-checked. That the realm could now
                # denote a different company file is an assumption made in the
                # cautious direction (see session.py), not a documented fact.
                name_mismatch=None,
                capabilities=None,
                synthetic=response.synthetic,
                consent_epoch=epoch,
            )
            document.rows[realm_id] = row
            # A consent for this realm is the one thing that replaces a row
            # this build could not read.
            document.malformed.pop(realm_id, None)
            self._commit_locked(document)
            # A consent supersedes anything a half-finished refresh left behind.
            # The pair is stored by now, so a journal that can't be cleared is
            # no failed commit: read as one, the pair would be revoked. Recovery
            # already cleared a readable journal under this lock; one it couldn't
            # read or move aside stops every row, this one too, at the next read.
            try:
                self._clear_journal_locked()
            except StoreError:
                log.warning("the consent is stored, but the store's journal could not be cleared; run status")
            return row

    def revoke_row(self, realm_id: str) -> TokenRow | None:
        """Remove a realm's row. A malformed row is removed too, and returns
        ``None``: this build can't read a token from it to revoke.

        Recovery runs first, under the same lock, exactly as it does before
        every other mutation. Without it a revoke of realm B would load the
        main document without adopting realm A's journalled result, commit A's
        old pair, and then clear the whole journal -- the only durable copy of
        the pair Intuit had issued to A. And the journal is cleared only of
        this realm's records: the rest is not this revoke's to discard.

        A revoke advances the realm's consent epoch, so a ceremony that began
        before it stops rather than resurrecting the row -- and it does so
        **whether or not a row exists**. If the epoch advanced only when a row
        was present, a ceremony that began with no row, was revoked while the
        human was at the consent screen, and then finished would commit
        anyway: the store would look exactly as the ceremony had found it.
        """
        with self.lock():
            document = self._load_locked()
            self._recover_locked(document)
            row = document.rows.pop(realm_id, None)
            document.malformed.pop(realm_id, None)
            # Present row, malformed row or no row at all: the realm's consent
            # identity has been cancelled, and the cancellation is persisted.
            document.advance_epoch(realm_id)
            self._commit_locked(document)
            self._drop_journal_records_locked(realm_id)
            return row

    # -- refresh -----------------------------------------------------------

    def refresh(
        self,
        realm_id: str,
        *,
        actor: RefreshActor,
        force: bool = False,
        min_age_seconds: float | None = None,
        entity_key: str | None = None,
    ) -> TokenRow:
        """Refresh one realm under the canonical lock.

        ``min_age_seconds`` is re-checked *inside* the lock, not before taking
        it. Checking outside would let two keepalive runs both decide a realm
        was due, queue on the lock, and refresh it twice -- the second one
        submitting a token the first had already spent.

        ``entity_key`` is the registry entry the caller is acting for. A refresh
        submits a credential, so it checks the same tuple any other use does;
        callers holding a registry entry pass it.
        """
        with self.lock():
            document = self._load_locked()
            self._recover_locked(document)

            row = self._row_of(document, realm_id)
            if row is None:
                raise NeedsConsentError("no stored credential for this realm")

            self._assert_configuration(row, expected_entity=entity_key)

            if row.refresh_uncertain:
                raise RefreshUncertainError(REFRESH_UNCERTAIN_TEXT)
            if row.needs_consent:
                raise NeedsConsentError(needs_consent_text(row.entity))

            if not force and min_age_seconds is not None:
                age = row.refresh_age_seconds()
                if age is not None and age < min_age_seconds:
                    return row

            return self._refresh_locked(document, realm_id, row, actor, retry_allowed=True)

    def _refresh_locked(
        self,
        document: StoreDocument,
        realm_id: str,
        row: TokenRow,
        actor: RefreshActor,
        *,
        retry_allowed: bool,
    ) -> TokenRow:
        credentials = self._require_credentials()
        submitted = row.refresh_token

        # The wait for the lock and the recovery after it came out of the
        # call's budget, so the budget is checked here, where nothing has been
        # submitted yet. Past this point the refresh runs to its durable write.
        deadline = call_deadline.current()
        if deadline is not None:
            deadline.checkpoint(need=REFRESH_BUDGET_SECONDS)

        intent = JournalRecord(
            op_id=uuid.uuid4().hex,
            stage="intent",
            realm_id=realm_id,
            environment=self.environment,
            client_id_fingerprint=credentials.fingerprint,
            generation_from=row.auth_generation,
            old_refresh_digest=oauth.token_digest(submitted),
            written_at=datetime.now(UTC),
        )
        self._write_journal_locked([intent])

        try:
            response = oauth.refresh_tokens(
                refresh_token=submitted,
                client_id=credentials.client_id,
                client_secret=credentials.client_secret,
                environment=self.environment,
                from_cli=actor.role != "server",
            )
        except oauth.TokenResponseLost as exc:
            self._quarantine_locked(document, realm_id, row, hint=_lost_response_hint(row.entity))
            raise RefreshUncertainError(REFRESH_UNCERTAIN_TEXT) from exc
        except oauth.InvalidClient as exc:
            # Configuration. Never needs_consent, never a journal adoption.
            # invalid_client is the client-authentication failure (RFC 6749,
            # section 5.2). That the grant went unevaluated, so the submitted
            # token is untouched and the intent is stale, is an ASSUMPTION;
            # were it wrong, the next refresh would fail loudly with
            # invalid_grant and ask for consent.
            self._drop_journal_after_a_definite_outcome()
            raise AppCredentialsRejected(str(exc)) from exc
        except oauth.InvalidGrant as exc:
            return self._handle_invalid_grant(
                document, row, realm_id, submitted, actor, retry_allowed=retry_allowed, cause=exc
            )
        except oauth.OAuthHttpError as exc:
            if exc.grant_outcome_known:
                # A 4xx: the request was received and refused, and the
                # submitted grant was not consumed (an assumption, recorded on
                # OAuthHttpError). The stored token is still the live one:
                # nothing rotates, nothing is quarantined, the status is
                # reported. Otherwise a throttled token endpoint would strand
                # a realm that was never at risk.
                self._drop_journal_after_a_definite_outcome()
                raise
            # A 5xx, or a status that is neither: a response arrived, so the
            # request was delivered, and whether the grant was consumed is
            # not knowable from here. That is the ambiguous outcome.
            self._quarantine_locked(
                document, realm_id, row, hint=_unknown_status_hint(row.entity, exc.status)
            )
            raise RefreshUncertainError(REFRESH_UNCERTAIN_TEXT) from exc
        except oauth.OAuthError:
            # `_post` raises the base error, or its policy-refusal subclass, only
            # when nothing was submitted: its client couldn't be built, the policy
            # refused the request, or the connection was never established. The
            # intent describes a refresh that did not happen. Anything else, a
            # policy error raised later included, keeps the intent.
            self._drop_journal_after_a_definite_outcome()
            raise

        result = JournalRecord(
            op_id=intent.op_id,
            stage="result",
            realm_id=realm_id,
            environment=self.environment,
            client_id_fingerprint=credentials.fingerprint,
            generation_from=row.auth_generation,
            old_refresh_digest=intent.old_refresh_digest,
            written_at=datetime.now(UTC),
            tokens={
                "access_token": response.access_token,
                "refresh_token": response.refresh_token,
                "access_token_expires_at": _iso(response.access_token_expires_at),
                "refresh_token_expires_at": _iso(response.refresh_token_expires_at),
                "refresh_token_hard_expires_at": (
                    _iso(response.refresh_token_hard_expires_at)
                    if response.refresh_token_hard_expires_at
                    else None
                ),
                "synthetic": response.synthetic,
            },
        )
        self._write_journal_locked([result])

        updated = row.with_tokens(response, actor)
        document.rows[realm_id] = updated
        # Persist before use. If this raises, the caller never sees the token.
        self._commit_locked(document)
        # The pair is durable. A failure to clear the journal now -- a full
        # disk, say -- is not a failure of the refresh: the surviving record
        # is a replay, its generation behind the row's, which the next
        # recovery ignores and clears.
        self._drop_journal_after_a_definite_outcome()
        return updated

    def _handle_invalid_grant(
        self,
        document: StoreDocument,
        row: TokenRow,
        realm_id: str,
        submitted: str,
        actor: RefreshActor,
        *,
        retry_allowed: bool,
        cause: Exception,
    ) -> TokenRow:
        """Re-read under the lock; retry only with a genuinely newer credential.

        The rejected token is never resubmitted. The only situation worth
        retrying is one where another process independently persisted a newer
        generation while this refresh was in flight -- then this process was
        simply working from a stale read, and the newer credential deserves one
        attempt.
        """
        if retry_allowed:
            fresh = self._load_locked()
            candidate = fresh.rows.get(realm_id)
            if (
                candidate is not None
                and candidate.auth_generation > row.auth_generation
                and candidate.refresh_token != submitted
                and not candidate.needs_consent
                and not candidate.refresh_uncertain
            ):
                return self._refresh_locked(fresh, realm_id, candidate, actor, retry_allowed=False)

        document.rows[realm_id] = replace(row, needs_consent=True, refresh_uncertain=False)
        self._commit_locked(document)
        # Intuit answered, so nothing about this refresh is ambiguous any more.
        self._drop_journal_after_a_definite_outcome()
        raise NeedsConsentError(needs_consent_text(row.entity)) from cause

    # -- 401 ---------------------------------------------------------------

    def on_unauthorized(
        self,
        realm_id: str,
        *,
        rejected_generation: int,
        actor: RefreshActor,
        entity_key: str | None = None,
    ) -> TokenRow:
        """Handle a 401 from the Accounting API.

        A 401 is not proof of expiry. Intuit's Authorization FAQ lists expiry,
        revocation by the user and the two refresh-token limits as reasons a
        token stops working, and the response does not say which one applied.

        So: identify which generation was rejected. If the store already holds a
        newer one -- another process refreshed while this call was in flight --
        use that, with no network call at all. Otherwise force exactly one
        refresh, even though the rejected token's own timestamp still looks
        valid. Exactly one: a loop here turns a permanently rejected token into
        a burst of refreshes.

        The blocked states are checked before any generation is returned. A
        newer generation can itself be quarantined or marked for consent --
        the other process refreshed, then a later refresh failed -- and the
        retry that follows this call sends whatever it returns, with none of
        the checks the ordinary read makes.
        """
        with self.lock():
            document = self._load_locked()
            self._recover_locked(document)
            row = self._row_of(document, realm_id)
            if row is None:
                raise NeedsConsentError("no stored credential for this realm")
            self._assert_configuration(row, expected_entity=entity_key)
            if row.refresh_uncertain:
                raise RefreshUncertainError(REFRESH_UNCERTAIN_TEXT)
            if row.needs_consent:
                raise NeedsConsentError(needs_consent_text(row.entity))
            if row.auth_generation > rejected_generation:
                return row
            return self._refresh_locked(document, realm_id, row, actor, retry_allowed=True)

    # -- the one exit from a quarantine -----------------------------------

    def verify_uncertain(
        self,
        realm_id: str,
        *,
        actor: RefreshActor,
        entity_key: str | None = None,
    ) -> VerifyOutcome:
        """Submit a quarantined realm's stored refresh token exactly once.

        The one deliberate exit from ``refresh_uncertain`` other than
        re-consent. Under the lock, through the same journalled path an
        ordinary refresh takes, so the flag clears only on a validated
        success. ``invalid_grant`` marks ``needs_consent``; a response lost
        again leaves the quarantine in place with a fresh hint; a configuration
        failure changes nothing. A realm that is not in quarantine has nothing
        submitted.

        The rule for an ambiguous outcome stops *automatic* retries and forbids
        resubmitting a token Intuit has *rejected*. This is neither. It is an
        operator's single, explicit submission of a token that was never
        rejected -- it may merely have been spent -- and its worst case,
        ``invalid_grant``, lands exactly where the only alternative,
        re-consent, starts.
        """
        with self.lock():
            document = self._load_locked()
            self._recover_locked(document)
            row = self._row_of(document, realm_id)
            if row is None:
                raise NeedsConsentError("no stored credential for this realm")
            self._assert_configuration(row, expected_entity=entity_key)
            if row.needs_consent:
                raise NeedsConsentError(needs_consent_text(row.entity))
            if not row.refresh_uncertain:
                return VerifyOutcome(row=row, submitted=False)
            updated = self._refresh_locked(document, realm_id, row, actor, retry_allowed=False)
            return VerifyOutcome(row=updated, submitted=True)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConsentBaseline:
    """What a consent ceremony saw before the human part began.

    A consent captures the state it starts from and commits under lock only
    if that state still holds. The state is the consent's identity, and its
    representation is the realm's **consent epoch**: a per-realm counter the
    document keeps, advanced by every consent and every revoke and by nothing
    else. A refresh of the same grant advances the token generation and leaves
    the epoch alone. Keyed on the token generation, the check would refuse a
    consent whenever the keepalive fired during the minutes a human spends at
    the consent screen, and revoke the only fresh grant on the way out.

    Whether a row existed and its ``consented_at`` will not do either: they
    cannot see a consent followed by a revoke, since a ceremony that began
    with no row finds no row at its end and would commit. The epoch outlives
    the row, so both events count. ``present``, ``consented_at`` and
    ``auth_generation`` are kept for the CLI's diagnostics; the check reads
    the epoch alone.
    """

    realm_id: str
    epoch: int
    present: bool
    consented_at: datetime | None
    auth_generation: int | None

    def still_holds(self, document: StoreDocument) -> bool:
        return document.epoch_of(self.realm_id) == self.epoch


@dataclass(frozen=True, slots=True)
class VerifyOutcome:
    """What :meth:`TokenStore.verify_uncertain` did."""

    row: TokenRow
    submitted: bool


class _JournalUnreadable(Exception):
    """The journal's contents make no sense, or something else stands in its
    place. Handled where it's read, and never raised past that."""


def _adoptable_tokens(record: JournalRecord) -> tuple[str, str, datetime, datetime] | None:
    """What a result needs for its pair to be adopted, or ``None``."""
    tokens = record.tokens if isinstance(record.tokens, dict) else {}
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    access_expiry = _parse_iso(tokens.get("access_token_expires_at"))
    refresh_expiry = _parse_iso(tokens.get("refresh_token_expires_at"))
    if not isinstance(access_token, str) or not isinstance(refresh_token, str):
        return None
    if access_expiry is None or refresh_expiry is None:
        return None
    return access_token, refresh_token, access_expiry, refresh_expiry


def _can_act_on(record: JournalRecord) -> bool:
    """An intent, or a result with a pair to adopt. Anything else may stand for a
    refresh that happened, with nothing here to act on."""
    if record.stage == "result":
        return _adoptable_tokens(record) is not None
    return record.stage == "intent"


def _unreadable_journal_hint(entity: str) -> str:
    return (
        "The refresh journal could not be read, so a refresh may have been accepted "
        "with no record of it. The stored refresh token is not resubmitted "
        f"automatically. Run 'qbo-multicompany-mcp-auth verify {entity}' to submit it "
        f"once, or 'qbo-multicompany-mcp-auth connect {entity}' to re-consent."
    )


def _unanswered_intent_hint(entity: str) -> str:
    return (
        "A refresh was submitted for this realm and no response was recorded "
        "before the process stopped. The stored refresh token may already have "
        "been spent, so it is not resubmitted automatically. Run "
        f"'qbo-multicompany-mcp-auth verify {entity}' to submit it once, or "
        f"'qbo-multicompany-mcp-auth connect {entity}' to re-consent."
    )


def _lost_response_hint(entity: str) -> str:
    return (
        "A refresh was submitted but its response was lost or couldn't be used. "
        "The stored refresh token may already have been spent, so it is not resubmitted "
        f"automatically. Run 'qbo-multicompany-mcp-auth verify {entity}' to "
        f"submit it once, or 'qbo-multicompany-mcp-auth connect {entity}' to "
        "re-consent."
    )


def _unknown_status_hint(entity: str, status: int) -> str:
    return (
        f"A refresh was submitted and Intuit's token endpoint answered with "
        f"status {status}, which does not say whether the submitted token was "
        f"consumed. It is not resubmitted automatically. Run "
        f"'qbo-multicompany-mcp-auth verify {entity}' to submit it once, or "
        f"'qbo-multicompany-mcp-auth connect {entity}' to re-consent."
    )


def _competing_results_hint(entity: str) -> str:
    return (
        "The recovery journal held two competing results for this realm's "
        "current generation, so the local record of which refresh token is "
        "live has been lost. The stored token is not resubmitted "
        f"automatically. Run 'qbo-multicompany-mcp-auth verify {entity}' to "
        f"submit it once, or 'qbo-multicompany-mcp-auth connect {entity}' to "
        "re-consent."
    )


def _row_from_journal_tokens(row: TokenRow, record: JournalRecord) -> TokenRow | None:
    adoptable = _adoptable_tokens(record)
    if adoptable is None:
        return None
    access_token, refresh_token, access_expiry, refresh_expiry = adoptable
    tokens = record.tokens or {}
    return replace(
        row,
        access_token=access_token,
        refresh_token=refresh_token,
        access_token_expires_at=access_expiry,
        refresh_token_expires_at=refresh_expiry,
        refresh_token_hard_expires_at=(
            _parse_iso(tokens.get("refresh_token_hard_expires_at"))
            or row.refresh_token_hard_expires_at
        ),
        auth_generation=record.generation_from + 1,
        last_refresh_at=record.written_at,
        needs_consent=False,
        refresh_uncertain=False,
        recovery_hint=None,
        synthetic=tokens.get("synthetic") is True,
    )


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _require_iso(value: Any) -> datetime:
    parsed = _parse_iso(value)
    if parsed is None:
        raise StoreError("token store holds a malformed timestamp")
    return parsed


def _grant_origin(value: Any) -> str:
    return value if value in ("connect", "import") else "unknown"


def _epoch(value: Any) -> int:
    """A persisted consent epoch; anything that is not a whole number is zero,
    the epoch of a row with none recorded."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def expiry_warnings(row: TokenRow, *, now: datetime | None = None) -> list[str]:
    """A warning for each clock that is close, each under its own name.

    The inactivity clock is never called hard. A refresh that is issued a new
    value restores it; only a human re-consent restores the hard one, which
    is why the two have different lead times and different advice.
    """
    moment = now or datetime.now(UTC)
    warnings: list[str] = []
    inactivity = row.days_to_inactivity_expiry(now=moment)
    if inactivity <= REFRESH_INACTIVITY_WARN_DAYS:
        warnings.append(
            f"inactivity expiry in {inactivity:.1f} days: the 100-day clock that "
            f"a refresh restarts when Intuit issues a new value (at most daily). "
            f"Refresh, or check the keepalive schedule."
        )
    hard = row.hard_expiry()
    days = hard.days(now=moment)
    if days is not None and days <= REFRESH_HARD_EXPIRY_WARN_DAYS:
        if hard.basis == "observed":
            warnings.append(
                f"hard expiry in {days:.1f} days: Intuit's five-year limit, as its "
                f"token response reported it. Only a re-consent restarts it."
            )
        else:
            warnings.append(
                f"hard expiry no later than {days:.1f} days from now: the five-year "
                f"maximum counted from this consent's date; exact hard expiry unknown "
                f"because no token response has reported it. Only a re-consent "
                f"restarts it."
            )
    return warnings
