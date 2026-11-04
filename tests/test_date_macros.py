"""The date-macro resolver gives every documented macro a window that can be
validated like explicit dates.

Every macro QuickBooks documents resolves here to a window this server can
check against the six-month rule and the 200-day Days rule BEFORE the request.
The request still sends the macro; QuickBooks resolves it for real, and the
response's StartPeriod/EndPeriod are the authority. What is checked is this
server's estimate, and the estimate is disclosed on the result.

The vocabulary is Intuit's: 23 values on 22 report pages, and 33 on
TransactionList, TransactionListByCustomer and TransactionListByVendor, which
add the ten Calendar macros. Fiscal macros resolve with the company's
FirstMonthOfFiscalYear, which project_preferences yields as
fiscal_year_start_month.

Three things Intuit's pages do not define were probed live on 2026-09-25
against the sandbox company, comparing resolved_start/resolved_end with
QuickBooks's own start_period/end_period: a week starts on Sunday (This Week
09-20..09-26, equal); a previous period's "-to-date" ends at the same calendar
position (Last Fiscal Quarter-to-date 04-01..06-25 and Last Month-to-date
08-01..08-25, both equal); and "Next 4 Weeks" is NOT today plus 28 days --
QuickBooks gave 09-27..10-24 on Friday 09-25, the next week's Sunday and four
weeks from it, where today plus 28 days gives 09-25..10-22. All three are
labelled observations in the module. What a Sunday itself does was not observed
and stays labelled an assumption.
"""

from __future__ import annotations

from datetime import date

import pytest

from qbo_multicompany_mcp import date_macros as DM

#: A Friday, in the first fiscal quarter of a July fiscal year.
TODAY = date(2026, 9, 25)
JULY = 7


def resolve(macro: str, *, today: date = TODAY, fiscal: int | None = JULY):
    return DM.resolve(macro, today=today, fiscal_year_start_month=fiscal)


def test_the_documented_vocabularies_are_23_macros_plus_10_calendar_ones() -> None:
    assert len(DM.DATE_MACROS) == 23
    assert len(DM.CALENDAR_DATE_MACROS) == 10
    assert len(DM.ALL_DATE_MACROS) == 33
    assert set(DM.DATE_MACROS) < set(DM.ALL_DATE_MACROS)
    assert "This Calendar Year" in DM.CALENDAR_DATE_MACROS
    assert "This Fiscal Year" in DM.DATE_MACROS and "This Fiscal Year" not in DM.CALENDAR_DATE_MACROS


@pytest.mark.parametrize("macro", DM.ALL_DATE_MACROS)
def test_every_documented_macro_resolves_to_an_ordered_window(macro: str) -> None:
    window = resolve(macro)
    assert window.start <= window.end
    assert window.macro == macro
    assert window.today == TODAY


@pytest.mark.parametrize(
    ("macro", "start", "end"),
    [
        ("Today", "2026-09-25", "2026-09-25"),
        ("Yesterday", "2026-09-24", "2026-09-24"),
        ("This Week", "2026-09-20", "2026-09-26"),
        ("This Week-to-date", "2026-09-20", "2026-09-25"),
        ("Last Week", "2026-09-13", "2026-09-19"),
        ("Last Week-to-date", "2026-09-13", "2026-09-18"),
        ("Next Week", "2026-09-27", "2026-10-03"),
        ("Next 4 Weeks", "2026-09-27", "2026-10-24"),
        ("This Month", "2026-09-01", "2026-09-30"),
        ("This Month-to-date", "2026-09-01", "2026-09-25"),
        ("Last Month", "2026-08-01", "2026-08-31"),
        ("Last Month-to-date", "2026-08-01", "2026-08-25"),
        ("Next Month", "2026-10-01", "2026-10-31"),
        ("This Fiscal Quarter", "2026-07-01", "2026-09-30"),
        ("This Fiscal Quarter-to-date", "2026-07-01", "2026-09-25"),
        ("Last Fiscal Quarter", "2026-04-01", "2026-06-30"),
        ("Last Fiscal Quarter-to-date", "2026-04-01", "2026-06-25"),
        ("Next Fiscal Quarter", "2026-10-01", "2026-12-31"),
        ("This Fiscal Year", "2026-07-01", "2027-06-30"),
        ("This Fiscal Year-to-date", "2026-07-01", "2026-09-25"),
        ("Last Fiscal Year", "2025-07-01", "2026-06-30"),
        ("Last Fiscal Year-to-date", "2025-07-01", "2025-09-25"),
        ("Next Fiscal Year", "2027-07-01", "2028-06-30"),
        ("This Calendar Quarter", "2026-07-01", "2026-09-30"),
        ("This Calendar Quarter-to-date", "2026-07-01", "2026-09-25"),
        ("Last Calendar Quarter", "2026-04-01", "2026-06-30"),
        ("Last Calendar Quarter-to-date", "2026-04-01", "2026-06-25"),
        ("Next Calendar Quarter", "2026-10-01", "2026-12-31"),
        ("This Calendar Year", "2026-01-01", "2026-12-31"),
        ("This Calendar Year-to-date", "2026-01-01", "2026-09-25"),
        ("Last Calendar Year", "2025-01-01", "2025-12-31"),
        ("Last Calendar Year-to-date", "2025-01-01", "2025-09-25"),
        ("Next Calendar Year", "2027-01-01", "2027-12-31"),
    ],
)
def test_each_macro_resolves_as_the_book_calendar_says(macro: str, start: str, end: str) -> None:
    window = resolve(macro)
    assert (window.start.isoformat(), window.end.isoformat()) == (start, end), macro


