"""Proves the read-only guarantee at the point where it matters: the request,
inspected immediately before it is sent.

Intuit's accounting scope grants writes, so nothing about the credential makes
this server read-only. These tests are the claim.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from qbo_multicompany_mcp.constants import (
    API_BASES,
    DISCOVERY_URLS,
    REVOKE_ENDPOINT,
    TOKEN_ENDPOINT,
)
from qbo_multicompany_mcp.errors import NetworkPolicyViolation, StartupError
from qbo_multicompany_mcp.netpolicy import (
    PolicyContext,
    RequestClass,
    build_accounting_url,
    enforce,
    send,
    validate_api_base,
)

from .conftest import REALM_MAIN, REALM_OPS

BASE = API_BASES["sandbox"]


def accounting_ctx(**overrides) -> PolicyContext:
    defaults = dict(
        request_class=RequestClass.ACCOUNTING,
        environment="sandbox",
        api_base=BASE,
        realm_id=REALM_MAIN,
    )
    defaults.update(overrides)
    return PolicyContext(**defaults)


def get(path: str, **kwargs) -> httpx.Request:
    return httpx.Request("GET", f"{BASE}{path}", **kwargs)


# --------------------------------------------------------------------------
# The allowlist
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tail",
    ["companyinfo/{realm}", "preferences", "query"],
)
def test_allowlisted_reads_are_permitted(tail: str) -> None:
    url = build_accounting_url(BASE, REALM_MAIN, tail)
    request = httpx.Request("GET", url, headers={"Authorization": "Bearer x"})
    enforce(request, accounting_ctx())


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
def test_no_method_but_get_reaches_the_accounting_api(method: str) -> None:
    url = build_accounting_url(BASE, REALM_MAIN, "preferences")
    with pytest.raises(NetworkPolicyViolation, match="method"):
        enforce(httpx.Request(method, url), accounting_ctx())


# --------------------------------------------------------------------------
# The six canaries
# --------------------------------------------------------------------------


def test_an_accounting_get_may_not_carry_a_body() -> None:
    request = get(f"/v3/company/{REALM_MAIN}/preferences", content=b"synthetic")
    with pytest.raises(NetworkPolicyViolation, match="body"):
        enforce(request, accounting_ctx())


@pytest.mark.parametrize("name", ["X-HTTP-Method-Override", "X-HTTP-Method", "X-Method-Override"])
def test_an_accounting_get_may_not_carry_a_method_override(name: str) -> None:
    """Nothing shows Intuit honours one, and no builder sends one. Read-only
    shouldn't rest on that."""
    request = get(f"/v3/company/{REALM_MAIN}/preferences", headers={name: "POST"})
    with pytest.raises(NetworkPolicyViolation, match="method-override"):
        enforce(request, accounting_ctx())


def test_an_accounting_host_header_must_be_the_urls() -> None:
    request = get(f"/v3/company/{REALM_MAIN}/preferences", headers={"Host": "other.example"})
    with pytest.raises(NetworkPolicyViolation, match="Host"):
        enforce(request, accounting_ctx())


def test_a_cookie_intuit_set_does_not_refuse_a_read() -> None:
    """Why the rule names what's refused rather than listing what's allowed: a
    client keeps cookies, and a read carrying one is still a read."""
    enforce(get(f"/v3/company/{REALM_MAIN}/preferences", headers={"Cookie": "synthetic=1"}), accounting_ctx())


def test_a_size_failure_ends_the_reader_thread() -> None:
    """A worker holding a chunk waits for the queue to clear. A failure that
    leaves it waiting costs a thread per call, for as long as the server runs."""
    import threading
    import time

    from qbo_multicompany_mcp.deadline import Deadline
    from qbo_multicompany_mcp.errors import ResponseTooLarge
    from qbo_multicompany_mcp.netpolicy import _read_capped

    class Body:
        def iter_bytes(self):
            return iter([b"aa"] * 8)

        def close(self) -> None:
            return None

    deadline = Deadline(budget=30.0, expires_at=time.monotonic() + 30.0)
    with pytest.raises(ResponseTooLarge):
        _read_capped(Body(), max_bytes=3, what="synthetic", deadline=deadline)

    limit = time.monotonic() + 2.0
    alive = [thread for thread in threading.enumerate() if thread.name == "qbo-bounded-read"]
    while alive and time.monotonic() < limit:
        time.sleep(0.02)
        alive = [thread for thread in alive if thread.is_alive()]
    assert alive == [], "the reader outlived the call"


def test_canary_invoice_send_is_rejected() -> None:
    with pytest.raises(NetworkPolicyViolation, match="read allowlist"):
        enforce(get(f"/v3/company/{REALM_MAIN}/invoice/1/send"), accounting_ctx())


def test_canary_bulk_endpoint_is_rejected() -> None:
    with pytest.raises(NetworkPolicyViolation, match="read allowlist"):
        enforce(get(f"/v3/company/{REALM_MAIN}/" + "batch"), accounting_ctx())


def test_canary_upload_is_rejected() -> None:
    with pytest.raises(NetworkPolicyViolation, match="read allowlist"):
        enforce(get(f"/v3/company/{REALM_MAIN}/upload"), accounting_ctx())


def test_canary_delete_verb_in_query_is_rejected() -> None:
    with pytest.raises(NetworkPolicyViolation, match="unknown query parameter"):
        enforce(
            get(f"/v3/company/{REALM_MAIN}/query?query=select+1&" + "operation=delete"),
            accounting_ctx(),
        )


def test_canary_void_verb_in_query_is_rejected() -> None:
    with pytest.raises(NetworkPolicyViolation, match="unknown query parameter"):
        enforce(
            get(f"/v3/company/{REALM_MAIN}/query?" + "operation=void"), accounting_ctx()
        )


def test_canary_include_void_is_rejected() -> None:
    with pytest.raises(NetworkPolicyViolation, match="unknown query parameter"):
        enforce(get(f"/v3/company/{REALM_MAIN}/query?include=void"), accounting_ctx())


# --------------------------------------------------------------------------
# URL canonicalisation
# --------------------------------------------------------------------------


def test_realm_substitution_in_the_path_is_rejected() -> None:
    """The realm in the URL must be the realm the call was bound to."""
    with pytest.raises(NetworkPolicyViolation):
        enforce(get(f"/v3/company/{REALM_OPS}/preferences"), accounting_ctx())


def test_companyinfo_with_a_foreign_trailing_realm_is_rejected() -> None:
    with pytest.raises(NetworkPolicyViolation, match="read allowlist"):
        enforce(
            get(f"/v3/company/{REALM_MAIN}/companyinfo/{REALM_OPS}"), accounting_ctx()
        )


