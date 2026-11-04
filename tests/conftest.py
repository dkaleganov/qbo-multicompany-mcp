"""Shared fixtures.

Every realm id here matches ``^0{12,15}[0-9]$`` and every company name is
obviously fake. A test fixture that looked like real configuration would
eventually be copied into real configuration, and a realistic realm id in a
public repository is a standing invitation to guess the rest.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest

from qbo_multicompany_mcp import oauth
from qbo_multicompany_mcp.config import Settings
from qbo_multicompany_mcp.constants import DEFAULT_REDIRECT_URI
from qbo_multicompany_mcp.crypto import KeySource, encode_key, generate_key
from qbo_multicompany_mcp.registry import Registry, load_registry
from qbo_multicompany_mcp.store import AppCredentials, RefreshActor, TokenStore

REALM_MAIN = "0000000000000001"
REALM_OPS = "0000000000000002"
REALM_ABSENT = "0000000000000009"

ENTITIES_YAML = f"""\
entities:
  acme_main:
    display_name: Acme Main
    realm_id: "{REALM_MAIN}"
    environment: sandbox
    writes: false
  acme_ops:
    display_name: Acme Ops
    realm_id: "{REALM_OPS}"
    environment: sandbox
"""


def pytest_configure(config: pytest.Config) -> None:
    """Drop what would point a test's git commands at another repository.

    A git hook or a shell can export ``GIT_DIR``, ``GIT_INDEX_FILE`` and the
    like. The tests build repositories of their own and read this one, and a
    ``git add`` run under a hook's ``GIT_INDEX_FILE`` would stage into the
    hook's index. Git lists these names itself. The ones that carry
    configuration stay: a CI checkout another user owns may need the
    ``safe.directory`` they set.
    """
    try:
        listed = subprocess.run(["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return
    for name in set(listed.stdout.split()) - {"GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"}:
        os.environ.pop(name, None)


def free_port() -> int:
    """Reserve an ephemeral port and release it.

    The tests must never bind the real callback port. Binding 8765 would stop
    this suite running while any consent is in flight on the machine, and an
    unrelated developer's live `connect` would make these tests fail with
    "Address already in use".
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(autouse=True)
def restore_environment() -> Iterator[None]:
    """Put ``os.environ`` back the way the test found it.

    ``load_dotenv`` writes straight into ``os.environ`` and never takes it
    back, and ``monkeypatch`` only undoes what monkeypatch itself did. So a
    test that points ``--env-file`` at a fixture file leaves
    ``QBO_CLIENT_ID=explicit-id`` set for every test that runs after it in the
    same process -- silently, unless something downstream compares a stored
    credential against the configured app. A suite can pass for the wrong
    reason that way; this makes each test start from the environment it was
    written against.
    """
    saved = os.environ.copy()
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


@pytest.fixture(autouse=True)
def restore_log_record_factory() -> Iterator[None]:
    """Put logging's record factory and ``Logger.makeRecord`` back.

    The redacting hooks install both for the whole process, and an entry point
    called in-process would otherwise leave them masking every record the tests
    after it make.
    """
    saved, make_record = logging.getLogRecordFactory(), logging.Logger.makeRecord
    try:
        yield
    finally:
        logging.setLogRecordFactory(saved)
        logging.Logger.makeRecord = make_record  # type: ignore[method-assign]


@pytest.fixture
def entities_file(tmp_path: Path) -> Path:
    path = tmp_path / "entities.yaml"
    path.write_text(ENTITIES_YAML, encoding="utf-8")
    return path


@pytest.fixture
def registry(entities_file: Path) -> Registry:
    return load_registry(entities_file, expected_environment="sandbox")


@pytest.fixture
def store_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def token_key(monkeypatch: pytest.MonkeyPatch) -> str:
    key = encode_key(generate_key())
    monkeypatch.setenv("QBO_TOKEN_KEY", key)
    return key


@pytest.fixture
def credentials() -> AppCredentials:
    # Not a secret: a fixed, obviously-fake pair used only against the mock.
    return AppCredentials(client_id="test-client-id", client_secret="test-client-secret")


@pytest.fixture
def settings(entities_file: Path, store_dir: Path, token_key: str) -> Settings:
    return Settings(
        entities_file=entities_file,
        environment="sandbox",
        api_base="https://sandbox-quickbooks.api.intuit.com",
        token_store_dir=store_dir,
        key_source=KeySource.ENV,
        allow_plaintext_store=False,
        allow_documents=False,
        allow_custom_api_base=False,
        redirect_uri=DEFAULT_REDIRECT_URI,
        verbose=False,
        callback_port=free_port(),
    )


