"""Regression protection over `src/`. **Not proof.**

This file can see that a literal ``"GET"`` was written at a call site. It cannot
see what a variable holds at run time, and the low-level send takes a *request
object*, not a method string -- so the effective method and URL are only knowable
at the moment of sending. That is why
:func:`qbo_multicompany_mcp.netpolicy.enforce` inspects the built request
immediately before the transport gets it, and why these checks are a tripwire
rather than the guarantee.

What a tripwire is genuinely good for: noticing that someone added a second HTTP
client, or a write path, months from now.

**Every scanner here has a canary**: without one, an import scanner that returns
the empty set leaves this whole file green. Each rule is a helper that returns
what it found, the test asserts the helper finds nothing in ``src/``, and a
sibling test plants a source that the helper must find something in. A scanner
whose passing result is an absence proves nothing until it has been seen to
fire.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path("src/qbo_multicompany_mcp")

#: Non-GET methods live here and nowhere else.
OAUTH_MODULE = "oauth.py"

#: The loopback OAuth callback listener. It opens a *listening* socket and makes
#: no outbound connection of any kind, which is why it is one small, separately
#: auditable file rather than part of the CLI.
LISTENER_MODULE = "callback_server.py"

#: Network-capable modules that would bypass the policy in netpolicy.py.
#: `urllib.parse` is deliberately absent: it is pure string parsing and carries
#: no network capability. `urllib.request` is the one that matters.
FORBIDDEN_MODULES = {
    "urllib.request",
    "urllib.error",
    "requests",
    "aiohttp",
    "http.client",
    "ftplib",
    "telnetlib",
    "socketserver",
    "asyncio.streams",
}

#: Modules the listener alone may use.
LISTENER_ONLY_MODULES = {"socket", "http.server"}

#: The attachment download client may import ``socket`` for NAME
#: RESOLUTION only -- the private-destination pre-check on a pinned host --
#: and, like the listener, must never dial out itself; the test below holds
#: it to that.
RESOLVER_MODULE = "attachments.py"

HTTP_METHOD_LITERALS = {
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "HEAD",
    "OPTIONS",
    "TRACE",
    "CONNECT",
}

#: Names that build or dispatch a request, whose first positional argument is
#: the HTTP method.
SEND_SITES = {"build_request", "request", "stream"}

#: The one module that constructs an HTTP client or transport.
#: Its builders read nothing from the environment -- no proxy, no CA file, no
#: TLS key log -- so a client built anywhere else is one that might.
CLIENT_BUILDER_MODULE = "netpolicy.py"

#: What constructs an HTTP client or transport in httpx.
CLIENT_CLASSES = {"Client", "AsyncClient", "HTTPTransport", "AsyncHTTPTransport"}


def source_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def imported_modules(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _matching(modules: set[str], prefixes: set[str]) -> set[str]:
    return {m for m in modules if any(m == f or m.startswith(f + ".") for f in prefixes)}


def forbidden_imports(tree: ast.AST) -> set[str]:
    """Imports of modules that carry network capability outside the policy."""
    return _matching(imported_modules(tree), FORBIDDEN_MODULES)


def listener_only_imports(tree: ast.AST) -> set[str]:
    """Imports of the modules only the listener may use."""
    return _matching(imported_modules(tree), LISTENER_ONLY_MODULES)


def outbound_calls(tree: ast.AST) -> list[str]:
    dialling = {"connect", "connect_ex", "create_connection", "urlopen"}
    return [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in dialling
    ]


def send_sites(tree: ast.AST) -> list[tuple[str, str | None]]:
    """``(call name, method literal)`` for every request-building call; the
    method is ``None`` when the first argument is not a string literal."""
    found: list[tuple[str, str | None]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (
            node.func.attr
            if isinstance(node.func, ast.Attribute)
            else node.func.id
            if isinstance(node.func, ast.Name)
            else None
        )
        if name not in SEND_SITES or not node.args:
            continue
        first = node.args[0]
        literal = first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None
        found.append((name, literal))
    return found


def request_constructions(tree: ast.AST) -> list[str]:
    """The method literal of every ``httpx.Request(...)`` construction."""
    return [
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Request"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ]


def client_constructions(tree: ast.AST) -> list[str]:
    """Every ``httpx.<Client class>(...)`` call, by class name."""
    return [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in CLIENT_CLASSES
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "httpx"
    ]


def non_get_method_literals(tree: ast.AST) -> set[str]:
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in HTTP_METHOD_LITERALS
        and node.value != "GET"
    }


def files_containing(word: str, paths: list[Path]) -> list[str]:
    return [path.name for path in paths if word in path.read_text(encoding="utf-8")]


def test_source_tree_is_not_empty() -> None:
    assert len(source_files()) >= 10


@pytest.mark.parametrize("path", source_files(), ids=lambda p: p.name)
def test_no_alternative_http_client_is_imported(path: Path) -> None:
    offending = forbidden_imports(ast.parse(path.read_text(encoding="utf-8")))
    assert not offending, f"{path.name} imports {offending}"


@pytest.mark.parametrize("path", source_files(), ids=lambda p: p.name)
def test_raw_sockets_and_http_server_live_only_in_the_listener(path: Path) -> None:
    used = listener_only_imports(ast.parse(path.read_text(encoding="utf-8")))
    if path.name == LISTENER_MODULE:
        return
    if path.name == RESOLVER_MODULE:
        assert used <= {"socket"}, f"{path.name} may import socket for resolution only, not {used}"
        return
    assert not used, f"{path.name} imports {used}, which only {LISTENER_MODULE} may"


def test_the_listener_makes_no_outbound_connection() -> None:
    """It is exempted for a listening socket, so prove it never dials out."""
    tree = ast.parse((SRC / LISTENER_MODULE).read_text(encoding="utf-8"))
    assert outbound_calls(tree) == []


def test_the_resolver_module_makes_no_outbound_connection_and_only_resolves() -> None:
    """Exempted for name resolution, so prove it never dials out and that
    the only socket call it makes is getaddrinfo."""
    tree = ast.parse((SRC / RESOLVER_MODULE).read_text(encoding="utf-8"))
    assert outbound_calls(tree) == []
    socket_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name) and node.func.value.id == "socket"
    }
    assert socket_calls == {"getaddrinfo"}, socket_calls


@pytest.mark.parametrize("path", source_files(), ids=lambda p: p.name)
def test_only_get_is_written_outside_the_oauth_module(path: Path) -> None:
    """Every request-building call site outside oauth.py names ``"GET"``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for name, method in send_sites(tree):
        if method is None:
            pytest.fail(f"{path.name}: {name}() called with a non-literal method")
        if method not in HTTP_METHOD_LITERALS or path.name == OAUTH_MODULE:
            continue
        assert method == "GET", (
            f"{path.name} builds a {method} request; non-GET belongs in {OAUTH_MODULE}"
        )


