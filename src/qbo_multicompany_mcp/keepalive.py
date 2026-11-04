"""``qbo-multicompany-mcp-keepalive``: keep each realm's refresh chain alive.

A thin CLI over the store's own refresh. It adds no refresh logic of its own --
it shares the canonical lock and the same write-ahead journal, because a
scheduled process and an interactive server refreshing the same realm is
exactly the race the journal exists for.

Two details matter most.

*The one-hour criterion is re-checked inside the lock.* Deciding "this realm is
due" before taking the lock lets two runs both decide yes, queue, and refresh
twice -- the second one submitting a token the first already spent. So the
decision is made where the decision can still be trusted.

*A lock timeout exits 1, never 2.* Exit 2 means "your configuration is wrong
and will stay wrong". A busy lock means another process is mid-refresh, which
is the system working. Conflating the two teaches an operator to ignore the
code that actually matters.

``stale_refresh`` is computed per realm and is a **passive observation, not an
alert**. It says "this realm's own last_refresh_at is old". It cannot say "the
keepalive is not running", because a process that never runs announces nothing:
if the schedule is broken, nothing here will tell you. Monitor the schedule
itself.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from .config import Settings, add_common_arguments, register_environment_secrets, settings_from_args
from .constants import STALE_REFRESH_SECONDS
from .errors import (
    AppCredentialsRejected,
    ConfigurationError,
    NeedsConsentError,
    QboMcpError,
    RefreshUncertainError,
    StartupError,
    StoreLockTimeout,
)
from .redaction import ValueFreeParser, install_redacting_hooks, redact
from .registry import Registry
from .store import RefreshActor, TokenStore, expiry_warnings

ROLE = "keepalive"


def out(message: str) -> None:
    print(message)


def run(settings: Settings, registry: Registry, args: argparse.Namespace) -> int:
    credentials = settings.require_credentials()
    store = settings.open_store(credentials=credentials)
    actor = RefreshActor.here(ROLE)
    now = datetime.now(UTC)

    try:
        document = _read_with_one_retry(store)
    except ConfigurationError as exc:
        # Configuration, wherever it is met: a store holding another
        # environment, a malformed key setting. Exit 2 here as at refresh
        # time, because 1 is the code a scheduler reads as transient.
        out(f"token store unavailable: {redact(exc)}")
        return 2
    except QboMcpError as exc:
        out(f"token store unavailable: {redact(exc)}")
        return 1
    rows = document.rows
    # Opt-in. By default a never-authorized realm is skipped, and a skip alone
    # leaves the exit status at 0; with --require-all every configured company
    # must be connected, and one that is not needs attention. Read with its
    # default because run() is also called with a namespace that lacks the
    # flag.
    require_all = bool(getattr(args, "require_all", False))

    exit_code = 0
    # Set once Intuit refuses the app's own credentials. Every later company
    # would get the same answer, so no other refresh token goes out with them.
    # Each still gets a line: a malformed, never-authorized, needs-consent or
    # uncertain row says so, and every other one reads "not attempted", one
    # refreshed within the hour or bound to another configuration included.
    rejected = False
    for entity in registry:
        if entity.realm_id in document.malformed:
            # Its own realm only. Not "never authorized": that would invite a
            # connect without saying why.
            out(f"{entity.key}: malformed store row; skipped (reconnect the entity to replace it)")
            exit_code = max(exit_code, 1)
            continue
        row = rows.get(entity.realm_id)
        if row is None:
            if require_all:
                out(
                    f"{entity.key}: never authorized; needs attention (--require-all: "
                    f"every configured company must be connected)"
                )
                exit_code = max(exit_code, 1)
                continue
            out(f"{entity.key}: never authorized; skipped")
            continue
        if row.needs_consent:
            out(f"{entity.key}: needs consent; skipped (no request made)")
            exit_code = max(exit_code, 1)
            continue
        if row.refresh_uncertain:
            out(f"{entity.key}: last refresh outcome unknown; skipped (never retried automatically)")
            exit_code = max(exit_code, 1)
            continue
        if rejected:
            out(f"{entity.key}: not attempted (Intuit rejected the app's credentials)")
            continue

        try:
            # min_age_seconds is re-evaluated inside the lock by the store.
            refreshed = _refresh_with_one_retry(
                store,
                entity.realm_id,
                actor=actor,
                force=args.force,
                entity_key=entity.key,
            )
        except StoreLockTimeout:
            # Transient, by definition. Exit 1, never 2.
            out(f"{entity.key}: store busy after two attempts; transient, nothing changed")
            exit_code = max(exit_code, 1)
            continue
        except RefreshUncertainError as exc:
            out(f"{entity.key}: {redact(exc)}")
            exit_code = max(exit_code, 1)
            continue
        except NeedsConsentError as exc:
            out(f"{entity.key}: {redact(exc)}")
            exit_code = max(exit_code, 1)
            continue
        except AppCredentialsRejected as exc:
            out(f"{entity.key}: {redact(exc)}")
            rejected = True
            exit_code = max(exit_code, 2)
            continue
        except ConfigurationError as exc:
            # Configuration, not consent. Distinct exit status on purpose, and
            # one company's problem: the others still need their refresh.
            out(f"{entity.key}: {redact(exc)}")
            exit_code = max(exit_code, 2)
            continue
        except QboMcpError as exc:
            out(f"{entity.key}: refresh failed: {redact(exc)}")
            exit_code = max(exit_code, 1)
            continue

        # The store commits before it returns, so any new pair is on disk by now.
        # ``now`` was read before the refresh: a row last refreshed before it was
        # left alone, and a row refreshed in this run has an age of zero or less.
        age = refreshed.refresh_age_seconds(now=now)
        if age is not None and age > 0 and not args.force:
            state = "unchanged (still within the hour)"
        else:
            state = "refreshed"
        # Two clocks, each by its own name. The inactivity clock is what the
        # token response reports; it restarts when Intuit issues a new
        # refresh-token value, at most once per 24 hours
        # (Intuit's Authorization FAQ), so a run within a day of the last
        # rotation may leave it where it was (observed 2026-09-26). The hard
        # clock is the five-year limit, observed only if Intuit reported it.
        line = (
            f"{entity.key}: {state}; generation {refreshed.auth_generation}; "
            f"inactivity expiry in {refreshed.days_to_inactivity_expiry():.1f} days; "
            f"hard expiry: {refreshed.hard_expiry().describe()}"
        )
        for warning in expiry_warnings(refreshed):
            line += f"  <-- WARNING: {warning}"
            exit_code = max(exit_code, 1)
        out(line)

    return exit_code


def _read_with_one_retry(store: TokenStore):
    """The store's first read waits for the lock once more too, as a refresh does."""
    try:
        return store.read()
    except StoreLockTimeout:
        return store.read()


