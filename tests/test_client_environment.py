"""Every HTTP client this package builds reads nothing from the environment.

With ``HTTPS_PROXY`` set to a local intercepting proxy and ``SSL_CERT_FILE``
to that proxy's CA, an accounting client built with httpx's default
``trust_env=True`` passes every host check and then hands its bearer to the
proxy. Token POSTs and discovery built the same way go the same route. These
tests are the package's own guarantee, whoever starts it.

What the installed HTTP stack reads from the environment is three things
(httpx 0.28.1, the version ``uv.lock`` pins):

* a proxy, through ``urllib.request.getproxies()`` -- the ``*_PROXY``
  variables, and on macOS the system proxy settings when none is set
  (``httpx/_utils.py:37``, reached only when ``trust_env`` is on,
  ``httpx/_client.py:685``);
* a CA file or directory, ``SSL_CERT_FILE`` / ``SSL_CERT_DIR``
  (``httpx/_config.py:34-37``, only when ``trust_env`` is on -- including the
  ``trust_env`` of a transport built explicitly, which the client's own flag
  does not reach);
* ``SSLKEYLOGFILE``, which the standard library's
  ``ssl.create_default_context`` honours whatever httpx is told, unless the
  interpreter ignores the environment (``python -I``/``-E``).

So each client is tested three ways: a proxy the environment names must see
nothing; an interceptor whose certificate comes from a CA the environment
names must receive no request; and no TLS session secret may be written to a
file the environment names. Intuit's host names resolve to loopback here,
and to nothing else: no test in this file can reach the network.
"""

from __future__ import annotations

import ipaddress
import socket
import socketserver
import ssl
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from qbo_multicompany_mcp import attachments, oauth
from qbo_multicompany_mcp.client import AccountingClient
from qbo_multicompany_mcp.constants import (
    API_BASES,
    DISCOVERY_URLS,
    REVOKE_ENDPOINT,
    TOKEN_ENDPOINT,
)

from .conftest import REALM_MAIN, make_token_response
from .test_attachments import FILES_HOST, PDF, _download_settings, public

TOKEN_HOST = urlsplit(TOKEN_ENDPOINT).hostname
REVOKE_HOST = urlsplit(REVOKE_ENDPOINT).hostname
DISCOVERY_HOST = urlsplit(DISCOVERY_URLS["sandbox"]).hostname
SANDBOX_API_HOST = urlsplit(API_BASES["sandbox"]).hostname

#: Every name a client in this file may try to reach. Each resolves to the
#: loopback interceptor, or is refused; none reaches a resolver.
INTERCEPTED_HOSTS = (TOKEN_HOST, REVOKE_HOST, DISCOVERY_HOST, SANDBOX_API_HOST, FILES_HOST)

_LOOPBACK_NAMES = ("127.0.0.1", "::1", "localhost")

#: Short synthetic values (the repository's secret rules match long ones).
REFRESH = "refresh-1"
CODE = "code-1"

#: The environment variables a proxy is read from, upper and lower case.
PROXY_VARIABLES = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
)


# --------------------------------------------------------------------------
# A synthetic CA and a certificate for the names above
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Pki:
    ca: Path
    leaf: Path
    key: Path