def test_httpx_request_constructions_outside_oauth_are_get() -> None:
    for path in source_files():
        if path.name == OAUTH_MODULE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert all(m == "GET" for m in request_constructions(tree)), path.name


def test_only_the_network_policy_module_constructs_an_http_client() -> None:
    """Every client and transport comes from netpolicy's builders, which read
    nothing from the environment. A client built anywhere else would take
    httpx's default, which reads a proxy and a CA file from it. The builders'
    behaviour is proven in tests/test_client_environment.py; this keeps new
    call sites on them."""
    elsewhere = {
        path.name: built
        for path in source_files()
        if path.name != CLIENT_BUILDER_MODULE
        and (built := client_constructions(ast.parse(path.read_text(encoding="utf-8"))))
    }
    assert elsewhere == {}, f"built outside {CLIENT_BUILDER_MODULE}: {elsewhere}"


def test_only_the_oauth_module_names_a_non_get_method() -> None:
    """A whole-tree sweep, so a method literal in a helper is caught too."""
    for path in source_files():
        if path.name in (OAUTH_MODULE, "netpolicy.py"):
            # netpolicy.py must name POST: it is the module that permits it for
            # exactly three endpoints and denies it everywhere else.
            continue
        literals = non_get_method_literals(ast.parse(path.read_text(encoding="utf-8")))
        assert not literals, f"{path.name} names {literals}"


