"""The opt-in attachment download client.

A SEPARATE client from the accounting one, and separate on purpose: the URL
it fetches is not one this package built from the registry and an
allowlisted tail. It is ``TempDownloadUri`` as an Attachable read returned
it -- a temporary link (the ``/download/`` endpoint's description on Intuit's
Attachable page says such links expire after 15 minutes) that the sample on
Intuit's page shows as a signed URL on a storage host, not on Intuit's API
origin. Upstream data, in other words, and treated as such:

* **no credential of any kind.** No bearer, no cookie, no proxy
  authorization -- the network policy refuses the request if one is present
  (:func:`qbo_multicompany_mcp.netpolicy._attachment`), the client and its
  transport are the network policy's, which read no proxy, CA file or TLS
  key log from the environment, requests are constructed directly rather
  than through the client's request builder so its cookie jar is never
  consulted, and the jar is cleared after every response so a ``Set-Cookie``
  on one hop cannot ride on the next;
* **an explicit host policy on the origin and on every redirect
  destination.** Only a host the operator pinned in ``QBO_ATTACHMENT_HOSTS``
  (exact, or ``*.suffix``) is contacted, over https on the default port; an
  IP literal is never a pinned host; redirects are followed by this module,
  never by httpx, at most :data:`MAX_REDIRECTS` hops, each destination put
  through the same policy before it is contacted. The host a sandbox link
  named was OBSERVED on 2026-09-25
  (:data:`qbo_multicompany_mcp.constants.OBSERVED_ATTACHMENT_HOSTS_SANDBOX`)
  and is what a no-host refusal names; it is not pinned by default, and
  production is unobserved;
* **private, loopback and link-local destinations refused.** Before the
  first request, the pinned name is resolved and every address it resolves
  to must be a global one. A PRE-CHECK, not a proof: the connection resolves
  the name again and DNS can answer differently; it refuses the obvious
  cases. The one admission is test-only: when the API base is itself a
  loopback mock (which the accounting side already treats as test-only) and
  the environment is not production, a loopback literal may be pinned so the
  stdio contract test can serve a file;
* **a supported-MIME allowlist**, decided from the Attachable's recorded
  ``ContentType`` before any request, checked again against the response's
  header, and then against the bytes with a check per type -- a PDF header,
  a PNG, JPEG or GIF signature, UTF-8 for text;
* **streaming with a final MCP output cap that accounts for base64
  expansion.** The blob leaves this server base64-encoded, which is four
  thirds of the raw size, so the raw cap is three quarters of the output cap
  (:data:`MAX_ATTACHMENT_RAW_BYTES`). The recorded ``Size`` is checked before
  the request, the stream is capped during it, and a body of any other length
  than a recorded ``Size`` is refused after it, so a wrong size in either
  direction returns nothing. The OUTPUT cap then counts the whole result --
  the metadata text, the structured metadata and the resource wrapper with
  the blob -- as the wire carries it
  (:func:`qbo_multicompany_mcp.tools_attachments.make_attachment_result`), so
  a file near the raw cap is refused rather than returned over the limit.

Nothing here is written to disk, and the temporary URL is in no error text:
a refusal names the host at most, never the signed query.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket  # name resolution only; the AST test asserts this module never dials
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

import httpx

from . import deadline as call_deadline
from .config import ENV_ATTACHMENT_HOSTS, Settings
from .constants import ACCOUNTING_TIMEOUT_SECONDS, ATTACHMENT_HOST_HINT, ENV_PRODUCTION
from .errors import (
    DeadlineExceeded,
    QboMcpError,
    ResponseTooLarge,
    deadline_text,
    download_deadline_text,
)
from .netpolicy import (
    PolicyContext,
    RequestClass,
    is_loopback_base,
    isolated_client,
    isolated_transport,
    send,
)
from .oauth import USER_AGENT

#: The final MCP output cap on the encoded blob, and the raw cap that keeps the
#: encoding under it.
MAX_ATTACHMENT_OUTPUT_BYTES: Final = 10 * 1024 * 1024
MAX_ATTACHMENT_RAW_BYTES: Final = (MAX_ATTACHMENT_OUTPUT_BYTES // 4) * 3

#: Hops this module will follow. Each destination is re-checked.
MAX_REDIRECTS: Final = 3

_REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})


class AttachmentRefused(QboMcpError):
    """The download was refused by one of the gates. Fixed text; never the
    signed URL."""


#: A hostname -> the addresses it resolves to, as text. Injected by the
#: tests; the default asks the resolver.
Resolver = Callable[[str], list[str]]


@dataclass(frozen=True, slots=True)
class DownloadedFile:
    content: bytes
    content_type: str
    final_host: str
    redirects: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


# --------------------------------------------------------------------------
# The MIME allowlist, each type with its own check
# --------------------------------------------------------------------------


def _is_pdf(content: bytes) -> bool:
    return content.startswith(b"%PDF-")


def _is_png(content: bytes) -> bool:
    return content.startswith(b"\x89PNG\r\n\x1a\n")


def _is_jpeg(content: bytes) -> bool:
    return content.startswith(b"\xff\xd8\xff")


def _is_gif(content: bytes) -> bool:
    return content.startswith((b"GIF87a", b"GIF89a"))


def _is_text(content: bytes) -> bool:
    if b"\x00" in content:
        return False
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


#: Supported content types and the check each must pass. Anything else --
#: archives, executables, HTML, ``application/octet-stream`` -- is refused
#: before any request. The list is this package's own choice: what people
#: attach to a transaction is a receipt, an invoice or a note.
MIME_ALLOWLIST: Final[dict[str, Callable[[bytes], bool]]] = {
    "application/pdf": _is_pdf,
    "image/png": _is_png,
    "image/jpeg": _is_jpeg,
    "image/gif": _is_gif,
    "text/plain": _is_text,
    "text/csv": _is_text,
}


def normalise_content_type(value: str | None) -> str:
    return (value or "").split(";", 1)[0].strip().lower()


def check_supported_type(content_type: str | None) -> str:
    """The recorded type, normalised, or a refusal that names no value."""
    normalised = normalise_content_type(content_type)
    if normalised not in MIME_ALLOWLIST:
        raise AttachmentRefused(
            f"the attachment's recorded content type is not a supported type, so it is "
            f"not downloaded. Supported: {', '.join(MIME_ALLOWLIST)}. No request was made."
        )
    return normalised


def check_size(expected_size: int | None) -> None:
    if expected_size is not None and expected_size > MAX_ATTACHMENT_RAW_BYTES:
        raise AttachmentRefused(
            f"the attachment's recorded size is larger than this server's limit "
            f"({MAX_ATTACHMENT_RAW_BYTES} bytes, so the encoded blob stays under "
            f"{MAX_ATTACHMENT_OUTPUT_BYTES}), so it is not downloaded. No request was made."
        )


# --------------------------------------------------------------------------
# Hosts and addresses
# --------------------------------------------------------------------------


def host_pinned(host: str, pinned: tuple[str, ...]) -> bool:
    """Exact hostname, or ``*.suffix`` matching one or more labels below it."""
    candidate = host.strip().lower().rstrip(".")
    for entry in pinned:
        entry = entry.strip().lower().rstrip(".")
        if not entry:
            continue
        if entry.startswith("*."):
            suffix = entry[1:]
            if candidate.endswith(suffix) and candidate != suffix[1:]:
                return True
        elif candidate == entry:
            return True
    return False


def loopback_test_mode(settings: Settings) -> bool:
    """The one admission of a loopback download host: the API base is a
    loopback mock (already test-only) and the environment is not production."""
    return settings.environment != ENV_PRODUCTION and is_loopback_base(settings.api_base)


def address_kind(text: str) -> str | None:
    """``None`` for a globally routable address, otherwise the fixed word for
    what it is. Decided by ``is_global``, after multicast, which it accepts:
    loopback, link-local, the private ranges, the unspecified address, the
    documentation ranges and every other reserved block are all refused, each
    named for the error."""
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return "unreadable"
    if address.is_multicast:
        # Before is_global, which says yes to multicast addresses.
        return "multicast"
    if address.is_global:
        return None
    if address.is_loopback:
        return "loopback"
    if address.is_link_local:
        return "link-local"
    if address.is_unspecified:
        return "unspecified"
    if address.is_private:
        return "private"
    return "reserved"


def _default_resolver(host: str) -> list[str]:
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})
    except (OSError, UnicodeError):  # UnicodeError: a label no DNS name can have.
        return []


def check_destination(host: str, settings: Settings, resolver: Resolver) -> None:
    """The private-destination pre-check on a pinned host."""
    literal = address_kind(host)
    if literal != "unreadable":
        # An IP literal reached the policy: only a loopback one in test mode
        # can have, and it needs no resolution.
        return
    addresses = resolver(host)
    if not addresses:
        raise AttachmentRefused("the pinned download host does not resolve, so it is not contacted")
    for address in addresses:
        kind = address_kind(address)
        if kind == "loopback" and loopback_test_mode(settings):
            continue
        if kind is not None:
            raise AttachmentRefused(
                f"the pinned download host resolves to a {kind} address, so it is not "
                f"contacted (private, loopback and link-local destinations "
                f"are refused)"
            )


# --------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------


def _transport() -> httpx.BaseTransport:
    """Indirection so the tests can put a recording transport under the
    client this module builds, and prove what that client carries.

    The network policy's: a transport built here with httpx's defaults would
    read ``SSL_CERT_FILE``, because the client's ``trust_env=False`` does not
    reach a transport the client did not build."""
    return isolated_transport()


def _build_client() -> httpx.Client:
    """A client that reads nothing from the environment and follows nothing."""
    return isolated_client(timeout=ACCOUNTING_TIMEOUT_SECONDS, transport=_transport())


def _context(settings: Settings) -> PolicyContext:
    return PolicyContext(
        request_class=RequestClass.ATTACHMENT,
        environment=settings.environment,
        api_base=settings.api_base,
        allow_documents=settings.allow_documents,
        attachment_hosts=tuple(settings.attachment_hosts),
    )


def download(
    url: str,
    *,
    settings: Settings,
    expected_content_type: str | None,
    expected_size: int | None,
    http: httpx.Client | None = None,
    resolver: Resolver | None = None,
) -> DownloadedFile:
    """Fetch one attachment through every gate, or refuse.

    ``url`` is the ``TempDownloadUri`` an Attachable read returned; it is
    never logged and never appears in an error. ``expected_content_type`` and
    ``expected_size`` are the Attachable's recorded ``ContentType`` and
    ``Size``: the type decides admission before any request and the response
    must agree with it; the size is checked before the request and the
    stream is capped regardless.
    """
    if not settings.allow_documents:
        raise AttachmentRefused(
            "document fetching is disabled; start with --allow-documents to permit it. "
            "No request was made."
        )
    if not settings.attachment_hosts:
        raise AttachmentRefused(
            f"no download host is pinned: set {ENV_ATTACHMENT_HOSTS} to the storage host "
            f"the attachment links name, or pass --attachment-host; "
            f"{ATTACHMENT_HOST_HINT}. No request was made."
        )
    content_type = check_supported_type(expected_content_type)
    check_size(expected_size)

    try:
        current = httpx.URL(url)
    except httpx.InvalidURL:
        raise AttachmentRefused("the download link is malformed, so it is not contacted") from None

    resolve = resolver or _default_resolver
    ctx = _context(settings)
    try:
        client = http if http is not None else _build_client()
    except OSError:
        # Building the TLS context opens files, the CA bundle and any key log
        # the environment names among them.
        raise AttachmentRefused(
            "the HTTP client for the download could not be built, so nothing was sent; "
            "check SSLKEYLOGFILE and the CA bundle"
        ) from None
    owns_client = http is None
    deadline = call_deadline.current()

    try:
        hops = 0
        while True:
            request = httpx.Request(
                "GET",
                current,
                headers={"Accept": content_type, "Accept-Encoding": "identity", "User-Agent": USER_AGENT},
            )
            # The policy runs inside `send` too; running it first keeps the
            # destination pre-check from resolving a host the policy refuses.
            from .netpolicy import enforce

            enforce(request, ctx)
            if deadline is not None:
                deadline.checkpoint()
            check_destination(request.url.host or "", settings, resolve)
            # What the budget still allows, after the lookup took what it took.
            request.extensions["timeout"] = httpx.Timeout(
                call_deadline.bounded(ACCOUNTING_TIMEOUT_SECONDS)
            ).as_dict()
            try:
                response = send(
                    client, request, ctx, max_bytes=MAX_ATTACHMENT_RAW_BYTES, what="attachment",
                    deadline=deadline,
                )
            except ResponseTooLarge:
                raise AttachmentRefused(
                    f"the attachment is larger than this server's limit "
                    f"({MAX_ATTACHMENT_RAW_BYTES} bytes); nothing from it is returned"
                ) from None
            except httpx.TimeoutException as exc:
                if deadline is not None and deadline.usable() <= 0.5:
                    # The timeout was the budget's, not the request's.
                    raise DeadlineExceeded(deadline_text(deadline.budget)) from exc
                raise AttachmentRefused("the download timed out, so nothing is returned") from None
            except (httpx.HTTPError, httpx.InvalidURL):
                raise AttachmentRefused("the download failed, so nothing is returned") from None
            finally:
                # A Set-Cookie on this hop must not ride on the next.
                client.cookies.clear()

            if response.status_code in _REDIRECT_STATUSES:
                hops += 1
                if hops > MAX_REDIRECTS:
                    raise AttachmentRefused(
                        f"the download redirected more than {MAX_REDIRECTS} times, so it is abandoned"
                    )
                location = response.headers.get("location")
                if not location:
                    raise AttachmentRefused("the download redirected without a destination")
                current = current.join(location)
                continue
            if response.status_code != 200:
                raise AttachmentRefused(
                    f"the download answered status {response.status_code}, so nothing is returned"
                )

            served = normalise_content_type(response.headers.get("content-type"))
            if served != content_type:
                raise AttachmentRefused(
                    "the response's content type disagrees with the attachment's recorded "
                    "type, so nothing from it is returned"
                )
            # `send` capped the stream at MAX_ATTACHMENT_RAW_BYTES; nothing
            # larger reaches this line.
            content = response.content
            # A recorded Size is the file's exact length, not an upper bound:
            # observed in sandbox on 2026-09-26, Intuit's Size (671) equalled
            # the file byte for byte. A shorter or longer body is not the
            # recorded file, whatever its signature says. Without a recorded
            # Size the stream cap alone bounds it.
            if expected_size is not None and len(content) != expected_size:
                raise AttachmentRefused(
                    f"the download is {len(content)} bytes and the attachment's recorded "
                    f"size is {expected_size} bytes, so it is not the recorded file and "
                    f"nothing from it is returned"
                )
            if not MIME_ALLOWLIST[content_type](content):
                raise AttachmentRefused(
                    "the downloaded bytes do not carry the signature of the recorded content "
                    "type (or are not text where text was recorded), so nothing from them is "
                    "returned"
                )
            return DownloadedFile(
                content=content, content_type=content_type,
                final_host=request.url.host or "", redirects=hops,
            )
    except DeadlineExceeded as exc:
        # The shared text suggests a shorter date window, and a file has none.
        raise DeadlineExceeded(download_deadline_text(deadline.budget)) from exc
    finally:
        if owns_client:
            client.close()