def test_userinfo_is_rejected() -> None:
    request = httpx.Request(
        "GET", f"https://user:pass@sandbox-quickbooks.api.intuit.com/v3/company/{REALM_MAIN}/preferences"
    )
    with pytest.raises(NetworkPolicyViolation, match="userinfo"):
        enforce(request, accounting_ctx())


def test_a_different_origin_is_rejected() -> None:
    request = httpx.Request("GET", f"https://evil.example.com/v3/company/{REALM_MAIN}/preferences")
    with pytest.raises(NetworkPolicyViolation):
        enforce(request, accounting_ctx())


def test_a_nonstandard_port_is_rejected() -> None:
    request = httpx.Request(
        "GET", f"https://sandbox-quickbooks.api.intuit.com:8443/v3/company/{REALM_MAIN}/preferences"
    )
    with pytest.raises(NetworkPolicyViolation, match="selected origin"):
        enforce(request, accounting_ctx())


@pytest.mark.parametrize("encoded", ["%2f", "%2F", "%5c", "%2e%2e"])
def test_encoded_path_separators_are_rejected(encoded: str) -> None:
    request = httpx.Request(
        "GET", f"{BASE}/v3/company/{REALM_MAIN}/preferences{encoded}invoice"
    )
    with pytest.raises(NetworkPolicyViolation, match="encoded separator"):
        enforce(request, accounting_ctx())


def test_empty_path_segments_are_rejected() -> None:
    request = httpx.Request("GET", f"{BASE}/v3//company/{REALM_MAIN}/preferences")
    with pytest.raises(NetworkPolicyViolation, match="empty segment"):
        enforce(request, accounting_ctx())


def test_a_fragment_is_rejected() -> None:
    request = httpx.Request("GET", f"{BASE}/v3/company/{REALM_MAIN}/preferences#x")
    with pytest.raises(NetworkPolicyViolation, match="fragment"):
        enforce(request, accounting_ctx())


def test_duplicate_query_parameters_are_rejected() -> None:
    request = httpx.Request(
        "GET", f"{BASE}/v3/company/{REALM_MAIN}/query?query=a&query=b"
    )
    with pytest.raises(NetworkPolicyViolation, match="duplicate query parameter"):
        enforce(request, accounting_ctx())


def test_unknown_query_parameters_are_rejected() -> None:
    request = httpx.Request(
        "GET", f"{BASE}/v3/company/{REALM_MAIN}/query?query=a&sneaky=1"
    )
    with pytest.raises(NetworkPolicyViolation, match="unknown query parameter"):
        enforce(request, accounting_ctx())


def test_an_absolute_user_supplied_tail_is_rejected() -> None:
    with pytest.raises(NetworkPolicyViolation, match="read allowlist"):
        build_accounting_url(BASE, REALM_MAIN, "https://evil.example.com/steal")


def test_a_traversal_tail_is_rejected() -> None:
    with pytest.raises(NetworkPolicyViolation, match="read allowlist"):
        build_accounting_url(BASE, REALM_MAIN, "../../other")


def test_a_non_numeric_realm_is_rejected_at_build_time() -> None:
    with pytest.raises(NetworkPolicyViolation, match="numeric"):
        build_accounting_url(BASE, "abc", "preferences")


def test_accounting_without_a_bound_realm_is_rejected() -> None:
    with pytest.raises(NetworkPolicyViolation, match="bound to a realm"):
        enforce(get(f"/v3/company/{REALM_MAIN}/preferences"), accounting_ctx(realm_id=None))


# --------------------------------------------------------------------------
# OAuth classes
# --------------------------------------------------------------------------


def _refresh_body() -> dict:
    """The body a real refresh carries. A bodiless POST is not a refresh, and
    the body checks below say so."""
    return {
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "content": b"grant_type=refresh_token&refresh_token=r",
    }


def test_token_refresh_may_post_to_the_pinned_endpoint() -> None:
    ctx = PolicyContext(request_class=RequestClass.TOKEN_REFRESH, environment="sandbox")
    enforce(httpx.Request("POST", TOKEN_ENDPOINT, **_refresh_body()), ctx)


def test_token_refresh_may_not_post_elsewhere() -> None:
    ctx = PolicyContext(request_class=RequestClass.TOKEN_REFRESH, environment="sandbox")
    with pytest.raises(NetworkPolicyViolation, match="pinned endpoint"):
        enforce(httpx.Request("POST", "https://oauth.platform.intuit.com/other"), ctx)


def test_token_refresh_may_not_get() -> None:
    ctx = PolicyContext(request_class=RequestClass.TOKEN_REFRESH, environment="sandbox")
    with pytest.raises(NetworkPolicyViolation, match="method"):
        enforce(httpx.Request("GET", TOKEN_ENDPOINT), ctx)


def test_exchange_and_revoke_are_cli_only() -> None:
    exchange_body = {
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "content": (
            b"grant_type=authorization_code&code=c&"
            b"redirect_uri=http%3A%2F%2Flocalhost%3A8765%2Fcallback"
        ),
    }
    revoke_body = {
        "headers": {"Content-Type": "application/json"},
        "content": b'{"token": "refresh-1"}',
    }
    for request_class, url, body in (
        (RequestClass.TOKEN_EXCHANGE, TOKEN_ENDPOINT, exchange_body),
        (RequestClass.TOKEN_REVOKE, REVOKE_ENDPOINT, revoke_body),
    ):
        server_ctx = PolicyContext(
            request_class=request_class, environment="sandbox", from_cli=False
        )
        with pytest.raises(NetworkPolicyViolation, match="auth CLI"):
            enforce(httpx.Request("POST", url, **body), server_ctx)

        cli_ctx = PolicyContext(
            request_class=request_class, environment="sandbox", from_cli=True
        )
        enforce(httpx.Request("POST", url, **body), cli_ctx)


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def test_discovery_is_pinned_per_environment() -> None:
    ctx = PolicyContext(request_class=RequestClass.DISCOVERY, environment="sandbox")
    enforce(httpx.Request("GET", DISCOVERY_URLS["sandbox"]), ctx)
    with pytest.raises(NetworkPolicyViolation, match="pinned endpoint"):
        enforce(httpx.Request("GET", DISCOVERY_URLS["production"]), ctx)


