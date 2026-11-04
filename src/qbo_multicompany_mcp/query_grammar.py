"""A parser for the subset of the QuickBooks query language this server serves.

The input is parsed **in full** and a canonical statement is rebuilt from the
parse tree. Caller text is never passed through, not even after validation:
validating a string and then forwarding the original means the thing that was
checked and the thing that was sent are two different objects, which is how
injection bugs happen.

What the grammar accepts::

    SELECT (* | COUNT(*)) FROM <entity>
        [WHERE <condition> [AND <condition>]*]
        [ORDERBY <field> [ASC|DESC]]
        [STARTPOSITION <n>]
        [MAXRESULTS <n <= 1000>]

Deliberate restrictions, each for a reason:

*No field lists.* Upstream does not support projections -- every attribute
comes back regardless of what is asked for. Accepting ``SELECT Id, Name`` would
therefore either error or, worse, silently return everything while the model
believes it asked for two fields and reads the result as filtered.

*``Id`` takes only ``=`` and ``IN``.* Ranges over an opaque identifier are not
meaningful, and ``Id > '5'`` invites a model to iterate a keyspace.

*``AND`` only.* ``OR`` is not supported upstream in this subset, and accepting
it would produce a silently wrong result set rather than an error.

*Explicit per-entity field and operator tables.* A general permission is not a
contract. A field is filterable, or sortable, for a **named entity**, or it is
rejected. A table that said "any field on any entity" would be a promise this
package cannot keep across API revisions.

*``ORDERBY Id`` is rejected -- and it currently works in sandbox.* A live probe
found it honoured there, and Intuit's change log says production stopped on
2026-01-27. Accepting a thing because sandbox accepts it is how a server works
in testing and fails on real books. Sandbox behaviour is evidence about sandbox.

*Employee is served nowhere.* It is not in the table, so it cannot be queried.
The seven reference entities (Class, Department, Item, Term, PaymentMethod,
TaxCode, TaxRate) are served, each behind a completed projection.

This package is **GET-only by product choice.** Upstream also documents a
read-only ``POST /query`` that carries the statement in a text body, which
exists precisely so long statements do not have to fit in a URL. Not every
upstream POST mutates data, and this file does not claim otherwise -- but a
single GET-only path is a boundary that can be stated in one sentence and
enforced in one place, and that is worth more here than a longer statement
limit. An over-long statement is an explicit error, never a quiet POST
fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from .errors import QboMcpError

#: The upstream page size: default 100, maximum 1,000 (Intuit's query syntax
#: page).
MAX_RESULTS_CEILING: Final = 1000
DEFAULT_PAGE_SIZE: Final = 100

#: This package's own ceiling on a statement's length: an ASSUMPTION, not a
#: documented limit. No Intuit page states a maximum URL length for
#: ``GET /query``, so 4000 is chosen to sit well inside common request-line
#: limits. An over-long statement is refused rather than silently switched to
#: POST.
MAX_STATEMENT_LENGTH: Final = 4000


class QueryError(QboMcpError):
    """The statement was rejected. The message is safe to show a model."""


# --------------------------------------------------------------------------
# Per-entity tables
# --------------------------------------------------------------------------

_DEFAULT_OPERATORS: Final[frozenset[str]] = frozenset({"=", "<", ">", "<=", ">=", "IN", "LIKE"})

#: ``Id`` is an opaque identifier. Equality and membership only.
_ID_OPERATORS: Final[frozenset[str]] = frozenset({"=", "IN"})

#: Booleans are equality or the documented ``IN (true, false)`` form
#: (Intuit's query syntax page).
_BOOL_OPERATORS: Final[frozenset[str]] = frozenset({"=", "IN"})


@dataclass(frozen=True, slots=True)
class EntitySpec:
    """What may be filtered and sorted, for one named entity."""

    name: str
    filterable: frozenset[str]
    sortable: frozenset[str]
    default_sort: str
    operators: dict[str, frozenset[str]]

    def operators_for(self, field: str) -> frozenset[str]:
        return self.operators.get(field, _DEFAULT_OPERATORS)


def _spec(
    name: str,
    *,
    filterable: set[str],
    sortable: set[str],
    default_sort: str,
    operators: dict[str, frozenset[str]] | None = None,
) -> EntitySpec:
    table = {"Id": _ID_OPERATORS}
    table.update(operators or {})
    return EntitySpec(
        name=name,
        filterable=frozenset(filterable | {"Id"}),
        sortable=frozenset(sortable),
        default_sort=default_sort,
        operators=table,
    )


#: Fields common to every entity's filter table.
_COMMON_FILTER: Final[set[str]] = {"Id", "MetaData.LastUpdatedTime"}

#: Name-list entities. Both keys are **confirmed sortable on Vendor** against
#: live sandbox. `DisplayName` remains the default because QuickBooks enforces
#: it unique per company, which makes it a stable paging key;
#: `MetaData.LastUpdatedTime` is not unique and two rows sharing a timestamp
#: can straddle a page boundary. Sortability on Customer is not separately
#: confirmed (see docs/api-findings.md).
_NAME_LIST_SORTABLE: Final[set[str]] = {"DisplayName", "MetaData.LastUpdatedTime"}

#: Transaction entities sort on TxnDate, **confirmed honoured on Purchase and
#: BillPayment** against live sandbox. It is NOT unique -- many transactions
#: share a date -- so a page boundary can repeat or skip a row. That is exactly
#: what the unique-id count check in `pagination.py` exists to catch, and why
#: this walk is complete-or-loud rather than trusting the sort.
#: `MetaData.LastUpdatedTime` is offered here but its sortability on
#: transaction entities is NOT confirmed.
_TXN_SORTABLE: Final[set[str]] = {"TxnDate", "MetaData.LastUpdatedTime"}

_TXN_FILTER: Final[set[str]] = _COMMON_FILTER | {"TxnDate", "DocNumber", "TotalAmt"}

_TRANSACTION_SPECS: Final[dict[str, dict[str, set[str]]]] = {
    "Bill": {"extra": {"VendorRef", "DueDate", "Balance"}},
    "BillPayment": {"extra": {"VendorRef", "PayType", "TotalAmt"}},
    "CreditMemo": {"extra": {"CustomerRef", "Balance"}},
    "Deposit": {"extra": set()},
    "Invoice": {"extra": {"CustomerRef", "DueDate", "Balance"}},
    "JournalEntry": {"extra": {"Adjustment"}},
    "Payment": {"extra": {"CustomerRef", "TotalAmt"}},
    "Purchase": {"extra": {"EntityRef", "PaymentType", "AccountRef", "Credit"}},
    "PurchaseOrder": {"extra": {"VendorRef", "POStatus"}},
    "RefundReceipt": {"extra": {"CustomerRef"}},
    "SalesReceipt": {"extra": {"CustomerRef"}},
    "Transfer": {"extra": set()},
    "VendorCredit": {"extra": {"VendorRef"}},
}

#: The 13 transaction types this server routes. Estimates are excluded: they
#: are non-posting, so they are not transactions in the ledger sense this
#: package serves.
TRANSACTION_TYPES: Final[tuple[str, ...]] = tuple(sorted(_TRANSACTION_SPECS))

#: The reference entities list_reference_data serves, with the extra
#: filterable fields each of Intuit's entity pages documents.
_REFERENCE_SPECS: Final[dict[str, set[str]]] = {
    "Class": {"FullyQualifiedName", "SubClass", "ParentRef"},
    "Department": {"FullyQualifiedName", "SubDepartment", "ParentRef"},
    "Item": {"FullyQualifiedName", "Type", "Sku", "SubItem", "ParentRef"},
    "Term": {"Type"},
    "PaymentMethod": {"Type"},
    "TaxCode": {"Taxable", "TaxGroup"},
    "TaxRate": {"AgencyRef"},
}
REFERENCE_ENTITIES: Final[tuple[str, ...]] = tuple(sorted(_REFERENCE_SPECS))


def _build_registry() -> dict[str, EntitySpec]:
    registry: dict[str, EntitySpec] = {}

    registry["Account"] = _spec(
        "Account",
        filterable=_COMMON_FILTER
        | {"Name", "FullyQualifiedName", "AccountType", "AccountSubType",
           "Classification", "Active", "CurrentBalance"},
        sortable={"Name", "FullyQualifiedName", "MetaData.LastUpdatedTime"},
        default_sort="Name",
        operators={"Active": _BOOL_OPERATORS},
    )

    # No tax ids, notes, addresses, phone numbers or email addresses appear in
    # any filter table. A filterable field is an oracle even when it is not a
    # returned field: equality probing recovers a value one guess at a time.
    registry["Vendor"] = _spec(
        "Vendor",
        filterable=_COMMON_FILTER
        | {"DisplayName", "CompanyName", "Active", "Vendor1099", "Balance"},
        sortable=_NAME_LIST_SORTABLE,
        default_sort="DisplayName",
        operators={"Active": _BOOL_OPERATORS, "Vendor1099": _BOOL_OPERATORS},
    )

    registry["Customer"] = _spec(
        "Customer",
        filterable=_COMMON_FILTER
        | {"DisplayName", "CompanyName", "Active", "Balance", "Job"},
        sortable=_NAME_LIST_SORTABLE,
        default_sort="DisplayName",
        operators={"Active": _BOOL_OPERATORS, "Job": _BOOL_OPERATORS},
    )

    for name, extra in _TRANSACTION_SPECS.items():
        registry[name] = _spec(
            name,
            filterable=_TXN_FILTER | extra["extra"],
            sortable=_TXN_SORTABLE,
            default_sort="TxnDate",
            operators={
                "Credit": _BOOL_OPERATORS,
                "Adjustment": _BOOL_OPERATORS,
            },
        )

    # The reference entities, each with a completed projection in
    # projections.REFERENCE_SPECS. Filterable: the identity, the name, the
    # active flag, the type where Intuit's page documents one, and the
    # hierarchy name. Sortable: Name and the modification time. ORDERBY Name:
    # OBSERVED 2026-09-25, accepted on all seven and rows in Name order on the
    # five non-empty ones, both sandbox companies; one page each, so paging
    # under the sort is unobserved, and the walk is count-anchored either way.
    # No description, note or free-text field is filterable: a filterable
    # field is a read oracle.
    for name, extra in _REFERENCE_SPECS.items():
        registry[name] = _spec(
            name,
            filterable=_COMMON_FILTER | {"Name", "Active"} | extra,
            sortable={"Name", "MetaData.LastUpdatedTime"},
            default_sort="Name",
            operators={
                "Active": _BOOL_OPERATORS,
                "SubClass": _BOOL_OPERATORS, "SubDepartment": _BOOL_OPERATORS, "SubItem": _BOOL_OPERATORS,
                "Taxable": _BOOL_OPERATORS, "TaxGroup": _BOOL_OPERATORS,
            },
        )

    # Attachable: the documented filter is on the linked transaction
    # (`AttachableRef.EntityRef.Type` and `.value`). No name, note or file-name
    # field is filterable. Sorting on the modification time is offered and not
    # confirmed live; list_attachments sends no ORDERBY.
    registry["Attachable"] = _spec(
        "Attachable",
        filterable=_COMMON_FILTER | {"AttachableRef.EntityRef.Type", "AttachableRef.EntityRef.value"},
        sortable={"MetaData.LastUpdatedTime"},
        default_sort="MetaData.LastUpdatedTime",
    )
    return registry


ENTITY_REGISTRY: Final[dict[str, EntitySpec]] = _build_registry()

#: Canonical names, keyed by their case-folded form.
#:
#: Intuit's documentation calls entity names case-sensitive in one place (the
#: parameter table on its query syntax page) and case-insensitive in another
#: (the same page's text). This package resolves the contradiction by accepting
#: either from a caller and emitting the canonical spelling upstream, which is
#: correct under both readings. See docs/api-findings.md.
_CANONICAL_BY_FOLD: Final[dict[str, str]] = {
    name.casefold(): name for name in ENTITY_REGISTRY
}

#: Explicitly not served. Named so the rejection can say why.
UNSERVED_ENTITIES: Final[frozenset[str]] = frozenset(
    {"Employee", "TimeActivity", "CompanyInfo", "Preferences"}
)


def served_entities() -> list[str]:
    return sorted(ENTITY_REGISTRY)


# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------

_IDENT_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_NUMBER_RE: Final = re.compile(r"-?\d+(?:\.\d+)?")
_ASCII_SPACE: Final = frozenset(" \t\n\r")
_PUNCT: Final = ("<=", ">=", "!=", "<>", "(", ")", ",", "*", "=", "<", ">")

_COMMENT_MARKERS: Final = ("--", "/*", "*/", "#")


@dataclass(frozen=True, slots=True)
class Token:
    kind: str  # IDENT | NUMBER | STRING | PUNCT
    value: str
    position: int


def tokenize(statement: str) -> list[Token]:
    tokens: list[Token] = []
    index = 0
    length = len(statement)

    while index < length:
        char = statement[index]

        if char in _ASCII_SPACE:
            index += 1
            continue

        # Non-ASCII outside a literal is refused. Homoglyphs and exotic spaces
        # are how a token gets read one way by a validator and another way by
        # the server; inside a quoted literal they are ordinary data and are
        # left alone.
        if ord(char) > 127:
            raise QueryError(
                f"unexpected non-ASCII character at position {index}; only text "
                f"inside quoted literals may be non-ASCII"
            )

        if statement.startswith(_COMMENT_MARKERS, index):
            raise QueryError(f"comments are not allowed (position {index})")

        if char == ";":
            raise QueryError(
                f"only one statement is allowed; ';' is not permitted "
                f"(position {index})"
            )

        if char == "'":
            value, index = _read_string(statement, index)
            tokens.append(Token("STRING", value, index))
            continue

        if statement.startswith(_PUNCT, index):
            for punct in _PUNCT:
                if statement.startswith(punct, index):
                    tokens.append(Token("PUNCT", punct, index))
                    index += len(punct)
                    break
            continue

        number = _NUMBER_RE.match(statement, index)
        if number and (char.isdigit() or (char == "-" and index + 1 < length)):
            tokens.append(Token("NUMBER", number.group(), index))
            index = number.end()
            continue

        ident = _IDENT_RE.match(statement, index)
        if ident:
            tokens.append(Token("IDENT", ident.group(), index))
            index = ident.end()
            continue

        raise QueryError(f"unexpected character at position {index}")

    return tokens


def _read_string(statement: str, index: int) -> tuple[str, int]:
    """Read a single-quoted literal, resolving ``\\'`` and ``\\\\``."""
    index += 1
    out: list[str] = []
    while index < len(statement):
        char = statement[index]
        if char == "\\":
            if index + 1 >= len(statement):
                raise QueryError("statement ends inside an escape sequence")
            nxt = statement[index + 1]
            if nxt not in ("'", "\\"):
                # The character is the caller's text, so it is not echoed; the
                # position is enough to find it.
                raise QueryError(
                    f"unsupported escape sequence in a literal (position {index}); "
                    f"only \\' and \\\\ are recognised"
                )
            out.append(nxt)
            index += 2
            continue
        if char == "'":
            return "".join(out), index + 1
        out.append(char)
        index += 1
    raise QueryError("unterminated string literal")


