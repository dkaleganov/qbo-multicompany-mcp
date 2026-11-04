"""Proves the consent flow cannot be hijacked, raced, or pointed at the wrong
company.

Anything on the machine can reach loopback. The flow's defences are: a 32-byte
single-use state compared in constant time, a callback slot that an invalid
request cannot consume, a realm comparison that happens before any token
exchange, and a code that is exchanged at most once.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shlex
import socket
import threading
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from qbo_multicompany_mcp import auth_cli, callback_server, crypto, oauth
from qbo_multicompany_mcp.callback_server import (
    Attempt,
    AttemptAlreadyUsed,
    CallbackParams,
    CallbackRejected,
    LoopbackReceiver,
    parse_callback_query,
    parse_pasted_url,
)
from qbo_multicompany_mcp.constants import (
    CALLBACK_PORT,
    DEFAULT_REDIRECT_URI,
    SCOPE_ACCOUNTING,
)
from qbo_multicompany_mcp.config import settings_from_args
from qbo_multicompany_mcp.errors import ConfigurationError, QboMcpError, StaleConsentError, StartupError

from .conftest import REALM_MAIN, REALM_OPS, make_token_response


def new_attempt(**overrides) -> Attempt:
    defaults = dict(
        entity_key="acme_main",
        environment="sandbox",
        client_fingerprint="a" * 64,
        redirect_uri=DEFAULT_REDIRECT_URI,
    )
    defaults.update(overrides)
    return Attempt(**defaults)


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------


def test_state_is_32_bytes_of_entropy_and_unique_per_attempt() -> None:
    first, second = new_attempt(), new_attempt()
    assert first.state != second.state
    # token_urlsafe(32) is 32 random bytes, base64url-encoded without padding.
    assert len(base64.urlsafe_b64decode(first.state + "=" * (-len(first.state) % 4))) == 32


def test_state_expires_after_fifteen_minutes() -> None:
    attempt = new_attempt(ttl=timedelta(minutes=15))
    assert (attempt.expires_at - attempt.created_at) == timedelta(minutes=15)

    stale = new_attempt(ttl=timedelta(seconds=-1))
    with pytest.raises(CallbackRejected, match="expired"):
        stale.check_state(stale.state)


def test_state_comparison_is_constant_time() -> None:
    """A handler that leaked how much of a state matched, via timing, hands an
    attacker a way to build a valid one byte at a time."""
    import inspect

    source = inspect.getsource(Attempt.check_state)
    assert 'hmac.compare_digest(self.state.encode(), candidate.encode("utf-8", "surrogatepass"))' in source
    assert "self.state ==" not in source


def test_state_is_single_use() -> None:
    attempt = new_attempt()
    params = CallbackParams(code="c", state=attempt.state, realm_id=REALM_MAIN)
    attempt.claim(params)
    with pytest.raises(AttemptAlreadyUsed):
        attempt.claim(params)


def test_the_attempt_records_what_it_is_bound_to() -> None:
    attempt = new_attempt()
    assert attempt.entity_key == "acme_main"
    assert attempt.environment == "sandbox"
    assert attempt.client_fingerprint == "a" * 64
    assert attempt.redirect_uri == DEFAULT_REDIRECT_URI


def test_a_state_from_another_attempt_is_refused() -> None:
    mine, theirs = new_attempt(), new_attempt(entity_key="acme_ops")
    with pytest.raises(CallbackRejected, match="state did not match"):
        mine.check_state(theirs.state)


# --------------------------------------------------------------------------
# callback parsing
# --------------------------------------------------------------------------


def test_duplicate_query_parameters_are_refused() -> None:
    """Servers, proxies and browsers disagree about which copy of a repeated
    parameter wins. A consent flow needs one answer."""
    with pytest.raises(CallbackRejected, match="duplicate query parameter"):
        parse_callback_query(f"code=a&state=s&realmId={REALM_MAIN}&realmId={REALM_OPS}")


def test_unexpected_query_parameters_are_refused() -> None:
    with pytest.raises(CallbackRejected, match="unexpected query parameter"):
        parse_callback_query(f"code=a&state=s&realmId={REALM_MAIN}&extra=1")


def test_missing_parameters_are_refused() -> None:
    with pytest.raises(CallbackRejected, match="missing a required parameter"):
        parse_callback_query("code=a&state=s")


def test_an_error_response_is_refused() -> None:
    with pytest.raises(CallbackRejected, match="error on the consent screen"):
        parse_callback_query("error=access_denied&state=s")


def test_a_pasted_url_from_the_wrong_page_is_refused() -> None:
    with pytest.raises(CallbackRejected, match="path is not /callback"):
        parse_pasted_url(f"https://example.invalid/other?code=a&state=s&realmId={REALM_MAIN}")


def test_a_pasted_callback_url_parses() -> None:
    params = parse_pasted_url(
        f"http://127.0.0.1:8765/callback?code=abc&state=xyz&realmId={REALM_MAIN}"
    )
    assert params == CallbackParams(code="abc", state="xyz", realm_id=REALM_MAIN)


# --------------------------------------------------------------------------
# the listener
# --------------------------------------------------------------------------


def test_an_invalid_local_request_does_not_consume_the_callback_slot() -> None:
    """Anything on the machine can reach loopback. If a wrong-state request
    consumed the attempt, any of it could cancel a consent in progress."""
    attempt = new_attempt()
    with LoopbackReceiver(attempt, port=0) as receiver:
        base = f"http://127.0.0.1:{receiver.bound_port}/callback"

        bad = httpx.get(base, params={"code": "x", "state": "wrong", "realmId": REALM_MAIN})
        assert bad.status_code == 400
        assert not attempt.completed.is_set()

        good = httpx.get(base, params={"code": "real", "state": attempt.state, "realmId": REALM_MAIN})
        assert good.status_code == 200
        assert attempt.params is not None and attempt.params.code == "real"


def _connect(port: int) -> socket.socket:
    connection = socket.create_connection(("127.0.0.1", port))
    time.sleep(0.3)  # the serve loop takes it before anything else arrives
    return connection


def test_an_idle_connection_doesnt_hold_the_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anything on the machine can open a connection and send nothing. The real
    callback is still answered, without waiting for the idle one's deadline,
    which is set well past this test's bound so it can't stand in for that."""
    monkeypatch.setattr(callback_server, "REQUEST_DEADLINE_SECONDS", 20)
    attempt = new_attempt()
    with LoopbackReceiver(attempt, port=0) as receiver:
        idle = _connect(receiver.bound_port)
        try:
            good = httpx.get(
                f"http://127.0.0.1:{receiver.bound_port}/callback",
                params={"code": "real", "state": attempt.state, "realmId": REALM_MAIN}, timeout=3,
            )
        finally:
            idle.close()
    assert good.status_code == 200
    assert attempt.params is not None and attempt.params.code == "real"


def test_closing_the_listener_doesnt_wait_for_an_idle_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """The consent command closes the listener on every path, a timeout
    included, so a connection that never sends must not keep it from exiting.
    The deadline is set well past this test's bound so it can't stand in."""
    monkeypatch.setattr(callback_server, "REQUEST_DEADLINE_SECONDS", 20)
    receiver = LoopbackReceiver(new_attempt(), port=0)
    receiver.bind()
    receiver.start()
    idle = _connect(receiver.bound_port)
    try:
        started = time.monotonic()
        receiver.close()
        waited = time.monotonic() - started
    finally:
        idle.close()
    assert waited < 3, "closing waited for an idle connection"


def test_a_connection_that_trickles_is_cut_at_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout on each read would let a byte every few seconds keep a
    connection for ever. The deadline covers the whole request."""
    monkeypatch.setattr(callback_server, "REQUEST_DEADLINE_SECONDS", 0.5)
    with LoopbackReceiver(new_attempt(), port=0) as receiver:
        connection = _connect(receiver.bound_port)
        connection.settimeout(0.05)
        started, cut_after = time.monotonic(), None
        try:
            while time.monotonic() - started < 3:
                try:
                    connection.sendall(b"G")
                    if connection.recv(1) == b"":
                        cut_after = time.monotonic() - started
                        break
                except TimeoutError:
                    continue  # still open, and nothing to read
                except OSError:
                    cut_after = time.monotonic() - started
                    break
        finally:
            connection.close()
    assert cut_after is not None and cut_after < 2, "a trickling connection outlived its deadline"


def test_a_flood_of_connections_cant_hold_the_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unbounded, a local flood of idle connections uses up the descriptors the
    rest of the command needs. One over the cap is closed at once, and the
    callback gets through when a slot frees."""
    monkeypatch.setattr(callback_server, "MAX_CONNECTIONS", 2)
    monkeypatch.setattr(callback_server, "REQUEST_DEADLINE_SECONDS", 20)
    attempt = new_attempt()
    with LoopbackReceiver(attempt, port=0) as receiver:
        held = [_connect(receiver.bound_port) for _ in range(2)]
        try:
            over = _connect(receiver.bound_port)
            over.settimeout(2)
            try:
                closed = over.recv(1) == b""
            except ConnectionResetError:
                closed = True
            except TimeoutError:
                closed = False
            finally:
                over.close()
        finally:
            for connection in held:
                connection.close()
        time.sleep(0.3)  # the held connections' handlers see their close
        good = httpx.get(
            f"http://127.0.0.1:{receiver.bound_port}/callback",
            params={"code": "real", "state": attempt.state, "realmId": REALM_MAIN}, timeout=3,
        )
    assert closed, "a connection over the cap was served"
    assert good.status_code == 200


def test_a_request_the_deadline_cut_never_claims_the_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cut off, a request line still parses as complete, and a slow client
    holding the state would claim the attempt with whatever had arrived."""
    monkeypatch.setattr(callback_server, "REQUEST_DEADLINE_SECONDS", 0.5)
    attempt = new_attempt()
    with LoopbackReceiver(attempt, port=0) as receiver:
        partial = _connect(receiver.bound_port)
        try:
            partial.sendall(f"GET /callback?code=cut&state={attempt.state}&realmId={REALM_MAIN} HTTP/1.1".encode())
            time.sleep(1.2)
        finally:
            partial.close()
        assert not attempt.completed.is_set(), "a request the deadline cut claimed the attempt"
        good = httpx.get(
            f"http://127.0.0.1:{receiver.bound_port}/callback",
            params={"code": "real", "state": attempt.state, "realmId": REALM_MAIN}, timeout=3,
        )
    assert good.status_code == 200
    assert attempt.params is not None and attempt.params.code == "real"


def test_a_malformed_query_gets_the_fixed_rejection_and_prints_nothing(capfd) -> None:
    """The parser's own message quotes the fragment, and an unhandled error
    reaches stderr as a traceback that can hold what a broken callback carried."""
    attempt = new_attempt()
    with LoopbackReceiver(attempt, port=0) as receiver:
        answer = httpx.get(f"http://127.0.0.1:{receiver.bound_port}/callback?synthetic-bare-fragment")
    captured = capfd.readouterr()
    assert answer.status_code == 400
    assert "synthetic-bare-fragment" not in answer.text
    assert "synthetic-bare-fragment" not in captured.err + captured.out
    assert not attempt.completed.is_set()


def test_a_malformed_paste_is_a_rejection_that_repeats_nothing() -> None:
    for pasted in ("http://127.0.0.1/callback?synthetic-bare-fragment", "http://[synthetic-bad-host/callback"):
        with pytest.raises(CallbackRejected) as refused:
            parse_pasted_url(pasted)
        assert "synthetic" not in str(refused.value)
        assert refused.value.__suppress_context__, "the original message stays out of any traceback"


def test_a_request_that_fails_leaves_one_fixed_line(capfd, monkeypatch: pytest.MonkeyPatch) -> None:
    """The second boundary: whatever else fails in a request, its text never
    reaches stderr by way of the standard server's traceback."""

    def failing(self: Attempt, params: CallbackParams) -> None:
        raise RuntimeError("synthetic-request-text")

    monkeypatch.setattr(Attempt, "claim", failing)
    attempt = new_attempt()
    with LoopbackReceiver(attempt, port=0) as receiver:
        base = f"http://127.0.0.1:{receiver.bound_port}/callback"
        with pytest.raises(httpx.HTTPError):
            httpx.get(base, params={"code": "x", "state": attempt.state, "realmId": REALM_MAIN})
    err = capfd.readouterr().err
    assert err.strip() == "qbo-multicompany-mcp: a callback request failed"


