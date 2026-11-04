"""`list_attachments` and the `get_attachment` gate.

`list_attachments` returns Attachable METADATA through the ordinary
accounting client: file name, MIME type, size, note, the AttachableRef to the
transaction, created and updated. Never a link: Intuit documents
TempDownloadUri and ThumbnailTempDownloadUri as temporary URLs, and
FileAccessUri is a full path; none leaves this process.

`get_attachment` is OPT-IN and GATED. It exists only when documents are
enabled, and it returns the file as an embedded blob beside a metadata block.
The download goes through a SEPARATE client that sends no bearer, cookie or
proxy-derived auth and ignores any proxy in the environment. That client
checks the host on the first request and again on each redirect, admits only
pinned storage hosts and supported MIME types, refuses private, loopback and
link-local destinations, and streams under a final MCP output cap that allows
for base64 expansion. The gates are the tests below: origin, redirect and auth
isolation, MIME, final-output size, and the two-block contract over a real
stdio connection.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Iterator
from dataclasses import replace as dc_replace
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from qbo_multicompany_mcp import deadline as call_deadline
from qbo_multicompany_mcp.constants import ACCOUNTING_TIMEOUT_SECONDS
from qbo_multicompany_mcp.errors import DeadlineExceeded, NetworkPolicyViolation
from qbo_multicompany_mcp.redaction import redact

from .conftest import MockIntuit, result_text, run_stdio, stdio_env
from .test_changes_and_tools import build, call

LEAK = "should-not-appear"

#: A globally routable address for the resolver seam to answer with. Python's
#: ``ipaddress`` counts the RFC 5737 documentation ranges as private, so a
#: documentation address cannot stand in for a public one here; this is a
#: well-known public resolver's address, and nothing is ever contacted -- the
#: transport under every test is a mock.
PUBLIC = "1.1.1.1"
FILES_HOST = "files.example"

PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16


def attachable_rows(mock_base: str | None = None) -> list[dict[str, Any]]:
    link = f"{mock_base}/files/receipt.pdf" if mock_base else f"https://{FILES_HOST}/a/b.pdf?Expires=1&Signature=x"
    return [
        {"Id": "5000", "FileName": "receipt-example.pdf", "ContentType": "application/pdf", "Size": len(PDF),
         "Note": "Example note", "Category": "Receipt", "Tag": "example",
         "AttachableRef": [{"EntityRef": {"type": "Purchase", "value": "611"}, "IncludeOnSend": False,
                            "Undeclared": LEAK}],
         "TempDownloadUri": link, "ThumbnailTempDownloadUri": link + "&thumb=1",
         "FileAccessUri": "/v3/company/0000000000000001/download/5000" + LEAK,
         "ThumbnailFileAccessUri": LEAK, "Lat": LEAK, "Long": LEAK, "PlaceName": LEAK,
         "SyncToken": LEAK, "domain": LEAK, "sparse": False,
         "MetaData": {"CreateTime": "2026-01-01T00:00:00-08:00", "LastUpdatedTime": "2026-02-01T00:00:00-08:00",
                      "LastModifiedByRef": {"value": LEAK}}},
        {"Id": "5001", "Note": "Example standalone note", "SyncToken": LEAK,
         "AttachableRef": [{"EntityRef": {"type": "Bill", "value": "70"}}],
         "MetaData": {"CreateTime": "2026-01-03T00:00:00-08:00", "LastUpdatedTime": "2026-01-03T00:00:00-08:00"}},
    ]


def _attachable_filter(row: dict[str, Any], statement: str) -> bool:
    """The mock honours the documented WHERE on AttachableRef.EntityRef."""
    import re

    by_id = re.search(r"Id = '([^']+)'", statement)
    if by_id:
        return row.get("Id") == by_id.group(1)
    if "AttachableRef.EntityRef.Type" not in statement:
        return True
    kind = re.search(r"AttachableRef\.EntityRef\.Type = '([^']+)'", statement).group(1)
    ident = re.search(r"AttachableRef\.EntityRef\.value = '([^']+)'", statement).group(1)
    return any(r.get("EntityRef", {}).get("type") == kind and r.get("EntityRef", {}).get("value") == ident
               for r in row.get("AttachableRef", []))


@pytest.fixture
def server(mock_settings, registry, credentials, actor, mock_intuit: MockIntuit) -> MCPServer:
    mock_intuit.tables["Attachable"] = attachable_rows()
    mock_intuit.filters["Attachable"] = [_attachable_filter]
    return build(mock_settings, registry, credentials, actor)


def body_of(result) -> dict[str, Any]:
    return result.structured_content


def statements(mock: MockIntuit) -> list[str]:
    return [q.get("query", "") for _m, path, q, _t in mock.requests if path.endswith("/query")]


# ==========================================================================
# list_attachments: metadata, no links
# ==========================================================================


def test_list_attachments_is_registered_and_get_attachment_is_not_by_default(server: MCPServer) -> None:
    names = {t.name for t in anyio.run(server.list_tools)}
    assert "list_attachments" in names
    assert "get_attachment" not in names, "opt-in: absent unless documents are enabled"


def test_attachments_are_walked_and_projected_to_metadata_without_links(server, mock_intuit) -> None:
    body = body_of(call(server, "list_attachments", {"entity": "acme_main"}))
    assert body["count"] == 2 and body["consistency"] == "non_snapshot"
    assert any("SELECT COUNT(*) FROM Attachable" in s for s in statements(mock_intuit))
    rows = {r["id"]: r for r in body["rows"]}
    receipt = rows["5000"]
    assert receipt["file_name"] == "receipt-example.pdf" and receipt["content_type"] == "application/pdf"
    assert receipt["size"] == len(PDF) and receipt["note"] == "Example note"
    assert receipt["category"] == "Receipt" and receipt["tag"] == "example"
    assert receipt["refs"] == [{"entity_type": "Purchase", "entity_id": "611", "include_on_send": False}]
    assert receipt["created_at"] == "2026-01-01T00:00:00-08:00"
    assert receipt["last_updated_at"] == "2026-02-01T00:00:00-08:00"
    dumped = json.dumps(body)
    assert LEAK not in dumped
    for never in ("TempDownloadUri", "ThumbnailTempDownloadUri", "FileAccessUri", "download_uri", "https://",
                  "Expires", "Signature", "/download/"):
        assert never not in dumped, never
    assert rows["5001"]["file_name"] is None and rows["5001"]["note"] == "Example standalone note"


def test_the_filter_pairs_transaction_type_with_transaction_id(server, mock_intuit) -> None:
    body = body_of(call(server, "list_attachments",
                        {"entity": "acme_main", "transaction_type": "Purchase", "transaction_id": "611"}))
    assert [r["id"] for r in body["rows"]] == ["5000"]
    assert body["transaction_type"] == "Purchase" and body["transaction_id"] == "611"
    assert any("AttachableRef.EntityRef.Type = 'Purchase' AND AttachableRef.EntityRef.value = '611'" in s
               for s in statements(mock_intuit))
    for half in ({"transaction_type": "Purchase"}, {"transaction_id": "611"}):
        with pytest.raises(ToolError, match="together"):
            call(server, "list_attachments", {"entity": "acme_main", **half})
    with pytest.raises(ToolError, match="transaction_type"):
        call(server, "list_attachments", {"entity": "acme_main", "transaction_type": "Employee", "transaction_id": "1"})


def test_the_list_says_whether_a_download_is_available(server) -> None:
    body = body_of(call(server, "list_attachments", {"entity": "acme_main"}))
    assert body["download_available"] is False
    assert "--allow-documents" in body["download_note"]


def test_attachable_is_served_by_run_query_through_the_same_projection(server) -> None:
    body = body_of(call(server, "run_query", {"entity": "acme_main", "statement": "SELECT * FROM Attachable"}))
    assert body["count"] == 2 and "TempDownloadUri" not in json.dumps(body) and LEAK not in json.dumps(body)


# ==========================================================================
# The download client: the gates as tests
# ==========================================================================


def _download_settings(settings, **overrides):
    base = dict(allow_documents=True, attachment_hosts=(FILES_HOST,))
    base.update(overrides)
    return dc_replace(settings, **base)


def _transport(files: dict[str, tuple[int, dict[str, str], bytes]]):
    reached: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reached.append(request)
        status, headers, content = files.get(str(request.url).split("?")[0], (404, {}, b""))
        return httpx.Response(status, headers=headers, content=content, request=request)

    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False), reached


def public(host: str) -> list[str]:
    return [PUBLIC]


@pytest.fixture
def budget() -> Iterator[Callable[[float], list[float]]]:
    """Install a call deadline on a clock the test moves by hand."""
    tokens = []

    def install(seconds: float) -> list[float]:
        now = [0.0]
        active = call_deadline.Deadline(budget=seconds, expires_at=seconds, clock=lambda: now[0])
        tokens.append(call_deadline._CURRENT.set(active))
        return now

    yield install
    for token in reversed(tokens):
        call_deadline._CURRENT.reset(token)


def _fetch(settings, http: httpx.Client, resolver: Callable[[str], list[str]]):
    from qbo_multicompany_mcp import attachments

    return attachments.download(f"https://{FILES_HOST}/a.pdf", settings=_download_settings(settings),
                                expected_content_type="application/pdf", expected_size=len(PDF),
                                http=http, resolver=resolver)


def _hops(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _moved(request: httpx.Request) -> httpx.Response:
    return httpx.Response(302, headers={"Location": f"https://{FILES_HOST}/b.pdf"}, request=request)


def _pdf(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, headers={"Content-Type": "application/pdf"}, content=PDF, request=request)


def test_a_spent_budget_ends_the_download_before_any_lookup(settings, budget) -> None:
    budget(0.0)
    asked: list[str] = []
    http, reached = _transport({f"https://{FILES_HOST}/a.pdf": (200, {"Content-Type": "application/pdf"}, PDF)})

    with pytest.raises(DeadlineExceeded):
        _fetch(settings, http, lambda host: asked.append(host) or [PUBLIC])
    assert asked == []
    assert reached == []


def test_a_lookup_that_spends_the_budget_ends_the_download_before_the_request(settings, budget) -> None:
    now = budget(60.0)

    def slow(_host: str) -> list[str]:
        now[0] = 58.0
        return [PUBLIC]

    http, reached = _transport({f"https://{FILES_HOST}/a.pdf": (200, {"Content-Type": "application/pdf"}, PDF)})

    with pytest.raises(DeadlineExceeded):
        _fetch(settings, http, slow)
    assert reached == []


def test_each_hop_waits_only_for_what_the_budget_has_left(settings, budget) -> None:
    """A slow host or a redirect spends the one budget. Five seconds are held
    back to build the result."""
    now = budget(60.0)
    waits: list[dict[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        waits.append(request.extensions["timeout"])
        now[0] += 20.0
        return _moved(request) if len(waits) == 1 else _pdf(request)

    got = _fetch(settings, _hops(handler), public)

    assert got.redirects == 1
    assert waits == [
        dict.fromkeys(("connect", "read", "write", "pool"), 55.0),
        dict.fromkeys(("connect", "read", "write", "pool"), 35.0),
    ]


def test_a_redirect_the_budget_cannot_reach_is_not_followed(settings, budget) -> None:
    now = budget(60.0)
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        now[0] = 58.0
        return _moved(request)

    with pytest.raises(DeadlineExceeded):
        _fetch(settings, _hops(handler), lambda host: asked.append(host) or [PUBLIC])
    assert len(asked) == 1


def test_a_hop_the_budget_timed_out_ends_at_the_deadline(settings, budget) -> None:
    now = budget(60.0)

    def handler(request: httpx.Request) -> httpx.Response:
        now[0] = 55.0
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(DeadlineExceeded):
        _fetch(settings, _hops(handler), public)


def test_a_hop_that_times_out_with_budget_left_is_refused_by_name(settings, budget) -> None:
    from qbo_multicompany_mcp import attachments

    budget(60.0)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(attachments.AttachmentRefused, match="timed out"):
        _fetch(settings, _hops(handler), public)


def test_a_download_whose_client_cant_be_built_is_refused_by_name(
    settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Building the TLS context opens files. A failure there is a plain refusal
    with nothing sent, not an unexpected error."""
    from qbo_multicompany_mcp import attachments

    def unbuildable(**kwargs: object) -> None:
        raise FileNotFoundError("the CA bundle")

    monkeypatch.setattr(attachments, "isolated_client", unbuildable)
    with pytest.raises(attachments.AttachmentRefused, match="could not be built"):
        attachments.download(
            f"https://{FILES_HOST}/a.pdf", settings=_download_settings(settings),
            expected_content_type="application/pdf", expected_size=len(PDF), resolver=public,
        )