def test_discovery_may_not_carry_a_credential() -> None:
    """Discovery confirms pinned values. It never decides where a secret goes,
    so it has no business holding one."""
    ctx = PolicyContext(request_class=RequestClass.DISCOVERY, environment="sandbox")
    request = httpx.Request(
        "GET", DISCOVERY_URLS["sandbox"], headers={"Authorization": "Bearer x"}
    )
    with pytest.raises(NetworkPolicyViolation, match="authorization header"):
        enforce(request, ctx)


def test_discovery_cannot_rewrite_a_secret_bearing_destination() -> None:
    """The pinned constants are the destination. Nothing reads a token
    endpoint out of a fetched document."""
    import inspect

    from qbo_multicompany_mcp import oauth

    source = inspect.getsource(oauth.confirm_pinned_endpoints)
    assert "return DiscoveryCheck" in source
    # The function's only outputs are booleans and field names.
    check = oauth.DiscoveryCheck(reached=True, matches=False, differences=("token_endpoint",))
    assert not any(value.startswith("http") for value in check.differences)


# --------------------------------------------------------------------------
# Attachments
# --------------------------------------------------------------------------


def test_attachments_are_disabled_by_default() -> None:
    ctx = PolicyContext(request_class=RequestClass.ATTACHMENT, environment="sandbox")
    with pytest.raises(NetworkPolicyViolation, match="--allow-documents"):
        enforce(httpx.Request("GET", "https://example.invalid/doc"), ctx)


@pytest.mark.parametrize(
    "header", ["Authorization", "Cookie", "Proxy-Authorization"]
)
def test_attachments_never_carry_credentials(header: str) -> None:
    """An attachment URL is pre-signed and often points at third-party storage.
    Attaching this app's bearer token would hand the credential away."""
    ctx = PolicyContext(
        request_class=RequestClass.ATTACHMENT, environment="sandbox", allow_documents=True,
        attachment_hosts=("example.invalid",),
    )
    request = httpx.Request("GET", "https://example.invalid/doc", headers={header: "x"})
    with pytest.raises(NetworkPolicyViolation, match="header where none is permitted"):
        enforce(request, ctx)


def test_attachment_get_without_credentials_to_a_pinned_host_is_permitted() -> None:
    ctx = PolicyContext(
        request_class=RequestClass.ATTACHMENT, environment="sandbox", allow_documents=True,
        attachment_hosts=("example.invalid",),
    )
    enforce(httpx.Request("GET", "https://example.invalid/doc?Expires=1&Signature=opaque"), ctx)


def test_attachment_get_to_an_unpinned_host_is_refused_and_no_host_pinned_refuses_all() -> None:
    """Only a pinned host is admitted; with none pinned, nothing is."""
    pinned = PolicyContext(
        request_class=RequestClass.ATTACHMENT, environment="sandbox", allow_documents=True,
        attachment_hosts=("example.invalid",),
    )
    with pytest.raises(NetworkPolicyViolation, match="not pinned"):
        enforce(httpx.Request("GET", "https://other.example/doc"), pinned)
    with pytest.raises(NetworkPolicyViolation, match="IP literal"):
        enforce(httpx.Request("GET", "https://203.0.113.9/doc"), pinned)
    nothing = PolicyContext(request_class=RequestClass.ATTACHMENT, environment="sandbox", allow_documents=True)
    with pytest.raises(NetworkPolicyViolation, match="QBO_ATTACHMENT_HOSTS"):
        enforce(httpx.Request("GET", "https://example.invalid/doc"), nothing)


def test_a_loopback_api_base_frees_the_port_for_a_loopback_literal_only() -> None:
    """The test setup's mock listens on whatever port it chose. A pinned name
    keeps the default port even then, as it does in every other mode."""
    ctx = PolicyContext(
        request_class=RequestClass.ATTACHMENT, environment="sandbox", allow_documents=True,
        attachment_hosts=("example.invalid", "127.0.0.1"), api_base=LOOPBACK,
    )
    enforce(httpx.Request("GET", "http://127.0.0.1:8443/doc"), ctx)
    with pytest.raises(NetworkPolicyViolation, match="port other than"):
        enforce(httpx.Request("GET", "https://example.invalid:8443/doc"), ctx)


# --------------------------------------------------------------------------
# The send path itself
#
# Everything above calls `enforce` directly, which proves the policy and
# nothing about where it is wired: delete the `enforce` call from `send` and
# those canaries still pass, because they never use `send`. These tests close
# that: a prohibited request goes through the real `send`, to a real httpx
# client, and the transport must never see it.
# --------------------------------------------------------------------------


def _recording_client() -> tuple[httpx.Client, list[httpx.Request]]:
    """A real httpx.Client whose transport records everything that reaches it."""
    reached: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reached.append(request)
        return httpx.Response(200, json={}, request=request)

    return httpx.Client(transport=httpx.MockTransport(handler)), reached


PROHIBITED_THROUGH_SEND = [
    # The six canaries, as whole requests this time.
    ("invoice send", "GET", f"/v3/company/{REALM_MAIN}/invoice/1/send", None),
    ("batch", "GET", f"/v3/company/{REALM_MAIN}/" + "batch", None),
    ("upload", "GET", f"/v3/company/{REALM_MAIN}/upload", None),
    ("delete verb", "GET", f"/v3/company/{REALM_MAIN}/query",
     {"query": "select 1", "operation": "delete"}),
    ("void verb", "GET", f"/v3/company/{REALM_MAIN}/query", {"operation": "void"}),
    ("include void", "GET", f"/v3/company/{REALM_MAIN}/query", {"include": "void"}),
    # A write method against a path that IS on the allowlist.
    ("post to an allowlisted path", "POST", f"/v3/company/{REALM_MAIN}/preferences", None),
    ("delete an allowlisted path", "DELETE", f"/v3/company/{REALM_MAIN}/preferences", None),
    # Another company's realm, with this call bound to REALM_MAIN.
    ("foreign realm", "GET", f"/v3/company/{REALM_OPS}/preferences", None),
    # A path that is simply not on the read allowlist: the FR-locale trial
    # balance on Intuit's TrialBalance page, a documented report this build
    # serves no tool for.
    ("unlisted endpoint", "GET", f"/v3/company/{REALM_MAIN}/reports/TrialBalanceFR", None),
    # A report that IS allowlisted, carrying a parameter Intuit does not
    # document for it: the per-tail query allowlist refuses it.
    ("foreign parameter on an allowlisted report", "GET",
     f"/v3/company/{REALM_MAIN}/reports/ProfitAndLoss", {"qzurl": "true"}),
    ("aging_method on the AP detail report", "GET",
     f"/v3/company/{REALM_MAIN}/reports/AgedPayableDetail", {"aging_method": "Current"}),
    ("accounting_method on the AR detail report", "GET",
     f"/v3/company/{REALM_MAIN}/reports/AgedReceivableDetail", {"accounting_method": "Cash"}),
    # BalanceSheet's documented parameters have no `columns`.
    ("columns on the balance sheet", "GET",
     f"/v3/company/{REALM_MAIN}/reports/BalanceSheet", {"columns": "account"}),
    # Intuit's page lists `columns` for TransactionListByCustomer, and this
    # server sends none there: Intuit faults on any value (SystemFault 10000,
    # observed in production 2026-09-28).
    ("columns on TransactionListByCustomer", "GET",
     f"/v3/company/{REALM_MAIN}/reports/TransactionListByCustomer",
     {"start_date": "2026-01-01", "end_date": "2026-06-30", "columns": "tx_date"}),
]