# --------------------------------------------------------------------------
# Canaries: every scanner above, seen to fire on a planted source
# --------------------------------------------------------------------------


def test_the_import_scanner_sees_plain_and_from_imports() -> None:
    planted = ast.parse("import requests\nfrom http.client import HTTPConnection\nimport os.path\n")
    assert imported_modules(planted) == {
        "requests", "http.client", "http.client.HTTPConnection", "os.path",
    }


def test_the_import_scanner_ignores_relative_imports() -> None:
    """The control for the canary above: a relative import is this package's
    own module, never a network client."""
    assert imported_modules(ast.parse("from . import oauth\nfrom .store import TokenStore\n")) == set()


def test_the_forbidden_import_scanner_fires_on_a_planted_source() -> None:
    planted = ast.parse("import requests\nfrom urllib.request import urlopen\nimport urllib.parse\n")
    assert forbidden_imports(planted) == {"requests", "urllib.request", "urllib.request.urlopen"}


def test_the_listener_only_import_scanner_fires_on_a_planted_source() -> None:
    planted = ast.parse("import socket\nfrom http.server import HTTPServer\nimport json\n")
    assert listener_only_imports(planted) == {"socket", "http.server", "http.server.HTTPServer"}


def test_the_outbound_call_scanner_fires_on_a_planted_source() -> None:
    planted = ast.parse(
        "def dial(sock, address):\n"
        "    sock.connect(address)\n"
        "    return socket.create_connection(address)\n"
    )
    assert outbound_calls(planted) == ["connect", "create_connection"]


def test_the_send_site_scanner_fires_on_a_planted_source() -> None:
    planted = ast.parse(
        "a = http.build_request(\"POST\", url)\n"
        "b = client.request(method, url)\n"
        "c = client.stream(\"GET\", url)\n"
    )
    assert send_sites(planted) == [("build_request", "POST"), ("request", None), ("stream", "GET")]


def test_the_request_construction_scanner_fires_on_a_planted_source() -> None:
    planted = ast.parse("r = httpx.Request(\"DELETE\", url)\n")
    assert request_constructions(planted) == ["DELETE"]


def test_the_client_construction_scanner_fires_on_a_planted_source() -> None:
    planted = ast.parse(
        "a = httpx.Client(timeout=1)\n"
        "b = httpx.HTTPTransport(retries=0)\n"
        "c = httpx.AsyncClient()\n"
        "d = other.Client()\n"
        "e = httpx.Request(\"GET\", url)\n"
    )
    assert sorted(client_constructions(planted)) == ["AsyncClient", "Client", "HTTPTransport"]


def test_the_method_literal_scanner_fires_on_a_planted_source() -> None:
    planted = ast.parse("METHOD = \"PUT\"\nother = \"GET\"\nword = \"PATCHWORK\"\n")
    assert non_get_method_literals(planted) == {"PUT"}


def test_the_write_shaped_word_scanner_fires_on_a_planted_source(tmp_path: Path) -> None:
    planted = tmp_path / "planted.py"
    planted.write_text("path = '/v3/company/1/batch'\n", encoding="utf-8")
    clean = tmp_path / "clean.py"
    clean.write_text("path = '/v3/company/1/query'\n", encoding="utf-8")
    assert files_containing("batch", [planted, clean]) == ["planted.py"]


# --------------------------------------------------------------------------
# Write-shaped vocabulary
# --------------------------------------------------------------------------


@pytest.mark.parametrize("forbidden", ["batch", "operation"])
def test_write_shaped_words_appear_nowhere_in_src(forbidden: str) -> None:
    """Both words name QuickBooks write affordances.

    Their absence from `src/` means no code path can be constructing one, even
    by string-building a path or a parameter name. The canaries that *do* use
    them live in the tests, where they belong.
    """
    offenders = files_containing(forbidden, source_files())
    assert not offenders, f"{forbidden!r} appears in {offenders}"