def make_pki(folder: Path) -> Pki:
    """A throwaway CA, and a leaf it signs for every intercepted name."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic Test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    names: list[x509.GeneralName] = [x509.DNSName(host) for host in INTERCEPTED_HOSTS]
    names.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, TOKEN_HOST)]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    pki = Pki(ca=folder / "ca.pem", leaf=folder / "leaf.pem", key=folder / "leaf.key")
    pki.ca.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    pki.leaf.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    pki.key.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return pki


# --------------------------------------------------------------------------
# Loopback servers: a proxy canary and a TLS interceptor
# --------------------------------------------------------------------------


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _read_head(connection: Any) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data and len(data) < 65536:
        chunk = connection.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


@dataclass
class ProxyCanary:
    """A loopback proxy that records what it is sent and forwards nothing.

    It answers every request -- a ``CONNECT`` or an absolute-form ``GET`` --
    with 407, which no client here retries.
    """

    heads: list[str] = field(default_factory=list)
    _server: _Server | None = None

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> None:
        canary = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                self.request.settimeout(5)
                try:
                    canary.heads.append(_read_head(self.request).decode("latin-1"))
                    self.request.sendall(
                        b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                        b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                    )
                except OSError:
                    canary.heads.append("<connection>")

        self._server = _Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    def first_lines(self) -> list[str]:
        return [head.split("\r\n", 1)[0] for head in self.heads]


@dataclass
class Interceptor:
    """A loopback TLS server holding a certificate from the synthetic CA.

    It stands where Intuit's hosts resolve to. A client that trusts the CA
    completes the handshake and hands it the request, credential included;
    ``requests`` records each request line and whether it carried one.
    """

    pki: Pki
    requests: list[tuple[str, bool]] = field(default_factory=list)
    handshakes_refused: int = 0
    _server: _Server | None = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.server_address[1])

    def start(self) -> None:
        interceptor = self
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.pki.leaf, self.pki.key)

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                self.request.settimeout(5)
                try:
                    with context.wrap_socket(self.request, server_side=True) as tls:
                        head = _read_head(tls).decode("latin-1")
                        lines = head.split("\r\n")
                        carries = any(
                            line.lower().startswith("authorization:") for line in lines[1:]
                        )
                        interceptor.requests.append((lines[0], carries))
                        body = b"{}"
                        tls.sendall(
                            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                            b"Connection: close\r\n\r\n" + body
                        )
                except (ssl.SSLError, OSError):
                    interceptor.handshakes_refused += 1

        self._server = _Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


@dataclass
class Resolver:
    """``socket.getaddrinfo`` for these tests: loopback names as usual, the
    intercepted names to the interceptor (when there is one), anything else
    refused. The names clients asked for are recorded."""

    target_port: int | None
    asked: list[str] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = socket.getaddrinfo

        def resolve(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
            name = host.decode() if isinstance(host, bytes) else str(host)
            if name in _LOOPBACK_NAMES:
                return real(host, port, *args, **kwargs)
            self.asked.append(name)
            if self.target_port is not None and name in INTERCEPTED_HOSTS:
                return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                         ("127.0.0.1", self.target_port))]
            raise socket.gaierror(socket.EAI_NONAME, f"test resolver: {name} is not reachable")

        monkeypatch.setattr(socket, "getaddrinfo", resolve)


@pytest.fixture
def pki(tmp_path: Path) -> Pki:
    return make_pki(tmp_path)


@pytest.fixture
def proxy_canary(monkeypatch: pytest.MonkeyPatch) -> Iterator[ProxyCanary]:
    """A proxy canary every proxy variable points at, NO_PROXY cleared."""
    canary = ProxyCanary()
    canary.start()
    for name in PROXY_VARIABLES:
        monkeypatch.setenv(name, canary.url)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    try:
        yield canary
    finally:
        canary.stop()


@pytest.fixture
def interceptor(pki: Pki) -> Iterator[Interceptor]:
    server = Interceptor(pki)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def app_in_environment(monkeypatch: pytest.MonkeyPatch, credentials: Any) -> None:
    """A server with no app configured sends no stored credential."""
    monkeypatch.setenv("QBO_CLIENT_ID", credentials.client_id)
    monkeypatch.setenv("QBO_CLIENT_SECRET", credentials.client_secret)


# --------------------------------------------------------------------------
# The calls, each through a client the package builds itself
# --------------------------------------------------------------------------


def _refresh(credentials: Any) -> None:
    oauth.refresh_tokens(
        refresh_token=REFRESH, client_id=credentials.client_id,
        client_secret=credentials.client_secret, environment="sandbox",
    )


def _exchange(credentials: Any) -> None:
    oauth.exchange_code(
        code=CODE, redirect_uri="http://localhost:8765/callback",
        client_id=credentials.client_id, client_secret=credentials.client_secret,
        environment="sandbox",
    )


def _revoke(credentials: Any) -> None:
    oauth.revoke_token(
        token=REFRESH, client_id=credentials.client_id,
        client_secret=credentials.client_secret, environment="sandbox",
    )


TOKEN_CALLS: dict[str, tuple[Callable[[Any], None], str]] = {
    "refresh": (_refresh, TOKEN_HOST),
    "exchange": (_exchange, TOKEN_HOST),
    "revoke": (_revoke, REVOKE_HOST),
}


def _attempt(call: Callable[[], Any]) -> None:
    """Run a call whose outcome here is a failure; what it reached is the point."""
    try:
        call()
    except Exception:  # noqa: BLE001 - each call fails its own way; the servers are the evidence
        pass


def _refused_a_handshake(interceptor: Interceptor) -> bool:
    """The interceptor's handler records a refused handshake on its own
    thread, a moment after the client has given up; wait for it."""
    import time

    deadline = time.monotonic() + 5
    while interceptor.handshakes_refused < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    return interceptor.handshakes_refused >= 1


def _accounting_call(settings: Any, store: Any, registry: Any) -> Callable[[], Any]:
    def call() -> Any:
        with AccountingClient(settings, store) as client:  # no client injected: the package builds it
            return client.get(registry["acme_main"], "preferences")

    return call


def _attachment_call(settings: Any) -> Callable[[], Any]:
    def call() -> Any:
        return attachments.download(
            f"https://{FILES_HOST}/a/b.pdf?Expires=1&Signature=x",
            settings=_download_settings(settings),
            expected_content_type="application/pdf",
            expected_size=len(PDF),
            resolver=public,  # the pre-check's resolver; the connection uses socket's
        )

    return call


# --------------------------------------------------------------------------
# 1. A proxy the environment names sees nothing
# --------------------------------------------------------------------------


def test_an_environment_proxy_never_sees_an_accounting_request(
    mock_settings, mock_intuit, seeded_store, registry, proxy_canary, monkeypatch
) -> None:
    """A client that trusts the environment hands the proxy its bearer. Here
    the request must go to the (loopback) API and nowhere else."""
    Resolver(target_port=None).install(monkeypatch)
    answered: list[Any] = []
    call = _accounting_call(mock_settings, seeded_store, registry)
    _attempt(lambda: answered.append(call()))
    assert proxy_canary.first_lines() == [], (
        f"a proxy named in the environment received: {proxy_canary.first_lines()}"
    )
    assert [token for _, _, _, token in mock_intuit.requests] == ["access-1"]
    assert answered and answered[0].payload is not None


@pytest.mark.parametrize("name", sorted(TOKEN_CALLS))
def test_an_environment_proxy_never_sees_a_token_request(
    name, credentials, proxy_canary, monkeypatch
) -> None:
    """The three OAuth POSTs carry the client secret as Basic auth, and two
    carry a grant: none may be handed to a proxy the environment names. The
    direct connection is refused by the test resolver, so the only thing
    that can reach a socket is the proxy."""
    call, host = TOKEN_CALLS[name]
    resolver = Resolver(target_port=None)
    resolver.install(monkeypatch)
    with pytest.raises(oauth.OAuthError):
        call(credentials)
    assert proxy_canary.first_lines() == [], (
        f"a proxy named in the environment received: {proxy_canary.first_lines()}"
    )
    assert resolver.asked == [host], "the request should have gone to Intuit's host directly"


def test_an_environment_proxy_never_sees_discovery(proxy_canary, monkeypatch) -> None:
    resolver = Resolver(target_port=None)
    resolver.install(monkeypatch)
    check = oauth.confirm_pinned_endpoints("sandbox")
    assert check.reached is False
    assert proxy_canary.first_lines() == [], (
        f"a proxy named in the environment received: {proxy_canary.first_lines()}"
    )
    assert resolver.asked == [DISCOVERY_HOST]


# --------------------------------------------------------------------------
# 2. A CA the environment names is not trusted
# --------------------------------------------------------------------------


def _intercepted(
    monkeypatch: pytest.MonkeyPatch, interceptor: Interceptor, *, ca_from_environment: Pki
) -> Resolver:
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_from_environment.ca))
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    resolver = Resolver(target_port=interceptor.port)
    resolver.install(monkeypatch)
    return resolver


def test_an_interceptor_with_a_ca_from_the_environment_gets_no_accounting_request(
    settings, seeded_store, registry, pki, interceptor, app_in_environment, monkeypatch
) -> None:
    """Intuit's sandbox host resolves to the interceptor, whose certificate a
    CA in SSL_CERT_FILE signed. Trusting that file hands it the bearer."""
    resolver = _intercepted(monkeypatch, interceptor, ca_from_environment=pki)
    _attempt(_accounting_call(settings, seeded_store, registry))
    assert resolver.asked == [SANDBOX_API_HOST]
    assert interceptor.requests == [], f"the interceptor read: {interceptor.requests}"
    assert _refused_a_handshake(interceptor), "the client should have reached the interceptor and refused it"


@pytest.mark.parametrize("name", sorted(TOKEN_CALLS))
def test_an_interceptor_with_a_ca_from_the_environment_gets_no_token_request(
    name, credentials, pki, interceptor, monkeypatch
) -> None:
    call, host = TOKEN_CALLS[name]
    resolver = _intercepted(monkeypatch, interceptor, ca_from_environment=pki)
    _attempt(lambda: call(credentials))
    assert resolver.asked == [host]
    assert interceptor.requests == [], f"the interceptor read: {interceptor.requests}"
    assert _refused_a_handshake(interceptor), "the client should have reached the interceptor and refused it"


def test_an_interceptor_with_a_ca_from_the_environment_gets_no_discovery_request(
    pki, interceptor, monkeypatch
) -> None:
    resolver = _intercepted(monkeypatch, interceptor, ca_from_environment=pki)
    check = oauth.confirm_pinned_endpoints("sandbox")
    assert interceptor.requests == [], f"the interceptor read: {interceptor.requests}"
    assert resolver.asked == [DISCOVERY_HOST]
    assert check.reached is False
    assert _refused_a_handshake(interceptor), "the client should have reached the interceptor and refused it"


def test_an_interceptor_with_a_ca_from_the_environment_gets_no_attachment_request(
    settings, pki, interceptor, monkeypatch
) -> None:
    """The download client carries no credential, but its URL is a signed
    link to a company's document. httpx does not pass a client's
    ``trust_env=False`` to a transport it did not build, so with an explicit
    transport the flag alone leaves SSL_CERT_FILE in force."""
    resolver = _intercepted(monkeypatch, interceptor, ca_from_environment=pki)
    _attempt(_attachment_call(settings))
    assert resolver.asked == [FILES_HOST]
    assert interceptor.requests == [], f"the interceptor read: {interceptor.requests}"
    assert _refused_a_handshake(interceptor), "the client should have reached the interceptor and refused it"


# --------------------------------------------------------------------------
# 3. No TLS session secret is written where the environment says
# --------------------------------------------------------------------------


def _secret_lines(keylog: Path) -> list[str]:
    """The key-log entries, by label. The file itself may exist: the standard
    library opens it (writing a comment header) when it builds a default
    context, before this package can clear the setting. What must never be
    in it is a secret."""
    if not keylog.exists():
        return []
    return sorted(
        {line.split()[0] for line in keylog.read_text().splitlines() if line and not line.startswith("#")}
    )


@pytest.fixture
def trusted_interceptor(
    pki: Pki, interceptor: Interceptor, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Interceptor, Path]:
    """The interceptor standing in for Intuit, with its CA trusted the way a
    real Intuit certificate is -- through certifi -- so every handshake
    completes and a session's traffic secrets exist to be logged. SSLKEYLOGFILE
    names where the environment would have them written."""
    import certifi

    monkeypatch.setattr(certifi, "where", lambda: str(pki.ca))
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    keylog = tmp_path / "tls-keys.log"
    monkeypatch.setenv("SSLKEYLOGFILE", str(keylog))
    Resolver(target_port=interceptor.port).install(monkeypatch)
    return interceptor, keylog


def test_no_tls_secret_of_an_accounting_request_is_written_where_the_environment_says(
    settings, seeded_store, registry, trusted_interceptor, app_in_environment
) -> None:
    interceptor, keylog = trusted_interceptor
    _attempt(_accounting_call(settings, seeded_store, registry))
    assert interceptor.requests, "the handshake should have completed"
    assert _secret_lines(keylog) == [], f"TLS secrets written to SSLKEYLOGFILE: {_secret_lines(keylog)}"


@pytest.mark.parametrize("name", sorted(TOKEN_CALLS))
def test_no_tls_secret_of_a_token_request_is_written_where_the_environment_says(
    name, credentials, trusted_interceptor
) -> None:
    interceptor, keylog = trusted_interceptor
    call, _ = TOKEN_CALLS[name]
    _attempt(lambda: call(credentials))
    assert interceptor.requests, "the handshake should have completed"
    assert _secret_lines(keylog) == [], f"TLS secrets written to SSLKEYLOGFILE: {_secret_lines(keylog)}"


def test_no_tls_secret_of_discovery_is_written_where_the_environment_says(trusted_interceptor) -> None:
    interceptor, keylog = trusted_interceptor
    oauth.confirm_pinned_endpoints("sandbox")
    assert interceptor.requests, "the handshake should have completed"
    assert _secret_lines(keylog) == [], f"TLS secrets written to SSLKEYLOGFILE: {_secret_lines(keylog)}"


def test_no_tls_secret_of_an_attachment_download_is_written_where_the_environment_says(
    settings, trusted_interceptor
) -> None:
    interceptor, keylog = trusted_interceptor
    _attempt(_attachment_call(settings))
    assert interceptor.requests, "the handshake should have completed"
    assert _secret_lines(keylog) == [], f"TLS secrets written to SSLKEYLOGFILE: {_secret_lines(keylog)}"


# --------------------------------------------------------------------------
# The detectors themselves
# --------------------------------------------------------------------------


def test_the_canary_and_the_interceptor_can_see_what_they_claim_to(
    pki, interceptor, proxy_canary, monkeypatch
) -> None:
    """A test whose passing result is an absence proves nothing until the
    detector is seen to fire. A client that DOES trust the environment reaches
    the proxy canary, and one that trusts the CA reaches the interceptor and
    hands it the credential."""
    Resolver(target_port=interceptor.port).install(monkeypatch)
    with httpx.Client(trust_env=True, timeout=5) as trusting:
        with pytest.raises(httpx.ProxyError):
            trusting.get(f"https://{TOKEN_HOST}/")
    assert proxy_canary.first_lines() == [f"CONNECT {TOKEN_HOST}:443 HTTP/1.1"]

    context = ssl.create_default_context(cafile=str(pki.ca))
    with httpx.Client(trust_env=False, verify=context, timeout=5) as direct:
        direct.get(f"https://{TOKEN_HOST}/probe", headers={"Authorization": "Basic x"})
    assert interceptor.requests == [("GET /probe HTTP/1.1", True)]
