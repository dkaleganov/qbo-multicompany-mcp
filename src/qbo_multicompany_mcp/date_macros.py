"""Resolving a QuickBooks ``date_macro`` into a window this server can check.

Resolved date macros are validated like explicit dates. The six-month limit on
every detail report and the 200-day ``Days`` rule are checked before any
request, and a macro that this server cannot turn into dates cannot be checked
at all. This module turns macros into dates so both rules can read them.

**The request still sends the macro.** Resolution here is this server's
estimate for the policy checks, disclosed on the result as ``resolved_start``
and ``resolved_end``; QuickBooks resolves the macro for real, and the
response's ``StartPeriod``/``EndPeriod`` are the authority on what was
reported. Sending the resolved dates instead would replace QuickBooks's
calendar with this one.

**The vocabulary is the documented one.** Twenty-three values on twenty-two
report pages, one identical line on each. Three pages -- TransactionList,
TransactionListByCustomer and TransactionListByVendor -- list thirty-three:
the same twenty-three plus ten Calendar macros. Four report pages list no
``date_macro`` at all (APAgingDetail, ARAgingDetail, AccountListDetail,
CustomerBalanceDetail). Which report admits which is the per-report table in
:mod:`qbo_multicompany_mcp.report_catalog`; this module only resolves.

**The book calendar.** Fiscal macros resolve against the company's
``FirstMonthOfFiscalYear`` (``AccountingInfoPrefs`` on Intuit's Preferences
page), which :func:`qbo_multicompany_mcp.projections.project_preferences`
yields as ``fiscal_year_start_month`` (a month NAME, ``"January"``) and
:func:`fiscal_start_month` reads as a number. Calendar macros anchor on
January. "Today" is the operator's local date (``date.today()`` in the
server process), which the caller supplies; no timezone is inferred beyond
that, and the result labels it as the operator's local date.

**Three things Intuit's pages do not define, each probed live on 2026-09-25
against the sandbox company by comparing this resolver's window with
QuickBooks's own ``start_period``/``end_period`` for the same request:**

* *A week starts on Sunday* (:data:`WEEK_STARTS_ON`). OBSERVED 2026-09-25:
  "This Week" on Friday 2026-09-25 resolved here to 09-20..09-26 and
  QuickBooks reported 09-20..09-26.
* *"Last ...-to-date" ends at the same calendar position in the previous
  period* -- the same month offset and day of month, clamped to that month
  and to the period (31 March: last month to date is all of February; 29
  February in a leap year: last year to date ends on the 28th). OBSERVED
  2026-09-25: Last Fiscal Quarter-to-date resolved here to 04-01..06-25 and
  QuickBooks reported 04-01..06-25; a day-count offset would have given
  06-26, so the observation discriminates. Last Month-to-date 08-01..08-25
  agreed likewise.
* *"Next 4 Weeks"* is not 28 days from today. OBSERVED 2026-09-25: on Friday
  2026-09-25 QuickBooks reported 09-27..10-24, where 28 days from today
  inclusive gives 09-25..10-22. The window starts on the first day of the
  NEXT week -- the same day "Next Week" starts -- and runs
  :data:`NEXT_FOUR_WEEKS_DAYS` days inclusive. One ASSUMPTION remains, not
  shown by an observation on a Friday: when today is itself a Sunday, "next
  week" is read as the week starting seven days on, as it is for "Next
  Week". Inside every limit either way.

Everything else follows from the calendar: "-to-date" runs from the period's
start to today. No reading here can move a window across the six-month or
200-day boundary.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

from .errors import QboMcpError

#: The 23 values on 22 of Intuit's report pages, the same line on each.
DATE_MACROS: Final[tuple[str, ...]] = (
    "Today", "Yesterday",
    "This Week", "Last Week", "This Week-to-date", "Last Week-to-date", "Next Week", "Next 4 Weeks",
    "This Month", "Last Month", "This Month-to-date", "Last Month-to-date", "Next Month",
    "This Fiscal Quarter", "Last Fiscal Quarter", "This Fiscal Quarter-to-date",
    "Last Fiscal Quarter-to-date", "Next Fiscal Quarter",
    "This Fiscal Year", "Last Fiscal Year", "This Fiscal Year-to-date", "Last Fiscal Year-to-date",
    "Next Fiscal Year",
)

#: The ten more that TransactionList, TransactionListByCustomer and
#: TransactionListByVendor list.
CALENDAR_DATE_MACROS: Final[tuple[str, ...]] = (
    "This Calendar Quarter", "This Calendar Quarter-to-date", "Last Calendar Quarter",
    "Last Calendar Quarter-to-date", "Next Calendar Quarter",
    "This Calendar Year", "This Calendar Year-to-date", "Last Calendar Year",
    "Last Calendar Year-to-date", "Next Calendar Year",
)

ALL_DATE_MACROS: Final[tuple[str, ...]] = DATE_MACROS + CALENDAR_DATE_MACROS

#: A week starts on Sunday (Python weekday 6). OBSERVED 2026-09-25 (module
#: docstring). Not defined on Intuit's pages.
WEEK_STARTS_ON: Final = 6

#: "Next 4 Weeks" covers 28 days inclusive, counted from the first day of the
#: NEXT week, not from today. OBSERVED 2026-09-25 (module docstring).
NEXT_FOUR_WEEKS_DAYS: Final = 28

#: ``FirstMonthOfFiscalYear`` is a month name (the sample on Intuit's
#: Preferences page shows ``"January"``); matched case-insensitively after
#: trimming.
_MONTH_NUMBERS: Final[dict[str, int]] = {
    name.casefold(): number for number, name in enumerate(calendar.month_name) if name
}


class UnresolvableMacro(QboMcpError):
    """The macro cannot be turned into dates here. Fixed text; the caller's
    value is never repeated."""


@dataclass(frozen=True, slots=True)
class ResolvedWindow:
    """What a macro means in the company's book calendar, as of ``today``."""

    macro: str
    start: date
    end: date
    today: date
    fiscal_year_start_month: int | None

    @property
    def days(self) -> int:
        """Calendar days covered, inclusive: the figure the Days rule reads."""
        return (self.end - self.start).days + 1

    def basis(self) -> str:
        """The disclosure that travels with the resolution."""
        fiscal = (
            f" and the company's fiscal year starting in "
            f"{calendar.month_name[self.fiscal_year_start_month]}"
            if self.fiscal_year_start_month
            else ""
        )
        return (
            f"resolved by this server from the operator's local date "
            f"{self.today.isoformat()}{fiscal}, for validation only; the request "
            f"sent the macro and QuickBooks resolved it itself, so start_period "
            f"and end_period are the authority on what was reported"
        )