def escape_literal(value: str) -> str:
    """Escape for a single-quoted QuickBooks literal.

    Backslash first, then apostrophe: doing it the other way round would
    re-escape the backslash this function just inserted, turning ``O'Brien``
    into ``O\\\\'Brien``.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


# --------------------------------------------------------------------------
# Parse tree
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Condition:
    field: str
    operator: str
    values: tuple[str, ...]
    literal: bool  # True when values are strings needing quotes

    def render(self) -> str:
        if self.operator == "IN":
            inner = ", ".join(self._render_one(v) for v in self.values)
            return f"{self.field} IN ({inner})"
        return f"{self.field} {self.operator} {self._render_one(self.values[0])}"

    def _render_one(self, value: str) -> str:
        return f"'{escape_literal(value)}'" if self.literal else value


@dataclass(frozen=True, slots=True)
class ParsedQuery:
    entity: str
    count_only: bool
    conditions: tuple[Condition, ...]
    order_by: tuple[str, str] | None
    start_position: int | None
    max_results: int | None

    def canonical(self) -> str:
        """Rebuild the statement from the parse tree.

        This, not the caller's text, is what is sent.
        """
        parts = [f"SELECT {'COUNT(*)' if self.count_only else '*'} FROM {self.entity}"]
        if self.conditions:
            parts.append("WHERE " + " AND ".join(c.render() for c in self.conditions))
        if self.order_by:
            field, direction = self.order_by
            parts.append(f"ORDERBY {field} {direction}")
        if self.start_position is not None:
            parts.append(f"STARTPOSITION {self.start_position}")
        if self.max_results is not None:
            parts.append(f"MAXRESULTS {self.max_results}")
        return " ".join(parts)

    def with_paging(self, *, start_position: int, max_results: int) -> ParsedQuery:
        from dataclasses import replace

        return replace(
            self, start_position=start_position, max_results=max_results
        )

    def as_count(self) -> ParsedQuery:
        """The same WHERE, as a COUNT(*), with paging stripped.

        Paging a count is meaningless, and a count anchored on a *different*
        WHERE would not anchor anything.
        """
        from dataclasses import replace

        return replace(
            self, count_only=True, order_by=None, start_position=None, max_results=None
        )


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

_BOOL_LITERALS: Final = frozenset({"true", "false"})


class _Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.index = 0

    def peek(self) -> Token | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def next(self) -> Token:
        token = self.peek()
        if token is None:
            raise QueryError("statement ended unexpectedly")
        self.index += 1
        return token

    def expect_keyword(self, keyword: str) -> None:
        token = self.next()
        if token.kind != "IDENT" or token.value.upper() != keyword:
            raise QueryError(f"expected {keyword}")

    def accept_keyword(self, keyword: str) -> bool:
        token = self.peek()
        if token and token.kind == "IDENT" and token.value.upper() == keyword:
            self.index += 1
            return True
        return False

    def expect_punct(self, punct: str) -> None:
        token = self.next()
        if token.kind != "PUNCT" or token.value != punct:
            raise QueryError(f"expected '{punct}'")


def parse(statement: str) -> ParsedQuery:
    """Parse and validate. Raises :class:`QueryError` with a model-safe message."""
    if not isinstance(statement, str) or not statement.strip():
        raise QueryError("the statement is empty")
    if len(statement) > MAX_STATEMENT_LENGTH:
        raise QueryError(
            f"the statement is longer than {MAX_STATEMENT_LENGTH} characters. "
            f"Narrow the filter; this server does not fall back to a POST body."
        )

    parser = _Parser(tokenize(statement))

    parser.expect_keyword("SELECT")
    count_only = _parse_select_list(parser)
    parser.expect_keyword("FROM")

    entity_token = parser.next()
    if entity_token.kind != "IDENT":
        raise QueryError("expected an entity name after FROM")
    spec = _resolve_entity(entity_token.value)

    conditions: tuple[Condition, ...] = ()
    if parser.accept_keyword("WHERE"):
        conditions = _parse_conditions(parser, spec)

    order_by = _parse_order_by(parser, spec)
    start_position = _parse_positive(parser, "STARTPOSITION")
    max_results = _parse_positive(parser, "MAXRESULTS")

    if max_results is not None and max_results > MAX_RESULTS_CEILING:
        raise QueryError(f"MAXRESULTS may not exceed {MAX_RESULTS_CEILING}")

    if parser.peek() is not None:
        raise QueryError("unexpected text after the end of the statement")

    return ParsedQuery(
        entity=spec.name,
        count_only=count_only,
        conditions=conditions,
        order_by=order_by,
        start_position=start_position,
        max_results=max_results,
    )


def _parse_select_list(parser: _Parser) -> bool:
    token = parser.next()
    if token.kind == "PUNCT" and token.value == "*":
        return False
    if token.kind == "IDENT" and token.value.upper() == "COUNT":
        parser.expect_punct("(")
        parser.expect_punct("*")
        parser.expect_punct(")")
        return True
    raise QueryError(
        "only 'SELECT *' and 'SELECT COUNT(*)' are supported. QuickBooks does "
        "not support field projections -- every attribute is returned whatever "
        "is asked for -- so a field list would be silently ignored rather than "
        "applied."
    )


def _resolve_entity(name: str) -> EntitySpec:
    canonical = _CANONICAL_BY_FOLD.get(name.casefold())
    if canonical is None:
        unserved = next(
            (u for u in UNSERVED_ENTITIES if u.casefold() == name.casefold()), None
        )
        if unserved is not None:
            # Named in this package's own spelling, never the caller's.
            raise QueryError(
                f"{unserved} is not served by this server. It has no projection, "
                f"so its fields have never been reviewed for what may leave this "
                f"process."
            )
        raise QueryError(
            f"unknown entity. This server serves: {', '.join(served_entities())}"
        )
    return ENTITY_REGISTRY[canonical]


def _parse_conditions(parser: _Parser, spec: EntitySpec) -> tuple[Condition, ...]:
    conditions = [_parse_condition(parser, spec)]
    while parser.accept_keyword("AND"):
        conditions.append(_parse_condition(parser, spec))
    if parser.peek() and parser.peek().kind == "IDENT":  # type: ignore[union-attr]
        upper = parser.peek().value.upper()  # type: ignore[union-attr]
        if upper == "OR":
            raise QueryError(
                "OR is not supported; conditions may only be joined by AND"
            )
    return tuple(conditions)


def _parse_condition(parser: _Parser, spec: EntitySpec) -> Condition:
    field_token = parser.next()
    if field_token.kind != "IDENT":
        raise QueryError("expected a field name in WHERE")
    field = _resolve_field(field_token.value, spec)

    operator = _parse_operator(parser, field, spec)

    if operator == "IN":
        values, literal = _parse_in_list(parser, field)
        return Condition(field=field, operator=operator, values=values, literal=literal)

    value_token = parser.next()
    value, literal = _literal_of(value_token, field)
    return Condition(field=field, operator=operator, values=(value,), literal=literal)


def _resolve_field(name: str, spec: EntitySpec) -> str:
    for candidate in spec.filterable:
        if candidate.casefold() == name.casefold():
            return candidate
    raise QueryError(
        f"the WHERE field is not a filterable field on {spec.name}. Filterable: "
        f"{', '.join(sorted(spec.filterable))}"
    )


def _parse_operator(parser: _Parser, field: str, spec: EntitySpec) -> str:
    token = parser.next()
    if token.kind == "IDENT" and token.value.upper() in ("IN", "LIKE"):
        operator = token.value.upper()
    elif token.kind == "PUNCT" and token.value in ("=", "<", ">", "<=", ">="):
        operator = token.value
    elif token.kind == "PUNCT" and token.value in ("!=", "<>"):
        raise QueryError("'!=' is not supported by the QuickBooks query language")
    else:
        raise QueryError(f"expected a comparison operator after {field}")

    allowed = spec.operators_for(field)
    if operator not in allowed:
        if field == "Id":
            raise QueryError(
                "Id accepts only '=' and 'IN'. Ranges over an opaque identifier "
                "are not meaningful."
            )
        raise QueryError(
            f"{field} on {spec.name} accepts only: {', '.join(sorted(allowed))}"
        )
    return operator


def _parse_in_list(parser: _Parser, field: str) -> tuple[tuple[str, ...], bool]:
    parser.expect_punct("(")
    values: list[str] = []
    literals: list[bool] = []
    while True:
        token = parser.next()
        if token.kind == "PUNCT" and token.value == ")":
            raise QueryError("IN (...) may not be empty")
        value, literal = _literal_of(token, field)
        values.append(value)
        literals.append(literal)
        nxt = parser.next()
        if nxt.kind == "PUNCT" and nxt.value == ")":
            break
        if not (nxt.kind == "PUNCT" and nxt.value == ","):
            raise QueryError(
                "IN (...) takes a comma-separated list of literals; expressions "
                "and subqueries are not permitted"
            )
    if len(set(literals)) != 1:
        raise QueryError("IN (...) may not mix quoted and unquoted literals")
    return tuple(values), literals[0]


def _literal_of(token: Token, field: str) -> tuple[str, bool]:
    if token.kind == "STRING":
        return token.value, True
    if token.kind == "NUMBER":
        return token.value, False
    if token.kind == "IDENT" and token.value.casefold() in _BOOL_LITERALS:
        # `Active IN (true, false)` is the documented way to include inactive
        # rows (Intuit's query syntax page), and booleans are unquoted.
        return token.value.casefold(), False
    raise QueryError(
        f"expected a quoted string, a number or true/false as the value for {field}"
    )


def _parse_order_by(parser: _Parser, spec: EntitySpec) -> tuple[str, str] | None:
    if not parser.accept_keyword("ORDERBY"):
        return None
    token = parser.next()
    if token.kind != "IDENT":
        raise QueryError("expected a field name after ORDERBY")
    if token.value.casefold() == "id":
        raise QueryError(
            "ORDERBY Id is not supported. It is honoured in the QuickBooks "
            "sandbox but was withdrawn from production in January 2026, so accepting "
            "it here would work in testing and fail on real books. Use "
            f"{spec.default_sort} instead."
        )
    field = None
    for candidate in spec.sortable:
        if candidate.casefold() == token.value.casefold():
            field = candidate
            break
    if field is None:
        raise QueryError(
            f"the ORDERBY field is not sortable on {spec.name}. Sortable: "
            f"{', '.join(sorted(spec.sortable))}"
        )
    direction = "ASC"
    if parser.accept_keyword("DESC"):
        direction = "DESC"
    elif parser.accept_keyword("ASC"):
        direction = "ASC"
    return field, direction


def _parse_positive(parser: _Parser, keyword: str) -> int | None:
    if not parser.accept_keyword(keyword):
        return None
    token = parser.next()
    if token.kind != "NUMBER" or "." in token.value:
        raise QueryError(f"{keyword} takes a whole number")
    value = int(token.value)
    if value < 1:
        raise QueryError(
            f"{keyword} must be 1 or greater; QuickBooks pages are 1-indexed"
        )
    return value
