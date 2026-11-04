"""Proves the parser accepts exactly what it should and rebuilds what it sends.

The parser is the gate between a model's text and a URL sent with a bearer
token attached. Its accept and reject tables are therefore per entity: a field
is filterable on a *named* entity or it is rejected, because a general
permission is not a contract.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from qbo_multicompany_mcp.query_grammar import (
    ENTITY_REGISTRY,
    MAX_RESULTS_CEILING,
    TRANSACTION_TYPES,
    QueryError,
    escape_literal,
    parse,
    served_entities,
)


# --------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------


def test_the_canonical_statement_is_rebuilt_not_forwarded() -> None:
    """Validating a string and forwarding the original means the thing checked
    and the thing sent are two different objects."""
    parsed = parse("select   *   from    vendor   where   active   =   true")
    assert parsed.canonical() == "SELECT * FROM Vendor WHERE Active = true"


def test_entity_names_are_emitted_canonically() -> None:
    """The documentation calls entity names case-sensitive in one place and
    case-insensitive in another. Accepting either and emitting the canonical
    spelling is correct under both readings."""
    for spelling in ("vendor", "VENDOR", "Vendor", "vEnDoR"):
        assert parse(f"SELECT * FROM {spelling}").entity == "Vendor"


def test_count_and_star_are_the_only_select_lists() -> None:
    assert parse("SELECT COUNT(*) FROM Vendor").count_only is True
    assert parse("SELECT * FROM Vendor").count_only is False


def test_a_field_list_is_rejected_with_the_reason() -> None:
    """Upstream ignores projections, so a field list would look like a filter
    and do nothing."""
    with pytest.raises(QueryError) as caught:
        parse("SELECT Id, DisplayName FROM Vendor")
    assert "does not support field projections" in str(caught.value)


def test_thirteen_transaction_types_are_served() -> None:
    assert len(TRANSACTION_TYPES) == 13
    assert "Estimate" not in TRANSACTION_TYPES  # non-posting
    for name in TRANSACTION_TYPES:
        assert name in ENTITY_REGISTRY


# --------------------------------------------------------------------------
# Per-entity accept / reject tables
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entity", "field"),
    [
        ("Vendor", "DisplayName"),
        ("Vendor", "Vendor1099"),
        ("Vendor", "Balance"),
        ("Customer", "Job"),
        ("Account", "AccountType"),
        ("Purchase", "PaymentType"),
        ("Purchase", "EntityRef"),
        ("BillPayment", "PayType"),
        ("JournalEntry", "Adjustment"),
        ("Invoice", "CustomerRef"),
    ],
)
def test_accepted_fields_per_entity(entity: str, field: str) -> None:
    parse(f"SELECT * FROM {entity} WHERE {field} = 'x'")


@pytest.mark.parametrize(
    ("entity", "field"),
    [
        # Wrong entity's field.
        ("Vendor", "PayType"),
        ("Customer", "Vendor1099"),
        ("Account", "TxnDate"),
        ("BillPayment", "PaymentType"),
        # Never filterable anywhere: these are oracles even when not returned.
        ("Vendor", "PrimaryEmailAddr"),
        ("Vendor", "TaxIdentifier"),
        ("Customer", "Notes"),
        ("Customer", "PrimaryPhone"),
        ("Purchase", "CreditChargeInfo"),
    ],
)
def test_rejected_fields_per_entity(entity: str, field: str) -> None:
    with pytest.raises(QueryError, match="not a filterable field"):
        parse(f"SELECT * FROM {entity} WHERE {field} = 'x'")


def test_a_pii_field_is_not_filterable_on_any_served_entity() -> None:
    """Equality probing recovers a value one guess at a time, so a filterable
    field is a read even when it is never a returned field."""
    forbidden = {
        "PrimaryEmailAddr", "PrimaryPhone", "Mobile", "Fax", "WebAddr",
        "BillAddr", "ShipAddr", "TaxIdentifier", "Notes", "AcctNum",
    }
    for name, spec in ENTITY_REGISTRY.items():
        leaked = forbidden & spec.filterable
        assert not leaked, f"{name} allows filtering on {leaked}"


def test_employee_is_served_nowhere() -> None:
    assert "Employee" not in served_entities()
    with pytest.raises(QueryError, match="not served"):
        parse("SELECT * FROM Employee")


def test_an_unknown_entity_lists_what_is_served() -> None:
    with pytest.raises(QueryError) as caught:
        parse("SELECT * FROM Sasquatch")
    assert "Vendor" in str(caught.value)


# --------------------------------------------------------------------------
# Operators
# --------------------------------------------------------------------------


@pytest.mark.parametrize("operator", ["=", "IN"])
def test_id_accepts_only_equality_and_membership(operator: str) -> None:
    if operator == "=":
        parse("SELECT * FROM Vendor WHERE Id = '7'")
    else:
        parse("SELECT * FROM Vendor WHERE Id IN ('7', '8')")


@pytest.mark.parametrize("operator", ["<", ">", "<=", ">=", "LIKE"])
def test_id_rejects_every_other_operator(operator: str) -> None:
    with pytest.raises(QueryError, match="Id accepts only"):
        parse(f"SELECT * FROM Vendor WHERE Id {operator} '7'")


@pytest.mark.parametrize("operator", ["=", "<", ">", "<=", ">=", "LIKE"])
def test_other_fields_take_the_full_operator_set(operator: str) -> None:
    parse(f"SELECT * FROM Vendor WHERE DisplayName {operator} 'a'")


def test_not_equal_is_rejected() -> None:
    with pytest.raises(QueryError, match="not supported"):
        parse("SELECT * FROM Vendor WHERE DisplayName != 'a'")


def test_or_is_rejected() -> None:
    with pytest.raises(QueryError, match="not a filterable field|only be joined by AND"):
        parse("SELECT * FROM Vendor WHERE DisplayName = 'a' OR Active = true")


def test_in_takes_literal_lists_only() -> None:
    parse("SELECT * FROM Vendor WHERE Id IN ('1', '2', '3')")
    with pytest.raises(QueryError):
        parse("SELECT * FROM Vendor WHERE Id IN (SELECT Id FROM Customer)")
    with pytest.raises(QueryError, match="may not be empty"):
        parse("SELECT * FROM Vendor WHERE Id IN ()")


def test_in_may_not_mix_quoted_and_unquoted() -> None:
    with pytest.raises(QueryError, match="mix quoted and unquoted"):
        parse("SELECT * FROM Vendor WHERE Id IN ('1', 2)")


def test_active_in_true_false_is_the_documented_inactive_form() -> None:
    parsed = parse("SELECT * FROM Vendor WHERE Active IN (true, false)")
    assert parsed.canonical().endswith("Active IN (true, false)")


# --------------------------------------------------------------------------
# Escaping and encoding
# --------------------------------------------------------------------------


def test_backslash_is_escaped_before_the_apostrophe() -> None:
    """The other order would re-escape the backslash just inserted, turning
    O'Brien into O\\\\'Brien."""
    assert escape_literal("O'Brien") == "O\\'Brien"
    assert escape_literal("a\\b") == "a\\\\b"
    assert escape_literal("a\\'b") == "a\\\\\\'b"


def test_an_apostrophe_survives_a_round_trip() -> None:
    parsed = parse("SELECT * FROM Vendor WHERE DisplayName = 'O\\'Brien'")
    assert parsed.conditions[0].values == ("O'Brien",)
    assert parsed.canonical() == "SELECT * FROM Vendor WHERE DisplayName = 'O\\'Brien'"


def test_the_statement_is_url_encoded_exactly_once() -> None:
    statement = parse(
        "SELECT * FROM Vendor WHERE DisplayName = 'O\\'Brien \\\\ Co'"
    ).canonical()
    request = httpx.Request(
        "GET", "https://x.example.com/v3/company/1/query", params={"query": statement}
    )
    raw = urlsplit(str(request.url)).query
    assert parse_qs(raw)["query"][0] == statement
    assert "%25" not in raw  # a second pass would produce this


def test_an_unterminated_literal_is_rejected() -> None:
    with pytest.raises(QueryError, match="unterminated"):
        parse("SELECT * FROM Vendor WHERE DisplayName = 'open")


def test_an_unsupported_escape_is_rejected() -> None:
    with pytest.raises(QueryError, match="unsupported escape"):
        parse("SELECT * FROM Vendor WHERE DisplayName = 'a\\nb'")


# --------------------------------------------------------------------------
# Injection surface
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM Vendor; SELECT * FROM Customer",
        "SELECT * FROM Vendor -- comment",
        "SELECT * FROM Vendor /* comment */",
        "SELECT * FROM Vendor # comment",
    ],
)
def test_comments_and_extra_statements_are_rejected(statement: str) -> None:
    with pytest.raises(QueryError):
        parse(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM Vendor WHERE Active = true",   # non-breaking space
        "SELECT * FROM Ｖendor",                        # fullwidth V
        "SELECT * FROM Vendor WHERE Аctive = true",    # Cyrillic A homoglyph
    ],
)
def test_unexpected_unicode_token_forms_are_rejected(statement: str) -> None:
    """A homoglyph is how a token gets read one way by a validator and another
    by the server."""
    with pytest.raises(QueryError):
        parse(statement)


def test_non_ascii_inside_a_literal_is_allowed() -> None:
    """Company names are not ASCII-only. Inside quotes it is data."""
    parsed = parse("SELECT * FROM Vendor WHERE DisplayName = 'Café Ω'")
    assert parsed.conditions[0].values == ("Café Ω",)


def test_trailing_text_is_rejected() -> None:
    with pytest.raises(QueryError, match="unexpected text"):
        parse("SELECT * FROM Vendor LIMIT 5")


# --------------------------------------------------------------------------
# Paging and ordering
# --------------------------------------------------------------------------


def test_max_results_is_capped() -> None:
    parse(f"SELECT * FROM Vendor MAXRESULTS {MAX_RESULTS_CEILING}")
    with pytest.raises(QueryError, match="may not exceed"):
        parse(f"SELECT * FROM Vendor MAXRESULTS {MAX_RESULTS_CEILING + 1}")


@pytest.mark.parametrize("value", ["0", "-1"])
def test_page_positions_must_be_positive(value: str) -> None:
    with pytest.raises(QueryError, match="1 or greater"):
        parse(f"SELECT * FROM Vendor STARTPOSITION {value}")


def test_orderby_id_is_rejected() -> None:
    with pytest.raises(QueryError, match="ORDERBY Id is not supported"):
        parse("SELECT * FROM Vendor ORDERBY Id")


def test_orderby_is_per_entity() -> None:
    parse("SELECT * FROM Vendor ORDERBY DisplayName DESC")
    parse("SELECT * FROM Purchase ORDERBY TxnDate ASC")
    with pytest.raises(QueryError, match="not sortable"):
        parse("SELECT * FROM Purchase ORDERBY DisplayName")
    with pytest.raises(QueryError, match="not sortable"):
        parse("SELECT * FROM Vendor ORDERBY TxnDate")


def test_as_count_keeps_the_where_and_drops_the_paging() -> None:
    """A count anchored on a different WHERE would anchor nothing."""
    parsed = parse(
        "SELECT * FROM Purchase WHERE TxnDate >= '2026-01-01' "
        "ORDERBY TxnDate ASC STARTPOSITION 11 MAXRESULTS 10"
    )
    counted = parsed.as_count()
    assert counted.canonical() == (
        "SELECT COUNT(*) FROM Purchase WHERE TxnDate >= '2026-01-01'"
    )


def test_an_over_long_statement_is_an_error_not_a_post_fallback() -> None:
    """This package is GET-only by product choice. The upstream read-only POST
    /query exists; silently switching to it would make the boundary a sentence
    nobody could state."""
    padding = "x" * 4100
    with pytest.raises(QueryError, match="does not fall back to a POST"):
        parse(f"SELECT * FROM Vendor WHERE DisplayName = '{padding}'")