def fiscal_start_month(name: object) -> int | None:
    """The month number for a ``FirstMonthOfFiscalYear`` name, or ``None``.

    ``None`` is the honest answer for anything unreadable: a fiscal macro is
    then refused rather than resolved against a January nobody stated.
    """
    if not isinstance(name, str):
        return None
    return _MONTH_NUMBERS.get(name.strip().casefold())


# --------------------------------------------------------------------------
# Calendar arithmetic
# --------------------------------------------------------------------------


def _months_later(first_of_month: date, months: int) -> date:
    """``first_of_month`` moved by ``months`` whole months, staying on the 1st."""
    total = first_of_month.month - 1 + months
    return date(first_of_month.year + total // 12, total % 12 + 1, 1)


def _month_start(moment: date) -> date:
    return date(moment.year, moment.month, 1)


def _period_end(period_start: date, months: int) -> date:
    return _months_later(period_start, months) - timedelta(days=1)


def _week_start(moment: date) -> date:
    return moment - timedelta(days=(moment.weekday() - WEEK_STARTS_ON) % 7)


def _year_start(moment: date, first_month: int) -> date:
    if moment.month >= first_month:
        return date(moment.year, first_month, 1)
    return date(moment.year - 1, first_month, 1)


def _quarter_start(moment: date, first_month: int) -> date:
    year_start = _year_start(moment, first_month)
    quarter = ((moment.month - first_month) % 12) // 3
    return _months_later(year_start, 3 * quarter)


def _to_date_of_previous(previous_start: date, current_start: date, today: date, months: int) -> date:
    """Today's calendar position in the current period, applied to the
    previous one: the same month offset and day of month, clamped to that
    month's length and to the period's end (OBSERVED 2026-09-25; module
    docstring)."""
    month_offset = (today.year - current_start.year) * 12 + today.month - current_start.month
    target_month = _months_later(previous_start, month_offset)
    day = min(today.day, calendar.monthrange(target_month.year, target_month.month)[1])
    candidate = date(target_month.year, target_month.month, day)
    return min(candidate, _period_end(previous_start, months))


# --------------------------------------------------------------------------
# The resolver
# --------------------------------------------------------------------------


def resolve(macro: str, *, today: date, fiscal_year_start_month: int | None) -> ResolvedWindow:
    """Resolve ``macro`` as of ``today`` in the book calendar, or raise.

    ``fiscal_year_start_month`` is needed only by the Fiscal macros; a
    Calendar or period macro resolves without it.
    """
    if macro not in ALL_DATE_MACROS:
        raise UnresolvableMacro(
            "the date_macro is not one of the documented values, so it cannot be "
            "resolved for validation. No request was made."
        )
    fiscal = "Fiscal" in macro
    if fiscal and fiscal_year_start_month is None:
        raise UnresolvableMacro(
            "a fiscal date_macro cannot be resolved for validation: the company's "
            "first month of fiscal year is not known (Preferences carried no "
            "readable FirstMonthOfFiscalYear). Give explicit dates, or a Calendar, "
            "Month, Week or Today macro. No request was made."
        )
    first_month = fiscal_year_start_month if fiscal else 1
    start, end = _bounds(macro, today, first_month)
    return ResolvedWindow(
        macro=macro, start=start, end=end, today=today,
        fiscal_year_start_month=fiscal_year_start_month if fiscal else None,
    )


def _bounds(macro: str, today: date, first_month: int) -> tuple[date, date]:
    if macro == "Today":
        return today, today
    if macro == "Yesterday":
        yesterday = today - timedelta(days=1)
        return yesterday, yesterday
    if macro == "Next 4 Weeks":
        # From the first day of the next week, as QuickBooks resolved it on
        # 2026-09-25 (module docstring): the same start as "Next Week".
        next_week = _week_start(today) + timedelta(days=7)
        return next_week, next_week + timedelta(days=NEXT_FOUR_WEEKS_DAYS - 1)

    tense, _, rest = macro.partition(" ")
    to_date = rest.endswith("-to-date")
    period = rest.removesuffix("-to-date")

    if period == "Week":
        this_start = _week_start(today)
        if tense == "This":
            return this_start, (today if to_date else this_start + timedelta(days=6))
        if tense == "Last":
            last_start = this_start - timedelta(days=7)
            return last_start, (today - timedelta(days=7) if to_date else last_start + timedelta(days=6))
        next_start = this_start + timedelta(days=7)
        return next_start, next_start + timedelta(days=6)

    if period == "Month":
        months = 1
        this_start = _month_start(today)
    elif period.endswith("Quarter"):
        months = 3
        this_start = _quarter_start(today, first_month)
    elif period.endswith("Year"):
        months = 12
        this_start = _year_start(today, first_month)
    else:  # pragma: no cover - every documented macro is one of the above
        raise UnresolvableMacro("the date_macro is not one this server can resolve")

    if tense == "This":
        return this_start, (today if to_date else _period_end(this_start, months))
    if tense == "Last":
        last_start = _months_later(this_start, -months)
        if to_date:
            return last_start, _to_date_of_previous(last_start, this_start, today, months)
        return last_start, _period_end(last_start, months)
    next_start = _months_later(this_start, months)
    return next_start, _period_end(next_start, months)