def test_a_host_that_cannot_be_reached_is_refused_by_name(settings) -> None:
    from qbo_multicompany_mcp import attachments

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(attachments.AttachmentRefused, match="the download failed"):
        _fetch(settings, _hops(handler), public)


def test_a_connection_lost_after_the_host_answered_is_refused_by_name(settings) -> None:
    from qbo_multicompany_mcp import attachments

    class Dropped(httpx.SyncByteStream):
        def __iter__(self):
            yield b"%PDF"
            raise httpx.ReadError("gone")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "application/pdf"}, stream=Dropped(), request=request)

    with pytest.raises(attachments.AttachmentRefused, match="the download failed"):
        _fetch(settings, _hops(handler), public)


def test_a_call_that_runs_out_of_budget_says_so_about_the_download(settings, budget) -> None:
    """The shared text suggests a shorter date window, and a file has none."""
    budget(0.0)
    http, _ = _transport({})

    with pytest.raises(DeadlineExceeded, match="before the download finished"):
        _fetch(settings, http, public)


def test_a_malformed_download_link_is_refused_by_name(settings) -> None:
    from qbo_multicompany_mcp import attachments

    http, reached = _transport({})
    with pytest.raises(attachments.AttachmentRefused, match="malformed") as refused:
        attachments.download(f"https://{FILES_HOST}/a\x00.pdf?Signature=marked", settings=_download_settings(settings),
                             expected_content_type="application/pdf", expected_size=1, http=http, resolver=public)
    assert "marked" not in str(refused.value)
    assert reached == []


@pytest.mark.parametrize("host", ["a" * 64 + ".files.example", "a..files.example"])
def test_a_host_no_dns_name_can_have_does_not_resolve(host: str) -> None:
    """The resolver answers with nothing, so the refusal is the ordinary one.
    Python refuses these labels before anything reaches the network."""
    from qbo_multicompany_mcp import attachments

    assert attachments._default_resolver(host) == []


@pytest.mark.parametrize("host", ["files12345678.example", "files_12345678.example"])
def test_a_host_the_operator_has_to_pin_is_named_whole(settings, host: str) -> None:
    """The one message that says what to pin, and production's host is unknown."""
    from qbo_multicompany_mcp import attachments

    http, reached = _transport({})
    with pytest.raises(NetworkPolicyViolation) as refused:
        attachments.download(f"https://{host}/a.pdf", settings=_download_settings(settings),
                             expected_content_type="application/pdf", expected_size=1, http=http, resolver=public)
    assert f"'{host}'" in redact(str(refused.value))
    assert reached == []


def test_a_redirect_httpx_cannot_build_is_refused_by_name(settings) -> None:
    """A relative ``Location`` that takes the link past httpx's limit."""
    from qbo_multicompany_mcp import attachments

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "y" * 1000}, request=request)

    with pytest.raises(attachments.AttachmentRefused, match="the download failed"):
        attachments.download(f"https://{FILES_HOST}/{'x' * 65000}/a.pdf", settings=_download_settings(settings),
                             expected_content_type="application/pdf", expected_size=1, http=_hops(handler),
                             resolver=public)