@pytest.mark.parametrize(
    ("label", "method", "path", "params"),
    PROHIBITED_THROUGH_SEND,
    ids=[case[0] for case in PROHIBITED_THROUGH_SEND],
)
def test_a_prohibited_request_never_reaches_the_transport(
    label: str, method: str, path: str, params: dict[str, str] | None
) -> None:
    client, reached = _recording_client()
    request = client.build_request(
        method, f"{BASE}{path}", params=params, headers={"Authorization": "Bearer x"}
    )
    with client:
        with pytest.raises(NetworkPolicyViolation):
            send(client, request, accounting_ctx())
    assert reached == [], f"{label}: the request was sent"


def test_the_transport_is_reachable_at_all() -> None:
    """The control for the test above.

    Without this, a transport that could never be reached -- a typo in the
    mock, a client that refuses every request -- would make every case pass
    for the wrong reason.
    """
    client, reached = _recording_client()
    url = build_accounting_url(BASE, REALM_MAIN, "preferences")
    request = client.build_request("GET", url, headers={"Authorization": "Bearer x"})
    with client:
        response = send(client, request, accounting_ctx())
    assert response.status_code == 200
    assert len(reached) == 1


def test_a_token_post_to_an_unpinned_endpoint_never_reaches_the_transport() -> None:
    client, reached = _recording_client()
    request = client.build_request("POST", "https://oauth.example.invalid/token", data={})
    ctx = PolicyContext(request_class=RequestClass.TOKEN_REFRESH, environment="sandbox")
    with client:
        with pytest.raises(NetworkPolicyViolation, match="pinned endpoint"):
            send(client, request, ctx)
    assert reached == []


def test_an_attachment_carrying_a_bearer_never_reaches_the_transport() -> None:
    """The one that hands this app's credential to a third party if it escapes."""
    client, reached = _recording_client()
    request = client.build_request(
        "GET", "https://storage.example.invalid/doc.pdf", headers={"Authorization": "Bearer x"}
    )
    ctx = PolicyContext(
        request_class=RequestClass.ATTACHMENT, environment="sandbox", allow_documents=True,
        attachment_hosts=("storage.example.invalid",),
    )
    with client:
        with pytest.raises(NetworkPolicyViolation, match="authorization"):
            send(client, request, ctx)
    assert reached == []


def test_a_discovery_request_to_an_unpinned_url_never_reaches_the_transport() -> None:
    """Discovery is the fourth send boundary, and the one with the worst failure.

    Runtime checks belong at *every* real send boundary, not at a list of
    request shapes. Discovery may report that a pinned endpoint has drifted; it
    may never supply one, because a single spoofed response would otherwise
    redirect every credential this server holds.
    """
    client, reached = _recording_client()
    request = client.build_request("GET", "https://discovery.example.invalid/.well-known/config")
    ctx = PolicyContext(request_class=RequestClass.DISCOVERY, environment="sandbox")
    with client:
        with pytest.raises(NetworkPolicyViolation, match="pinned endpoint"):
            send(client, request, ctx)
    assert reached == []


def test_a_discovery_request_carrying_a_credential_never_reaches_the_transport() -> None:
    client, reached = _recording_client()
    request = client.build_request(
        "GET", DISCOVERY_URLS["sandbox"], headers={"Authorization": "Bearer x"}
    )
    ctx = PolicyContext(request_class=RequestClass.DISCOVERY, environment="sandbox")
    with client:
        with pytest.raises(NetworkPolicyViolation, match="authorization"):
            send(client, request, ctx)
    assert reached == []


def test_the_accounting_client_refuses_a_disallowed_origin_before_sending(
    seeded_store, settings, registry
) -> None:
    """The same guarantee one level up, through the client callers actually use.

    `send` is where the check lives, but nothing above proves the client goes
    through `send` rather than round its side.
    """
    from dataclasses import replace as dc_replace

    from qbo_multicompany_mcp.client import AccountingClient

    http, reached = _recording_client()
    elsewhere = dc_replace(settings, api_base="https://not-intuit.example.invalid")
    with AccountingClient(elsewhere, seeded_store, http=http) as client:
        with pytest.raises(NetworkPolicyViolation, match="disallowed API base"):
            client.get(registry["acme_main"], "preferences")
    assert reached == [], "a bearer token was sent to an origin the policy refuses"


# --------------------------------------------------------------------------
# Redirects
# --------------------------------------------------------------------------


def test_redirects_are_disabled_on_credential_bearing_requests() -> None:
    """A redirect would forward the Authorization header to a host the policy
    never approved, and httpx would re-send the body too."""
    captured: dict[str, object] = {}

    class FakeClient:
        def send(self, request, follow_redirects=True, stream=False):
            captured["follow_redirects"] = follow_redirects
            captured["stream"] = stream
            return httpx.Response(200, request=request)

    url = build_accounting_url(BASE, REALM_MAIN, "preferences")
    request = httpx.Request("GET", url, headers={"Authorization": "Bearer x"})
    send(FakeClient(), request, accounting_ctx())
    assert captured["follow_redirects"] is False
    # Streamed, so the body can be capped as it arrives rather than after.
    assert captured["stream"] is True