def test_the_runtime_policy_is_what_actually_enforces_this() -> None:
    """Named so that anyone reading this file knows where to look instead."""
    from qbo_multicompany_mcp import netpolicy

    assert callable(netpolicy.enforce)
    flattened = " ".join((netpolicy.__doc__ or "").split())
    assert "regression protection, not proof" in flattened
    assert "inspects a fully built" in flattened


# --------------------------------------------------------------------------
# Nothing that sends a credential may read it through `get_row`
# --------------------------------------------------------------------------
#
# `get_row` is the inspection read: status, keepalive reporting, diagnostics.
# `usable_row` is the read for a credential about to be sent, and it checks the
# configuration binding first. Every sending call site uses `usable_row` today,
# and the mutation gate catches the check being removed from it. What that
# does not catch is a NEW call site reaching for `get_row` and sending what it
# got, which skips the binding check. The two modules that send are `client`
# and `session`; neither may call `get_row` at all.

SENDING_MODULES = ("client.py", "session.py")


def calls_to(tree: ast.AST, method: str) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == method
    ]


@pytest.mark.parametrize("name", SENDING_MODULES)
def test_modules_that_send_never_read_a_credential_with_get_row(name: str) -> None:
    tree = ast.parse((SRC / name).read_text(encoding="utf-8"))
    assert calls_to(tree, "get_row") == [], (
        f"{name} calls get_row; a credential that is sent must come from usable_row"
    )


def test_the_get_row_detector_fires_on_a_planted_source() -> None:
    """The canary. A detector whose passing result is an absence proves
    nothing until it has been seen to fire."""
    planted = ast.parse(
        "def _usable_row(self, entity):\n"
        "    row = self.store.get_row(entity.realm_id)\n"
        "    return row.access_token\n"
    )
    assert calls_to(planted, "get_row") == [2]


def test_the_sending_modules_do_read_credentials_somehow() -> None:
    """The other control: if both modules stopped reading credentials at all,
    the absence test above would pass for the wrong reason."""
    for name in SENDING_MODULES:
        tree = ast.parse((SRC / name).read_text(encoding="utf-8"))
        assert calls_to(tree, "usable_row"), f"{name} no longer calls usable_row"


# --------------------------------------------------------------------------
# Nothing that submits a credential may read it through an inspection path
# --------------------------------------------------------------------------
#
# A revoke that reads `store.read().rows` and submits the token it finds sends
# it under whatever app the CLI is configured with. The detector above watches
# `get_row` in two modules. This one is not scoped to a module: in every source
# file, a function that hands a credential to the token or revocation endpoint
# (`refresh_tokens`, `revoke_token`, `exchange_code`) may not read the store
# through `get_row`, `read` or `rows` in that same function. The store's own
# methods read the document under the lock through `_load_locked`, which is not
# on this list, and `usable_row` is the read for anything about to be sent.

CREDENTIAL_SUBMITTERS = {"refresh_tokens", "revoke_token", "exchange_code"}
INSPECTION_READS = {"get_row", "read", "rows"}


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None


def submitting_functions_that_inspect(tree: ast.AST) -> list[tuple[str, int, str]]:
    """``(function, line, read)`` for every inspection read inside a function
    that also submits a credential."""
    found: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = [c for c in ast.walk(node) if isinstance(c, ast.Call)]
        if not any(_call_name(c) in CREDENTIAL_SUBMITTERS for c in calls):
            continue
        for call in calls:
            name = _call_name(call)
            if name in INSPECTION_READS:
                found.append((node.name, call.lineno, name))
    return found