def test_outside_a_tool_call_a_download_keeps_its_ordinary_timeout(settings) -> None:
    waits: list[dict[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        waits.append(request.extensions["timeout"])
        return _pdf(request)

    _fetch(settings, _hops(handler), public)

    assert waits == [dict.fromkeys(("connect", "read", "write", "pool"), ACCOUNTING_TIMEOUT_SECONDS)]


def test_download_fetches_an_allowlisted_https_origin_and_validates_the_bytes(settings) -> None:
    from qbo_multicompany_mcp import attachments

    http, reached = _transport({f"https://{FILES_HOST}/a/b.pdf": (200, {"Content-Type": "application/pdf"}, PDF)})
    got = attachments.download(f"https://{FILES_HOST}/a/b.pdf?Expires=1&Signature=x",
                               settings=_download_settings(settings), expected_content_type="application/pdf",
                               expected_size=len(PDF), http=http, resolver=public)
    assert got.content == PDF and got.content_type == "application/pdf"
    assert got.sha256 == hashlib.sha256(PDF).hexdigest()
    assert got.final_host == FILES_HOST and got.redirects == 0
    assert len(reached) == 1


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        (f"https://other.example/a.pdf", "not pinned"),
        (f"http://{FILES_HOST}/a.pdf", "https"),
        ("https://203.0.113.5/a.pdf", "pinned"),
        (f"https://{FILES_HOST}:8443/a.pdf", "port"),
        ("https://" + "user:pw@" + FILES_HOST + "/a.pdf", "userinfo"),
    ],
)
def test_origin_gate_refuses_a_host_that_is_not_pinned_and_every_other_origin_trick(settings, url, reason) -> None:
    from qbo_multicompany_mcp import attachments

    http, reached = _transport({})
    with pytest.raises((attachments.AttachmentRefused, NetworkPolicyViolation), match=reason):
        attachments.download(url, settings=_download_settings(settings), expected_content_type="application/pdf",
                             expected_size=1, http=http, resolver=public)
    assert reached == []


def test_origin_gate_refuses_everything_when_no_host_is_pinned(settings) -> None:
    from qbo_multicompany_mcp import attachments

    http, reached = _transport({})
    with pytest.raises(attachments.AttachmentRefused, match="QBO_ATTACHMENT_HOSTS"):
        attachments.download(f"https://{FILES_HOST}/a.pdf", settings=_download_settings(settings, attachment_hosts=()),
                             expected_content_type="application/pdf", expected_size=1, http=http, resolver=public)
    assert reached == []


def test_origin_gate_admits_a_wildcard_suffix_and_nothing_wider(settings) -> None:
    from qbo_multicompany_mcp import attachments

    pinned = _download_settings(settings, attachment_hosts=("*.storage.example",))
    files = {"https://bucket.storage.example/a.pdf": (200, {"Content-Type": "application/pdf"}, PDF)}
    http, _ = _transport(files)
    got = attachments.download("https://bucket.storage.example/a.pdf", settings=pinned,
                               expected_content_type="application/pdf", expected_size=len(PDF), http=http, resolver=public)
    assert got.final_host == "bucket.storage.example"
    http, reached = _transport(files)
    with pytest.raises(NetworkPolicyViolation, match="not pinned"):
        attachments.download("https://storage.example.evil.example/a.pdf", settings=pinned,
                             expected_content_type="application/pdf", expected_size=1, http=http, resolver=public)
    with pytest.raises(NetworkPolicyViolation, match="not pinned"):
        attachments.download("https://storage.example/a.pdf", settings=pinned,
                             expected_content_type="application/pdf", expected_size=1, http=http, resolver=public)
    assert reached == []


@pytest.mark.parametrize("address", ["224.0.0.1", "239.1.2.3", "ff02::1"])
def test_a_multicast_address_is_refused_although_is_global_accepts_it(address: str) -> None:
    from qbo_multicompany_mcp import attachments

    assert attachments.address_kind(address) == "multicast"


@pytest.mark.parametrize("address", ["10.0.0.7", "127.0.0.1", "169.254.1.1", "192.168.1.1", "172.16.0.1", "::1", "fe80::1", "0.0.0.0"])
def test_a_pinned_host_that_resolves_to_a_private_loopback_or_link_local_address_is_refused(settings, address) -> None:
    """The pre-check on what the name resolves to: a pinned host rebinding
    to an internal address gets no request. A pre-check, not a proof: the
    connection resolves the name again, and the module says so."""
    from qbo_multicompany_mcp import attachments

    http, reached = _transport({})
    with pytest.raises(attachments.AttachmentRefused, match="private|loopback|link-local|unspecified"):
        attachments.download(f"https://{FILES_HOST}/a.pdf", settings=_download_settings(settings),
                             expected_content_type="application/pdf", expected_size=1, http=http,
                             resolver=lambda host: [address])
    assert reached == []


def test_a_name_that_does_not_resolve_is_refused(settings) -> None:
    from qbo_multicompany_mcp import attachments

    http, reached = _transport({})
    with pytest.raises(attachments.AttachmentRefused, match="resolve"):
        attachments.download(f"https://{FILES_HOST}/a.pdf", settings=_download_settings(settings),
                             expected_content_type="application/pdf", expected_size=1, http=http,
                             resolver=lambda host: [])
    assert reached == []


def test_redirect_gate_follows_only_to_a_pinned_https_host_and_only_so_far(settings) -> None:
    from qbo_multicompany_mcp import attachments

    pinned = _download_settings(settings, attachment_hosts=(FILES_HOST, "cdn.example"))
    # An allowlisted hop is followed.
    files = {
        f"https://{FILES_HOST}/a.pdf": (302, {"Location": "https://cdn.example/real.pdf"}, b""),
        "https://cdn.example/real.pdf": (200, {"Content-Type": "application/pdf"}, PDF),
    }
    http, reached = _transport(files)
    got = attachments.download(f"https://{FILES_HOST}/a.pdf", settings=pinned, expected_content_type="application/pdf",
                               expected_size=len(PDF), http=http, resolver=public)
    assert got.content == PDF and got.redirects == 1 and got.final_host == "cdn.example"
    assert [str(r.url) for r in reached] == [f"https://{FILES_HOST}/a.pdf", "https://cdn.example/real.pdf"]
    # A hop to a host that is not pinned never leaves.
    http, reached = _transport({f"https://{FILES_HOST}/a.pdf": (302, {"Location": "https://evil.example/x.pdf"}, b"")})
    with pytest.raises(NetworkPolicyViolation, match="not pinned"):
        attachments.download(f"https://{FILES_HOST}/a.pdf", settings=pinned, expected_content_type="application/pdf",
                             expected_size=1, http=http, resolver=public)
    assert len(reached) == 1
    # A hop down to http never leaves.
    http, reached = _transport({f"https://{FILES_HOST}/a.pdf": (302, {"Location": f"http://{FILES_HOST}/x.pdf"}, b"")})
    with pytest.raises((attachments.AttachmentRefused, NetworkPolicyViolation), match="https"):
        attachments.download(f"https://{FILES_HOST}/a.pdf", settings=pinned, expected_content_type="application/pdf",
                             expected_size=1, http=http, resolver=public)
    assert len(reached) == 1
    # A relative Location resolves against the current URL.
    http, reached = _transport({
        f"https://{FILES_HOST}/a.pdf": (302, {"Location": "/moved/a.pdf"}, b""),
        f"https://{FILES_HOST}/moved/a.pdf": (200, {"Content-Type": "application/pdf"}, PDF),
    })
    assert attachments.download(f"https://{FILES_HOST}/a.pdf", settings=pinned, expected_content_type="application/pdf",
                                expected_size=len(PDF), http=http, resolver=public).redirects == 1
    # Too many hops.
    loop = {f"https://{FILES_HOST}/{i}.pdf": (302, {"Location": f"https://{FILES_HOST}/{i + 1}.pdf"}, b"") for i in range(10)}
    http, reached = _transport(loop)
    with pytest.raises(attachments.AttachmentRefused, match="redirect"):
        attachments.download(f"https://{FILES_HOST}/0.pdf", settings=pinned, expected_content_type="application/pdf",
                             expected_size=1, http=http, resolver=public)
    assert len(reached) == attachments.MAX_REDIRECTS + 1


def test_auth_isolation_no_bearer_no_cookie_no_proxy_auth_and_no_environment_proxy(settings, monkeypatch, store, actor) -> None:
    """The download client is a separate client: it never carries the app's
    bearer even while one is held, it sends no cookie (including one a
    redirect tried to set), no proxy authorization, and it does not read
    HTTPS_PROXY from the environment."""
    from qbo_multicompany_mcp import attachments
    from .conftest import REALM_MAIN, make_token_response

    store.commit_consent(entity="acme_main", realm_id=REALM_MAIN, response=make_token_response(), actor=actor)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")
    files = {
        f"https://{FILES_HOST}/a.pdf": (302, {"Location": f"https://{FILES_HOST}/b.pdf", "Set-Cookie": "session=leak"}, b""),
        f"https://{FILES_HOST}/b.pdf": (200, {"Content-Type": "application/pdf"}, PDF),
    }
    reached: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reached.append(request)
        status, headers, content = files[str(request.url)]
        return httpx.Response(status, headers=headers, content=content, request=request)

    # No client injected: the module builds its own, which is what must have
    # trust_env=False; the transport is patched in through the builder.
    monkeypatch.setattr(attachments, "_transport", lambda: httpx.MockTransport(handler))
    got = attachments.download(f"https://{FILES_HOST}/a.pdf", settings=_download_settings(settings),
                               expected_content_type="application/pdf", expected_size=len(PDF), resolver=public)
    assert got.content == PDF
    for request in reached:
        for header in ("authorization", "cookie", "proxy-authorization"):
            assert header not in request.headers, header
    built = attachments._build_client()
    assert built.trust_env is False
    built.close()