def test_a_redirect_comes_back_as_itself_and_nothing_is_sent_again() -> None:
    """With redirects off a 3xx is the answer, and its Location is never
    contacted. httpx's own client ties a response to the request it sent, so
    no response can arrive for another URL."""
    seen: list[str] = []

    def redirecting(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://evil.example.com/"})

    url = build_accounting_url(BASE, REALM_MAIN, "preferences")
    with httpx.Client(transport=httpx.MockTransport(redirecting)) as client:
        response = send(client, httpx.Request("GET", url), accounting_ctx())
    assert response.status_code == 302
    assert seen == [url]


# --------------------------------------------------------------------------
# Default deny
# --------------------------------------------------------------------------


def test_an_unclassified_request_is_denied() -> None:
    ctx = PolicyContext(request_class="something_new", environment="sandbox")  # type: ignore[arg-type]
    with pytest.raises(NetworkPolicyViolation, match="no policy for request class"):
        enforce(httpx.Request("GET", "https://example.invalid/"), ctx)


# --------------------------------------------------------------------------
# --api-base
# --------------------------------------------------------------------------


@pytest.mark.parametrize("environment", ["sandbox", "production"])
def test_each_intuit_host_is_accepted_in_its_own_environment(environment: str) -> None:
    """Each environment's own base, as Intuit documents it. The other
    environment's host is refused below."""
    base = API_BASES[environment]
    assert validate_api_base(base, environment, allow_custom=False) == base


def test_loopback_mock_is_accepted() -> None:
    assert validate_api_base("http://127.0.0.1:9999", "sandbox", allow_custom=False)


def test_other_hosts_need_the_override() -> None:
    with pytest.raises(StartupError, match="--allow-custom-api-base"):
        validate_api_base("https://proxy.example.com", "sandbox", allow_custom=False)
    assert validate_api_base("https://proxy.example.com", "sandbox", allow_custom=True)


def test_a_custom_base_must_still_be_https() -> None:
    with pytest.raises(StartupError, match="https"):
        validate_api_base("http://proxy.example.com", "sandbox", allow_custom=True)


def test_api_base_must_be_an_origin() -> None:
    with pytest.raises(StartupError, match="origin"):
        validate_api_base(f"{BASE}/v3/company", "sandbox", allow_custom=False)


def test_api_base_must_not_carry_userinfo() -> None:
    with pytest.raises(StartupError, match="userinfo"):
        validate_api_base("https://a:b@quickbooks.api.intuit.com", "sandbox", allow_custom=True)


# --------------------------------------------------------------------------
# Test-only origins carry test-only credentials
# --------------------------------------------------------------------------
#
# A custom or loopback API base exists for tests, so the credential it receives
# must be synthetic and a real one never goes there. The
# `--allow-custom-api-base` flag cannot ensure that alone. With a custom base
# admitted on the boolean and a loopback base on nothing at all, the client
# attaches whatever access token it was handed, and a flag an operator sets once
# cannot tell a real Intuit token from a fixture's.

LOOPBACK = "http://127.0.0.1:9999"
CUSTOM = "https://qbo.internal.example"

#: Synthetic, in the shape of a real one: long, mixed case, and not a word anybody typed.
REAL_SHAPED_ACCESS_TOKEN = (
    "eyJlbmMiOiJBMTI4Q0JDLUhTMjU2IiwiYWxnIjoiZGlyIn0..8Xq2pLrNfV1wKcTb"
    "QzYmJA.7HsWnE3vRtYuIoPaSdFgHjKlZxCvBnM"
)


def _loopback_get(base: str, token: str | None) -> httpx.Request:
    url = build_accounting_url(base, REALM_MAIN, "preferences")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Request("GET", url, headers=headers)


@pytest.mark.parametrize(
    ("base", "allow_custom"),
    [
        pytest.param(LOOPBACK, False, id="loopback"),
        pytest.param(CUSTOM, True, id="custom-with-the-flag"),
    ],
)
def test_a_real_credential_never_reaches_a_test_only_origin(
    base: str, allow_custom: bool
) -> None:
    request = _loopback_get(base, REAL_SHAPED_ACCESS_TOKEN)
    ctx = accounting_ctx(api_base=base, allow_custom_api_base=allow_custom)
    with pytest.raises(NetworkPolicyViolation, match="synthetic"):
        enforce(request, ctx)


@pytest.mark.parametrize(
    ("base", "allow_custom"),
    [
        pytest.param(LOOPBACK, False, id="loopback"),
        pytest.param(CUSTOM, True, id="custom-with-the-flag"),
    ],
)
def test_a_synthetic_credential_is_what_a_test_only_origin_may_have(
    base: str, allow_custom: bool
) -> None:
    """The control. Without it the test above would pass on a policy that
    refuses every request to a loopback base, which would break the suite's own
    mock and prove nothing about credentials."""
    request = _loopback_get(base, "access-1")
    enforce(request, accounting_ctx(api_base=base, allow_custom_api_base=allow_custom))


def test_the_real_intuit_origin_still_takes_a_real_credential() -> None:
    """The other control: this condition applies to test-only origins only."""
    url = build_accounting_url(BASE, REALM_MAIN, "preferences")
    request = httpx.Request("GET", url, headers={"Authorization": f"Bearer {REAL_SHAPED_ACCESS_TOKEN}"})
    enforce(request, accounting_ctx())


# --------------------------------------------------------------------------
# OAuth is a method, path AND body policy
# --------------------------------------------------------------------------
#
# Each OAuth request is checked by method, path and body; no module gets a
# blanket pass. With the body unread, a refresh-class POST to the pinned
# endpoint carrying an unrelated grant passes `enforce`.


def _form_post(body: str, url: str = TOKEN_ENDPOINT) -> httpx.Request:
    return httpx.Request(
        "POST", url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        content=body.encode(),
    )


def refresh_ctx(**overrides) -> PolicyContext:
    defaults = dict(request_class=RequestClass.TOKEN_REFRESH, environment="sandbox")
    defaults.update(overrides)
    return PolicyContext(**defaults)


def test_a_refresh_post_carrying_another_grant_type_is_rejected() -> None:
    request = _form_post("grant_type=password&username=x&password=y")
    with pytest.raises(NetworkPolicyViolation, match="body"):
        enforce(request, refresh_ctx())


def test_a_refresh_post_carrying_an_extra_form_field_is_rejected() -> None:
    request = _form_post("grant_type=refresh_token&refresh_token=r&scope=com.intuit.quickbooks.accounting")
    with pytest.raises(NetworkPolicyViolation, match="body"):
        enforce(request, refresh_ctx())


def test_a_refresh_post_with_a_duplicated_field_is_rejected() -> None:
    request = _form_post("grant_type=refresh_token&refresh_token=r&refresh_token=other")
    with pytest.raises(NetworkPolicyViolation, match="body"):
        enforce(request, refresh_ctx())


def test_a_refresh_post_with_no_body_is_rejected() -> None:
    with pytest.raises(NetworkPolicyViolation, match="body"):
        enforce(_form_post(""), refresh_ctx())


def test_the_real_refresh_body_is_permitted() -> None:
    enforce(_form_post("grant_type=refresh_token&refresh_token=r"), refresh_ctx())


def test_an_exchange_post_may_not_use_the_refresh_grant() -> None:
    request = _form_post("grant_type=refresh_token&refresh_token=r")
    ctx = PolicyContext(
        request_class=RequestClass.TOKEN_EXCHANGE, environment="sandbox", from_cli=True
    )
    with pytest.raises(NetworkPolicyViolation, match="body"):
        enforce(request, ctx)


def test_the_real_exchange_body_is_permitted() -> None:
    ctx = PolicyContext(
        request_class=RequestClass.TOKEN_EXCHANGE, environment="sandbox", from_cli=True
    )
    enforce(
        _form_post("grant_type=authorization_code&code=c&redirect_uri=http%3A%2F%2Flocalhost%3A8765%2Fcallback"),
        ctx,
    )
    enforce(
        _form_post(
            "grant_type=authorization_code&code=c&"
            "redirect_uri=http%3A%2F%2Flocalhost%3A8765%2Fcallback&code_verifier=v"
        ),
        ctx,
    )


def test_a_revoke_body_may_carry_only_the_token() -> None:
    ctx = PolicyContext(
        request_class=RequestClass.TOKEN_REVOKE, environment="sandbox", from_cli=True
    )
    good = httpx.Request(
        "POST", REVOKE_ENDPOINT,
        headers={"Content-Type": "application/json"},
        content=b'{"token": "refresh-1"}',
    )
    enforce(good, ctx)

    bad = httpx.Request(
        "POST", REVOKE_ENDPOINT,
        headers={"Content-Type": "application/json"},
        content=b'{"token": "refresh-1", "client_id": "leaked"}',
    )
    with pytest.raises(NetworkPolicyViolation, match="body"):
        enforce(bad, ctx)


@pytest.mark.parametrize(
    ("base", "allow_custom"),
    [
        pytest.param("http://127.0.0.1:9", False, id="loopback"),
        pytest.param("https://qbo.internal.example", True, id="custom-with-the-flag"),
    ],
)
def test_a_real_stored_credential_is_not_sent_to_a_test_only_origin(
    store, settings, registry, actor, base: str, allow_custom: bool
) -> None:
    """The same rule one level up: a real-shaped token in the store, and a
    client pointed at a test-only base. Nothing reaches the transport."""
    from dataclasses import replace as dc_replace

    from qbo_multicompany_mcp.client import AccountingClient

    from .conftest import make_token_response

    store.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN,
        response=make_token_response(access_token=REAL_SHAPED_ACCESS_TOKEN), actor=actor,
    )
    http, reached = _recording_client()
    pointed = dc_replace(settings, api_base=base, allow_custom_api_base=allow_custom)
    with AccountingClient(pointed, store, http=http) as client:
        with pytest.raises(NetworkPolicyViolation, match="synthetic"):
            client.get(registry["acme_main"], "preferences")
    assert reached == [], "a real credential was sent to a test-only origin"