def test_a_state_that_isnt_ascii_is_a_rejection() -> None:
    """compare_digest refuses non-ASCII text with a TypeError, which would end
    connect --manual with a traceback."""
    attempt = new_attempt()
    with pytest.raises(CallbackRejected, match="state did not match"):
        attempt.check_state("\u00e9")
    with pytest.raises(CallbackRejected, match="state did not match"):
        attempt.check_state("\udcff")


def test_a_replayed_callback_is_refused_after_the_slot_is_taken() -> None:
    attempt = new_attempt()
    with LoopbackReceiver(attempt, port=0) as receiver:
        base = f"http://127.0.0.1:{receiver.bound_port}/callback"
        params = {"code": "real", "state": attempt.state, "realmId": REALM_MAIN}
        assert httpx.get(base, params=params).status_code == 200
        assert httpx.get(base, params=params).status_code == 409


def test_every_listener_answer_forbids_caching() -> None:
    """The wording of Intuit's security requirements under App server
    configuration is "no-cache, no-store". Every answer the listener gives
    carries both: the page for the URL that carries the code, and each
    refusal."""
    attempt = new_attempt()
    with LoopbackReceiver(attempt, port=0) as receiver:
        base = f"http://127.0.0.1:{receiver.bound_port}"
        good = {"code": "real", "state": attempt.state, "realmId": REALM_MAIN}
        answers = {
            "refused": httpx.get(f"{base}/callback", params={**good, "state": "wrong"}),
            "not found": httpx.get(f"{base}/elsewhere"),
            "accepted": httpx.get(f"{base}/callback", params=good),
            "replayed": httpx.get(f"{base}/callback", params=good),
        }
    assert {name: answer.status_code for name, answer in answers.items()} == {
        "refused": 400, "not found": 404, "accepted": 200, "replayed": 409,
    }
    assert {name: answer.headers.get("Cache-Control") for name, answer in answers.items()} == (
        dict.fromkeys(answers, "no-cache, no-store")
    )


def test_both_loopback_literals_are_bound() -> None:
    """A browser resolving localhost may pick either family; binding one turns
    into an intermittent, machine-dependent failure."""
    attempt = new_attempt()
    with LoopbackReceiver(attempt, port=0) as receiver:
        for host in ("127.0.0.1", "[::1]"):
            response = httpx.get(f"http://{host}:{receiver.bound_port}/callback?code=a&state=b&realmId=1")
            assert response.status_code == 400  # reached the handler


def test_a_bind_failure_raises_rather_than_choosing_another_port() -> None:
    """The redirect URI registered with Intuit is fixed. A listener that moved
    to another port would consent successfully and drop every callback."""
    blocker = LoopbackReceiver(new_attempt(), port=0)
    blocker.bind()
    blocker.start()
    try:
        occupied = blocker.bound_port
        with pytest.raises(StartupError, match="will not silently use a different port"):
            LoopbackReceiver(new_attempt(), port=occupied).bind()
    finally:
        blocker.close()


def test_an_ipv6_only_conflict_stops_consent_and_releases_the_ipv4_listener() -> None:
    """Whatever holds [::1] gets the callback from a browser that resolves
    localhost to IPv6. So a conflict there stops consent as one on IPv4 does,
    and the IPv4 listener already bound is released."""
    import socket

    holder = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        holder.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        holder.bind(("::1", 0))
        holder.listen()
        port = holder.getsockname()[1]
        receiver = LoopbackReceiver(new_attempt(), port=port)
        with pytest.raises(StartupError, match=r"\[::1\].*will not silently use a different port"):
            receiver.bind()
        with pytest.raises(StartupError, match="not bound yet"):
            _ = receiver.bound_port
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
        finally:
            probe.close()
    finally:
        holder.close()