@pytest.fixture
def store(settings: Settings, credentials: AppCredentials) -> TokenStore:
    return settings.open_store(credentials=credentials)


@pytest.fixture
def actor() -> RefreshActor:
    return RefreshActor(role="test", pid=os.getpid(), hostname="test-host")


def make_token_response(
    *,
    access_token: str = "access-1",
    refresh_token: str = "refresh-1",
    access_seconds: int = 3600,
    refresh_days: int = 100,
    realm_id: str | None = None,
) -> oauth.TokenResponse:
    """A fixture pair. Marked synthetic, because it is.

    That is what keeps it out of the live-value registry, and so what lets it
    reach the loopback mock: a test-only base refuses anything the registry
    holds. A test that needs a pair treated as a real one builds
    ``oauth.TokenResponse`` itself and leaves the flag off.
    """
    now = datetime.now(UTC)
    return oauth.TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        access_token_expires_at=now + timedelta(seconds=access_seconds),
        refresh_token_expires_at=now + timedelta(days=refresh_days),
        realm_id=realm_id,
        synthetic=True,
    )


@pytest.fixture
def seeded_store(store: TokenStore, actor: RefreshActor) -> TokenStore:
    """A store holding one authorized realm."""
    store.commit_consent(
        entity="acme_main",
        realm_id=REALM_MAIN,
        response=make_token_response(),
        actor=actor,
    )
    return store


# --------------------------------------------------------------------------
# A loopback mock of the Accounting API
# --------------------------------------------------------------------------