# --------------------------------------------------------------------------
# The shape rule is the fallback, not the defence
# --------------------------------------------------------------------------
#
# The shape rule rejects every real Intuit token shape it has been tested on,
# and admits a UUID: lowercase hex and hyphens, 36 characters, no run over 12.
# So two legs that do not depend on how a token looks sit in front of it.
# Leg 1: a test-only base is refused in production, whatever the credential.
# Leg 2: a value the store actually holds never reaches a test-only base,
# matched by identity through the live-value registry.

#: Admitted by the shape rule. The point of these tests is that it is refused
#: anyway, by a leg that never looks at its shape. Generated rather than
#: written down: a literal would be a credential-shaped value in the tree for
#: the scanner to find, and a fresh one each run is the stronger test.
UUID_TOKEN = str(uuid.uuid4())


def test_the_shape_rule_admits_a_uuid() -> None:
    """The control for everything below: a UUID passes the shape leg alone."""
    from qbo_multicompany_mcp.netpolicy import _looks_synthetic

    assert _looks_synthetic(UUID_TOKEN)


@pytest.mark.parametrize(
    ("base", "allow_custom"),
    [
        pytest.param(LOOPBACK, False, id="loopback"),
        pytest.param(CUSTOM, True, id="custom-with-the-flag"),
    ],
)
@pytest.mark.parametrize("token", ["access-1", UUID_TOKEN, None], ids=["synthetic", "uuid", "none"])
def test_production_refuses_a_test_only_base_whatever_the_credential(
    base: str, allow_custom: bool, token: str | None
) -> None:
    request = _loopback_get(base, token)
    ctx = accounting_ctx(
        environment="production", api_base=base, allow_custom_api_base=allow_custom
    )
    with pytest.raises(NetworkPolicyViolation, match="production"):
        enforce(request, ctx)


def test_production_still_reaches_the_real_production_origin() -> None:
    """The control for leg 1: it is about test-only bases, not production."""
    base = API_BASES["production"]
    url = build_accounting_url(base, REALM_MAIN, "preferences")
    request = httpx.Request("GET", url, headers={"Authorization": f"Bearer {REAL_SHAPED_ACCESS_TOKEN}"})
    enforce(request, accounting_ctx(environment="production", api_base=base))


@pytest.mark.parametrize("base", [LOOPBACK, "http://localhost:9999"])
def test_production_with_a_test_only_base_fails_at_startup(base: str) -> None:
    with pytest.raises(StartupError, match="production"):
        validate_api_base(base, "production", allow_custom=False)
    with pytest.raises(StartupError, match="production"):
        validate_api_base(CUSTOM, "production", allow_custom=True)


# --------------------------------------------------------------------------
# An Intuit host only for its own environment
# --------------------------------------------------------------------------
#
# If either member of API_BASES passes at startup, and both origins count as
# "real" at send time, `--api-base https://sandbox-quickbooks.api.intuit.com`
# in production sends a production bearer token to the sandbox host. Intuit
# names one base per environment; a token goes to its own environment's host
# and no other, in both directions, whatever the token looks like and whatever
# --allow-custom-api-base says.

#: Every leg and direction: the running environment, and the OTHER one's host.
CROSSED = [
    pytest.param("production", API_BASES["sandbox"], id="production-to-the-sandbox-host"),
    pytest.param("sandbox", API_BASES["production"], id="sandbox-to-the-production-host"),
]


@pytest.mark.parametrize(("environment", "base"), CROSSED)
@pytest.mark.parametrize("allow_custom", [False, True], ids=["plain", "custom-flag"])
def test_startup_refuses_the_other_environments_intuit_host(
    environment: str, base: str, allow_custom: bool
) -> None:
    with pytest.raises(StartupError, match="other environment"):
        validate_api_base(base, environment, allow_custom=allow_custom)


