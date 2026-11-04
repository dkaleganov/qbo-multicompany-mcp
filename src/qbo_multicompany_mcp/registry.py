"""The entity registry: the mapping from an explicit ``entity`` key to a realm.

The registry is the routing table, and ``realm_id`` is the identity. Nothing in
this package ever routes on a company name -- two QuickBooks companies may
carry byte-identical names, and a name is user-editable inside QuickBooks at
any time. ``display_name`` exists to be *verified* against Intuit (see
:mod:`qbo_multicompany_mcp.guard`) and to be shown to a human. It is never an
input to a lookup.

There is no default entity and no name matching. A tool call that omits
``entity`` is a schema error before any of this runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from .constants import ENVIRONMENTS
from .errors import StartupError
from .redaction import redact

ENTITY_KEY_RE: Final = re.compile(r"^[a-z0-9_]+$")
REALM_ID_RE: Final = re.compile(r"^[0-9]+$")

_ALLOWED_ENTITY_FIELDS: Final = frozenset({"display_name", "realm_id", "environment", "writes"})


@dataclass(frozen=True, slots=True)
class Entity:
    key: str
    display_name: str
    realm_id: str
    environment: str

    @property
    def realm_id_last4(self) -> str:
        """The only part of a realm id that ever leaves this process.

        Enough to tell two connected companies apart in a transcript; not
        enough to address an API call.
        """
        return self.realm_id[-4:]


@dataclass(frozen=True, slots=True)
class Registry:
    entities: dict[str, Entity]
    source_path: Path

    def __getitem__(self, key: str) -> Entity:
        return self.entities[key]

    def __contains__(self, key: object) -> bool:
        return key in self.entities

    def __iter__(self):
        return iter(self.entities.values())

    def __len__(self) -> int:
        return len(self.entities)

    def get(self, key: str) -> Entity | None:
        return self.entities.get(key)

    def keys(self) -> list[str]:
        return list(self.entities)

    def by_realm(self, realm_id: str) -> Entity | None:
        for entity in self.entities.values():
            if entity.realm_id == realm_id:
                return entity
        return None


def load_registry(path: str | Path, *, expected_environment: str | None = None) -> Registry:
    """Read and validate ``entities.yaml``.

    Every failure raises :class:`StartupError`, which the entry points turn
    into one redacted line on stderr and exit status 2. A registry that is
    wrong in any of these ways cannot be repaired at run time, and starting
    anyway would mean serving an ambiguous or mis-routed company file.
    """
    source = Path(path)
    try:
        raw_text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise StartupError(f"cannot read entities file {source}: {exc.strerror}") from exc

    try:
        document = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        # YAML's own message can carry a snippet of the file. The file holds
        # realm ids, so only the position survives.
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise StartupError(f"entities file {source} is not valid YAML{where}") from exc

    if document is None:
        raise StartupError(f"entities file {source} is empty")
    if not isinstance(document, dict):
        raise StartupError(f"entities file {source} must contain a mapping at the top level")

    unknown_top = set(document) - {"entities"}
    if unknown_top:
        raise StartupError(
            f"entities file {source} has unknown top-level key(s): "
            f"{', '.join(sorted(unknown_top))}"
        )

    section = document.get("entities")
    if not isinstance(section, dict) or not section:
        raise StartupError(f"entities file {source} must define a non-empty 'entities' mapping")

    carrying = _key_carrying_a_realm_id(section)
    if carrying is not None:
        # Checked before any message that names a key, since each would
        # print this one.
        raise StartupError(
            f"entity '{carrying}' has a key that contains a company's realm_id; a key "
            f"is printed in results and stored in the rate-limit file, so it may carry "
            f"at most the last four digits of one"
        )
    if any(redact(str(key)) != str(key) for key in section):
        # Such a key couldn't be read or pasted from an error, and two of them
        # would look the same.
        raise StartupError(
            f"entities file {source} has a key that errors and logs would mask, so it "
            f"couldn't be read or pasted from them: eight or more digits in a row, or "
            f"forty or more letters and digits"
        )

    entities: dict[str, Entity] = {}
    realm_owner: dict[str, str] = {}

    for key, value in section.items():
        entity = _parse_entity(key, value, source)

        if expected_environment is not None and entity.environment != expected_environment:
            raise StartupError(
                f"entity '{entity.key}' declares environment '{entity.environment}' but "
                f"QBO_ENV is '{expected_environment}'"
            )

        previous = realm_owner.get(entity.realm_id)
        if previous is not None:
            # Two keys pointing at one realm makes every result ambiguous: the
            # echoed entity would no longer identify the company file the data
            # came from, which is the one promise this package makes.
            raise StartupError(
                f"entities '{previous}' and '{entity.key}' share the same realm_id; "
                f"each entity must name a distinct QuickBooks company"
            )
        realm_owner[entity.realm_id] = entity.key
        entities[entity.key] = entity

    return Registry(entities=entities, source_path=source)


def _key_carrying_a_realm_id(section: dict[Any, Any]) -> str | None:
    """The first key containing a realm id this file declares, else ``None``.

    An entity key goes wherever its company is named: every result, every
    error, and the rate-limit file, which is plaintext. A key that spelled a
    company's id would take the id there with it. The last four digits are
    what every result prints anyway, so an id of four digits or fewer is not
    counted, and the key is returned with each id it carries cut to its last
    four.
    """
    realm_ids = {
        str(value["realm_id"]).strip()
        for value in section.values()
        if isinstance(value, dict)
        and isinstance(value.get("realm_id"), (str, int))
        and not isinstance(value.get("realm_id"), bool)
    }
    realm_ids = {realm for realm in realm_ids if len(realm) > 4 and REALM_ID_RE.match(realm)}
    for key in section:
        if not isinstance(key, str):
            continue
        carried = sorted((realm for realm in realm_ids if realm in key), key=len, reverse=True)
        if carried:
            for realm in carried:
                key = key.replace(realm, f"...{realm[-4:]}")
            return key
    return None


def _parse_entity(key: Any, value: Any, source: Path) -> Entity:
    if not isinstance(key, str) or not ENTITY_KEY_RE.match(key):
        raise StartupError(
            f"entities file {source} has an invalid entity key; keys must match "
            f"[a-z0-9_]+"
        )
    if not isinstance(value, dict):
        raise StartupError(f"entity '{key}' must be a mapping")

    unknown = set(value) - _ALLOWED_ENTITY_FIELDS
    if unknown:
        raise StartupError(
            f"entity '{key}' has unknown field(s): {', '.join(sorted(unknown))}"
        )

    display_name = value.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        raise StartupError(f"entity '{key}' needs a non-empty display_name")

    realm_raw = value.get("realm_id")
    if isinstance(realm_raw, bool) or not isinstance(realm_raw, (str, int)):
        raise StartupError(f"entity '{key}' needs a realm_id")
    realm_id = str(realm_raw).strip()
    if not REALM_ID_RE.match(realm_id):
        # Not echoed: a malformed realm id is still a realm id.
        raise StartupError(f"entity '{key}' has a non-numeric realm_id")

    environment = value.get("environment")
    if environment not in ENVIRONMENTS:
        raise StartupError(
            f"entity '{key}' needs environment: one of {', '.join(ENVIRONMENTS)}"
        )

    if "writes" in value:
        # Accepted only as an explicit, redundant `false`. The package has no
        # write path to switch on, so any other value means the author expected
        # a capability that does not exist -- and silently reading `true` as
        # "no" would be the worst possible answer.
        if value["writes"] is not False:
            raise StartupError(
                f"entity '{key}' sets writes to something other than false; this "
                f"server is read-only and 'writes' may only be false"
            )

    return Entity(
        key=key,
        display_name=display_name,
        realm_id=realm_id,
        environment=environment,
    )