@pytest.mark.parametrize("path", source_files(), ids=lambda p: p.name)
def test_no_function_that_submits_a_credential_reads_it_through_inspection(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert submitting_functions_that_inspect(tree) == [], (
        f"{path.name}: a credential that is submitted must come from usable_row"
    )


def test_the_submitter_detector_fires_on_the_revoke_shape() -> None:
    """The canary: read the document, take a row from it, submit its token."""
    planted = ast.parse(
        "def cmd_revoke(settings, registry, args):\n"
        "    document = store.read()\n"
        "    row = document.rows.get(realm)\n"
        "    oauth.revoke_token(token=row.refresh_token, client_id=c)\n"
    )
    assert submitting_functions_that_inspect(planted) == [("cmd_revoke", 2, "read")]


def test_the_submitter_detector_fires_on_get_row_and_rows_too() -> None:
    planted = ast.parse(
        "def a(store):\n"
        "    row = store.get_row(realm)\n"
        "    return oauth.refresh_tokens(refresh_token=row.refresh_token)\n"
        "def b(store):\n"
        "    rows = store.rows()\n"
        "    return exchange_code(code=rows[realm].code)\n"
    )
    assert submitting_functions_that_inspect(planted) == [
        ("a", 2, "get_row"), ("b", 5, "rows"),
    ]


def test_the_submitter_detector_ignores_a_function_that_does_not_submit() -> None:
    """The other control: inspection alone is what status and keepalive do,
    and must stay allowed."""
    planted = ast.parse(
        "def cmd_status(store):\n"
        "    for realm, row in store.read().rows.items():\n"
        "        print(row.auth_state())\n"
    )
    assert submitting_functions_that_inspect(planted) == []


# --------------------------------------------------------------------------
# Exchange and revoke stay in the auth CLI
# --------------------------------------------------------------------------
#
# The network policy permits these two only from the auth CLI, but `oauth`
# marks them as the CLI's itself, so the policy can't tell who called. The rule
# rests on the call sites, and this keeps them where they are.

CLI_ONLY_SUBMITTERS = {"revoke_token", "exchange_code"}


def cli_only_callers(tree: ast.AST) -> list[int]:
    """The line of every call to an exchange or a revoke."""
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _call_name(node) in CLI_ONLY_SUBMITTERS
    ]


def test_only_the_auth_cli_exchanges_a_code_or_revokes_a_token() -> None:
    callers = {path.name: cli_only_callers(ast.parse(path.read_text(encoding="utf-8"))) for path in source_files()}
    assert callers["auth_cli.py"], "the detector found none of the auth CLI's own calls"
    assert {name for name, lines in callers.items() if lines} == {"auth_cli.py"}, callers


def test_the_caller_detector_fires_on_a_revoke_outside_the_cli() -> None:
    planted = ast.parse(
        "def a_tool(row):\n"
        "    oauth.revoke_token(token=row.refresh_token)\n"
    )
    assert cli_only_callers(planted) == [2]


CLI_ONLY_REQUEST_CLASSES = {"TOKEN_EXCHANGE", "TOKEN_REVOKE"}


def test_the_token_post_and_the_cli_only_request_classes_stay_in_oauth() -> None:
    """The same request built another way, through ``oauth._post`` or by
    naming its request class, would pass the policy just as well."""
    posts: dict[str, list[int]] = {}
    classes: dict[str, list[int]] = {}
    for path in source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        posts[path.name] = [
            node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _call_name(node) == "_post"
        ]
        classes[path.name] = [
            node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr in CLI_ONLY_REQUEST_CLASSES
        ]
    assert posts["oauth.py"] and classes["oauth.py"], "the detectors found none of oauth's own uses"
    assert {name for name, lines in posts.items() if lines} == {"oauth.py"}, posts
    assert {name for name, lines in classes.items() if lines} <= {"oauth.py", "netpolicy.py"}, classes


@pytest.mark.parametrize("name", SENDING_MODULES)
@pytest.mark.parametrize("method", sorted(INSPECTION_READS))
def test_modules_that_send_never_read_the_store_through_inspection(name: str, method: str) -> None:
    """The same two-module rule, for read and rows as well as get_row."""
    tree = ast.parse((SRC / name).read_text(encoding="utf-8"))
    assert calls_to(tree, method) == [], (
        f"{name} calls {method}; a credential that is sent must come from usable_row"
    )


def test_the_get_row_detector_fires_on_read_and_rows_as_well() -> None:
    planted = ast.parse("x = store.read()\ny = store.rows()\n")
    assert calls_to(planted, "read") == [1]
    assert calls_to(planted, "rows") == [2]