@pytest.mark.parametrize(("environment", "base"), CROSSED)
@pytest.mark.parametrize("allow_custom", [False, True], ids=["plain", "custom-flag"])
@pytest.mark.parametrize("token", [REAL_SHAPED_ACCESS_TOKEN, "access-1"], ids=["real-shaped", "synthetic"])
def test_send_time_refuses_the_other_environments_intuit_host(
    environment: str, base: str, allow_custom: bool, token: str
) -> None:
    """The send-time half: a context carrying the crossed pair -- however it
    was built -- is refused before the request leaves, whatever the token's
    shape. A synthetic token is refused too: the rule is about the host, not
    about the credential, so no shape can buy a crossing."""
    url = build_accounting_url(base, REALM_MAIN, "preferences")
    request = httpx.Request("GET", url, headers={"Authorization": f"Bearer {token}"})
    ctx = accounting_ctx(environment=environment, api_base=base, allow_custom_api_base=allow_custom)
    with pytest.raises(NetworkPolicyViolation, match="other environment"):
        enforce(request, ctx)


@pytest.mark.parametrize("environment", ["sandbox", "production"])
def test_each_environment_still_reaches_its_own_intuit_host(environment: str) -> None:
    """The control for both halves: the rule refuses the crossing, not Intuit."""
    base = API_BASES[environment]
    assert validate_api_base(base, environment, allow_custom=False) == base
    url = build_accounting_url(base, REALM_MAIN, "preferences")
    request = httpx.Request("GET", url, headers={"Authorization": f"Bearer {REAL_SHAPED_ACCESS_TOKEN}"})
    enforce(request, accounting_ctx(environment=environment, api_base=base))


def _live_pair(access: str, refresh: str):
    from datetime import UTC, datetime, timedelta

    from qbo_multicompany_mcp import oauth

    now = datetime.now(UTC)
    return oauth.TokenResponse(
        access_token=access,
        refresh_token=refresh,
        access_token_expires_at=now + timedelta(hours=1),
        refresh_token_expires_at=now + timedelta(days=100),
    )


@pytest.mark.parametrize(
    ("base", "allow_custom"),
    [
        pytest.param("http://127.0.0.1:9", False, id="loopback"),
        pytest.param("https://qbo.internal.example", True, id="custom-with-the-flag"),
    ],
)
def test_a_uuid_the_store_holds_never_reaches_a_test_only_base(
    store, settings, registry, actor, base: str, allow_custom: bool
) -> None:
    """Leg 2, end to end. The stored access token is UUID-shaped, so the shape
    rule would let it through; the store holds it, so identity refuses it."""
    from dataclasses import replace as dc_replace

    from qbo_multicompany_mcp.client import AccountingClient

    store.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN,
        response=_live_pair(UUID_TOKEN, str(uuid.uuid4())),
        actor=actor,
    )
    http, reached = _recording_client()
    pointed = dc_replace(settings, api_base=base, allow_custom_api_base=allow_custom)
    with AccountingClient(pointed, store, http=http) as client:
        with pytest.raises(NetworkPolicyViolation, match="holds"):
            client.get(registry["acme_main"], "preferences")
    assert reached == [], "a credential the store holds was sent to a test-only base"


def test_identity_refuses_a_held_value_anywhere_in_the_request() -> None:
    """Not only in the Authorization header: a held value in the URL is the
    same value reaching the same origin."""
    from qbo_multicompany_mcp.store import AppCredentials

    held = "zq-held-client-secret-in-a-query-77"
    AppCredentials(client_id="x", client_secret=held)
    url = build_accounting_url(LOOPBACK, REALM_MAIN, "query")
    request = httpx.Request("GET", url, params={"query": f"select * from Vendor where x = '{held}'"})
    with pytest.raises(NetworkPolicyViolation, match="holds"):
        enforce(request, accounting_ctx(api_base=LOOPBACK))


def test_a_uuid_nobody_holds_is_still_admitted_by_the_fallback() -> None:
    """The control for leg 2: it refuses by identity, not by shape. An
    unregistered UUID reaches a loopback base because only the shape rule is
    left to judge it -- which is exactly why the shape rule is the fallback."""
    unheld = str(uuid.uuid4())
    enforce(_loopback_get(LOOPBACK, unheld), accounting_ctx(api_base=LOOPBACK))


# --------------------------------------------------------------------------
# The identity leg does not depend on length
# --------------------------------------------------------------------------

#: Eight characters: under the sixteen-character floor text scanning keeps,
#: admitted by the shape rule, and not marked synthetic -- a live value as far
#: as this process can tell. Generated so no credential-shaped literal sits in
#: the tree; short so the length-independent leg is what refuses it.
SHORT_LIVE_TOKEN = uuid.uuid4().hex[:8]


def test_the_shape_rule_admits_the_short_token() -> None:
    """The control: only identity can refuse it."""
    from qbo_multicompany_mcp.netpolicy import _looks_synthetic

    assert _looks_synthetic(SHORT_LIVE_TOKEN)


@pytest.mark.parametrize(
    ("base", "allow_custom"),
    [
        pytest.param("http://127.0.0.1:9", False, id="loopback"),
        pytest.param("https://qbo.internal.example", True, id="custom-with-the-flag"),
    ],
)
def test_a_short_value_the_store_holds_never_reaches_a_test_only_base(
    store, settings, registry, actor, base: str, allow_custom: bool
) -> None:
    """Leg 2 covers any value the store holds, at any length. A registry that
    skips short values leaves a short live token neither redacted by identity
    nor refused here, and it reaches a loopback mock. Identity is exact and
    length-blind; whatever floor TEXT redaction keeps is a false-positive
    policy for redaction alone."""
    from dataclasses import replace as dc_replace

    from qbo_multicompany_mcp.client import AccountingClient

    store.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN,
        response=_live_pair(SHORT_LIVE_TOKEN, uuid.uuid4().hex[:8]),
        actor=actor,
    )
    http, reached = _recording_client()
    pointed = dc_replace(settings, api_base=base, allow_custom_api_base=allow_custom)
    with AccountingClient(pointed, store, http=http) as client:
        with pytest.raises(NetworkPolicyViolation, match="holds"):
            client.get(registry["acme_main"], "preferences")
    assert reached == [], "a short credential the store holds reached a test-only base"


