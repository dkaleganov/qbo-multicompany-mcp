"""Redaction of secrets, realm ids and authorization codes.

Everything this package writes to stderr or a log, or returns as an error --
the ``--verbose`` trace, a startup failure line, a tool error, an exception
that escapes -- passes through :func:`redact` first. The command-line tools'
own stdout lines are composed to be safe and redact only the exception text
they quote; they show what an operator asked for, and ``export-key`` prints
the key, which is its job.

The redaction list is deliberately broad and deliberately dumb. It is a last
line of defence: the code above it is written not to pass secrets to a logger
in the first place, and the tests assert the client-visible text directly. A
regex that occasionally over-redacts a harmless number costs nothing; one that
under-redacts a refresh token costs a company file.

Shapes are only half of it. A credential with no recognisable shape -- a short
punctuated client secret, a token issued in a new format -- matches none of the
patterns below, so the exact values this process holds are removed first, from
:mod:`qbo_multicompany_mcp.live_values`: any value that is the whole text, and any
value of 16 characters or more inside a longer one. Below that, a scan would
redact ordinary words.

Four-digit runs are never touched, because ``realm_id_last4``,
``employer_id_last4`` and ``AcctNum`` last-four are the deliberate, diagnostic
remainders this package *does* publish.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections.abc import Callable, Iterable
from types import TracebackType
from typing import Any, Final, NoReturn

from . import live_values

REDACTED: Final = "[redacted]"

#: Names whose value is a secret wherever they appear as ``name=value``,
#: ``"name": "value"`` or ``name: value``.
_SECRET_KEYS: Final[tuple[str, ...]] = (
    "access_token",
    "refresh_token",
    "client_secret",
    "code",
    "code_verifier",
    "id_token",
    "state",
    "authorization",
    "token",
    "qbo_token_key",
    # This package's own environment names for the two secrets it reads. The
    # key rules anchor on a word boundary, so `client_secret` alone never
    # matched inside `QBO_CLIENT_SECRET` -- the spelling a `.env` file or an
    # environment dump actually uses.
    "qbo_client_secret",
    "qbo_import_refresh_token",
    "x_refresh_token_expires_in",
)

_KEY_ALTERNATION: Final = "|".join(re.escape(k) for k in _SECRET_KEYS)

_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    # "access_token": "…"  /  'refresh_token': '…'
    (
        re.compile(
            rf'(?i)(["\']?(?:{_KEY_ALTERNATION})["\']?\s*[:=]\s*)(["\'])(?:(?!\2).)*(\2)',
        ),
        rf"\g<1>\g<2>{REDACTED}\g<3>",
    ),
    # access_token=…  client_secret: …  refresh_token = …  -- an UNQUOTED
    # value after either separator, with or without spaces around it.
    #
    # Both separators matter. A rule for `=` alone would let a log line or an
    # exception in the colon form carry its value out untouched, and the
    # long-alphanumeric fallback below does not rescue a short or punctuated
    # secret.
    #
    # Two lookaheads keep it from doing harm. A quoted value is left to the
    # rule above, which has already replaced it. And an auth scheme word is
    # left to the bearer and basic rules: redacting `Bearer` here would leave
    # the token after it standing, with nothing left for those rules to anchor
    # on -- a leak caused by the redactor itself.
    (
        re.compile(
            rf"(?i)(\b(?:{_KEY_ALTERNATION})\b[\"']?[ \t]*[:=][ \t]*)"
            rf"(?![\"'])(?!(?:bearer|basic)\b)[^&\s\"']+"
        ),
        rf"\g<1>{REDACTED}",
    ),
    # Authorization: Bearer …
    (re.compile(r"(?i)\b(bearer)\s+\S+"), rf"\g<1> {REDACTED}"),
    (re.compile(r"(?i)\b(basic)\s+\S+"), rf"\g<1> {REDACTED}"),
    # JWT-shaped values anywhere, an encrypted one's empty key segment included.
    # Possessive, because backtracking over a long dashed line takes seconds and
    # this runs on failure paths.
    (re.compile(r"(?<![A-Za-z0-9])eyJ[A-Za-z0-9_-]{8,}+(?:\.[A-Za-z0-9_-]*){2,4}"), REDACTED),
)

#: The last rules go by length alone, and run last. Refresh tokens and
#: authorization codes are long opaque alphanumerics (the samples on Intuit's
#: "Set up OAuth 2.0" page; up to 512 characters; the 40-character floor is this
#: package's own choice). Realm ids and any other long digit run go the same way,
#: wherever they sit: `_` is a word character, so `\b` would spare `realm_1234...`
#: in a file name. Four-digit "last four" values and ordinary small integers stay
#: legible.
#:
#: ``kept`` is what length would cut into and a reader needs whole: Intuit's
#: support id, in the shape ``client.intuit_tid_of`` admits, and the storage host
#: an operator has to pin. Neither is a secret, the exact-value scan has already
#: run by then, and each is anchored on the exact text its error prints.
_LONG_RUN: Final = re.compile(
    r"(?P<kept>(?<= intuit_tid=)[A-Za-z0-9][A-Za-z0-9-]{0,79}|(?<=the download host ')[A-Za-z0-9._-]+)"
    r"|[A-Za-z0-9]{40,}"
    r"|\d{8,}"
)


def redact(value: Any) -> str:
    """Return ``value`` as text with every recognised secret replaced.

    Never raises: redaction sits on the failure path, and a redactor that
    throws would replace a masked message with an unmasked traceback.
    """
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:  # pragma: no cover - a __str__ that throws
        return REDACTED
    try:
        # Identity first, at any length: a text that IS a held value is
        # redacted whole before any scan, because the substring scan below
        # keeps a length floor and would let a short held value through (an
        # eight-character client secret would come back unchanged while
        # holds() said true).
        if live_values.holds(text):
            return REDACTED
        # Exact values next: a shape rule that fired first could consume part
        # of a value and leave the rest of it standing.
        for value in live_values.snapshot():
            if value in text:
                text = text.replace(value, REDACTED)
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
        text = _LONG_RUN.sub(lambda match: match["kept"] or REDACTED, text)
    except Exception:  # pragma: no cover - defensive
        return REDACTED
    return text


def redact_all(values: Iterable[Any]) -> list[str]:
    """Redact each item of ``values``."""
    return [redact(v) for v in values]


class RedactingFilter(logging.Filter):
    """Rewrites a log record's message, args, exception and stack text in place.

    Run on every record as it is made (:func:`install_redacting_hooks`), so a
    traceback the SDK logs on our behalf is masked before any handler sees it.
    What it leaves alone: fields a caller attaches with ``extra=``, and a record
    rebuilt by ``logging.makeLogRecord``. Nothing in this package logs either
    way.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
        except Exception:  # pragma: no cover - defensive
            record.msg = REDACTED
        record.args = ()
        if record.exc_info is not None:
            try:
                record.exc_text = redact(_format_exc(*record.exc_info))
            except Exception:
                # A malformed exc_info from some library: this runs inside
                # every logging call in the process, so it must never raise.
                record.exc_text = REDACTED
            record.exc_info = None
        if record.exc_text is not None:
            record.exc_text = redact(record.exc_text)
        if record.stack_info is not None:
            record.stack_info = redact(record.stack_info)
        return True