@pytest.mark.parametrize("code", sorted(callback_server.IPV6_UNAVAILABLE_ERRNOS))
def test_a_machine_without_ipv6_still_listens_on_ipv4(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    """The control: an IPv6 loopback that doesn't exist is skipped, not fatal."""
    import os

    def no_ipv6(*args: object, **kwargs: object) -> None:
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(callback_server, "_V6Server", no_ipv6)
    with LoopbackReceiver(new_attempt(), port=0) as receiver:
        response = httpx.get(f"http://127.0.0.1:{receiver.bound_port}/callback?code=a&state=b&realmId=1")
    assert response.status_code == 400  # reached the handler


def test_the_listener_never_logs_the_code_or_the_query_string() -> None:
    import inspect

    from qbo_multicompany_mcp import callback_server

    source = inspect.getsource(callback_server._Handler.log_message)
    assert source.strip().endswith("return")


# --------------------------------------------------------------------------
# connect, end to end
# --------------------------------------------------------------------------


def connect_args(entity: str = "acme_main", *, manual: bool = False) -> argparse.Namespace:
    return argparse.Namespace(entity=entity, manual=manual)


@pytest.fixture
def cli_settings(settings, credentials, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("QBO_CLIENT_ID", credentials.client_id)
    monkeypatch.setenv("QBO_CLIENT_SECRET", credentials.client_secret)
    return settings


def test_the_listener_is_bound_before_the_browser_opens(
    cli_settings, registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the listener is not accepting when the browser is launched, a fast
    consent leaves the callback hitting a closed port."""
    reachable: dict[str, object] = {}

    def fake_open(url: str) -> bool:
        # The browser step: by now the callback port must already accept.
        try:
            response = httpx.get(
                f"http://127.0.0.1:{cli_settings.callback_port}/callback"
                f"?code=&state=&realmId=",
                timeout=5,
            )
            reachable["status"] = response.status_code
        except httpx.HTTPError as exc:  # pragma: no cover - failure path
            reachable["error"] = repr(exc)
        # Deliver the real callback so connect() finishes.
        threading.Thread(target=deliver, daemon=True).start()
        return True

    exchanged: list[str] = []

    def deliver() -> None:
        # cmd_connect owns the receiver, so the port comes from settings here.
        time.sleep(0.05)
        httpx.get(
            f"http://127.0.0.1:{cli_settings.callback_port}/callback",
            params={"code": "thecode", "state": captured["state"], "realmId": REALM_MAIN},
            timeout=5,
        )

    captured: dict[str, str] = {}
    real_authorize_url = auth_cli.authorize_url

    def spy_url(*, client_id: str, state: str, redirect_uri: str) -> str:
        captured["state"] = state
        return real_authorize_url(
            client_id=client_id, state=state, redirect_uri=redirect_uri
        )

    monkeypatch.setattr(auth_cli, "authorize_url", spy_url)
    monkeypatch.setattr(auth_cli.webbrowser, "open", fake_open)
    monkeypatch.setattr(
        auth_cli.oauth,
        "exchange_code",
        lambda **kw: (exchanged.append(kw["code"]), make_token_response(realm_id=REALM_MAIN))[1],
    )
    # The consent guard reads CompanyName before storing; this test is about
    # bind-before-browser, so give it a name that matches.
    monkeypatch.setattr(auth_cli, "_probe_company_name", lambda *a, **k: "Acme Main")

    code = auth_cli.cmd_connect(cli_settings, registry, connect_args())
    assert code == 0
    assert reachable.get("status") == 400  # bound, and rejecting an invalid request
    assert exchanged == ["thecode"]


def test_a_realm_that_is_not_the_registry_realm_is_fatal_before_any_exchange(
    cli_settings, registry, credentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The realm is the identity. A matching company name cannot override it,
    because two QuickBooks companies may carry byte-identical names."""
    exchanged: list[str] = []
    monkeypatch.setattr(
        auth_cli.oauth,
        "exchange_code",
        lambda **kw: (exchanged.append(kw["code"]), make_token_response())[1],
    )

    store = cli_settings.open_store(credentials=credentials)
    attempt = new_attempt()
    entity = registry["acme_main"]

    code = auth_cli._finish_connect(
        cli_settings,
        store,
        entity,
        credentials,
        attempt,
        CallbackParams(code="thecode", state=attempt.state, realm_id=REALM_OPS),
    )

    assert code == 2
    assert exchanged == []  # no token was requested
    assert store.get_row(REALM_MAIN) is None
    assert store.get_row(REALM_OPS) is None


def test_a_token_issued_for_another_realm_is_refused_after_exchange(
    cli_settings, registry, credentials, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The exchange spent the grant and nobody will hold the pair, so it is
    revoked, as on every other refusal after an exchange."""
    revoked: list[str] = []
    monkeypatch.setattr(
        auth_cli.oauth,
        "exchange_code",
        lambda **kw: make_token_response(realm_id=REALM_OPS, refresh_token="issued"),
    )
    monkeypatch.setattr(oauth, "revoke_token", lambda **kw: revoked.append(str(kw["token"])))
    store = cli_settings.open_store(credentials=credentials)
    attempt = new_attempt()
    code = auth_cli._finish_connect(
        cli_settings,
        store,
        registry["acme_main"],
        credentials,
        attempt,
        CallbackParams(code="c", state=attempt.state, realm_id=REALM_MAIN),
    )
    assert code == 2
    assert store.get_row(REALM_MAIN) is None
    assert store.get_row(REALM_OPS) is None
    assert revoked == ["issued"], "the refused pair was left live at Intuit"
    assert "was revoked at Intuit" in capsys.readouterr().out


def test_a_wrong_realm_pair_intuit_wont_revoke_is_still_refused(
    cli_settings, registry, credentials, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Revocation is best effort: a refusal from Intuit changes neither the
    exit status nor what is stored, and the output says the pair stays unused."""

    def failing_revoke(**kwargs: object) -> None:
        raise QboMcpError("Intuit refused the revocation")

    monkeypatch.setattr(
        auth_cli.oauth,
        "exchange_code",
        lambda **kw: make_token_response(realm_id=REALM_OPS),
    )
    monkeypatch.setattr(oauth, "revoke_token", failing_revoke)
    store = cli_settings.open_store(credentials=credentials)
    attempt = new_attempt()
    code = auth_cli._finish_connect(
        cli_settings,
        store,
        registry["acme_main"],
        credentials,
        attempt,
        CallbackParams(code="c", state=attempt.state, realm_id=REALM_MAIN),
    )
    assert code == 2
    assert store.get_row(REALM_MAIN) is None
    assert store.get_row(REALM_OPS) is None
    out = capsys.readouterr().out
    assert "Nothing was stored." in out
    assert "could not revoke the unstored credential" in out


def test_the_code_is_exchanged_at_most_once_including_after_a_timeout() -> None:
    """An authorization code is single use (Intuit's Authorization FAQ); a
    second exchange returns invalid_grant, which a wrong client secret also
    produces."""
    attempt = new_attempt()
    attempt.begin_exchange()
    with pytest.raises(AttemptAlreadyUsed, match="already been exchanged"):
        attempt.begin_exchange()


def test_browser_and_manual_paths_compete_for_one_attempt(
    cli_settings, registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user who does both must not end up exchanging one code twice."""
    attempt = new_attempt()
    with LoopbackReceiver(attempt, port=0) as receiver:
        httpx.get(
            f"http://127.0.0.1:{receiver.bound_port}/callback",
            params={"code": "from-browser", "state": attempt.state, "realmId": REALM_MAIN},
        )
        pasted = f"http://127.0.0.1:8765/callback?code=from-paste&state={attempt.state}&realmId={REALM_MAIN}"
        monkeypatch.setattr("builtins.input", lambda _prompt="": pasted)

        params = auth_cli._await_manual(attempt, receiver)

    assert params is not None
    assert params.code == "from-browser"  # the first arrival won


def test_manual_paste_wins_when_the_browser_never_arrives(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = new_attempt()
    with LoopbackReceiver(attempt, port=0) as receiver:
        pasted = f"http://127.0.0.1:8765/callback?code=from-paste&state={attempt.state}&realmId={REALM_MAIN}"
        monkeypatch.setattr("builtins.input", lambda _prompt="": pasted)
        params = auth_cli._await_manual(attempt, receiver)
    assert params is not None and params.code == "from-paste"


def test_manual_paste_with_a_wrong_state_leaves_the_attempt_open(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = new_attempt()
    pastes = iter(
        [
            f"http://127.0.0.1:8765/callback?code=a&state=wrong&realmId={REALM_MAIN}",
            f"http://127.0.0.1:8765/callback?code=b&state={attempt.state}&realmId={REALM_MAIN}",
        ]
    )
    with LoopbackReceiver(attempt, port=0) as receiver:
        monkeypatch.setattr("builtins.input", lambda _prompt="": next(pastes))
        params = auth_cli._await_manual(attempt, receiver)
    assert params is not None and params.code == "b"


# --------------------------------------------------------------------------
# the authorize URL
# --------------------------------------------------------------------------


def test_authorize_url_requests_only_the_accounting_scope() -> None:
    from urllib.parse import parse_qs, urlsplit

    url = auth_cli.authorize_url(
        client_id="cid", state="st", redirect_uri=DEFAULT_REDIRECT_URI
    )
    query = parse_qs(urlsplit(url).query)
    assert query["scope"] == [SCOPE_ACCOUNTING]
    assert query["response_type"] == ["code"]
    assert query["state"] == ["st"]
    assert query["redirect_uri"] == [DEFAULT_REDIRECT_URI]


def test_authorize_url_does_not_claim_pkce() -> None:
    """code_challenge is sent only once a sandbox consent has been observed to
    succeed with it present. Until then this package makes no PKCE claim."""
    url = auth_cli.authorize_url(
        client_id="cid", state="st", redirect_uri=DEFAULT_REDIRECT_URI
    )
    assert "code_challenge" not in url


# --------------------------------------------------------------------------
# the forwarder page
# --------------------------------------------------------------------------


FORWARDER = Path("docs/callback.html")


def test_forwarder_page_hash_is_pinned() -> None:
    """The page's content is the security boundary; pin it so a change has to
    be deliberate."""
    digest = hashlib.sha256(FORWARDER.read_bytes()).hexdigest()
    assert digest == "398144f398d917c26d81d7491a3deee750034cc922b73ffef6295703cd61fc36"


def test_forwarder_script_target_is_hard_coded() -> None:
    """The page never reads a destination from its own URL, so a crafted link
    cannot redirect the authorization code anywhere else."""
    body = FORWARDER.read_text(encoding="utf-8")
    assert 'location.replace("http://127.0.0.1:8765/callback"+location.search)' in body


def test_forwarder_csp_carries_the_scripts_own_hash() -> None:
    body = FORWARDER.read_text(encoding="utf-8")
    script = 'location.replace("http://127.0.0.1:8765/callback"+location.search)'
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    assert f"script-src 'sha256-{digest}'" in body
    assert "default-src 'none'" in body


def test_forwarder_is_no_referrer_and_has_no_external_resources() -> None:
    import re

    body = FORWARDER.read_text(encoding="utf-8")
    assert 'name="referrer" content="no-referrer"' in body
    assert re.search(r'(src|href)\s*=', body) is None


def test_forwarder_tells_a_scriptless_visitor_what_to_do() -> None:
    body = FORWARDER.read_text(encoding="utf-8")
    assert "<noscript>" in body
    assert "--manual" in body


# --------------------------------------------------------------------------
# keys
# --------------------------------------------------------------------------


@pytest.fixture
def fake_keychain(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """An in-memory stand-in. The real login keychain is never touched here."""
    import subprocess

    vault: dict[str, str] = {}

    def fake_security(*args: str, input_text: str | None = None):
        argv = list(args)
        if argv[0] == "-i":
            # What the real tool does: the command comes from stdin.
            argv = shlex.split(input_text or "")
        account = argv[argv.index("-a") + 1] if "-a" in argv else ""
        if argv[0] == "find-generic-password":
            if account not in vault:
                return subprocess.CompletedProcess(argv, 44, "", "not found")
            return subprocess.CompletedProcess(argv, 0, vault[account], "")
        if argv[0] == "add-generic-password":
            vault[account] = argv[argv.index("-w") + 1]
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(crypto, "_security", fake_security)
    monkeypatch.setattr(crypto, "_require_macos", lambda: None)
    return vault


def test_init_key_keeps_the_key_out_of_the_process_arguments(
    fake_keychain: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An argument sits in the process listing while the child runs, and in any
    tool that records command lines. The key goes through stdin."""
    inner = crypto._security
    calls: list[tuple[str, ...]] = []

    def recording(*args: str, input_text: str | None = None):
        calls.append(args)
        return inner(*args, input_text=input_text)

    monkeypatch.setattr(crypto, "_security", recording)
    assert crypto.keychain_create_key("sandbox") is True
    key = fake_keychain[crypto.keychain_account("sandbox")]
    assert key, "the key reached the keychain"
    assert all(key not in argument for call in calls for argument in call)


def test_init_key_reports_a_failed_add(
    fake_keychain: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interactive mode passes the failed command's status back, so a refused
    add is an error, never a key that was never stored."""
    import subprocess

    inner = crypto._security

    def refusing(*args: str, input_text: str | None = None):
        if args[0] == "-i":
            return subprocess.CompletedProcess(list(args), 45, "", "refused")
        return inner(*args, input_text=input_text)

    monkeypatch.setattr(crypto, "_security", refusing)
    with pytest.raises(ConfigurationError, match="could not add"):
        crypto.keychain_create_key("sandbox")


def test_init_key_is_idempotent_and_never_overwrites(fake_keychain: dict[str, str]) -> None:
    """Overwriting would leave every stored refresh token permanently
    undecryptable, and no backup could hold a live replacement."""
    assert crypto.keychain_create_key("sandbox") is True
    first = fake_keychain["token-key-sandbox"]
    assert crypto.keychain_create_key("sandbox") is False
    assert fake_keychain["token-key-sandbox"] == first


def test_keys_are_separate_per_environment(fake_keychain: dict[str, str]) -> None:
    crypto.keychain_create_key("sandbox")
    crypto.keychain_create_key("production")
    assert fake_keychain["token-key-sandbox"] != fake_keychain["token-key-production"]


def test_export_key_refuses_a_non_interactive_terminal(
    settings, registry, fake_keychain, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Into a pipe, key material lands in a file, a CI log or a scrollback
    buffer that outlives the session."""
    from dataclasses import replace as dc_replace

    crypto.keychain_create_key("sandbox")
    keychain_settings = dc_replace(settings, key_source=crypto.KeySource.KEYCHAIN)
    monkeypatch.setattr(auth_cli.sys.stdin, "isatty", lambda: False, raising=False)

    with pytest.raises(StartupError, match="interactive terminal"):
        auth_cli.cmd_export_key(keychain_settings, registry, argparse.Namespace())


def test_env_key_source_reads_only_the_named_variable(token_key: str) -> None:
    assert crypto.load_key(crypto.KeySource.ENV, "sandbox") == base64.b64decode(token_key)


def test_env_key_source_names_the_variable_when_absent(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """Name the variable and stop. Never ask anyone to paste a secret."""
    from qbo_multicompany_mcp.errors import ConfigurationError

    monkeypatch.delenv("QBO_TOKEN_KEY", raising=False)
    with pytest.raises(ConfigurationError) as caught:
        crypto.load_key(crypto.KeySource.ENV, "sandbox")
    assert "QBO_TOKEN_KEY" in str(caught.value)
    assert "never reads a secret from" in str(caught.value)


# --------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------


def test_import_reads_the_token_from_the_environment_not_a_prompt(
    cli_settings, registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("QBO_IMPORT_REFRESH_TOKEN", raising=False)
    with pytest.raises(StartupError) as caught:
        auth_cli.cmd_import(cli_settings, registry, argparse.Namespace(entity="acme_main"))
    assert "QBO_IMPORT_REFRESH_TOKEN" in str(caught.value)
    assert "never asks you to paste a secret" in str(caught.value)


def test_import_stores_nothing_when_the_token_cannot_read_the_realm(
    mock_settings, registry, credentials, monkeypatch: pytest.MonkeyPatch, mock_intuit
) -> None:
    """A refresh response does not name a realm, so the only honest way to
    learn which company a token belongs to is to use it. Verification precedes
    the commit, so nothing is stored to remove; the test with an existing
    consent is at the end of this file."""
    monkeypatch.setenv("QBO_CLIENT_ID", credentials.client_id)
    monkeypatch.setenv("QBO_CLIENT_SECRET", credentials.client_secret)
    monkeypatch.setenv("QBO_IMPORT_REFRESH_TOKEN", "someone-elses-token")
    monkeypatch.setattr(
        oauth, "refresh_tokens", lambda **kw: make_token_response(access_token="rejected")
    )
    mock_intuit.unauthorized_tokens.add("rejected")

    code = auth_cli.cmd_import(mock_settings, registry, argparse.Namespace(entity="acme_main"))
    assert code == 2
    assert mock_settings.open_store(credentials=credentials).get_row(REALM_MAIN) is None


def test_import_stores_the_row_when_the_token_reads_the_realm(
    mock_settings, registry, credentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QBO_CLIENT_ID", credentials.client_id)
    monkeypatch.setenv("QBO_CLIENT_SECRET", credentials.client_secret)
    monkeypatch.setenv("QBO_IMPORT_REFRESH_TOKEN", "a-live-token")
    monkeypatch.setattr(oauth, "refresh_tokens", lambda **kw: make_token_response())

    code = auth_cli.cmd_import(mock_settings, registry, argparse.Namespace(entity="acme_main"))
    assert code == 0
    row = mock_settings.open_store(credentials=credentials).get_row(REALM_MAIN)
    assert row is not None and row.entity == "acme_main"


# --------------------------------------------------------------------------
# unknown entity
# --------------------------------------------------------------------------


def test_an_unknown_entity_is_a_startup_error_listing_the_known_keys(registry) -> None:
    with pytest.raises(StartupError, match="acme_main, acme_ops") as refused:
        auth_cli._resolve(registry, "nope")
    assert "nope" not in str(refused.value)


# --------------------------------------------------------------------------
# The redirect URI sent to Intuit
# --------------------------------------------------------------------------
#
# Intuit's Redirect URIs screen states: "The URI itself cannot be an IP address
# or a URI fragment or relative path." So the value SENT to Intuit must name a
# host, while the listener still binds both loopback literals to receive the
# browser whichever family it resolves `localhost` to. Those are two different
# addresses, and conflating them breaks the consent.


def test_the_default_redirect_uri_contains_no_ip_literal() -> None:
    """An IP literal here cannot be registered at all,
    and a consent attempt fails with "The redirect_uri query parameter value is
    invalid"."""
    import ipaddress
    from urllib.parse import urlsplit

    host = urlsplit(DEFAULT_REDIRECT_URI).hostname
    assert host == "localhost"
    assert "127.0.0.1" not in DEFAULT_REDIRECT_URI
    assert "[::1]" not in DEFAULT_REDIRECT_URI
    with pytest.raises(ValueError):
        ipaddress.ip_address(host or "")


def test_the_authorize_url_sends_a_hostname_not_a_loopback_literal() -> None:
    from urllib.parse import parse_qs, urlsplit

    url = auth_cli.authorize_url(
        client_id="cid", state="st", redirect_uri=DEFAULT_REDIRECT_URI
    )
    sent = parse_qs(urlsplit(url).query)["redirect_uri"][0]
    assert sent == DEFAULT_REDIRECT_URI
    assert "127.0.0.1" not in url


def test_an_https_override_is_accepted() -> None:
    """Production registers an HTTPS forwarder page on a domain the operator
    controls, which then navigates to the local listener."""
    from qbo_multicompany_mcp.config import validate_redirect_uri

    for accepted in (
        "https://oauth.example.com/callback",
        "https://example.com/qbo/callback",
        "https://example.com",
    ):
        assert validate_redirect_uri(accepted) == accepted


def test_an_ip_literal_override_is_rejected() -> None:
    from qbo_multicompany_mcp.config import validate_redirect_uri

    for rejected in (
        "http://127.0.0.1:8765/callback",
        "https://192.0.2.10/callback",
        "https://[2001:db8::1]/callback",
    ):
        with pytest.raises(StartupError):
            validate_redirect_uri(rejected)


def test_the_ip_literal_rejection_names_intuits_actual_rule() -> None:
    """The refusal states Intuit's own rule."""
    from qbo_multicompany_mcp.config import validate_redirect_uri

    with pytest.raises(StartupError) as caught:
        validate_redirect_uri("https://192.0.2.10/callback")
    assert "cannot be an IP address" in str(caught.value) or "not an IP address" in str(
        caught.value
    )


@pytest.mark.parametrize(
    "rejected",
    [
        "https://example.com/cb?state=x",  # Intuit appends its own query
        "https://example.com/cb#frag",
        "https://user:pass@example.com/cb",
        "http://example.com/cb",  # http only for the localhost default
        "ftp://example.com/cb",
    ],
)
def test_other_unregistrable_shapes_are_rejected(rejected: str) -> None:
    from qbo_multicompany_mcp.config import validate_redirect_uri

    with pytest.raises(StartupError):
        validate_redirect_uri(rejected)


def test_the_override_is_what_reaches_the_token_exchange(
    settings, registry, credentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Intuit compares the redirect_uri on the authorize request, the one
    registered, and the one in the exchange. They have to be byte-identical."""
    from dataclasses import replace as dc_replace

    forwarder = "https://oauth.example.com/callback"
    overridden = dc_replace(settings, redirect_uri=forwarder)

    seen: dict[str, str] = {}

    def fake_exchange(**kwargs):
        seen["redirect_uri"] = kwargs["redirect_uri"]
        return make_token_response(realm_id=REALM_MAIN)

    monkeypatch.setattr(auth_cli.oauth, "exchange_code", fake_exchange)
    monkeypatch.setattr(auth_cli, "_probe_company_name", lambda *a, **k: "Acme Main")

    store = overridden.open_store(credentials=credentials)
    attempt = new_attempt(redirect_uri=forwarder)
    code = auth_cli._finish_connect(
        overridden,
        store,
        registry["acme_main"],
        credentials,
        attempt,
        CallbackParams(code="c", state=attempt.state, realm_id=REALM_MAIN),
    )

    assert code == 0
    assert seen["redirect_uri"] == forwarder


def test_the_forwarder_page_still_targets_the_loopback_ip_literal() -> None:
    """Deliberately NOT localhost.

    That hop is browser-to-loopback and local only: Intuit never sees it, so
    Intuit's no-IP-address rule does not apply. Changing it to `localhost` to
    'match' the redirect URI would be a regression, because the page's whole
    security property is a hard-coded target that a crafted link cannot
    redirect.
    """
    body = FORWARDER.read_text(encoding="utf-8")
    assert 'location.replace("http://127.0.0.1:8765/callback"+location.search)' in body
    assert "localhost" not in body


def test_the_cli_always_binds_the_fixed_port(entities_file, monkeypatch) -> None:
    """The test-only seam must not have loosened the CLI's guarantee.

    `Settings.callback_port` exists so tests can use an ephemeral port. It is
    not reachable from a flag or an environment variable, so a real run always
    binds CALLBACK_PORT and fails loudly rather than moving.
    """
    import argparse

    from qbo_multicompany_mcp.config import add_common_arguments

    monkeypatch.setenv("QBO_CALLBACK_PORT", "9999")  # no such knob exists
    parser = argparse.ArgumentParser()
    add_common_arguments(parser)
    settings = settings_from_args(parser.parse_args(["--entities", str(entities_file)]))
    assert settings.callback_port == CALLBACK_PORT
    assert "--callback-port" not in parser.format_help()


# --------------------------------------------------------------------------
# The consent-time company-name guard
# --------------------------------------------------------------------------
#
# With no guard at consent, a wrong-company approval is stored and is only
# discovered at first use -- by which point the operator has left the terminal.


def _connect_params(attempt: Attempt) -> CallbackParams:
    return CallbackParams(code="c", state=attempt.state, realm_id=REALM_MAIN)


def test_consent_prints_both_names_when_they_match(
    cli_settings, registry, credentials, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(auth_cli.oauth, "exchange_code", lambda **kw: make_token_response())
    monkeypatch.setattr(auth_cli, "_probe_company_name", lambda *a, **k: "Acme Main")

    store = cli_settings.open_store(credentials=credentials)
    attempt = new_attempt()
    code = auth_cli._finish_connect(
        cli_settings, store, registry["acme_main"], credentials, attempt,
        _connect_params(attempt),
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "display name (registry): 'Acme Main'" in out
    assert "company name (QuickBooks): 'Acme Main'" in out
    assert "names match:             True" in out
    assert store.get_row(REALM_MAIN) is not None


def test_consent_masks_the_realm_id_to_the_last_four(
    cli_settings, registry, credentials, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(auth_cli.oauth, "exchange_code", lambda **kw: make_token_response())
    monkeypatch.setattr(auth_cli, "_probe_company_name", lambda *a, **k: "Acme Main")

    store = cli_settings.open_store(credentials=credentials)
    attempt = new_attempt()
    auth_cli._finish_connect(
        cli_settings, store, registry["acme_main"], credentials, attempt,
        _connect_params(attempt),
    )
    out = capsys.readouterr().out
    assert "...0001" in out
    assert REALM_MAIN not in out


def test_a_mismatch_refuses_to_store_without_confirmation(
    cli_settings, registry, credentials, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A wrong display_name must not be written silently and discovered later."""
    monkeypatch.setattr(auth_cli.oauth, "exchange_code", lambda **kw: make_token_response())
    monkeypatch.setattr(auth_cli, "_probe_company_name", lambda *a, **k: "Acme Main ")
    monkeypatch.setattr(auth_cli.oauth, "revoke_token", lambda **kw: None)
    monkeypatch.setattr(auth_cli, "_confirm", lambda question: False)

    store = cli_settings.open_store(credentials=credentials)
    attempt = new_attempt()
    code = auth_cli._finish_connect(
        cli_settings, store, registry["acme_main"], credentials, attempt,
        _connect_params(attempt),
    )
    out = capsys.readouterr().out

    assert code == 2
    assert store.get_row(REALM_MAIN) is None, "nothing may be stored on refusal"
    # repr() on both, so a trailing space is visible on the page.
    assert "'Acme Main'" in out
    assert "'Acme Main '" in out
    assert "differs by: whitespace" in out


@pytest.mark.parametrize(
    ("intuit_name", "expected_class"),
    [
        ("Acme Main ", "whitespace"),
        ("acme main", "case"),
        ("Acme, Main.", "punctuation"),
        ("Something Else", "other"),
    ],
)
def test_a_mismatch_names_its_difference_class(
    cli_settings, registry, credentials, monkeypatch: pytest.MonkeyPatch, capsys,
    intuit_name: str, expected_class: str,
) -> None:
    monkeypatch.setattr(auth_cli.oauth, "exchange_code", lambda **kw: make_token_response())
    monkeypatch.setattr(auth_cli, "_probe_company_name", lambda *a, **k: intuit_name)
    monkeypatch.setattr(auth_cli.oauth, "revoke_token", lambda **kw: None)
    monkeypatch.setattr(auth_cli, "_confirm", lambda question: False)

    store = cli_settings.open_store(credentials=credentials)
    attempt = new_attempt()
    auth_cli._finish_connect(
        cli_settings, store, registry["acme_main"], credentials, attempt,
        _connect_params(attempt),
    )
    assert f"differs by: {expected_class}" in capsys.readouterr().out


def test_confirming_a_mismatch_stores_and_prints_the_value_to_paste(
    cli_settings, registry, credentials, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(auth_cli.oauth, "exchange_code", lambda **kw: make_token_response())
    monkeypatch.setattr(auth_cli, "_probe_company_name", lambda *a, **k: "Acme Main LLC")
    monkeypatch.setattr(auth_cli, "_confirm", lambda question: True)

    store = cli_settings.open_store(credentials=credentials)
    attempt = new_attempt()
    code = auth_cli._finish_connect(
        cli_settings, store, registry["acme_main"], credentials, attempt,
        _connect_params(attempt),
    )
    out = capsys.readouterr().out

    assert code == 0
    assert store.get_row(REALM_MAIN) is not None
    assert 'display_name: "Acme Main LLC"' in out
    assert "except get_company_info refuses this" in out


@pytest.mark.parametrize(
    "name",
    ["Acme: Main", "Acme #1 Ltd", "Trailing Space ", 'Quote " Inc', "Acme\x92s", "Next\x85Line", "Back\\slash"],
)
def test_the_printed_display_name_survives_being_pasted_as_yaml(
    name: str, cli_settings, registry, credentials, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The guard compares names byte for byte, so the line has to parse back to
    the exact name: ': ' would break the registry and ' #' would cut it short."""
    import yaml

    monkeypatch.setattr(auth_cli.oauth, "exchange_code", lambda **kw: make_token_response())
    monkeypatch.setattr(auth_cli, "_probe_company_name", lambda *a, **k: name)
    monkeypatch.setattr(auth_cli, "_confirm", lambda question: True)

    store = cli_settings.open_store(credentials=credentials)
    attempt = new_attempt()
    auth_cli._finish_connect(
        cli_settings, store, registry["acme_main"], credentials, attempt,
        _connect_params(attempt),
    )
    line = next(text for text in capsys.readouterr().out.splitlines() if text.strip().startswith("display_name:"))
    assert yaml.safe_load(line.strip()) == {"display_name": name}


def test_an_unreadable_company_name_refuses_to_store(
    cli_settings, registry, credentials, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """An unverified credential is not stored. Better a retry than a silent
    wrong-company connection."""
    monkeypatch.setattr(auth_cli.oauth, "exchange_code", lambda **kw: make_token_response())
    monkeypatch.setattr(auth_cli, "_probe_company_name", lambda *a, **k: None)
    monkeypatch.setattr(auth_cli.oauth, "revoke_token", lambda **kw: None)

    store = cli_settings.open_store(credentials=credentials)
    attempt = new_attempt()
    code = auth_cli._finish_connect(
        cli_settings, store, registry["acme_main"], credentials, attempt,
        _connect_params(attempt),
    )
    assert code == 2
    assert store.get_row(REALM_MAIN) is None
    assert "Refusing to store an unverified credential" in capsys.readouterr().out


def _unbuildable_accounting_client(monkeypatch: pytest.MonkeyPatch) -> None:
    from qbo_multicompany_mcp import client as client_module

    def unbuildable(**kwargs: object) -> None:
        raise FileNotFoundError("the CA bundle")

    monkeypatch.setattr(client_module, "isolated_client", unbuildable)


def test_a_consent_whose_client_cant_be_built_is_refused_like_any_failed_read(
    cli_settings, registry, credentials, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Building the TLS context opens files. A failure there refuses the
    connection like any other failed read: nothing stored, the pair revoked."""
    revoked: list[str] = []
    monkeypatch.setattr(auth_cli.oauth, "exchange_code", lambda **kw: make_token_response(refresh_token="issued"))
    monkeypatch.setattr(oauth, "revoke_token", lambda **kw: revoked.append(str(kw["token"])))
    _unbuildable_accounting_client(monkeypatch)

    store = cli_settings.open_store(credentials=credentials)
    attempt = new_attempt()
    code = auth_cli._finish_connect(
        cli_settings, store, registry["acme_main"], credentials, attempt, _connect_params(attempt),
    )
    out = capsys.readouterr().out
    assert code == 2, out
    assert store.get_row(REALM_MAIN) is None
    assert revoked == ["issued"], "the issued pair was left live at Intuit"
    assert "could not be built" in out


@pytest.mark.parametrize("raised", [RuntimeError("synthetic"), KeyboardInterrupt()], ids=["error", "interrupt"])
def test_a_consent_stopped_before_storing_revokes_the_issued_pair(
    raised: BaseException, cli_settings, registry, credentials, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """An error no refusal names, or an interrupt at the confirmation prompt,
    ends the step after Intuit issued the pair and before anything holds it."""
    revoked: list[str] = []

    def stopped(question: str) -> bool:
        raise raised

    monkeypatch.setattr(auth_cli.oauth, "exchange_code", lambda **kw: make_token_response(refresh_token="issued"))
    monkeypatch.setattr(auth_cli, "_probe_company_name", lambda *a, **k: "Another Company")
    monkeypatch.setattr(auth_cli, "_confirm", stopped)
    monkeypatch.setattr(oauth, "revoke_token", lambda **kw: revoked.append(str(kw["token"])))

    store = cli_settings.open_store(credentials=credentials)
    attempt = new_attempt()
    with pytest.raises(type(raised)):
        auth_cli._finish_connect(
            cli_settings, store, registry["acme_main"], credentials, attempt, _connect_params(attempt),
        )
    assert store.get_row(REALM_MAIN) is None
    assert revoked == ["issued"], "the issued pair was left live at Intuit"
    assert "Stopped before the credential was stored" in capsys.readouterr().out


def test_an_interrupted_revoke_is_not_attempted_again(
    cli_settings, registry, credentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal's revoke is the one attempt: an interrupt during it doesn't
    start another on the way out."""
    attempts: list[str] = []

    def interrupted(**kwargs: object) -> None:
        attempts.append(str(kwargs["token"]))
        raise KeyboardInterrupt

    monkeypatch.setattr(auth_cli.oauth, "exchange_code", lambda **kw: make_token_response(refresh_token="issued"))
    monkeypatch.setattr(auth_cli, "_probe_company_name", lambda *a, **k: None)
    monkeypatch.setattr(oauth, "revoke_token", interrupted)

    store = cli_settings.open_store(credentials=credentials)
    attempt = new_attempt()
    with pytest.raises(KeyboardInterrupt):
        auth_cli._finish_connect(
            cli_settings, store, registry["acme_main"], credentials, attempt, _connect_params(attempt),
        )
    assert attempts == ["issued"]


def test_an_import_whose_client_cant_be_built_is_refused_like_any_failed_read(
    cli_settings, registry, credentials, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The refresh spent the supplied token, so the new pair is the only live
    one. A read that can't start refuses it like any other failed read."""
    revoked: list[str] = []
    monkeypatch.setenv("QBO_IMPORT_REFRESH_TOKEN", "zq-imported-refresh-token-78")
    monkeypatch.setattr(auth_cli.oauth, "refresh_tokens", lambda **kw: make_token_response(refresh_token="issued"))
    monkeypatch.setattr(oauth, "revoke_token", lambda **kw: revoked.append(str(kw["token"])))
    _unbuildable_accounting_client(monkeypatch)

    code = auth_cli.cmd_import(cli_settings, registry, argparse.Namespace(entity="acme_main"))
    out = capsys.readouterr().out
    assert code == 2, out
    assert revoked == ["issued"], "the new pair was left live at Intuit"
    assert cli_settings.open_store(credentials=credentials).get_row(REALM_MAIN) is None


def test_an_import_stopped_before_storing_revokes_the_new_pair(
    cli_settings, registry, credentials, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The same for an error no refusal names: the new pair is revoked and the
    error still reaches the caller."""
    revoked: list[str] = []

    def broken(*args: object, **kwargs: object) -> str:
        raise RuntimeError("synthetic")

    monkeypatch.setenv("QBO_IMPORT_REFRESH_TOKEN", "zq-imported-refresh-token-78")
    monkeypatch.setattr(auth_cli.oauth, "refresh_tokens", lambda **kw: make_token_response(refresh_token="issued"))
    monkeypatch.setattr(auth_cli, "_probe_company_name", broken)
    monkeypatch.setattr(oauth, "revoke_token", lambda **kw: revoked.append(str(kw["token"])))

    with pytest.raises(RuntimeError):
        auth_cli.cmd_import(cli_settings, registry, argparse.Namespace(entity="acme_main"))
    assert revoked == ["issued"], "the new pair was left live at Intuit"
    assert "Stopped before the credential was stored" in capsys.readouterr().out


def _failing_line(monkeypatch: pytest.MonkeyPatch, line: str, failure: BaseException) -> None:
    printed = auth_cli.out

    def closing(message: str = "") -> None:
        if message.startswith(line):
            raise failure
        printed(message)

    monkeypatch.setattr(auth_cli, "out", closing)


@pytest.mark.parametrize(
    ("line", "failure", "expected"),
    [
        pytest.param("Stopped before", BrokenPipeError("the terminal closed"), RuntimeError, id="the-stopped-line"),
        pytest.param(
            "  (the unstored credential was revoked", ValueError("I/O operation on closed file"), RuntimeError,
            id="the-revoked-line",
        ),
        pytest.param("Stopped before", KeyboardInterrupt(), KeyboardInterrupt, id="an-interrupt-in-the-stopped-line"),
    ],
)
def test_a_failing_line_on_the_way_out_still_revokes_the_pair(
    line: str, failure: BaseException, expected: type[BaseException], cli_settings, registry, credentials,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal that has closed can fail a line printed on the way out. The
    revoke still runs, and the caller gets the error that ended the step, or
    the interrupt that cut the line short."""
    revoked: list[str] = []

    def broken(*args: object, **kwargs: object) -> str:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(auth_cli.oauth, "exchange_code", lambda **kw: make_token_response(refresh_token="issued"))
    monkeypatch.setattr(auth_cli, "_probe_company_name", broken)
    monkeypatch.setattr(oauth, "revoke_token", lambda **kw: revoked.append(str(kw["token"])))
    _failing_line(monkeypatch, line, failure)

    store = cli_settings.open_store(credentials=credentials)
    attempt = new_attempt()
    with pytest.raises(BaseException) as caught:
        auth_cli._finish_connect(
            cli_settings, store, registry["acme_main"], credentials, attempt, _connect_params(attempt),
        )
    assert revoked == ["issued"], "the issued pair was left live at Intuit"
    assert caught.type is expected


@pytest.mark.parametrize("path", ["connect", "import"])
@pytest.mark.parametrize(
    ("failed", "line"),
    [
        pytest.param(StaleConsentError("synthetic"), "STALE CEREMONY", id="stale"),
        pytest.param(QboMcpError("synthetic"), "Could not store", id="store-error"),
    ],
)
def test_a_failing_line_after_a_failed_commit_still_revokes_the_pair(
    path: str, failed: QboMcpError, line: str, cli_settings, registry, credentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same when the commit itself refuses: the pair is revoked and the
    step ends with its own exit status."""
    from qbo_multicompany_mcp.store import TokenStore

    def failing(self: TokenStore, **kwargs: object) -> None:
        raise failed

    revoked: list[str] = []
    monkeypatch.setattr(auth_cli.oauth, "exchange_code", lambda **kw: make_token_response(refresh_token="issued"))
    monkeypatch.setattr(auth_cli.oauth, "refresh_tokens", lambda **kw: make_token_response(refresh_token="issued"))
    monkeypatch.setattr(auth_cli, "_probe_company_name", lambda *a, **k: "Acme Main")
    monkeypatch.setattr(TokenStore, "commit_consent", failing)
    monkeypatch.setattr(oauth, "revoke_token", lambda **kw: revoked.append(str(kw["token"])))
    monkeypatch.setenv("QBO_IMPORT_REFRESH_TOKEN", "zq-imported-refresh-token-78")
    _failing_line(monkeypatch, line, BrokenPipeError("the terminal closed"))

    if path == "connect":
        store = cli_settings.open_store(credentials=credentials)
        attempt = new_attempt()
        code = auth_cli._finish_connect(
            cli_settings, store, registry["acme_main"], credentials, attempt, _connect_params(attempt),
        )
    else:
        code = auth_cli.cmd_import(cli_settings, registry, argparse.Namespace(entity="acme_main"))
    assert code == 1
    assert revoked == ["issued"], "the new pair was left live at Intuit"


@pytest.mark.parametrize("path", ["connect", "import"])
def test_a_stored_pair_whose_journal_cant_be_cleared_is_kept(
    path: str, cli_settings, registry, credentials, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """The store holds the pair by then. Read as a failed commit, it would be
    revoked and the connection broken."""
    import logging

    from qbo_multicompany_mcp.errors import StoreError
    from qbo_multicompany_mcp.store import TokenStore

    def full(self: TokenStore) -> None:
        raise StoreError("could not write the token store: No space left on device")

    revoked: list[str] = []
    monkeypatch.setattr(auth_cli.oauth, "exchange_code", lambda **kw: make_token_response(refresh_token="issued"))
    monkeypatch.setattr(auth_cli.oauth, "refresh_tokens", lambda **kw: make_token_response(refresh_token="issued"))
    monkeypatch.setattr(auth_cli, "_probe_company_name", lambda *a, **k: "Acme Main")
    monkeypatch.setattr(TokenStore, "_clear_journal_locked", full)
    monkeypatch.setattr(oauth, "revoke_token", lambda **kw: revoked.append(str(kw["token"])))
    monkeypatch.setenv("QBO_IMPORT_REFRESH_TOKEN", "zq-imported-refresh-token-78")

    store = cli_settings.open_store(credentials=credentials)
    with caplog.at_level(logging.WARNING):
        if path == "connect":
            attempt = new_attempt()
            code = auth_cli._finish_connect(
                cli_settings, store, registry["acme_main"], credentials, attempt, _connect_params(attempt),
            )
        else:
            code = auth_cli.cmd_import(cli_settings, registry, argparse.Namespace(entity="acme_main"))
    assert code == 0
    assert revoked == [], "a stored pair was revoked"
    row = cli_settings.open_store(credentials=credentials).get_row(REALM_MAIN)
    assert row is not None and row.refresh_token == "issued"
    assert "journal could not be cleared" in caplog.text


def test_a_mismatch_cannot_be_confirmed_without_a_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Nobody is there to confirm, so the answer is no."""
    monkeypatch.setattr(auth_cli.sys.stdin, "isatty", lambda: False, raising=False)
    assert auth_cli._confirm("Store anyway?") is False
    assert "not an interactive terminal" in capsys.readouterr().out


def test_status_masks_realm_ids_to_the_last_four(
    cli_settings, registry, credentials, actor, capsys
) -> None:
    """So status output can be pasted into a bug report safely."""
    import argparse

    store = cli_settings.open_store(credentials=credentials)
    store.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN,
        response=make_token_response(), actor=actor,
    )
    auth_cli.cmd_status(cli_settings, registry, argparse.Namespace())
    out = capsys.readouterr().out

    assert "realm id (last 4):       ...0001" in out
    assert REALM_MAIN not in out
    assert REALM_OPS not in out


# --------------------------------------------------------------------------
# revoke
#
# A race test that calls `revoke_row` proves nothing about revocation: it still
# passes with the CLI command and `oauth.revoke_token` replaced by no-ops. These
# assert the HTTP request and the local removal.
# --------------------------------------------------------------------------


def _recording_transport() -> tuple[httpx.Client, list[httpx.Request]]:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, request=request)

    return httpx.Client(transport=httpx.MockTransport(handler)), sent


def test_revoke_token_posts_the_token_to_the_pinned_revocation_endpoint() -> None:
    from qbo_multicompany_mcp.constants import REVOKE_ENDPOINT

    client, sent = _recording_transport()
    with client:
        oauth.revoke_token(
            token="refresh-1",
            client_id="test-client-id",
            client_secret="test-client-secret",
            environment="sandbox",
            client=client,
        )

    assert len(sent) == 1, "no request was made to the revocation endpoint"
    request = sent[0]
    assert request.method == "POST"
    assert str(request.url) == REVOKE_ENDPOINT
    assert json.loads(request.content) == {"token": "refresh-1"}
    # The app authenticates itself; a revocation that skipped this would be
    # refused, and a revocation that is refused is one that did not happen.
    assert request.headers["authorization"].startswith("Basic ")


def test_revoke_token_raises_when_intuit_refuses() -> None:
    """A revocation that failed must not read as one that succeeded."""

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_token"}, request=request)

    client = httpx.Client(transport=httpx.MockTransport(refuse))
    with client, pytest.raises(oauth.OAuthHttpError):
        oauth.revoke_token(
            token="refresh-1",
            client_id="test-client-id",
            client_secret="test-client-secret",
            environment="sandbox",
            client=client,
        )


def test_revoke_submits_the_stored_token_and_removes_the_row(
    cli_settings, registry, credentials, actor, capsys
) -> None:
    store = cli_settings.open_store(credentials=credentials)
    store.commit_consent(
        entity="acme_main",
        realm_id=REALM_MAIN,
        response=make_token_response(refresh_token="refresh-to-revoke"),
        actor=actor,
    )
    assert store.get_row(REALM_MAIN) is not None

    revoked: list[dict[str, object]] = []
    original = oauth.revoke_token

    def recording_revoke(**kwargs: object) -> None:
        revoked.append(dict(kwargs))

    auth_cli.oauth.revoke_token = recording_revoke  # type: ignore[assignment]
    try:
        code = auth_cli.cmd_revoke(
            cli_settings, registry, argparse.Namespace(entity="acme_main", all=False)
        )
    finally:
        auth_cli.oauth.revoke_token = original  # type: ignore[assignment]

    assert code == 0
    assert [call["token"] for call in revoked] == ["refresh-to-revoke"], (
        "the stored refresh token was not submitted for revocation"
    )
    assert cli_settings.open_store(credentials=credentials).get_row(REALM_MAIN) is None, (
        "the row survived a revoke, so the credential is still usable locally"
    )
    out = capsys.readouterr().out
    assert "revoked at Intuit" in out
    assert "removed from the local store" in out


def test_revoke_with_nothing_stored_still_cancels_a_pending_ceremony(
    cli_settings, registry, credentials, actor, capsys
) -> None:
    """`auth revoke` on an entity with nothing stored must not skip the store,
    or a consent ceremony that began with no row commits afterwards. It submits
    nothing to Intuit (there is no token), still advances the realm's consent
    epoch through the store, and says so."""
    store = cli_settings.open_store(credentials=credentials)
    baseline = store.consent_baseline(REALM_MAIN)
    assert store.get_row(REALM_MAIN) is None

    revoked: list[dict[str, object]] = []
    original = oauth.revoke_token

    def recording_revoke(**kwargs: object) -> None:
        revoked.append(dict(kwargs))

    auth_cli.oauth.revoke_token = recording_revoke  # type: ignore[assignment]
    try:
        code = auth_cli.cmd_revoke(
            cli_settings, registry, argparse.Namespace(entity="acme_main", all=False)
        )
    finally:
        auth_cli.oauth.revoke_token = original  # type: ignore[assignment]

    assert code == 0
    assert revoked == [], "there was no token to submit"
    reopened = cli_settings.open_store(credentials=credentials)
    assert reopened.consent_baseline(REALM_MAIN).epoch == baseline.epoch + 1, (
        "the CLI skipped the store, so a ceremony begun before this revoke would commit"
    )
    out = capsys.readouterr().out
    assert "nothing stored" in out
    assert "cancelled" in out


def test_revoke_removes_the_row_even_when_intuit_refuses(
    cli_settings, registry, credentials, actor, capsys
) -> None:
    """A token that cannot be revoked is one we should certainly stop using."""
    from qbo_multicompany_mcp.errors import QboMcpError

    store = cli_settings.open_store(credentials=credentials)
    store.commit_consent(
        entity="acme_main",
        realm_id=REALM_MAIN,
        response=make_token_response(refresh_token="refresh-to-revoke"),
        actor=actor,
    )

    original = oauth.revoke_token

    def failing_revoke(**kwargs: object) -> None:
        raise QboMcpError("Intuit refused the revocation")

    auth_cli.oauth.revoke_token = failing_revoke  # type: ignore[assignment]
    try:
        code = auth_cli.cmd_revoke(
            cli_settings, registry, argparse.Namespace(entity="acme_main", all=False)
        )
    finally:
        auth_cli.oauth.revoke_token = original  # type: ignore[assignment]

    assert code == 1
    assert cli_settings.open_store(credentials=credentials).get_row(REALM_MAIN) is None
    assert "removing locally anyway" in capsys.readouterr().out


def test_a_realm_mismatch_prints_last_four_of_both_realms_only(
    cli_settings, registry, credentials, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The full realm never leaves this tool: printed whole, the approved realm
    would land in whatever the operator's terminal is being logged to."""
    monkeypatch.setattr(auth_cli.oauth, "exchange_code", lambda **kw: make_token_response())
    store = cli_settings.open_store(credentials=credentials)
    attempt = new_attempt()
    code = auth_cli._finish_connect(
        cli_settings, store, registry["acme_main"], credentials, attempt,
        CallbackParams(code="thecode", state=attempt.state, realm_id=REALM_OPS),
    )
    out = capsys.readouterr().out
    assert code == 2
    assert REALM_OPS not in out and REALM_MAIN not in out
    assert "...0002" in out and "...0001" in out


# --------------------------------------------------------------------------
# Revoke through the binding, and import verifying before it commits
# --------------------------------------------------------------------------


def _recording_revoke(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    revoked: list[dict[str, object]] = []
    monkeypatch.setattr(oauth, "revoke_token", lambda **kw: revoked.append(dict(kw)))
    return revoked


def _tamper(store, realm: str, **fields) -> None:
    from dataclasses import replace

    with store.lock():
        document = store._load_locked()
        document.rows[realm] = replace(document.rows[realm], **fields)
        store._commit_locked(document)


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        pytest.param({"environment": "production"}, "environment", id="environment"),
        pytest.param({"client_id_fingerprint": "f" * 64}, "client id", id="client-id"),
        pytest.param({"entity": "acme_ops"}, "entity", id="entity"),
    ],
)
def test_revoke_refuses_a_mismatched_binding_and_leaves_the_row_intact(
    cli_settings, registry, credentials, actor, monkeypatch: pytest.MonkeyPatch, capsys,
    fields: dict, expected: str,
) -> None:
    """A revoke removes the row whether Intuit accepts or not, so without a
    binding check a wrong QBO_ENV or a stale QBO_CLIENT_ID would destroy a
    valid connection. It takes the same binding as every other send: a
    mismatch is a configuration error, nothing is submitted, the row stays,
    exit 2."""
    store = cli_settings.open_store(credentials=credentials)
    store.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN,
        response=make_token_response(refresh_token="keep-me"), actor=actor,
    )
    _tamper(store, REALM_MAIN, **fields)
    revoked = _recording_revoke(monkeypatch)

    code = auth_cli.cmd_revoke(
        cli_settings, registry, argparse.Namespace(entity="acme_main", all=False)
    )

    assert code == 2
    assert revoked == [], "a mis-bound row's token was submitted for revocation"
    row = cli_settings.open_store(credentials=credentials).get_row(REALM_MAIN)
    assert row is not None and row.refresh_token == "keep-me", "the row was removed"
    out = capsys.readouterr().out
    assert "configuration problem" in out and expected in out
    assert "removed from the local store" not in out


def test_revoke_all_leaves_a_mismatched_row_intact_and_still_revokes_the_others(
    cli_settings, registry, credentials, actor, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    store = cli_settings.open_store(credentials=credentials)
    store.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN,
        response=make_token_response(refresh_token="main-keep"), actor=actor,
    )
    store.commit_consent(
        entity="acme_ops", realm_id=REALM_OPS,
        response=make_token_response(refresh_token="ops-revoke"), actor=actor,
    )
    _tamper(store, REALM_MAIN, client_id_fingerprint="f" * 64)
    revoked = _recording_revoke(monkeypatch)

    code = auth_cli.cmd_revoke(cli_settings, registry, argparse.Namespace(entity=None, all=True))

    assert code == 2
    assert [call["token"] for call in revoked] == ["ops-revoke"]
    reopened = cli_settings.open_store(credentials=credentials)
    assert reopened.get_row(REALM_MAIN) is not None
    assert reopened.get_row(REALM_OPS) is None


def test_revoke_removes_a_malformed_row_without_a_request_and_exits_1(
    cli_settings, registry, credentials, actor, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The control for the binding: a malformed row can't be bound, so nothing
    is submitted and revoke removes it locally. A token in it may still be live
    at Intuit, so the run says so and exits 1, which "A suspected leak" in
    docs/production.md reads as the signal to disconnect the app in QuickBooks."""
    from .test_store_lifecycle import _write_document

    store = cli_settings.open_store(credentials=credentials)
    store.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN, response=make_token_response(), actor=actor
    )
    document = store.read().to_json()
    document["rows"][REALM_MAIN] = {"not": "a row"}
    _write_document(store, document)
    revoked = _recording_revoke(monkeypatch)

    code = auth_cli.cmd_revoke(
        cli_settings, registry, argparse.Namespace(entity="acme_main", all=False)
    )

    assert code == 1
    assert revoked == []
    assert cli_settings.open_store(credentials=credentials).get_row(REALM_MAIN) is None
    out = capsys.readouterr().out
    assert "was not revoked at Intuit" in out
    assert "disconnect the app in QuickBooks" in out
    assert "acme_main: removed from the local store" in out


@pytest.mark.parametrize("stored", ["malformed", "nothing"])
def test_a_store_failure_on_a_row_without_a_token_does_not_end_a_revoke_all(
    cli_settings, registry, credentials, actor, monkeypatch: pytest.MonkeyPatch, capsys, stored
) -> None:
    """Answering a suspected leak (docs/production.md), the next company still
    has to go through, whatever the first one held."""
    from qbo_multicompany_mcp.store import TokenStore

    from .test_store_lifecycle import _write_document

    store = cli_settings.open_store(credentials=credentials)
    store.commit_consent(
        entity="acme_ops", realm_id=REALM_OPS, response=make_token_response(refresh_token="ops"), actor=actor
    )
    if stored == "malformed":
        store.commit_consent(
            entity="acme_main", realm_id=REALM_MAIN, response=make_token_response(), actor=actor
        )
        document = store.read().to_json()
        document["rows"][REALM_MAIN] = {"not": "a row"}
        _write_document(store, document)
    real = TokenStore.revoke_row

    def fails_for_the_first(self: TokenStore, realm_id: str):
        if realm_id == REALM_MAIN:
            raise QboMcpError("synthetic")
        return real(self, realm_id)

    submitted: list[str] = []
    monkeypatch.setattr(TokenStore, "revoke_row", fails_for_the_first)
    monkeypatch.setattr(oauth, "revoke_token", lambda **kw: submitted.append(str(kw["token"])))
    code = auth_cli.cmd_revoke(cli_settings, registry, argparse.Namespace(entity=None, all=True))
    out = capsys.readouterr().out
    assert code == 1
    assert submitted == ["ops"], "the second company was revoked at Intuit"
    assert "acme_main:" in out and "run revoke again" in out
    assert "acme_ops: removed from the local store" in out


def test_a_revoke_that_cant_build_its_client_removes_nothing(
    cli_settings, registry, credentials, actor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Building the TLS context opens files, a key log the environment names
    among them. A revoke that can't send must not remove what it was to revoke,
    or a rerun once that's fixed finds nothing to revoke."""
    store = cli_settings.open_store(credentials=credentials)
    for key, realm in (("acme_main", REALM_MAIN), ("acme_ops", REALM_OPS)):
        store.commit_consent(entity=key, realm_id=realm, response=make_token_response(), actor=actor)
    submitted: list[str] = []

    def unbuildable(**kwargs: object) -> None:
        raise FileNotFoundError("the key log the environment names")

    monkeypatch.setattr(oauth, "isolated_client", unbuildable)
    monkeypatch.setattr(oauth, "revoke_token", lambda **kw: submitted.append(str(kw["token"])))
    with pytest.raises(StartupError, match="nothing was revoked or removed"):
        auth_cli.cmd_revoke(cli_settings, registry, argparse.Namespace(entity=None, all=True))
    reopened = cli_settings.open_store(credentials=credentials)
    assert submitted == []
    assert reopened.get_row(REALM_MAIN) is not None and reopened.get_row(REALM_OPS) is not None


def test_a_cookie_from_one_revoke_never_reaches_the_next(
    cli_settings, registry, credentials, actor, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """revoke --all shares one client. A cookie Intuit's first answer set would
    ride on the next request, the policy refuses any token request carrying
    one, and that company would be removed locally with nothing sent."""
    store = cli_settings.open_store(credentials=credentials)
    for key, realm in (("acme_main", REALM_MAIN), ("acme_ops", REALM_OPS)):
        store.commit_consent(entity=key, realm_id=realm, response=make_token_response(), actor=actor)
    cookies: list[str | None] = []

    def answering(request: httpx.Request) -> httpx.Response:
        cookies.append(request.headers.get("cookie"))
        return httpx.Response(200, headers={"Set-Cookie": "lb=synthetic; Path=/"})

    monkeypatch.setattr(
        oauth, "isolated_client", lambda **kw: httpx.Client(transport=httpx.MockTransport(answering), **kw)
    )
    code = auth_cli.cmd_revoke(cli_settings, registry, argparse.Namespace(entity=None, all=True))
    out = capsys.readouterr().out
    assert code == 0, out
    assert cookies == [None, None], "a revoke went out carrying a cookie, or not at all"
    assert out.count("revoked at Intuit") == 2


def test_a_pair_stored_under_another_app_during_the_revoke_is_not_sent(
    cli_settings, registry, credentials, actor, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The binding check every other send makes: a consent from a process
    configured with another app isn't revoked under this one's credentials."""
    from qbo_multicompany_mcp.store import TokenStore

    from .test_store_lifecycle import _write_document

    store = cli_settings.open_store(credentials=credentials)
    store.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN, response=make_token_response(refresh_token="other-app"), actor=actor
    )
    document = store.read().to_json()
    document["rows"][REALM_MAIN]["client_id_fingerprint"] = "another-app"
    _write_document(store, document)
    submitted: list[str] = []
    monkeypatch.setattr(TokenStore, "usable_row", lambda self, realm_id, **kw: None)
    monkeypatch.setattr(oauth, "revoke_token", lambda **kw: submitted.append(str(kw["token"])))
    code = auth_cli.cmd_revoke(cli_settings, registry, argparse.Namespace(entity="acme_main", all=False))
    assert code == 1
    assert submitted == []
    assert "belongs to another configuration, so it was not sent" in capsys.readouterr().out


def test_a_pair_another_app_stored_during_the_revoke_request_is_not_sent(
    cli_settings, registry, credentials, actor, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The main revoke's request runs unlocked too, and a consent from a process
    configured with another app can land in it. The pair the removal hands back
    gets the same binding check, so it isn't posted under this app's
    credentials."""
    from .test_store_lifecycle import _write_document

    store = cli_settings.open_store(credentials=credentials)
    store.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN, response=make_token_response(refresh_token="first"), actor=actor
    )
    submitted: list[str] = []

    def revoke_while_another_app_connects(**kwargs: object) -> None:
        submitted.append(str(kwargs["token"]))
        if len(submitted) == 1:
            store.commit_consent(
                entity="acme_main", realm_id=REALM_MAIN,
                response=make_token_response(refresh_token="other-app"), actor=actor,
            )
            document = store.read().to_json()
            document["rows"][REALM_MAIN]["client_id_fingerprint"] = "another-app"
            _write_document(store, document)

    monkeypatch.setattr(oauth, "revoke_token", revoke_while_another_app_connects)
    code = auth_cli.cmd_revoke(cli_settings, registry, argparse.Namespace(entity="acme_main", all=False))
    assert code == 1
    assert submitted == ["first"], "another app's pair was posted under this app's credentials"
    assert "belongs to another configuration, so it was not sent" in capsys.readouterr().out


@pytest.mark.parametrize("seen", ["malformed", "nothing"])
def test_a_pair_stored_during_the_revoke_is_revoked_too(
    cli_settings, registry, credentials, actor, monkeypatch: pytest.MonkeyPatch, capsys, seen
) -> None:
    """A consent can commit between this company's read and its removal. The
    removal then hands the new row back, and left alone its pair would be live
    at Intuit with no local copy."""
    from qbo_multicompany_mcp.errors import MalformedRowError
    from qbo_multicompany_mcp.store import TokenStore

    store = cli_settings.open_store(credentials=credentials)
    store.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN, response=make_token_response(refresh_token="landed"), actor=actor
    )

    def read_before_it_landed(self: TokenStore, realm_id: str, **kwargs: object):
        if seen == "malformed":
            raise MalformedRowError("synthetic")
        return None

    submitted: list[str] = []
    monkeypatch.setattr(TokenStore, "usable_row", read_before_it_landed)
    monkeypatch.setattr(oauth, "revoke_token", lambda **kw: submitted.append(str(kw["token"])))
    auth_cli.cmd_revoke(cli_settings, registry, argparse.Namespace(entity="acme_main", all=False))
    assert submitted == ["landed"]
    assert "a pair stored during the revoke was revoked as well" in capsys.readouterr().out
    assert cli_settings.open_store(credentials=credentials).get_row(REALM_MAIN) is None


def test_an_interrupt_after_a_finished_step_never_claims_nothing_changed(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """revoke --all can finish one company before the operator interrupts the
    next, so the message can't say nothing changed."""
    import logging
    import sys

    def done_then_interrupted(settings: object, registry: object, args: object) -> int:
        auth_cli.out("acme_main: revoked at Intuit")
        raise KeyboardInterrupt

    monkeypatch.setattr(auth_cli, "cmd_status", done_then_interrupted)
    monkeypatch.setattr(auth_cli, "settings_from_args", lambda args: argparse.Namespace(load_registry=lambda: None))
    root = logging.getLogger()
    handlers, level, hook = list(root.handlers), root.level, sys.excepthook
    try:
        code = auth_cli.main(["status"])
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
        sys.excepthook = hook
    err = capsys.readouterr().err
    assert code == 1
    assert "nothing was changed" not in err
    assert "may have finished" in err and "status" in err


def test_import_leaves_the_existing_consent_intact_when_verification_fails(
    mock_settings, registry, credentials, actor, monkeypatch: pytest.MonkeyPatch, mock_intuit
) -> None:
    """A healthy pair is stored. The operator imports a token that cannot read
    this realm (the wrong company's, say). Committing the replacement first
    and verifying second would let a failed replacement destroy the connection
    that worked. So import verifies with the fresh access token, storing
    nothing, and commits only a verified pair; the unstored pair is revoked at
    Intuit."""
    monkeypatch.setenv("QBO_CLIENT_ID", credentials.client_id)
    monkeypatch.setenv("QBO_CLIENT_SECRET", credentials.client_secret)
    monkeypatch.setenv("QBO_IMPORT_REFRESH_TOKEN", "someone-elses-token")
    store = mock_settings.open_store(credentials=credentials)
    store.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN,
        response=make_token_response(access_token="healthy-access", refresh_token="healthy-refresh"),
        actor=actor,
    )
    monkeypatch.setattr(
        oauth, "refresh_tokens",
        lambda **kw: make_token_response(access_token="rejected", refresh_token="rejected-refresh"),
    )
    mock_intuit.unauthorized_tokens.add("rejected")
    revoked = _recording_revoke(monkeypatch)

    code = auth_cli.cmd_import(mock_settings, registry, argparse.Namespace(entity="acme_main"))

    assert code == 2
    row = mock_settings.open_store(credentials=credentials).get_row(REALM_MAIN)
    assert row is not None, "the healthy consent was removed"
    assert row.refresh_token == "healthy-refresh"
    assert row.auth_generation == 1
    assert [call["token"] for call in revoked] == ["rejected-refresh"], (
        "the unstored replacement was not revoked"
    )
    assert "healthy-access" not in {r[3] for r in mock_intuit.requests}, (
        "the stored credential was sent during an import"
    )


@pytest.mark.parametrize("name", ["\ufffe", "\uffff", "\ud800", "\udfff", "\u2028", "\ufeff", "Ok"])
def test_the_yaml_scalar_parses_back_to_the_exact_text(name: str) -> None:
    import yaml

    assert yaml.safe_load(f"display_name: {auth_cli._yaml_scalar(name)}") == {"display_name": name}


def test_a_pair_that_cant_be_stored_is_not_left_live(
    cli_settings, registry, credentials, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The exchange spent the grant, so a rerun can't fetch this pair again and
    nobody holds it."""
    from qbo_multicompany_mcp.store import TokenStore

    def failing(self: TokenStore, **kwargs: object) -> None:
        raise QboMcpError("synthetic")

    revoked: list[str] = []
    monkeypatch.setattr(auth_cli.oauth, "exchange_code", lambda **kw: make_token_response(refresh_token="issued"))
    monkeypatch.setattr(auth_cli, "_probe_company_name", lambda *a, **k: "Acme Main")
    monkeypatch.setattr(TokenStore, "commit_consent", failing)
    monkeypatch.setattr(oauth, "revoke_token", lambda **kw: revoked.append(str(kw["token"])))

    store = cli_settings.open_store(credentials=credentials)
    attempt = new_attempt()
    code = auth_cli._finish_connect(
        cli_settings, store, registry["acme_main"], credentials, attempt, _connect_params(attempt)
    )
    assert code == 1
    assert revoked == ["issued"]
    assert "Could not store the credential" in capsys.readouterr().out


def test_an_imported_pair_that_cant_be_stored_is_not_left_live(
    cli_settings, registry, credentials, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The refresh spent the supplied token, so the new pair is the only live one
    and nobody holds it."""
    from qbo_multicompany_mcp.store import TokenStore

    def failing(self: TokenStore, **kwargs: object) -> None:
        raise QboMcpError("synthetic")

    revoked: list[str] = []
    monkeypatch.setenv("QBO_IMPORT_REFRESH_TOKEN", "zq-imported-refresh-token-78")
    monkeypatch.setattr(auth_cli.oauth, "refresh_tokens", lambda **kw: make_token_response(refresh_token="issued"))
    monkeypatch.setattr(auth_cli, "_probe_company_name", lambda *a, **k: "Acme Main")
    monkeypatch.setattr(TokenStore, "commit_consent", failing)
    monkeypatch.setattr(oauth, "revoke_token", lambda **kw: revoked.append(str(kw["token"])))

    code = auth_cli.cmd_import(cli_settings, registry, argparse.Namespace(entity="acme_main"))
    assert code == 1
    assert revoked == ["issued"]
    assert "Could not store the credential" in capsys.readouterr().out


def test_one_companys_store_failure_does_not_end_a_revoke_all(
    cli_settings, registry, credentials, actor, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Answering a suspected leak (docs/production.md), the next company still
    has to go through. A rerun finishes the first, so the exit status says so."""
    from qbo_multicompany_mcp.store import TokenStore

    store = cli_settings.open_store(credentials=credentials)
    for key, realm in (("acme_main", REALM_MAIN), ("acme_ops", REALM_OPS)):
        store.commit_consent(entity=key, realm_id=realm, response=make_token_response(), actor=actor)
    real = TokenStore.revoke_row

    def fails_for_the_first(self: TokenStore, realm_id: str):
        if realm_id == REALM_MAIN:
            raise QboMcpError("synthetic")
        return real(self, realm_id)

    submitted: list[str] = []
    monkeypatch.setattr(TokenStore, "revoke_row", fails_for_the_first)
    monkeypatch.setattr(oauth, "revoke_token", lambda **kw: submitted.append(str(kw["token"])))
    code = auth_cli.cmd_revoke(cli_settings, registry, argparse.Namespace(entity=None, all=True))
    out = capsys.readouterr().out
    assert code == 1
    assert len(submitted) == 2, "both companies were revoked at Intuit"
    assert "acme_main: the local row could not be removed" in out
    assert "acme_ops: removed from the local store" in out


def test_one_companys_unreadable_store_does_not_end_a_revoke_all(
    cli_settings, registry, credentials, actor, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from qbo_multicompany_mcp.store import TokenStore

    store = cli_settings.open_store(credentials=credentials)
    for key, realm in (("acme_main", REALM_MAIN), ("acme_ops", REALM_OPS)):
        store.commit_consent(entity=key, realm_id=realm, response=make_token_response(), actor=actor)
    real = TokenStore.usable_row

    def fails_for_the_first(self: TokenStore, realm_id: str, *, entity_key: str):
        if realm_id == REALM_MAIN:
            raise QboMcpError("synthetic")
        return real(self, realm_id, entity_key=entity_key)

    monkeypatch.setattr(TokenStore, "usable_row", fails_for_the_first)
    monkeypatch.setattr(oauth, "revoke_token", lambda **kw: None)
    code = auth_cli.cmd_revoke(cli_settings, registry, argparse.Namespace(entity=None, all=True))
    out = capsys.readouterr().out
    assert code == 1
    assert "acme_main: nothing was submitted" in out
    assert "acme_ops: removed from the local store" in out


def test_status_stops_when_the_store_cannot_be_read(
    cli_settings, registry, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """'Never authorized' over a store that only lost its key sends an operator
    to connect, and a new key can't read the old store."""
    from qbo_multicompany_mcp.store import TokenStore

    def unavailable(self: TokenStore):
        raise ConfigurationError("no key")

    monkeypatch.setattr(TokenStore, "read", unavailable)
    code = auth_cli.cmd_status(cli_settings, registry, argparse.Namespace())
    out = capsys.readouterr().out
    assert code == 2
    assert "Token store unavailable" in out
    assert "never authorized" not in out and "connect with" not in out


def test_revoke_exits_nonzero_when_a_pair_refreshed_during_it_stays_live(
    cli_settings, registry, credentials, actor, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Answering a suspected leak (docs/production.md) reads this exit status.
    A token nobody could confirm dead is never a success."""
    from .test_token_store import make_record, write_journal

    store = cli_settings.open_store(credentials=credentials)
    store.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN,
        response=make_token_response(refresh_token="first"), actor=actor,
    )
    submitted: list[str] = []

    def revoke_and_race(**kwargs: object) -> None:
        submitted.append(str(kwargs["token"]))
        if len(submitted) == 1:
            write_journal(
                store,
                [make_record(store, credentials, generation_from=1, refresh_token="issued-during")],
            )
            return
        raise QboMcpError("Intuit refused the revocation")

    monkeypatch.setattr(oauth, "revoke_token", revoke_and_race)
    code = auth_cli.cmd_revoke(
        cli_settings, registry, argparse.Namespace(entity="acme_main", all=False)
    )
    out = capsys.readouterr().out
    assert code == 1
    assert submitted == ["first", "issued-during"]
    assert "could not be revoked" in out and "disconnect the app" in out


def test_revoke_also_revokes_a_pair_refreshed_during_its_own_request(
    cli_settings, registry, credentials, actor, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The revocation request runs unlocked. A refresh that lands during it
    (another process, journalled result, then a crash) is recovered by
    revoke_row and handed back; the CLI revokes that newer token too, so
    nothing is left live at Intuit with no local copy."""
    from .test_token_store import make_record, write_journal

    store = cli_settings.open_store(credentials=credentials)
    store.commit_consent(
        entity="acme_main", realm_id=REALM_MAIN,
        response=make_token_response(refresh_token="first"), actor=actor,
    )
    submitted: list[str] = []

    def revoke_and_race(**kwargs: object) -> None:
        submitted.append(str(kwargs["token"]))
        if len(submitted) == 1:
            write_journal(
                store,
                [make_record(store, credentials, generation_from=1, refresh_token="issued-during")],
            )

    monkeypatch.setattr(oauth, "revoke_token", revoke_and_race)
    code = auth_cli.cmd_revoke(
        cli_settings, registry, argparse.Namespace(entity="acme_main", all=False)
    )
    assert code == 0
    assert submitted == ["first", "issued-during"]
    assert cli_settings.open_store(credentials=credentials).get_row(REALM_MAIN) is None
    assert "revoked as well" in capsys.readouterr().out