@pytest.mark.parametrize(
    ("content_type", "content", "ok"),
    [
        ("application/pdf", PDF, True),
        ("image/png", PNG, True),
        ("image/jpeg", JPEG, True),
        ("image/gif", b"GIF89a" + b"\x00" * 8, True),
        ("text/plain", b"a plain note\n", True),
        ("text/csv", b"a,b\n1,2\n", True),
        ("application/pdf", PNG, False),            # says PDF, is not
        ("image/png", PDF, False),                  # says PNG, is not
        ("text/plain", b"\x00\xff\xfe", False),     # not text
        ("application/octet-stream", PDF, False),   # not on the allowlist
        ("application/zip", b"PK\x03\x04", False),
        ("text/html", b"<html>", False),
        ("application/pdf; charset=binary", PDF, True),
    ],
)
def test_mime_gate_admits_only_supported_types_and_checks_each_by_its_own_signature(settings, content_type, content, ok) -> None:
    """Each admitted type has its own check, and a type outside the allowlist
    is refused before any bytes are read."""
    from qbo_multicompany_mcp import attachments

    http, reached = _transport({f"https://{FILES_HOST}/f": (200, {"Content-Type": content_type}, content)})
    kwargs = dict(settings=_download_settings(settings), expected_content_type=content_type.split(";")[0].strip(),
                  expected_size=len(content), http=http, resolver=public)
    if ok:
        assert attachments.download(f"https://{FILES_HOST}/f", **kwargs).content == content
    else:
        with pytest.raises(attachments.AttachmentRefused, match="type|signature|text"):
            attachments.download(f"https://{FILES_HOST}/f", **kwargs)


def test_mime_gate_refuses_a_response_type_that_disagrees_with_the_metadata(settings) -> None:
    """The header is checked on its own, before the bytes: PDF bytes served
    under an image/png header for a recorded PDF are refused for the
    disagreement, although the signature would have passed."""
    from qbo_multicompany_mcp import attachments

    http, reached = _transport({f"https://{FILES_HOST}/f": (200, {"Content-Type": "image/png"}, PDF)})
    with pytest.raises(attachments.AttachmentRefused, match="disagrees"):
        attachments.download(f"https://{FILES_HOST}/f", settings=_download_settings(settings),
                             expected_content_type="application/pdf", expected_size=len(PDF), http=http, resolver=public)
    http, reached = _transport({f"https://{FILES_HOST}/f": (200, {"Content-Type": "image/png"}, PNG)})
    with pytest.raises(attachments.AttachmentRefused, match="disagrees"):
        attachments.download(f"https://{FILES_HOST}/f", settings=_download_settings(settings),
                             expected_content_type="application/pdf", expected_size=len(PNG), http=http, resolver=public)


def test_mime_gate_refuses_an_unsupported_type_before_any_request(settings) -> None:
    from qbo_multicompany_mcp import attachments

    http, reached = _transport({})
    with pytest.raises(attachments.AttachmentRefused, match="type"):
        attachments.download(f"https://{FILES_HOST}/f", settings=_download_settings(settings),
                             expected_content_type="application/x-msdownload", expected_size=1, http=http, resolver=public)
    assert reached == []


def test_size_gate_refuses_by_metadata_before_the_request_and_by_the_stream_during_it(settings) -> None:
    """The final MCP output cap accounts for base64 expansion: the raw cap is
    three quarters of the output cap, so the encoded blob never exceeds it."""
    from qbo_multicompany_mcp import attachments

    assert attachments.MAX_ATTACHMENT_RAW_BYTES * 4 <= attachments.MAX_ATTACHMENT_OUTPUT_BYTES * 3
    http, reached = _transport({})
    with pytest.raises(attachments.AttachmentRefused, match="larger"):
        attachments.download(f"https://{FILES_HOST}/f", settings=_download_settings(settings),
                             expected_content_type="application/pdf",
                             expected_size=attachments.MAX_ATTACHMENT_RAW_BYTES + 1, http=http, resolver=public)
    assert reached == []
    # Metadata understated the size: the stream is capped anyway and nothing
    # of it is returned.
    big = PDF + b"0" * attachments.MAX_ATTACHMENT_RAW_BYTES
    http, reached = _transport({f"https://{FILES_HOST}/f": (200, {"Content-Type": "application/pdf"}, big)})
    with pytest.raises(attachments.AttachmentRefused, match="larger"):
        attachments.download(f"https://{FILES_HOST}/f", settings=_download_settings(settings),
                             expected_content_type="application/pdf", expected_size=10, http=http, resolver=public)
    assert len(reached) == 1


@pytest.mark.parametrize("served", [PDF[:14], PDF + b"% trailing\n"], ids=["shorter", "longer"])
def test_a_body_of_any_other_length_than_the_recorded_size_is_refused_naming_both(settings, served) -> None:
    """A recorded Size read as an upper bound lets a 14-byte body for a larger
    recorded Size pass every gate and come back as the file. Observed in
    sandbox on 2026-09-26: the Size Intuit recorded (671) matched the file to
    the byte. With Size recorded, a body of any other length is refused,
    naming both lengths, and nothing from it is returned -- though its
    signature would pass."""
    from qbo_multicompany_mcp import attachments

    assert served.startswith(b"%PDF-"), "the signature gate alone admits it"
    http, reached = _transport({f"https://{FILES_HOST}/f": (200, {"Content-Type": "application/pdf"}, served)})
    with pytest.raises(attachments.AttachmentRefused) as refused:
        attachments.download(f"https://{FILES_HOST}/f", settings=_download_settings(settings),
                             expected_content_type="application/pdf", expected_size=len(PDF), http=http, resolver=public)
    text = str(refused.value)
    assert f"{len(served)} bytes" in text and f"{len(PDF)} bytes" in text
    assert len(reached) == 1


def test_without_a_recorded_size_a_download_is_unchanged(settings) -> None:
    """When the Attachable records no Size, the stream cap bounds the body
    and a body under it is returned."""
    from qbo_multicompany_mcp import attachments

    http, _ = _transport({f"https://{FILES_HOST}/f": (200, {"Content-Type": "application/pdf"}, PDF)})
    got = attachments.download(f"https://{FILES_HOST}/f", settings=_download_settings(settings),
                               expected_content_type="application/pdf", expected_size=None, http=http, resolver=public)
    assert got.content == PDF