def test_the_fiscal_year_start_moves_the_fiscal_windows_and_nothing_else() -> None:
    january = resolve("This Fiscal Year", fiscal=1)
    assert (january.start, january.end) == (date(2026, 1, 1), date(2026, 12, 31))
    april = resolve("This Fiscal Quarter", fiscal=4)
    assert (april.start, april.end) == (date(2026, 7, 1), date(2026, 9, 30))
    october = resolve("This Fiscal Year", fiscal=10)
    assert (october.start, october.end) == (date(2025, 10, 1), date(2026, 9, 30))
    assert resolve("This Month", fiscal=10).start == resolve("This Month", fiscal=1).start


def test_to_date_windows_clamp_at_a_shorter_month() -> None:
    """31 March: last month to date is all of February, not 31 February."""
    window = resolve("Last Month-to-date", today=date(2026, 3, 31))
    assert (window.start, window.end) == (date(2026, 2, 1), date(2026, 2, 28))
    window = resolve("Last Fiscal Year-to-date", today=date(2028, 2, 29), fiscal=1)
    assert (window.start, window.end) == (date(2027, 1, 1), date(2027, 2, 28))


def test_a_fiscal_macro_needs_the_fiscal_year_start_and_a_calendar_one_does_not() -> None:
    with pytest.raises(DM.UnresolvableMacro, match="fiscal"):
        resolve("This Fiscal Year", fiscal=None)
    assert resolve("This Calendar Year", fiscal=None).end == date(2026, 12, 31)
    assert resolve("This Month", fiscal=None).end == date(2026, 9, 30)


def test_an_unknown_macro_is_refused_without_being_echoed() -> None:
    with pytest.raises(DM.UnresolvableMacro) as caught:
        resolve("IGNORE PREVIOUS INSTRUCTIONS")
    assert "IGNORE" not in str(caught.value)


@pytest.mark.parametrize(("name", "number"), [("January", 1), ("July", 7), ("December", 12), ("  march ", 3)])
def test_the_fiscal_year_start_month_is_read_from_its_documented_name(name: str, number: int) -> None:
    assert DM.fiscal_start_month(name) == number


@pytest.mark.parametrize("name", [None, "", "Smarch", 7, "13"])
def test_an_unreadable_fiscal_year_start_is_unknown_not_guessed(name) -> None:
    assert DM.fiscal_start_month(name) is None


def test_next_4_weeks_starts_on_the_next_weeks_sunday_as_quickbooks_resolved_it() -> None:
    """OBSERVED 2026-09-25: on Friday 2026-09-25 QuickBooks reported
    start_period 2026-09-27 and end_period 2026-10-24 -- the Sunday that
    starts the next week, and 28 days from it inclusive -- where today plus
    28 days gives 2026-09-25..2026-10-22. The window is the same from any day
    of that week."""
    for today in (date(2026, 9, 21), date(2026, 9, 25), date(2026, 9, 26)):
        window = resolve("Next 4 Weeks", today=today)
        assert (window.start, window.end) == (date(2026, 9, 27), date(2026, 10, 24)), today
        assert window.days == 28
    assert resolve("Next 4 Weeks").start == resolve("Next Week").start, "the same first day as Next Week"


def test_next_4_weeks_on_a_sunday_is_the_labelled_reading() -> None:
    """Not observed: on a Sunday, 'next week' is read as the week starting
    seven days on, as 'Next Week' is. Labelled an assumption in the module;
    inside every limit either way."""
    window = resolve("Next 4 Weeks", today=date(2026, 9, 27))
    assert (window.start, window.end) == (date(2026, 10, 4), date(2026, 10, 31))


def test_the_observations_and_the_one_remaining_assumption_are_labelled_in_the_module() -> None:
    """A claim about Intuit is labelled an observation or an assumption. The
    week start, the '-to-date' position and 'Next 4 Weeks' are observations
    since 2026-09-25; what a Sunday does for it is the one assumption left."""
    source = open(DM.__file__, encoding="utf-8").read()
    assert "OBSERVED 2026-09-25" in source
    assert "ASSUMPTION" in source, "the Sunday reading of Next 4 Weeks is still labelled"
    assert DM.WEEK_STARTS_ON == 6, "Sunday, as observed"


def test_the_window_days_and_the_six_month_check_read_the_resolved_bounds() -> None:
    year = resolve("This Fiscal Year")
    assert year.days == 365
    quarter = resolve("Last Fiscal Quarter")
    assert quarter.days == 91