@dataclass
class MockIntuit:
    """A real HTTP server on loopback, so the client, the network policy and
    httpx are all genuinely exercised rather than stubbed out."""

    company_name: str = "Acme Main"
    accounts: list[dict[str, Any]] = field(default_factory=list)
    preferences: dict[str, Any] = field(default_factory=dict)
    unauthorized_tokens: set[str] = field(default_factory=set)
    requests: list[tuple[str, str, dict[str, str], str]] = field(default_factory=list)
    page_size: int = 1000
    #: Rows per entity name, as the query endpoint would return them.
    tables: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: Optional per-entity predicates, so a test can make the mock respect a
    #: WHERE clause where that is the thing under test.
    filters: dict[str, list[Any]] = field(default_factory=dict)
    #: Per-entity ``(status, body)`` the query endpoint answers with instead of
    #: rows -- the shape of Intuit's feature fault on a plan without the
    #: feature, which is how accounts payable is established.
    faults: dict[str, tuple[int, dict[str, Any]]] = field(default_factory=dict)
    reports: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Per-report ``(status, body)`` the report endpoint answers with instead
    #: of a report -- the shape of Intuit's permission fault 5020 (observed on
    #: TaxSummary, 2026-09-24) or its feature fault 5030.
    report_faults: dict[str, tuple[int, dict[str, Any]]] = field(default_factory=dict)
    #: Headers added to every JSON response, such as the ``intuit_tid`` a
    #: REST response carries (Intuit's Basic schema and data formats page).
    response_headers: dict[str, str] = field(default_factory=dict)
    cdc: dict[str, Any] = field(default_factory=dict)
    #: Files served at ``/files/<name>`` as ``(content type, bytes)``, for
    #: the opt-in attachment download: the mock stands in for the storage
    #: host a TempDownloadUri names, on the same loopback origin.
    files: dict[str, tuple[str, bytes]] = field(default_factory=dict)
    report_queries: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    cdc_queries: list[dict[str, str]] = field(default_factory=list)
    omit_empty_key: bool = False
    #: Seconds every request waits before it is answered. For the whole-call
    #: deadline and cancellation tests, which need a request that takes time.
    delay_seconds: float = 0.0
    _server: HTTPServer | None = None
    _thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> None:
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parts = urlsplit(self.path)
                query = dict(parse_qsl(parts.query, keep_blank_values=True))
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                mock.requests.append((self.command, parts.path, query, token))
                if mock.delay_seconds:
                    import time

                    time.sleep(mock.delay_seconds)

                if token in mock.unauthorized_tokens:
                    return self._json(401, {"fault": "AuthenticationFailed"})

                if parts.path in mock.files:
                    content_type, content = mock.files[parts.path]
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                    return None

                segments = [s for s in parts.path.split("/") if s]
                if len(segments) < 4 or segments[0] != "v3" or segments[1] != "company":
                    return self._json(404, {})
                resource = "/".join(segments[3:])

                if resource.startswith("companyinfo/"):
                    return self._json(
                        200, {"CompanyInfo": mock.company_info_payload(segments[2])}
                    )
                if resource == "preferences":
                    return self._json(200, {"Preferences": mock.preferences})
                if resource == "query":
                    statement = query.get("query", "")
                    fault = mock.faults.get(mock.entity_of(statement))
                    if fault is not None:
                        return self._json(*fault)
                    return self._json(200, mock.query_payload(statement))
                if resource.startswith("reports/"):
                    name = resource.split("/", 1)[1]
                    mock.report_queries.append((name, dict(query)))
                    fault = mock.report_faults.get(name)
                    if fault is not None:
                        return self._json(*fault)
                    return self._json(200, mock.report_payload(name, query))
                if resource == "cdc":
                    return self._json(200, mock.cdc_payload(query))
                return self._json(404, {})

            def _json(self, status: int, body: dict[str, Any]) -> None:
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for name, value in mock.response_headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, fmt: str, *args: object) -> None:
                return

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def company_info_payload(self, realm: str) -> dict[str, Any]:
        return {
            "Id": realm,
            "CompanyName": self.company_name,
            "LegalName": f"{self.company_name} LLC",
            "Country": "US",
            "EmployerId": "00-0000123",
            "CompanyAddr": {"Line1": "1 Example Way", "PostalCode": "00000"},
            "Email": {"Address": "nobody@example.invalid"},
            "PrimaryPhone": {"FreeFormNumber": "555-0100"},
            "WebAddr": {"URI": "https://example.invalid"},
            "NameValue": [{"Name": "Marker", "Value": "should-not-appear"}],
            "MetaData": {"CreateTime": "2020-01-01T00:00:00Z", "Undeclared": "x"},
        }

    @staticmethod
    def entity_of(statement: str) -> str:
        tokens = statement.split()
        for index, word in enumerate(tokens):
            if word.upper() == "FROM" and index + 1 < len(tokens):
                return tokens[index + 1]
        return ""

    def query_payload(self, statement: str) -> dict[str, Any]:
        """Serve any entity, and answer COUNT(*) from the same table.

        The count must come from the same rows the pages come from, or the
        walker's anchor would be testing the mock rather than the walk.
        """
        tokens = statement.split()
        entity = self.entity_of(statement)

        table = self.tables.get(entity)
        if table is None and entity == "Account":
            table = self.accounts
        table = list(table or [])
        for predicate in self.filters.get(entity, []):
            table = [row for row in table if predicate(row, statement)]

        if "COUNT(*)" in statement.upper().replace(" ", ""):
            return {"QueryResponse": {"totalCount": len(table)}}

        start, maximum = 1, self.page_size
        for index, word in enumerate(tokens):
            if word.upper() == "STARTPOSITION" and index + 1 < len(tokens):
                start = int(tokens[index + 1])
            if word.upper() == "MAXRESULTS" and index + 1 < len(tokens):
                maximum = int(tokens[index + 1])
        page = table[start - 1 : start - 1 + maximum]
        body: dict[str, Any] = {"startPosition": start}
        if page or not self.omit_empty_key:
            body[entity] = page
        return {"QueryResponse": body}

    def report_payload(self, name: str, query: dict[str, str]) -> dict[str, Any]:
        return self.reports.get(name, {"Header": {}, "Columns": {"Column": []}, "Rows": {}})

    def cdc_payload(self, query: dict[str, str]) -> dict[str, Any]:
        self.cdc_queries.append(dict(query))
        return self.cdc


@pytest.fixture
def mock_intuit() -> Iterator[MockIntuit]:
    mock = MockIntuit(
        preferences={
            "AccountingInfoPrefs": {
                "FirstMonthOfFiscalYear": "January",
                "BookCloseDate": "2025-12-31",
                "ClassTrackingPerTxn": False,
                "ClassTrackingPerTxnLine": False,
                "TrackDepartments": False,
                "UseAccountNumbers": True,
            },
            "CurrencyPrefs": {
                "MultiCurrencyEnabled": False,
                "HomeCurrency": {"value": "USD"},
            },
            "ReportPrefs": {"ReportBasis": "Accrual"},
            "ProductAndServicesPrefs": {"QuantityOnHand": False},
            "EmailMessagesPrefs": {"InvoiceMessage": {"Message": "should-not-appear"}},
        },
        accounts=[
            {
                "Id": "1",
                "Name": "Checking",
                "AccountType": "Bank",
                "AcctNum": "000000009876",
                "CurrentBalance": 100.0,
                "CurrentBalanceWithSubAccounts": 150.0,
            },
            {
                "Id": "2",
                "Name": "Rent",
                "AccountType": "Expense",
                "AcctNum": "6000",
                "CurrentBalance": 25.0,
                "SubAccount": True,
                "ParentRef": {"value": "1", "name": "should-not-appear"},
            },
        ],
    )
    mock.start()
    try:
        yield mock
    finally:
        mock.stop()


