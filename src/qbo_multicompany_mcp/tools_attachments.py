"""``list_attachments`` and the opt-in ``get_attachment``.

``list_attachments`` answers "is there a receipt": Attachable METADATA
through the ordinary accounting client, walked by the shared walker and
projected to file name, MIME type, size, note, category, tag, the
AttachableRef to the transaction (type and id, routable through
``get_transaction`` for a served type) and the two timestamps. No link:
``TempDownloadUri`` and ``ThumbnailTempDownloadUri`` are temporary signed
URLs and ``FileAccessUri`` a full path; none leaves this process. The filter
pairs ``transaction_type`` with ``transaction_id``, as the documented query
does.

``get_attachment`` is OPT-IN and GATED. It is registered only when documents
are enabled, so the default surface is eighteen tools; it refuses before any
request unless a download host is pinned; it reads the Attachable, decides from
the recorded content type and size whether the file is one it will fetch, and
only then hands the temporary link to the separate download client in
:mod:`qbo_multicompany_mcp.attachments`, which applies the origin, redirect,
auth-isolation, destination, MIME and size gates. The result is two blocks: the
metadata as a text block (and as the structured content), then the file as an
embedded blob resource.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from typing import Annotated, Any, Final

from mcp.server import MCPServer
from mcp_types import BlobResourceContents, CallToolResult, EmbeddedResource, TextContent
from pydantic import Field

from . import attachments as attachments_mod
from .config import ENV_ATTACHMENT_HOSTS, Settings
from .constants import ATTACHMENT_HOST_HINT
from .envelopes import AttachableBlock, AttachmentEnvelope, AttachmentListEnvelope, dump
from .errors import QboMcpError
from .pagination import rows_of
from .projections import UnreadableSize, project_attachable, recorded_size
from .query_grammar import TRANSACTION_TYPES, QueryError, escape_literal, parse
from .registry import Registry
from .session import QboSession
from .tools import FREE_TEXT_DISCLOSURE, EntityKey, make_result, read_only, tool_errors
from .tools_data import DEFAULT_LIMIT, MAX_LIMIT, _identity, _limit

import json

DESCRIPTION_LIST: Final = (
    "List one company's attachments (files and notes on transactions) as "
    "metadata: file name, content type, size, note, category, tag, the "
    "transaction each is linked to (type and id), created and updated. Never "
    "a link. Use this to learn whether a receipt exists and what it is; use "
    "get_attachment, when this server has it enabled, for the file itself, and "
    "get_transaction for the transaction. transaction_type and transaction_id "
    "filter together or not at all. Read-only; requires an explicit 'entity'."
    + FREE_TEXT_DISCLOSURE
)

DESCRIPTION_GET: Final = (
    "Fetch one attachment's file as an embedded blob beside its metadata "
    "(opt-in; present only when this server was started with "
    "--allow-documents). Use list_attachments first for the id and to see "
    "what the file is. The file comes through a separate client that carries "
    "no credential, contacts only a pinned storage host over https, refuses "
    "private destinations and unsupported content types, and caps the size; "
    "a refusal says which gate refused. Read-only; requires an explicit "
    "'entity'." + FREE_TEXT_DISCLOSURE
)

_DOWNLOAD_NOTE_OFF: Final = (
    "get_attachment is not enabled on this server: start it with --allow-documents "
    "(or QBO_ALLOW_DOCUMENTS=1) and pin the storage host with QBO_ATTACHMENT_HOSTS "
    "to fetch a file. Metadata only here."
)
_DOWNLOAD_NOTE_NO_HOST: Final = (
    "get_attachment is enabled but no download host is pinned "
    f"({ENV_ATTACHMENT_HOSTS}), so every download is refused until one is; "
    f"{ATTACHMENT_HOST_HINT}."
)
_DOWNLOAD_NOTE_ON: Final = "get_attachment is enabled and a download host is pinned."

_RESULT_NOTE: Final = (
    "Two blocks: this metadata as text and as the structured content, then the "
    "file as an embedded blob resource, base64-encoded, at resource_uri. The "
    "bytes were checked against the recorded content type's own signature and "
    "capped; sha256 is over the raw bytes."
)


def _download_note(settings: Settings) -> tuple[bool, str]:
    if not settings.allow_documents:
        return False, _DOWNLOAD_NOTE_OFF
    if not settings.attachment_hosts:
        return False, _DOWNLOAD_NOTE_NO_HOST
    return True, _DOWNLOAD_NOTE_ON


def _canonical_transaction_type(value: str) -> str:
    for candidate in TRANSACTION_TYPES:
        if candidate.casefold() == value.casefold():
            return candidate
    raise QueryError(f"transaction_type must be one of: {', '.join(TRANSACTION_TYPES)}")


def make_attachment_result(envelope: AttachmentEnvelope, content: bytes) -> CallToolResult:
    """The two-block contract: metadata text, then the blob resource -- or a
    refusal when the complete result would exceed the output cap.

    The cap counts the whole result as the wire carries it: the metadata text,
    the structured metadata and the resource wrapper as well as the blob. A
    cap on the blob alone would let a file at the raw cap go out over the
    limit. Measured the way the SDK serializes a result (``model_dump`` by
    alias, without nulls), before anything returns.
    """
    payload = dump(envelope)
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    result = CallToolResult(
        content=[
            TextContent(type="text", text=text),
            EmbeddedResource(
                type="resource",
                resource=BlobResourceContents(
                    uri=envelope.resource_uri,
                    mime_type=envelope.content_type,
                    blob=base64.b64encode(content).decode("ascii"),
                ),
            ),
        ],
        structured_content=payload,
    )
    size = len(result.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8"))
    if size > attachments_mod.MAX_ATTACHMENT_OUTPUT_BYTES:
        raise attachments_mod.AttachmentRefused(
            f"the complete result -- the metadata, its text block and the encoded file -- "
            f"would be {size} bytes, over this server's output limit of "
            f"{attachments_mod.MAX_ATTACHMENT_OUTPUT_BYTES} bytes, so nothing is returned"
        )
    return result


def register_attachment_tools(
    server: MCPServer,
    settings: Settings,
    registry: Registry,
    session: QboSession,
    *,
    downloader: Any = attachments_mod.download,
) -> None:
    # ------------------------------------------------- list_attachments ---
    @server.tool(
        name="list_attachments",
        title="List attachments on transactions",
        annotations=read_only("List attachments on transactions"),
        description=DESCRIPTION_LIST,
    )
    def list_attachments(
        entity: EntityKey,
        transaction_type: Annotated[str | None, Field(description=f"With transaction_id: one of {', '.join(TRANSACTION_TYPES)}.")] = None,
        transaction_id: Annotated[str | None, Field(description="With transaction_type: the transaction's id.")] = None,
        limit: Annotated[
            int | None, Field(ge=1, le=MAX_LIMIT, description="Rows returned; the walk still runs to the end.")
        ] = DEFAULT_LIMIT,
    ) -> Annotated[CallToolResult, AttachmentListEnvelope]:
        with tool_errors():
            target = session.resolve(entity)
            if (transaction_type is None) != (transaction_id is None):
                raise QueryError(
                    "transaction_type and transaction_id filter together or not at all: "
                    "the documented filter is on the linked transaction's type and id."
                )
            canonical = _canonical_transaction_type(transaction_type) if transaction_type else None
            if transaction_id is not None and not str(transaction_id).strip():
                raise QueryError("transaction_id must not be empty")

            session.ensure_ready(target)
            started = datetime.now(UTC)
            statement = "SELECT * FROM Attachable"
            if canonical is not None:
                statement += (
                    f" WHERE AttachableRef.EntityRef.Type = '{escape_literal(canonical)}' AND "
                    f"AttachableRef.EntityRef.value = '{escape_literal(str(transaction_id).strip())}'"
                )
            walk = session.walk(target, parse(statement), client_limit=_limit(limit))
            available, note = _download_note(settings)
            return make_result(
                AttachmentListEnvelope(
                    **_identity(target, started, datetime.now(UTC)),
                    count=len(walk.rows),
                    rows=[AttachableBlock(**project_attachable(row)) for row in walk.rows],
                    transaction_type=canonical,
                    transaction_id=str(transaction_id).strip() if transaction_id is not None else None,
                    download_available=available,
                    download_note=note,
                    truncated=walk.truncated,
                    duplicates_dropped=walk.duplicates_dropped,
                    pages_walked=walk.pages_walked,
                    walk_started_at=walk.walk_started_at,
                    walk_finished_at=walk.walk_finished_at,
                )
            )

    if not settings.allow_documents:
        return

    # -------------------------------------------- get_attachment, opt-in ---
    @server.tool(name="get_attachment", title="Fetch one attachment's file (opt-in)", description=DESCRIPTION_GET,
                 annotations=read_only("Fetch one attachment's file (opt-in)"))
    def get_attachment(
        entity: EntityKey,
        attachable_id: Annotated[str, Field(description="The attachment's id, from list_attachments.")],
    ) -> Annotated[CallToolResult, AttachmentEnvelope]:
        with tool_errors() as bound:
            target = session.resolve(entity)
            # From here every failure names this company, never its realm id
            # and never the signed link: otherwise two companies' failed
            # downloads in one session read "status 404" alike.
            bound.bind(target)
            if not str(attachable_id).strip():
                raise QueryError("attachable_id must not be empty")
            if not settings.attachment_hosts:
                # Before the guard, before any request: nothing this call
                # could learn changes the answer.
                raise attachments_mod.AttachmentRefused(
                    f"no download host is pinned: set {ENV_ATTACHMENT_HOSTS} to the storage "
                    f"host the attachment links name, or pass --attachment-host; "
                    f"{ATTACHMENT_HOST_HINT}. Nothing was requested. list_attachments "
                    f"still answers with metadata."
                )

            session.ensure_ready(target)
            statement = parse(f"SELECT * FROM Attachable WHERE Id = '{escape_literal(str(attachable_id).strip())}'")
            result = session.client().query(target, statement.canonical())
            rows = rows_of(result.payload, "Attachable")
            if not rows:
                raise QboMcpError(
                    "no Attachable with that id in this company: not found, which is a "
                    "different statement from having no file."
                )
            raw = rows[0]
            metadata = project_attachable(raw)
            link = raw.get("TempDownloadUri")
            if not metadata.get("file_name") or not metadata.get("content_type"):
                raise QboMcpError(
                    "this attachable carries no file: it is a standalone note, which "
                    "list_attachments already returns in full."
                )
            content_type = attachments_mod.check_supported_type(metadata.get("content_type"))
            # The recorded Size, read strictly: the exact-size check needs it, and
            # a Size that cannot be read is refused rather than taken as absent.
            try:
                size = recorded_size(raw.get("Size"))
            except UnreadableSize as unreadable:
                raise attachments_mod.AttachmentRefused(
                    f"the attachment's recorded Size {unreadable}, so the download "
                    f"cannot be checked against it and is refused. The file was not "
                    f"requested."
                ) from None
            attachments_mod.check_size(size)
            if not isinstance(link, str) or not link:
                raise attachments_mod.AttachmentRefused(
                    "the Attachable read carried no temporary download link, so there is "
                    "nothing to fetch (this server assumes the read returns TempDownloadUri "
                    "for a file attachment, as Intuit documents)."
                )

            downloaded = downloader(
                link, settings=settings, expected_content_type=content_type,
                expected_size=size,
            )
            content = downloaded.content
            uri = f"qbo://{target.key}/attachable/{str(attachable_id).strip()}"
            envelope = AttachmentEnvelope(
                **_identity(target, result.request_started_at, datetime.now(UTC)),
                attachable=AttachableBlock(**metadata),
                content_type=downloaded.content_type,
                bytes=len(content),
                base64_length=len(base64.b64encode(content)),
                sha256=hashlib.sha256(content).hexdigest(),
                download_host=downloaded.final_host,
                redirects_followed=downloaded.redirects,
                resource_uri=uri,
                note=_RESULT_NOTE,
            )
            return make_attachment_result(envelope, content)