def test_get_attachment_returns_nothing_when_the_body_differs_from_the_recorded_size(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    """The same through the tool: no blob, the refusal names both lengths."""
    server = _documents_server(mock_settings, registry, credentials, actor, mock_intuit)
    mock_intuit.tables["Attachable"][0]["Size"] = len(PDF) + 60
    with pytest.raises(ToolError) as refused:
        call(server, "get_attachment", {"entity": "acme_main", "attachable_id": "5000"})
    assert f"{len(PDF)} bytes" in str(refused.value) and f"{len(PDF) + 60} bytes" in str(refused.value)


# ==========================================================================
# A recorded Size must be readable
# ==========================================================================
#
# A projection that maps any Size that is not an int or a float to None lets
# a numeric string such as "100" switch off the exact-size check, and a
# 14-byte body passes as the file. Intuit documents Size as Decimal; the one
# value observed in sandbox (2026-09-25) was the JSON integer 671, the file's
# exact length. A string has not been observed. It is read as its number; a
# Size that is present but not a non-negative whole number refuses the
# download, naming the problem; with no Size at all, the stream cap alone
# bounds the body.


@pytest.mark.parametrize(
    ("raw", "size"),
    [(671, 671), (671.0, 671), ("671", 671), ("671.0", 671), (0, 0), ("0", 0), (None, None)],
)
def test_the_listing_reads_a_recorded_size_as_its_whole_number(raw, size) -> None:
    from qbo_multicompany_mcp.projections import project_attachable

    row = {"Id": "1", "FileName": "a.pdf", "ContentType": "application/pdf"}
    if raw is not None:
        row["Size"] = raw
    assert project_attachable(row).get("size") == size


@pytest.mark.parametrize(
    ("raw", "size"),
    [(671, 671), (671.0, 671), ("671", 671), ("671.0", 671), (0, 0), ("0", 0), (None, None)],
)
def test_the_size_parser_reads_a_recorded_size_as_its_whole_number(raw, size) -> None:
    from qbo_multicompany_mcp.projections import recorded_size

    assert recorded_size(raw) == size


@pytest.mark.parametrize(
    ("raw", "problem"),
    [
        (-1, "is negative"),
        (-1.0, "is negative"),
        (671.5, "is not a whole number"),
        ("671.5", "is not a whole number"),
        (float("nan"), "is not a whole number"),
        ("abc", "is text that is not a non-negative number"),
        ("-5", "is text that is not a non-negative number"),
        (" 671", "is text that is not a non-negative number"),
        ("", "is text that is not a non-negative number"),
        (True, "is true or false"),
        ([671], "is not a number"),
        ({"value": 671}, "is not a number"),
    ],
)
def test_a_recorded_size_that_is_not_a_non_negative_whole_number_is_named(raw, problem) -> None:
    from qbo_multicompany_mcp.projections import UnreadableSize, recorded_size

    with pytest.raises(UnreadableSize) as unreadable:
        recorded_size(raw)
    assert str(unreadable.value) == problem


@pytest.mark.parametrize("raw", [-1, -1.0, 671.5, "671.5", "abc", " 671", True, [671]])
def test_the_listing_shows_no_size_for_an_unreadable_one(raw) -> None:
    """The listing still answers; an unreadable Size is not shown as a size."""
    from qbo_multicompany_mcp.projections import project_attachable

    assert project_attachable({"Id": "1", "Size": raw}).get("size") is None


def _file_requests(mock_intuit: MockIntuit) -> list[str]:
    return [path for _, path, _, _ in mock_intuit.requests if path.startswith("/files/")]


def test_get_attachment_holds_a_numeric_string_size_to_the_exact_size_check(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    """Through the tool: Size "100" against a shorter file is refused, naming
    both lengths. Read as unsized, the file would come back."""
    server = _documents_server(mock_settings, registry, credentials, actor, mock_intuit)
    mock_intuit.tables["Attachable"][0]["Size"] = "100"
    with pytest.raises(ToolError) as refused:
        call(server, "get_attachment", {"entity": "acme_main", "attachable_id": "5000"})
    assert f"{len(PDF)} bytes" in str(refused.value) and "100 bytes" in str(refused.value)


def test_get_attachment_returns_the_file_when_a_numeric_string_size_matches(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    server = _documents_server(mock_settings, registry, credentials, actor, mock_intuit)
    mock_intuit.tables["Attachable"][0]["Size"] = str(len(PDF))
    result = call(server, "get_attachment", {"entity": "acme_main", "attachable_id": "5000"})
    assert result.structured_content["bytes"] == len(PDF)
    assert result.structured_content["attachable"]["size"] == len(PDF)


@pytest.mark.parametrize(
    ("raw", "problem"),
    [("abc", "is text that is not a non-negative number"), (-1, "is negative"),
     (671.5, "is not a whole number"), (True, "is true or false")],
)
def test_get_attachment_refuses_an_unreadable_size_before_any_download(
    mock_settings, registry, credentials, actor, mock_intuit, raw, problem
) -> None:
    server = _documents_server(mock_settings, registry, credentials, actor, mock_intuit)
    mock_intuit.tables["Attachable"][0]["Size"] = raw
    with pytest.raises(ToolError) as refused:
        call(server, "get_attachment", {"entity": "acme_main", "attachable_id": "5000"})
    text = str(refused.value)
    assert f"recorded Size {problem}" in text, text
    assert "(entity=acme_main environment=sandbox)" in text
    assert _file_requests(mock_intuit) == [], "the file was requested"


def test_get_attachment_without_a_recorded_size_is_unchanged(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    server = _documents_server(mock_settings, registry, credentials, actor, mock_intuit)
    del mock_intuit.tables["Attachable"][0]["Size"]
    result = call(server, "get_attachment", {"entity": "acme_main", "attachable_id": "5000"})
    assert result.structured_content["bytes"] == len(PDF)
    assert result.structured_content["attachable"]["size"] is None


# ==========================================================================
# The output cap counts the whole result
# ==========================================================================
#
# A 10 MiB cap on the base64 blob alone is not enough: make_attachment_result
# adds the metadata text, the structured metadata and the resource wrapper, so
# a maximum-size file would make a 10,487,362-byte result against a
# 10,485,760-byte cap. The complete result is what is counted, as it goes on
# the wire.


def _envelope_for(content: bytes, *, note: str = "synthetic"):
    from datetime import UTC, datetime

    from qbo_multicompany_mcp.envelopes import AttachableBlock, AttachmentEnvelope

    now = datetime(2026, 9, 26, tzinfo=UTC)
    return AttachmentEnvelope(
        entity="acme_main", environment="sandbox", display_name_from_registry="Acme Main",
        realm_id_last4="0001", request_started_at=now, fetched_at=now,
        attachable=AttachableBlock(id="1", file_name="synthetic.pdf", content_type="application/pdf",
                                   size=len(content)),
        content_type="application/pdf", bytes=len(content),
        base64_length=len(base64.b64encode(content)), sha256=hashlib.sha256(content).hexdigest(),
        download_host=FILES_HOST, redirects_followed=0, resource_uri="qbo://acme_main/attachable/1", note=note,
    )


def _wire_size(result) -> int:
    return len(result.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8"))


def test_a_maximum_size_file_is_refused_because_the_whole_result_exceeds_the_cap() -> None:
    """A file at the raw cap passes every download gate, and its complete
    result would exceed the output cap, so nothing is returned."""
    from qbo_multicompany_mcp import attachments
    from qbo_multicompany_mcp.tools_attachments import make_attachment_result

    content = b"%PDF-" + b"0" * (attachments.MAX_ATTACHMENT_RAW_BYTES - 5)
    attachments.check_size(len(content))
    with pytest.raises(attachments.AttachmentRefused) as refused:
        make_attachment_result(_envelope_for(content), content)
    assert str(attachments.MAX_ATTACHMENT_OUTPUT_BYTES) in str(refused.value)


def test_the_cap_holds_at_the_boundary_of_the_complete_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """At the boundary: a result exactly the cap's size is returned, one byte
    over is refused -- the complete result as the wire carries it, not the
    blob."""
    from qbo_multicompany_mcp import attachments
    from qbo_multicompany_mcp.tools_attachments import make_attachment_result

    content = PDF * 50
    envelope = _envelope_for(content)
    size = _wire_size(make_attachment_result(envelope, content))
    assert size > len(base64.b64encode(content)), "the metadata is counted too"

    monkeypatch.setattr(attachments, "MAX_ATTACHMENT_OUTPUT_BYTES", size)
    assert _wire_size(make_attachment_result(envelope, content)) == size
    monkeypatch.setattr(attachments, "MAX_ATTACHMENT_OUTPUT_BYTES", size - 1)
    with pytest.raises(attachments.AttachmentRefused) as refused:
        make_attachment_result(envelope, content)
    assert f"{size} bytes" in str(refused.value)


def test_get_attachment_returns_nothing_when_the_whole_result_would_exceed_the_cap(
    mock_settings, registry, credentials, actor, mock_intuit, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the tool: the blob alone is under the cap, the result is not."""
    from qbo_multicompany_mcp import attachments

    server = _documents_server(mock_settings, registry, credentials, actor, mock_intuit)
    monkeypatch.setattr(attachments, "MAX_ATTACHMENT_OUTPUT_BYTES", len(base64.b64encode(PDF)) + 16)
    with pytest.raises(ToolError, match="output limit"):
        call(server, "get_attachment", {"entity": "acme_main", "attachable_id": "5000"})


def test_a_download_needs_documents_enabled_whatever_else_is_pinned(settings) -> None:
    from qbo_multicompany_mcp import attachments

    http, reached = _transport({})
    with pytest.raises((attachments.AttachmentRefused, NetworkPolicyViolation), match="--allow-documents"):
        attachments.download(f"https://{FILES_HOST}/a.pdf",
                             settings=_download_settings(settings, allow_documents=False),
                             expected_content_type="application/pdf", expected_size=1, http=http, resolver=public)
    assert reached == []


# ==========================================================================
# get_attachment through the server, and the two-block contract over stdio
# ==========================================================================


def _documents_server(mock_settings, registry, credentials, actor, mock_intuit: MockIntuit, **overrides) -> MCPServer:
    mock_intuit.tables["Attachable"] = attachable_rows(mock_intuit.base_url)
    mock_intuit.filters["Attachable"] = [_attachable_filter]
    mock_intuit.files["/files/receipt.pdf"] = ("application/pdf", PDF)
    enabled = dc_replace(mock_settings, **{"allow_documents": True, "attachment_hosts": ("127.0.0.1",), **overrides})
    return build(enabled, registry, credentials, actor)


def test_get_attachment_is_registered_only_when_documents_are_enabled(mock_settings, registry, credentials, actor, mock_intuit) -> None:
    server = _documents_server(mock_settings, registry, credentials, actor, mock_intuit)
    tools = {t.name: t for t in anyio.run(server.list_tools)}
    assert "get_attachment" in tools
    assert "attachable_id" in tools["get_attachment"].input_schema["required"]
    from qbo_multicompany_mcp.tools import FREE_TEXT_DISCLOSURE
    from qbo_multicompany_mcp.tools_attachments import DESCRIPTION_GET, DESCRIPTION_LIST

    assert FREE_TEXT_DISCLOSURE in DESCRIPTION_GET and FREE_TEXT_DISCLOSURE in DESCRIPTION_LIST


def test_get_attachment_returns_the_two_blocks_and_never_the_link(mock_settings, registry, credentials, actor, mock_intuit) -> None:
    """A metadata text block first, then the file as an embedded blob; the
    structured content is the metadata. The temporary URL is in none of it.
    Loopback is admitted here only because the API base is itself a loopback
    mock (test-only, like the accounting side); in production the API base is
    Intuit's and a loopback download host is refused."""
    server = _documents_server(mock_settings, registry, credentials, actor, mock_intuit)
    result = call(server, "get_attachment", {"entity": "acme_main", "attachable_id": "5000"})
    assert result.is_error is not True
    assert [block.type for block in result.content] == ["text", "resource"]
    text = json.loads(result.content[0].text)
    assert text == result.structured_content
    blob = result.content[1].resource
    assert blob.mime_type == "application/pdf"
    assert base64.b64decode(blob.blob) == PDF
    assert blob.uri.startswith("qbo://acme_main/attachable/5000")
    body = result.structured_content
    assert body["attachable"]["id"] == "5000" and body["attachable"]["file_name"] == "receipt-example.pdf"
    assert body["content_type"] == "application/pdf" and body["bytes"] == len(PDF)
    assert body["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert body["base64_length"] == len(base64.b64encode(PDF))
    assert body["download_host"] == "127.0.0.1" and body["redirects_followed"] == 0
    dumped = json.dumps(body) + result.content[0].text
    assert "/files/receipt.pdf" not in dumped and LEAK not in dumped and "TempDownloadUri" not in dumped
    assert body["entity"] == "acme_main" and body["realm_id_last4"] == "0001"


def test_get_attachment_refuses_before_any_request_when_no_host_is_pinned(mock_settings, registry, credentials, actor, mock_intuit) -> None:
    server = _documents_server(mock_settings, registry, credentials, actor, mock_intuit, attachment_hosts=())
    before = len(mock_intuit.requests)
    with pytest.raises(ToolError, match="QBO_ATTACHMENT_HOSTS"):
        call(server, "get_attachment", {"entity": "acme_main", "attachable_id": "5000"})
    assert not any(path.startswith("/files/") for _m, path, _q, _t in mock_intuit.requests[before:])


def test_get_attachment_on_a_note_with_no_file_is_a_fixed_error(mock_settings, registry, credentials, actor, mock_intuit) -> None:
    server = _documents_server(mock_settings, registry, credentials, actor, mock_intuit)
    with pytest.raises(ToolError, match="no file"):
        call(server, "get_attachment", {"entity": "acme_main", "attachable_id": "5001"})
    with pytest.raises(ToolError, match="not found"):
        call(server, "get_attachment", {"entity": "acme_main", "attachable_id": "9999"})


def test_get_attachment_refuses_an_unsupported_type_from_the_metadata_before_downloading(mock_settings, registry, credentials, actor, mock_intuit) -> None:
    server = _documents_server(mock_settings, registry, credentials, actor, mock_intuit)
    mock_intuit.tables["Attachable"][0]["ContentType"] = "application/zip"
    before = len(mock_intuit.requests)
    with pytest.raises(ToolError, match="type"):
        call(server, "get_attachment", {"entity": "acme_main", "attachable_id": "5000"})
    assert not any(path.startswith("/files/") for _m, path, _q, _t in mock_intuit.requests[before:])


def test_a_loopback_download_host_is_refused_when_the_api_base_is_intuit(settings) -> None:
    """The test-only admission is tied to a loopback API base. With the real
    Intuit base, a loopback download host is refused like any private one."""
    from qbo_multicompany_mcp import attachments

    http, reached = _transport({})
    with pytest.raises((attachments.AttachmentRefused, NetworkPolicyViolation), match="loopback|not pinned"):
        attachments.download("https://127.0.0.1/a.pdf", settings=_download_settings(settings, attachment_hosts=("127.0.0.1",)),
                             expected_content_type="application/pdf", expected_size=1, http=http, resolver=public)
    assert reached == []


def test_the_two_block_contract_over_a_real_stdio_connection(
    entities_file: Path, store_dir: Path, token_key: str, credentials, actor, mock_intuit, mock_settings
) -> None:
    """The client-compatibility gate: what a real MCP client receives.
    The server subprocess talks to the loopback mock for the Attachable read
    and for the file; the pinned host is the mock's loopback address, which
    the policy admits only because the API base is that same loopback."""
    from .conftest import REALM_MAIN, make_token_response

    mock_intuit.tables["Attachable"] = attachable_rows(mock_intuit.base_url)
    mock_intuit.filters["Attachable"] = [_attachable_filter]
    mock_intuit.files["/files/receipt.pdf"] = ("application/pdf", PDF)
    store = mock_settings.open_store(credentials=credentials)
    store.commit_consent(entity="acme_main", realm_id=REALM_MAIN, response=make_token_response(), actor=actor)
    env = stdio_env(
        entities_file=entities_file, store_dir=store_dir, token_key=token_key,
        extra={"QBO_CLIENT_ID": credentials.client_id, "QBO_CLIENT_SECRET": credentials.client_secret,
               "QBO_ALLOW_DOCUMENTS": "1", "QBO_ATTACHMENT_HOSTS": "127.0.0.1"},
    )

    async def body(client):
        tools = {t.name for t in (await client.list_tools()).tools}
        got = await client.call_tool("get_attachment", {"entity": "acme_main", "attachable_id": "5000"})
        listed = await client.call_tool("list_attachments", {"entity": "acme_main"})
        return tools, got, listed

    tools, got, listed = run_stdio(env, ["--api-base", mock_intuit.base_url], body)
    assert "get_attachment" in tools
    assert got.is_error is False
    assert [block.type for block in got.content] == ["text", "resource"]
    assert base64.b64decode(got.content[1].resource.blob) == PDF
    assert got.content[1].resource.mime_type == "application/pdf"
    assert got.structured_content["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert "/files/" not in result_text(got)
    assert listed.structured_content["download_available"] is True


# ==========================================================================
# Every get_attachment failure names the bound company
# ==========================================================================
#
# A bare status sentence leaves two companies' failed receipts in one session
# both reading "status 404". Once the entity is bound, each failure says which
# key and environment it was for, and shows neither the realm id nor the signed
# URL.

IDENTITY = "entity=acme_main environment=sandbox"


def _break_nothing(mock_intuit, monkeypatch) -> None:
    pass


def _no_file_at_the_link(mock_intuit, monkeypatch) -> None:
    mock_intuit.files.clear()


def _recorded_size_differs(mock_intuit, monkeypatch) -> None:
    mock_intuit.tables["Attachable"][0]["Size"] = len(PDF) + 60


def _unsupported_type(mock_intuit, monkeypatch) -> None:
    mock_intuit.tables["Attachable"][0]["ContentType"] = "application/zip"


def _recorded_size_over_the_cap(mock_intuit, monkeypatch) -> None:
    from qbo_multicompany_mcp import attachments

    mock_intuit.tables["Attachable"][0]["Size"] = attachments.MAX_ATTACHMENT_RAW_BYTES + 1


def _a_bug(mock_intuit, monkeypatch) -> None:
    from qbo_multicompany_mcp import tools_attachments

    def broken(row):
        raise RuntimeError("a bug in this package")

    monkeypatch.setattr(tools_attachments, "project_attachable", broken)


@pytest.mark.parametrize(
    ("attachable_id", "arrange", "overrides"),
    [
        pytest.param("5000", _no_file_at_the_link, {}, id="download-status-404"),
        pytest.param("5000", _recorded_size_differs, {}, id="size-differs"),
        pytest.param("5000", _unsupported_type, {}, id="unsupported-type"),
        pytest.param("5000", _recorded_size_over_the_cap, {}, id="recorded-size-over-cap"),
        pytest.param("9999", _break_nothing, {}, id="not-found"),
        pytest.param("5001", _break_nothing, {}, id="a-note-with-no-file"),
        pytest.param(" ", _break_nothing, {}, id="an-empty-id"),
        pytest.param("5000", _break_nothing, {"attachment_hosts": ()}, id="no-host-pinned"),
        pytest.param("5000", _a_bug, {}, id="an-unexpected-failure"),
    ],
)
def test_every_get_attachment_failure_names_the_bound_entity_and_environment(
    mock_settings, registry, credentials, actor, mock_intuit, monkeypatch, attachable_id, arrange, overrides
) -> None:
    from .conftest import REALM_MAIN

    server = _documents_server(mock_settings, registry, credentials, actor, mock_intuit, **overrides)
    arrange(mock_intuit, monkeypatch)
    with pytest.raises(ToolError) as refused:
        call(server, "get_attachment", {"entity": "acme_main", "attachable_id": attachable_id})
    text = str(refused.value)
    assert IDENTITY in text
    assert REALM_MAIN not in text
    assert "/files/" not in text and "receipt.pdf" not in text, "never the signed link"


def test_a_failed_download_names_its_company_over_stdio(
    entities_file: Path, store_dir: Path, token_key: str, credentials, actor, mock_intuit, mock_settings
) -> None:
    """Over a real stdio connection: the text a client reads."""
    from .conftest import REALM_MAIN, make_token_response

    mock_intuit.tables["Attachable"] = attachable_rows(mock_intuit.base_url)
    mock_intuit.filters["Attachable"] = [_attachable_filter]
    store = mock_settings.open_store(credentials=credentials)
    store.commit_consent(entity="acme_main", realm_id=REALM_MAIN, response=make_token_response(), actor=actor)
    env = stdio_env(
        entities_file=entities_file, store_dir=store_dir, token_key=token_key,
        extra={"QBO_CLIENT_ID": credentials.client_id, "QBO_CLIENT_SECRET": credentials.client_secret,
               "QBO_ALLOW_DOCUMENTS": "1", "QBO_ATTACHMENT_HOSTS": "127.0.0.1"},
    )

    async def body(client):
        return await client.call_tool("get_attachment", {"entity": "acme_main", "attachable_id": "5000"})

    got = run_stdio(env, ["--api-base", mock_intuit.base_url], body)
    text = result_text(got)
    assert got.is_error is True
    assert "status 404" in text and IDENTITY in text
    assert REALM_MAIN not in text and "/files/" not in text


def test_get_attachment_is_absent_over_stdio_by_default(entities_file: Path, tmp_path: Path) -> None:
    async def body(client):
        return {t.name for t in (await client.list_tools()).tools}

    tools = run_stdio(stdio_env(entities_file=entities_file, store_dir=tmp_path / "never"), [], body)
    assert "list_attachments" in tools and "get_attachment" not in tools
    assert len(tools) == 18, "the 18 default tools; the opt-in is absent"


# ==========================================================================
# The pin. The storage host was observed in sandbox; see docs/api-findings.md
# item 17.
# ==========================================================================

#: The storage host observed in sandbox on 2026-09-25, read once from the raw
#: Attachable and recorded host-only. Sandbox only; production is unobserved.
OBSERVED_HOST = "financialdocument.platform.intuit.com"

#: A synthetic PDF, 671 bytes, attached in a sandbox company to test the whole
#: download path live. The copy under tests/fixtures/ is byte-identical.
SYNTHETIC_PDF_PATH = Path(__file__).parent / "fixtures" / "synthetic-attachment-test.pdf"
SYNTHETIC_PDF_SHA256 = "044e24a2fed8d3105b70426c2d8500c3b136064d15cad853a7020be15c86d3db"
SYNTHETIC_PDF_SIZE = 671


def observed_rows(link: str) -> list[dict[str, Any]]:
    """Structure of a live sandbox response, with every value synthetic and
    the temporary link supplied as ``link``, beside the two synthetic rows the
    other tests use."""
    return [
        {"Id": "1000000001", "FileName": "synthetic-attachment-test.pdf", "ContentType": "application/pdf",
         "Size": SYNTHETIC_PDF_SIZE, "SyncToken": "1", "domain": "QBO", "sparse": False,
         "documentId": LEAK, "FileAccessUri": "/v3/company/0000000000000001/download/1000000001",
         "TempDownloadUri": link,
         "AttachableRef": [{"EntityRef": {"value": "145", "type": "BillPayment"}, "IncludeOnSend": False}],
         "MetaData": {"CreateTime": "2026-01-01T09:00:00-08:00", "LastUpdatedTime": "2026-01-01T09:00:10-08:00"}},
        *attachable_rows(),
    ]


def test_the_synthetic_fixture_matches_its_recorded_hash() -> None:
    content = SYNTHETIC_PDF_PATH.read_bytes()
    assert len(content) == SYNTHETIC_PDF_SIZE
    assert hashlib.sha256(content).hexdigest() == SYNTHETIC_PDF_SHA256
    assert content.startswith(b"%PDF-")


def test_the_observed_storage_host_is_a_plain_hostname_labelled_sandbox_only() -> None:
    """The constant holds the host observed in sandbox as a plain hostname,
    and the hint a user reads says production may differ."""
    from qbo_multicompany_mcp import attachments, constants

    assert constants.OBSERVED_ATTACHMENT_HOSTS_SANDBOX == (OBSERVED_HOST,)
    assert attachments.address_kind(OBSERVED_HOST) == "unreadable", "a hostname, never an IP literal"
    assert OBSERVED_HOST == OBSERVED_HOST.lower() and "://" not in OBSERVED_HOST
    assert "sandbox" in constants.ATTACHMENT_HOST_HINT and "production" in constants.ATTACHMENT_HOST_HINT
    # The hint is what a user reads, so it names a host and no file path or tool.
    assert "/" not in constants.ATTACHMENT_HOST_HINT and "probe" not in constants.ATTACHMENT_HOST_HINT
    assert "before pinning" in constants.ATTACHMENT_HOST_HINT


def test_the_observed_host_is_never_pinned_by_default(entities_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No arbitrary storage hosts. Observing the host does not pin it; the
    operator does, per deployment."""
    import argparse

    from qbo_multicompany_mcp.config import add_common_arguments, settings_from_args

    def parse(argv: list[str]) -> argparse.Namespace:
        parser = argparse.ArgumentParser()
        add_common_arguments(parser)
        return parser.parse_args(argv)

    monkeypatch.delenv("QBO_ATTACHMENT_HOSTS", raising=False)
    monkeypatch.setenv("QBO_ALLOW_DOCUMENTS", "1")
    settings = settings_from_args(parse(["--entities", str(entities_file)]))
    assert settings.allow_documents is True
    assert settings.attachment_hosts == ()
    monkeypatch.setenv("QBO_ATTACHMENT_HOSTS", OBSERVED_HOST)
    pinned = settings_from_args(parse(["--entities", str(entities_file)]))
    assert pinned.attachment_hosts == (OBSERVED_HOST,)


def test_every_no_host_refusal_names_the_observed_host_as_sandbox_only(
    settings, mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    """The operator learns which host to pin from the refusal itself, with
    the qualifier that it was observed in sandbox and production may differ."""
    from qbo_multicompany_mcp import attachments, netpolicy
    from qbo_multicompany_mcp.netpolicy import PolicyContext, RequestClass

    http, reached = _transport({})
    with pytest.raises(attachments.AttachmentRefused) as refused:
        attachments.download(f"https://{OBSERVED_HOST}/a.pdf", settings=_download_settings(settings, attachment_hosts=()),
                             expected_content_type="application/pdf", expected_size=1, http=http, resolver=public)
    assert OBSERVED_HOST in str(refused.value) and "sandbox" in str(refused.value) and reached == []

    ctx = PolicyContext(request_class=RequestClass.ATTACHMENT, environment="sandbox",
                        api_base=settings.api_base, allow_documents=True, attachment_hosts=())
    with pytest.raises(NetworkPolicyViolation) as violation:
        netpolicy.enforce(httpx.Request("GET", f"https://{OBSERVED_HOST}/a.pdf"), ctx)
    assert OBSERVED_HOST in str(violation.value) and "QBO_ATTACHMENT_HOSTS" in str(violation.value)

    server = _documents_server(mock_settings, registry, credentials, actor, mock_intuit, attachment_hosts=())
    listed = body_of(call(server, "list_attachments", {"entity": "acme_main"}))
    assert listed["download_available"] is False and OBSERVED_HOST in listed["download_note"]
    with pytest.raises(ToolError) as error:
        call(server, "get_attachment", {"entity": "acme_main", "attachable_id": "5000"})
    assert OBSERVED_HOST in str(error.value) and "sandbox" in str(error.value)


def test_a_download_from_the_observed_host_passes_every_gate_and_reproduces_the_fixture(settings) -> None:
    """The observed host pinned, a signed link on it, the fixture's bytes.
    sha256 equals the fixture's, content_type is the recorded one,
    download_host the pinned host, no redirect, and no credential on the
    request."""
    from qbo_multicompany_mcp import attachments

    content = SYNTHETIC_PDF_PATH.read_bytes()
    link = f"https://{OBSERVED_HOST}/v1/documents/1000000001?Expires=1&Signature=x"
    http, reached = _transport({link.split("?")[0]: (200, {"Content-Type": "application/pdf"}, content)})
    got = attachments.download(link, settings=_download_settings(settings, attachment_hosts=(OBSERVED_HOST,)),
                               expected_content_type="application/pdf", expected_size=SYNTHETIC_PDF_SIZE,
                               http=http, resolver=public)
    assert got.sha256 == SYNTHETIC_PDF_SHA256 and len(got.content) == SYNTHETIC_PDF_SIZE
    assert got.content_type == "application/pdf"
    assert got.final_host == OBSERVED_HOST and got.redirects == 0
    assert len(reached) == 1
    for header in ("authorization", "cookie", "proxy-authorization"):
        assert header not in {k.lower() for k in reached[0].headers}


def test_a_redirect_off_the_observed_host_is_refused_and_a_pinned_hop_is_followed(settings) -> None:
    """A pinned hop is followed, and a hop to a host the operator did not pin
    never leaves."""
    from qbo_multicompany_mcp import attachments

    content = SYNTHETIC_PDF_PATH.read_bytes()
    link = f"https://{OBSERVED_HOST}/v1/documents/1000000001"
    only_observed = _download_settings(settings, attachment_hosts=(OBSERVED_HOST,))
    http, reached = _transport({link: (302, {"Location": "https://files.example/x.pdf"}, b"")})
    with pytest.raises(NetworkPolicyViolation, match="not pinned"):
        attachments.download(link, settings=only_observed, expected_content_type="application/pdf",
                             expected_size=SYNTHETIC_PDF_SIZE, http=http, resolver=public)
    assert len(reached) == 1

    both = _download_settings(settings, attachment_hosts=(OBSERVED_HOST, "files.example"))
    http, reached = _transport({link: (302, {"Location": "https://files.example/x.pdf"}, b""),
                                "https://files.example/x.pdf": (200, {"Content-Type": "application/pdf"}, content)})
    got = attachments.download(link, settings=both, expected_content_type="application/pdf",
                               expected_size=SYNTHETIC_PDF_SIZE, http=http, resolver=public)
    assert got.redirects == 1 and got.final_host == "files.example" and got.sha256 == SYNTHETIC_PDF_SHA256


def test_list_attachments_filter_returns_exactly_the_matching_row_for_a_billpayment(
    mock_settings, registry, credentials, actor, mock_intuit
) -> None:
    """The filter, on synthetic values in the shape observed in sandbox:
    transaction_type=BillPayment, transaction_id=145 returns exactly the one
    row, with its file name, type, size and ref, and no link, Expires or
    /download/ anywhere. The canonical entity name is sent."""
    mock_intuit.tables["Attachable"] = observed_rows(f"https://{OBSERVED_HOST}/v1/documents/1000000001?Signature=x")
    mock_intuit.filters["Attachable"] = [_attachable_filter]
    server = build(mock_settings, registry, credentials, actor)
    body = body_of(call(server, "list_attachments",
                        {"entity": "acme_main", "transaction_type": "billpayment", "transaction_id": "145"}))
    assert body["count"] == 1 and body["transaction_type"] == "BillPayment" and body["transaction_id"] == "145"
    (row,) = body["rows"]
    assert row["id"] == "1000000001" and row["file_name"] == "synthetic-attachment-test.pdf"
    assert row["content_type"] == "application/pdf" and row["size"] == SYNTHETIC_PDF_SIZE
    assert row["refs"][0]["entity_type"] == "BillPayment" and row["refs"][0]["entity_id"] == "145"
    dumped = json.dumps(body)
    assert "://" not in dumped and "Expires" not in dumped and "/download/" not in dumped and LEAK not in dumped
    assert any("AttachableRef.EntityRef.Type = 'BillPayment' AND AttachableRef.EntityRef.value = '145'" in s
               for s in statements(mock_intuit))
    everything = body_of(call(server, "list_attachments", {"entity": "acme_main"}))
    assert everything["count"] == 3


def test_get_attachment_through_the_server_with_the_observed_host_pinned_returns_the_fixture(
    mock_settings, registry, credentials, actor, mock_intuit, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole tool path in-process: the Attachable read through the mock,
    the observed host pinned, the download client built by the module itself
    with its transport seam serving the fixture's bytes and its resolver seam
    answering a public address. Two blocks; sha256 equals the fixture's;
    content_type as recorded; download_host the pinned host;
    redirects_followed 0."""
    from qbo_multicompany_mcp import attachments

    content = SYNTHETIC_PDF_PATH.read_bytes()
    link = f"https://{OBSERVED_HOST}/v1/documents/1000000001?Expires=1&Signature=x"
    mock_intuit.tables["Attachable"] = observed_rows(link)
    mock_intuit.filters["Attachable"] = [_attachable_filter]
    reached: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reached.append(request)
        assert request.url.host == OBSERVED_HOST and request.url.scheme == "https"
        return httpx.Response(200, headers={"Content-Type": "application/pdf"}, content=content, request=request)

    monkeypatch.setattr(attachments, "_transport", lambda: httpx.MockTransport(handler))
    monkeypatch.setattr(attachments, "_default_resolver", public)
    enabled = dc_replace(mock_settings, allow_documents=True, attachment_hosts=(OBSERVED_HOST,))
    server = build(enabled, registry, credentials, actor)

    result = call(server, "get_attachment", {"entity": "acme_main", "attachable_id": "1000000001"})
    assert result.is_error is not True
    assert [block.type for block in result.content] == ["text", "resource"]
    body = result.structured_content
    assert json.loads(result.content[0].text) == body
    assert base64.b64decode(result.content[1].resource.blob) == content
    assert result.content[1].resource.mime_type == "application/pdf"
    assert body["sha256"] == SYNTHETIC_PDF_SHA256 and body["bytes"] == SYNTHETIC_PDF_SIZE
    assert body["content_type"] == "application/pdf"
    assert body["download_host"] == OBSERVED_HOST and body["redirects_followed"] == 0
    assert body["attachable"]["id"] == "1000000001"
    assert body["attachable"]["refs"][0] == {"entity_type": "BillPayment", "entity_id": "145", "include_on_send": False}
    dumped = json.dumps(body) + result.content[0].text
    assert "Signature" not in dumped and "/v1/documents/" not in dumped and LEAK not in dumped
    assert len(reached) == 1 and "authorization" not in {k.lower() for k in reached[0].headers}