def _refresh_with_one_retry(
    store: TokenStore,
    realm_id: str,
    *,
    actor: RefreshActor,
    force: bool,
    entity_key: str,
):
    """Wait for the lock once more before giving up.

    One retry, not a loop: the lock is normally held for a token POST and its
    writes, so a second wait covers an ordinary overlap. Nothing bounds a
    slow token response, and a retry loop would merely hide one.
    """
    kwargs = {"actor": actor, "force": force, "entity_key": entity_key}
    if not force:
        kwargs["min_age_seconds"] = STALE_REFRESH_SECONDS
    try:
        return store.refresh(realm_id, **kwargs)
    except StoreLockTimeout:
        return store.refresh(realm_id, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    # ValueFreeParser turns abbreviations off. argparse's default reads any
    # unambiguous prefix as the option: --token-st would be --token-store.
    parser = ValueFreeParser(
        prog="qbo-multicompany-mcp-keepalive",
        description=(
            "Refresh each configured realm whose credential is older than an hour. "
            "Run it on a schedule; it reports per realm and never writes a line "
            "before the new token pair is on disk."
        ),
    )
    add_common_arguments(parser)
    parser.add_argument(
        "--force",
        action="store_true",
        help="refresh every realm regardless of how recently it was refreshed",
    )
    parser.add_argument(
        "--require-all",
        action="store_true",
        help=(
            "count a configured company with no stored credential as needing "
            "attention (exit 1) instead of skipping it; for a deployment where "
            "every company in the registry is meant to be connected"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    install_redacting_hooks()
    register_environment_secrets()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = settings_from_args(args)
        registry = settings.load_registry()
        return run(settings, registry, args)
    except (StartupError, ConfigurationError) as exc:
        print(f"qbo-multicompany-mcp-keepalive: {redact(exc)}", file=sys.stderr)
        return 2
    except StoreLockTimeout:
        print("qbo-multicompany-mcp-keepalive: store busy; transient", file=sys.stderr)
        return 1
    except QboMcpError as exc:
        print(f"qbo-multicompany-mcp-keepalive: {redact(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