# --------------------------------------------------------------------------
# OAuth headers are enumerated too
# --------------------------------------------------------------------------
#
# Headers follow the same principle as method, path and body. A token request
# carries exactly the headers the OAuth builders put on it --
# and the hard-expiry opt-in only on the two calls that issue a token -- and
# nothing else: no cookie, no method override, no proxy credential, no
# bearer where the app's Basic credential belongs.

from qbo_multicompany_mcp import oauth  # noqa: E402
from qbo_multicompany_mcp.constants import (  # noqa: E402
    HARD_EXPIRY_OPT_IN_HEADER,
    HARD_EXPIRY_OPT_IN_VALUE,
)

#: What the builders send, by name, as observed through a recording
#: transport. httpx adds Host, Accept-Encoding, Connection and
#: Content-Length itself.
BUILDER_HEADERS = {
    "host", "accept-encoding", "connection", "accept", "user-agent",
    "content-type", "content-length", "authorization",
}

_HEADER_APP = dict(client_id="test-client-id", client_secret="test-client-secret", environment="sandbox")

#: Plainly synthetic, distinct from every other fixture value (a pair received
#: from a mock token endpoint is registered as live), and held in names so the
#: secret scanner reads references, not literals (.gitleaks.toml: reshape first).
_HDR_ACCESS = "zq-hdr-access-1"
_HDR_REFRESH = "zq-hdr-refresh-1"
_HDR_SUBMITTED = "zq-hdr-submitted-1"
_HDR_REVOKED = "zq-hdr-revoked-1"


def _recorded_builder_requests() -> dict[RequestClass, httpx.Request]:
    """One request of each OAuth class, as the real builders make it, having
    passed the real send boundary."""
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if str(request.url) == REVOKE_ENDPOINT:
            return httpx.Response(200, request=request)
        return httpx.Response(200, request=request, json={
            "access_token": _HDR_ACCESS,
            "refresh_token": _HDR_REFRESH,
            "expires_in": 3600, "x_refresh_token_expires_in": 8640000,
        })

    with httpx.Client(transport=httpx.MockTransport(answer)) as client:
        oauth.refresh_tokens(refresh_token=_HDR_SUBMITTED, client=client, **_HEADER_APP)
        oauth.exchange_code(code="zq-header-policy-code", redirect_uri="http://localhost:8765/callback",
                            code_verifier="v", client=client, **_HEADER_APP)
        oauth.revoke_token(token=_HDR_REVOKED, client=client, **_HEADER_APP)
    refresh, exchange, revoke = sent
    return {
        RequestClass.TOKEN_REFRESH: refresh,
        RequestClass.TOKEN_EXCHANGE: exchange,
        RequestClass.TOKEN_REVOKE: revoke,
    }


def _ctx(request_class: RequestClass) -> PolicyContext:
    return PolicyContext(
        request_class=request_class, environment="sandbox",
        from_cli=request_class is not RequestClass.TOKEN_REFRESH,
    )


def _with_headers(request: httpx.Request, changes: dict[str, str | None]) -> httpx.Request:
    """``request`` rebuilt with header ``changes`` (None removes one)."""
    headers = [(k, v) for k, v in request.headers.multi_items() if k.lower() not in {c.lower() for c in changes}]
    headers += [(k, v) for k, v in changes.items() if v is not None]
    return httpx.Request(request.method, request.url, headers=headers, content=request.content)


def test_each_builder_sends_exactly_the_enumerated_headers() -> None:
    """The set the policy admits is the set the builders send, and the opt-in
    rides only on the two calls that issue a token."""
    requests = _recorded_builder_requests()
    for request_class, request in requests.items():
        names = {name.lower() for name in request.headers}
        issues_a_token = request_class is not RequestClass.TOKEN_REVOKE
        expected = BUILDER_HEADERS | ({HARD_EXPIRY_OPT_IN_HEADER} if issues_a_token else set())
        assert names == expected, request_class
        enforce(request, _ctx(request_class))


@pytest.mark.parametrize("request_class", [
    RequestClass.TOKEN_REFRESH, RequestClass.TOKEN_EXCHANGE, RequestClass.TOKEN_REVOKE,
])
@pytest.mark.parametrize("header", [
    ("Cookie", "session=zq"),
    ("X-HTTP-Method-Override", "DELETE"),
    ("Proxy-Authorization", "Basic eg=="),
    ("X-Forwarded-Host", "attacker.example"),
    ("Intuit-Tid", "gw-zq"),
])
def test_a_token_request_carrying_an_unlisted_header_is_refused(request_class, header) -> None:
    request = _with_headers(_recorded_builder_requests()[request_class], {header[0]: header[1]})
    with pytest.raises(NetworkPolicyViolation, match="header"):
        enforce(request, _ctx(request_class))


def test_the_opt_in_header_is_refused_on_a_revocation() -> None:
    revoke = _recorded_builder_requests()[RequestClass.TOKEN_REVOKE]
    request = _with_headers(revoke, {HARD_EXPIRY_OPT_IN_HEADER: HARD_EXPIRY_OPT_IN_VALUE})
    with pytest.raises(NetworkPolicyViolation, match="header"):
        enforce(request, _ctx(RequestClass.TOKEN_REVOKE))


@pytest.mark.parametrize(("name", "value"), [
    (HARD_EXPIRY_OPT_IN_HEADER, "false"),
    ("Authorization", "Bearer zq-an-access-token"),
    ("User-Agent", "something-else"),
    ("Accept", "text/html"),
    ("Host", "attacker.example"),
    ("Content-Length", "1"),
])
def test_an_enumerated_header_with_a_value_the_builders_never_send_is_refused(name, value) -> None:
    refresh = _recorded_builder_requests()[RequestClass.TOKEN_REFRESH]
    request = _with_headers(refresh, {name: value})
    with pytest.raises(NetworkPolicyViolation, match="header"):
        enforce(request, _ctx(RequestClass.TOKEN_REFRESH))


@pytest.mark.parametrize("name", ["Accept", "Connection"])
def test_a_duplicated_token_request_header_is_refused(name: str) -> None:
    """Connection carries no pinned value, so only the duplicate check can
    refuse its repeat: an Accept repeat alone would also fail the value pin
    (httpx joins the two), which hid this check's deletion from the gate."""
    refresh = _recorded_builder_requests()[RequestClass.TOKEN_REFRESH]
    headers = [*refresh.headers.multi_items(), (name, refresh.headers[name])]
    request = httpx.Request("POST", refresh.url, headers=headers, content=refresh.content)
    with pytest.raises(NetworkPolicyViolation, match="duplicated header"):
        enforce(request, _ctx(RequestClass.TOKEN_REFRESH))
