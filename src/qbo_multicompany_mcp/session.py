"""Per-process session state: the store, the client, the guard, the cache.

This is where "once per entity per process" is implemented. On first use of an
entity the session fetches CompanyInfo (to verify the company name against the
registry) and Preferences (to derive capabilities), caches both, and persists
the guard verdict and the capability block into the store under the canonical
lock -- so ``list_entities`` can report them later without making a request.

The cache is invalidated by re-consent and by nothing else. A refresh issues
new tokens for the same grant; a re-consent is a new grant, so it is the event
after which the company name and the capabilities are checked again. That a
re-consent could leave a realm id denoting a different company file is an
ASSUMPTION made in the cautious direction, not something Intuit documents;
re-checking costs two requests. The row's consent epoch is therefore the cache
key: it is the consent's identity, which a refresh leaves untouched and a
re-consent advances.

Failures here are per entity. A guard mismatch on ``acme_main`` raises when
``acme_main`` is used and leaves ``acme_ops`` working normally.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from .capabilities import AP_UNKNOWN, Capabilities, derive_capabilities
from .client import AccountingClient, ApiResult
from .config import Settings
from .errors import (
    EntityUnknown,
    GuardMismatch,
    NeedsConsentError,
    QboMcpError,
    guard_mismatch_text,
    needs_consent_text,
)
from .guard import names_match
from .projections import project_preferences
from .redaction import redact
from .registry import Entity, Registry
from .store import NameMismatch, TokenStore, registry_fingerprint


@dataclass(frozen=True, slots=True)
class EntityState:
    """What the first call per entity established, cached for the process."""

    #: ``None`` when the guard tripped. Preferences are not fetched for a
    #: mismatched entity, so there is nothing to report -- and reporting every
    #: feature as ``false`` would be a lie dressed as an answer.
    capabilities: Capabilities | None
    guard_checked_at: datetime
    #: The consent this state was established under. A re-consent advances
    #: it and so invalidates everything here; a refresh does not.
    consent_epoch: int
    company_name_matches_registry: bool
    #: What QuickBooks calls this company. Populated only on a mismatch, where
    #: it is the one thing the operator needs in order to fix the registry.
    intuit_company_name: str | None = None


@dataclass(frozen=True, slots=True)
class ReadyOutcome:
    """The cached state, plus anything the guard pass happened to fetch.

    The guard reads CompanyInfo and Preferences on first use, and those are the
    same two resources ``get_company_info`` reports. Handing them back lets the
    first call reuse them instead of fetching CompanyInfo twice in a row; on
    later calls both are ``None`` and the tool fetches what it needs itself.
    """

    state: EntityState
    company_info: ApiResult | None = None
    preferences: ApiResult | None = None


class QboSession:
    def __init__(self, settings: Settings, registry: Registry) -> None:
        self.settings = settings
        self.registry = registry
        self._store: TokenStore | None = None
        self._client: AccountingClient | None = None
        self._states: dict[str, EntityState] = {}

    # -- lazily-created collaborators -------------------------------------

    def store(self) -> TokenStore:
        """Construct the store on first use, never at startup.

        ``tools/list`` must answer with only ``QBO_ENTITIES_FILE`` set and must
        create no store and no directory. Constructing a ``TokenStore`` touches
        no disk; reading or writing one does, and that only happens inside a
        tool call.
        """
        if self._store is None:
            self._store = self.settings.open_store()
        return self._store

    def store_or_refusal(self) -> tuple[TokenStore | None, str | None]:
        """The store, or why it cannot be opened as configured.

        Used by the two tools that make no request. They stay useful as
        diagnostics even when the store is unreadable -- which is exactly the
        moment an operator most needs ``server_info`` to tell them why. So the
        reason is the refusal's own text: a store refused for its location
        says so, and only the plaintext refusal advises the plaintext override.
        """
        try:
            return self.store(), None
        except QboMcpError as exc:
            return None, redact(str(exc))

    def client(self) -> AccountingClient:
        if self._client is None:
            self._client = AccountingClient(self.settings, self.store())
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- entity resolution -------------------------------------------------

    def resolve(self, entity_key: str) -> Entity:
        """Resolve an explicit key. There is no default and no fallback.

        The error names the known keys because they are operator-chosen
        identifiers from a local file, not caller-supplied data -- and a model
        that guessed a key wrong needs to see the real list to recover.
        """
        entity = self.registry.get(entity_key)
        if entity is None:
            known = ", ".join(sorted(self.registry.keys())) or "(none configured)"
            raise EntityUnknown(
                f"unknown entity. This server has no default company: every call must "
                f"name one of: {known}"
            )
        return entity

    # -- guard and capabilities -------------------------------------------

    def ensure_ready(
        self, entity: Entity, *, allow_name_mismatch: bool = False
    ) -> ReadyOutcome:
        """Verify the guard and load capabilities, at most once per process.

        Costs two extra requests the first time an entity is used, and none
        afterwards. Re-verifying on every call would double this server's
        request count permanently in order to re-answer a question whose answer
        changes when someone reconnects the entity -- which is what invalidates
        the cache.

        ``allow_name_mismatch`` is for ``get_company_info`` alone. Every other
        tool fails closed on a mismatch, because a mismatch means the registry
        and reality disagree about which company this is and no financial
        answer should be given under that doubt. ``get_company_info`` is the
        exception that makes the mismatch fixable: it returns the name
        QuickBooks reports so a model can tell the operator exactly what to put
        in ``entities.yaml``, instead of sending them to a terminal blind. It
        exposes the company's info for a connection that is already authorized,
        and Preferences are not fetched for a mismatched entity.
        """
        store = self.store()
        # The binding is checked here too, so a mismatch is refused before the
        # guard read rather than by the request it would have made.
        row = store.usable_row(entity.realm_id, entity_key=entity.key)
        if row is None:
            raise NeedsConsentError(needs_consent_text(entity.key))

        cached = self._states.get(entity.key)
        if cached is not None and cached.consent_epoch == row.consent_epoch:
            if not cached.company_name_matches_registry and not allow_name_mismatch:
                raise GuardMismatch(guard_mismatch_text(entity.key))
            return ReadyOutcome(state=cached)

        client = self.client()

        # -- guard ---------------------------------------------------------
        info = client.company_info(entity)
        intuit_name = _company_name(info.payload)
        matches = intuit_name is not None and names_match(entity.display_name, intuit_name)

        verdict = NameMismatch(
            mismatch=not matches,
            checked_at=datetime.now(UTC),
            auth_generation=row.auth_generation,
            # Bound to the registry name it was checked against, so a corrected
            # display_name retires it without a request -- and to the consent
            # it was checked under, so a re-consent that landed while this read
            # was in flight refuses it.
            registry_fingerprint=registry_fingerprint(entity.display_name),
            consent_epoch=row.consent_epoch,
        )
        store.record_guard_verdict(entity.realm_id, verdict)

        if not matches:
            mismatched = EntityState(
                capabilities=None,
                guard_checked_at=verdict.checked_at,
                consent_epoch=row.consent_epoch,
                company_name_matches_registry=False,
                intuit_company_name=intuit_name,
            )
            self._states[entity.key] = mismatched
            if allow_name_mismatch:
                return ReadyOutcome(state=mismatched, company_info=info)
            # Every other tool: the ToolError echoes neither name. A model has
            # no use for them and a transcript is a bad place for a company
            # name nobody expected to be there.
            raise GuardMismatch(guard_mismatch_text(entity.key))

        # -- capabilities --------------------------------------------------
        preferences = client.preferences(entity)
        projected = project_preferences(_preferences_body(preferences.payload))
        capabilities = derive_capabilities(projected)
        # Accounts payable is not in Preferences and is not read from the
        # store either: it is established by THIS process's own first use and
        # cached per entity per process. A persisted answer carried across
        # restarts would leave a company whose QuickBooks plan gained accounts
        # payable "unavailable" until a re-consent. A restart asks again.

        store.update_row(
            entity.realm_id,
            lambda r: replace(r, capabilities=capabilities.to_json()),
        )

        state = EntityState(
            capabilities=capabilities,
            guard_checked_at=verdict.checked_at,
            consent_epoch=row.consent_epoch,
            company_name_matches_registry=True,
        )
        self._states[entity.key] = state
        return ReadyOutcome(state=state, company_info=info, preferences=preferences)

    # ----------------------------------------------------------------
    # Walks
    # ----------------------------------------------------------------

    def walk(self, entity: Entity, query, *, client_limit: int | None = None):
        """Run a complete-or-loud walk for ``entity``."""
        from . import pagination

        client = self.client()

        def fetch(page_query):
            return client.query(entity, page_query.canonical()).payload

        return pagination.walk(query, fetch=fetch, client_limit=client_limit)

    def vendor_index(self, entity: Entity) -> dict[str, dict[str, Any]]:
        """Every vendor, **including inactive ones**, keyed by id.

        Inactive on purpose: a vendor deactivated in March was still paid in
        February, and an index of active vendors only would leave those
        payments unnamed or drop them.

        Not cached. This is used where the `active` flag is reported, and a
        cached flag that went stale mid-session would be reported as current.
        """
        from .projections import project_vendor
        from .query_grammar import parse

        result = self.walk(
            entity, parse("SELECT * FROM Vendor WHERE Active IN (true, false)")
        )
        index: dict[str, dict[str, Any]] = {}
        for row in result.rows:
            projected = project_vendor(row)
            identity = projected.get("id")
            if identity:
                index[str(identity)] = projected
        return index

    # ----------------------------------------------------------------
    # Accounts payable: established by first use
    # ----------------------------------------------------------------

    def accounts_payable(self, entity: Entity) -> str:
        """``available``, ``unavailable`` or ``unknown`` for this entity.

        Never guessed from the chart of accounts and never read from
        Preferences: only a BillPayment read that succeeded, or that Intuit
        refused with the feature fault, changes it from ``unknown``.
        """
        state = self._states.get(entity.key)
        capabilities = state.capabilities if state is not None else None
        return capabilities.accounts_payable if capabilities is not None else AP_UNKNOWN

    def note_accounts_payable(
        self, entity: Entity, *, available: bool, fault_code: str | None = None
    ) -> None:
        """Record what first use established, in this process's cache only.

        Not in the store row: the answer is cached per entity per process, and
        a persisted answer read by a fresh process would outlive that. It is
        invalidated with the rest of the capability state by a re-consent (the
        consent epoch is the key), and ``list_entities`` reports it from this
        cache -- ``unknown`` in a process that has not asked yet.
        """
        state = self._states.get(entity.key)
        if state is None or state.capabilities is None:
            return
        updated = state.capabilities.with_accounts_payable(
            available=available, fault_code=fault_code
        )
        self._states[entity.key] = replace(state, capabilities=updated)

    def cached_capabilities(self, entity: Entity) -> Capabilities | None:
        """What this process has established for ``entity``, or ``None``."""
        state = self._states.get(entity.key)
        return state.capabilities if state is not None else None


def _company_name(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    body = payload.get("CompanyInfo")
    if not isinstance(body, dict):
        return None
    name = body.get("CompanyName")
    return name if isinstance(name, str) else None


def _preferences_body(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    body = payload.get("Preferences")
    return body if isinstance(body, dict) else {}


def company_info_body(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    body = payload.get("CompanyInfo")
    return body if isinstance(body, dict) else {}
