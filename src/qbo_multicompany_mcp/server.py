"""The MCP server entry point.

Two things about startup matter.

*Redacting hooks are installed first*, before argument parsing, before the
registry is read, before anything can raise. A startup failure is one redacted
line on stderr and exit status 2, with no traceback -- and it can only be
redacted if the hooks were already in place when it happened.

*Nothing opens the token store.* Building the server reads the registry and
registers its tools. ``tools/list`` answers from that alone: with only
``QBO_ENTITIES_FILE`` set, a client can enumerate this server's tools and the
server will not have created a store, a directory, or a key. Opening a
credential store is something this server does when asked to fetch data, not
when asked what it can do.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from pydantic import ValidationError

from . import __version__
from .config import Settings, add_common_arguments, register_environment_secrets, settings_from_args
from .errors import (
    NO_APP_CREDENTIALS_STARTUP_TEXT,
    UNKNOWN_TOOL_TEXT,
    ConfigurationError,
    StartupError,
    argument_error_text,
)
from .redaction import ValueFreeParser, install_redacting_hooks, redact
from .registry import Registry
from .tools import register_tools

INSTRUCTIONS = """\
Read-only access to several QuickBooks Online company files.

Every tool that touches QuickBooks requires an explicit `entity` key naming one
company. There is no default company and no matching on company name -- call
list_entities first to see the configured keys.

This server never writes to QuickBooks. Intuit publishes no read-only scope for
the Accounting API: its one accounting scope covers reads and writes alike, so
read-only is enforced by this server's own network policy: only GET requests to
an allowlisted set of accounting paths are permitted, and every request is
checked immediately before it is sent.

Results identify their source by entity key, environment and the last four
digits of the realm id. No field this server builds carries the full realm id;
free text from the company file is returned as it is.
"""


class QboMCPServer(MCPServer):
    """The SDK's server with one change, at the call boundary.

    Every ``tools/call`` reaches this method: the SDK's handler awaits
    ``self.call_tool`` for each one (mcp/server/mcpserver/server.py). The SDK
    validates the arguments inside it, before any tool runs, and reports a
    failure with pydantic's text, which quotes the value -- over real stdio
    a configured client secret sent as an argument would come back that way,
    and an unknown tool's name would be repeated too. Both are rebuilt here:
    an argument refusal from ``errors(include_input=False)``, the parameter
    and the reason, never the value, and an unknown name as fixed text.
    Everything else passes through unchanged; a tool's own refusals are fixed
    text already.
    """

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
        try:
            return await super().call_tool(name, arguments, context)
        except UnexpectedToolError:
            raise
        except ToolError as exc:
            cause = exc.__cause__
            if isinstance(cause, ValidationError):
                declared = await self._declared_parameters(name)
                raise ToolError(
                    argument_error_text(name, cause.errors(include_input=False), declared)
                ) from None
            if cause is None and str(exc) == f"Unknown tool: {name}":
                raise ToolError(UNKNOWN_TOOL_TEXT) from None
            raise

    async def _declared_parameters(self, name: str) -> frozenset[str]:
        """The parameter names ``name`` publishes: the only locations an
        argument refusal may name."""
        for tool in await self.list_tools():
            if tool.name == name:
                return frozenset((tool.input_schema or {}).get("properties", {}))
        return frozenset()


def build_server(settings: Settings, registry: Registry) -> MCPServer:
    server = QboMCPServer(
        name="qbo-multicompany-mcp",
        title="QuickBooks Online (multi-company, read-only)",
        version=__version__,
        instructions=INSTRUCTIONS,
    )
    register_tools(server, settings, registry)
    return server


def build_parser() -> argparse.ArgumentParser:
    # ValueFreeParser turns abbreviations off. argparse's default reads any
    # unambiguous prefix as the option: --allow-doc would be --allow-documents.
    parser = ValueFreeParser(
        prog="qbo-multicompany-mcp",
        description="Read-only MCP server for several QuickBooks Online company files.",
    )
    add_common_arguments(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    # First, before anything can log or raise.
    install_redacting_hooks()
    register_environment_secrets()

    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        settings = settings_from_args(args)
        registry = settings.load_registry()
    except (StartupError, ConfigurationError) as exc:
        # One line, redacted, no traceback, exit 2.
        print(f"qbo-multicompany-mcp: {redact(exc)}", file=sys.stderr)
        return 2

    if settings.credentials() is None:
        # Not a startup failure: registries start servers with no secrets to
        # list their tools, and that must keep working. Every call that would
        # send a stored credential refuses on its own.
        print(f"qbo-multicompany-mcp: {NO_APP_CREDENTIALS_STARTUP_TEXT}", file=sys.stderr)

    if settings.verbose:
        logging.getLogger("qbo_multicompany_mcp").setLevel(logging.DEBUG)
        logging.getLogger().setLevel(logging.DEBUG)

    server = build_server(settings, registry)
    server.run("stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