@pytest.fixture
def mock_settings(
    settings: Settings,
    mock_intuit: MockIntuit,
    credentials: AppCredentials,
    monkeypatch: pytest.MonkeyPatch,
) -> Settings:
    """Settings pointed at the loopback mock, with the fixture app configured.

    A loopback origin is permitted by the network policy without
    ``--allow-custom-api-base`` precisely so this is possible -- and, being
    test-only, it is sent synthetic credentials or nothing. The fixture tokens
    (``access-1`` and the like) are synthetic by shape and marked synthetic by
    :func:`make_token_response`.

    The app is in the environment because a server with no app configured
    sends no stored credential at all, and the server reads its app from the
    environment. A test about running without one removes it.
    """
    from dataclasses import replace as dc_replace

    monkeypatch.setenv("QBO_CLIENT_ID", credentials.client_id)
    monkeypatch.setenv("QBO_CLIENT_SECRET", credentials.client_secret)
    return dc_replace(settings, api_base=mock_intuit.base_url)


@pytest.fixture
def patch_refresh(monkeypatch: pytest.MonkeyPatch) -> Callable[[Callable[..., Any]], list]:
    """Replace the OAuth refresh call and record what was submitted."""

    def install(behaviour: Callable[..., Any]) -> list:
        submitted: list[str] = []

        def fake(**kwargs: Any) -> oauth.TokenResponse:
            submitted.append(kwargs["refresh_token"])
            return behaviour(len(submitted), kwargs)

        monkeypatch.setattr(oauth, "refresh_tokens", fake)
        return submitted

    return install


# --------------------------------------------------------------------------
# A real stdio client against a real server subprocess
# --------------------------------------------------------------------------


def source_root() -> Path:
    """The ``src`` directory the package under test was imported from."""
    import qbo_multicompany_mcp

    return Path(qbo_multicompany_mcp.__file__).resolve().parents[1]


def with_source_root(env: dict[str, str]) -> dict[str, str]:
    """``env`` with ``PYTHONPATH`` naming the active source root first.

    Every subprocess a test starts must import the tree under test. Without
    this it falls back to the installed package, which under the mutation
    gate is the UNmutated tree -- so a test that asserts through a
    subprocess passes whatever the gate deleted. Setting PYTHONPATH to the
    repository root alone, for the test plugin, is not enough: in this src
    layout that path does not hold the package. So every environment a test
    builds goes through here, and
    ``test_every_subprocess_environment_imports_the_active_source_root``
    asserts the subprocess's actual module path.
    """
    root = str(source_root())
    others = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p and p != root]
    return {**env, "PYTHONPATH": os.pathsep.join([root, *others])}


def stdio_env(
    *,
    entities_file: Path,
    store_dir: Path | None = None,
    token_key: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    env = with_source_root(
        {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "QBO_ENTITIES_FILE": str(entities_file),
        }
    )
    if store_dir is not None:
        env["QBO_TOKEN_STORE"] = str(store_dir)
    if token_key is not None:
        env["QBO_TOKEN_KEY"] = token_key
        env["QBO_TOKEN_KEY_SOURCE"] = "env"
    if extra:
        env.update(extra)
    return env


def run_stdio(env: dict[str, str], args: list[str], body: Callable[[Any], Any]) -> Any:
    """Drive a real server subprocess over a real stdio MCP connection.

    The tests that matter here assert what a *client* sees. An assertion
    against a raised exception inside the process proves nothing about the text
    that reaches a model, because the SDK rewrites it on the way out.
    """
    import sys

    import anyio
    from mcp import Client, StdioServerParameters

    async def main() -> Any:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "qbo_multicompany_mcp.server", *args],
            env=env,
        )
        async with Client(params) as client:
            return await body(client)

    return anyio.run(main)


def result_text(result: Any) -> str:
    return "\n".join(
        block.text for block in (result.content or []) if getattr(block, "text", None)
    )