def _masked_as_made(
    previous: Callable[..., logging.LogRecord],
) -> Callable[..., logging.LogRecord]:
    """A record factory that masks each record the moment it is made."""
    masking = RedactingFilter()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        masking.filter(record)
        return record

    factory.masks_records = True  # type: ignore[attr-defined]
    return factory


def _masked_on_make(
    previous: Callable[..., logging.LogRecord],
) -> Callable[..., logging.LogRecord]:
    """``Logger.makeRecord``, masking each record it returns."""
    masking = RedactingFilter()

    def make_record(self: logging.Logger, *args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(self, *args, **kwargs)
        masking.filter(record)
        return record

    make_record.masks_records = True  # type: ignore[attr-defined]
    return make_record


def _format_exc(
    exc_type: type[BaseException] | None,
    exc: BaseException | None,
    tb: TracebackType | None,
) -> str:
    import traceback

    return "".join(traceback.format_exception(exc_type, exc, tb))


#: What a word on a refused command line must look like to be printed.
_OPTION_NAME: Final = re.compile(r"--[A-Za-z][A-Za-z0-9-]*|-[A-Za-z]")


def _option_name(word: str) -> str:
    name = word.partition("=")[0]
    return name if _OPTION_NAME.fullmatch(name) else "<value>"


class ValueFreeParser(argparse.ArgumentParser):
    """A parser whose refusals keep argparse's reason and option-shaped words.

    ``argparse`` prints what it refuses straight to stderr, past every hook
    below: an unknown argument, a bad choice, a value a flag can't take. A
    stray argument can be a company id or a secret, and stderr can end up in a
    client's log. So a refusal keeps argparse's own reason and any word shaped
    like an option, and drops the rest.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Off for every parser of this class, subcommands included. argparse
        # would read an unambiguous prefix as the option it names, and a prefix
        # that fits two options is the one refusal it prints unquoted.
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def parse_args(self, args: Any = None, namespace: Any = None) -> Any:
        parsed, stray = self.parse_known_args(args, namespace)
        if stray:
            self.error(f"unrecognized arguments: {' '.join(_option_name(word) for word in stray)}")
        return parsed

    def error(self, message: str) -> NoReturn:
        # Any other value argparse repeats, it repeats with %r, so what follows
        # the first quote isn't argparse's own text.
        reason = re.split("['\"]", message, maxsplit=1)[0].rstrip(": ")
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {redact(reason)}\n")


_INSTALLED = False


def install_redacting_hooks(*, stream: Any = None, level: int = logging.WARNING) -> None:
    """Install redaction before anything else can log.

    Called first thing in every entry point, ahead of argument parsing, so that
    even a failure while reading the registry is masked on its way to stderr.

    Logging goes to stderr only. stdout carries the MCP stdio transport, and a
    stray byte there corrupts the session.
    """
    global _INSTALLED
    target = stream if stream is not None else sys.stderr

    root = logging.getLogger()
    handler = logging.StreamHandler(target)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)
    root.setLevel(level)

    # These two log whole URLs, and a signed attachment link is a credential no
    # pattern can recognise. Their own level holds whatever --verbose sets on
    # the root logger.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)

    # Masked as it is made, not only where it is handled. A logger's filter
    # never sees what its children log, and a child's record reaches every
    # handler up the tree: one installed before this one, or later by the SDK
    # or a host embedding this server, would otherwise get it unmasked.
    current = logging.getLogRecordFactory()
    if not getattr(current, "masks_records", False):
        logging.setLogRecordFactory(_masked_as_made(current))
    # And where every logger makes its records, so a library that later
    # replaces the factory without calling this one unmasks nothing. Each
    # covers the other's gap: the factory reaches a logger class that makes
    # its own records. Masking a standard record twice gives the same text.
    if not getattr(logging.Logger.makeRecord, "masks_records", False):
        logging.Logger.makeRecord = _masked_on_make(logging.Logger.makeRecord)  # type: ignore[method-assign]

    def _excepthook(
        exc_type: type[BaseException],
        exc: BaseException,
        tb: TracebackType | None,
    ) -> None:
        print(redact(_format_exc(exc_type, exc, tb)), file=target)

    sys.excepthook = _excepthook
    _INSTALLED = True


def hooks_installed() -> bool:
    return _INSTALLED
