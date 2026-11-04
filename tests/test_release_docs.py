"""The release documentation describes the package as built.

A release is read from its documents as much as from its code, so the parts a
reader relies on are checked here: docs/tools.md has a section for every
registered tool, the full surface with the opt-in; the CHANGELOG says what is
verified live in sandbox, what is verified live in production, what rests on
tests alone and what is not done; and the design doc states the document
opt-in's contract and the hard-expiry clock.
"""

from __future__ import annotations

import re
from dataclasses import replace as dc_replace
from pathlib import Path

import anyio
from mcp.server import MCPServer

from qbo_multicompany_mcp.tools import register_tools

REPO = Path(__file__).resolve().parents[1]


def registered(mock_settings, registry) -> set[str]:
    server = MCPServer(name="t", version="0")
    register_tools(server, dc_replace(mock_settings, allow_documents=True), registry)
    return {t.name for t in anyio.run(server.list_tools)}


def test_tools_md_has_a_section_for_every_registered_tool_and_the_choosing_table(mock_settings, registry) -> None:
    text = (REPO / "docs" / "tools.md").read_text(encoding="utf-8")
    sections = set(re.findall(r"^## `([a-z_]+)`", text, flags=re.MULTILINE))
    tools = registered(mock_settings, registry)
    assert len(tools) == 19
    assert tools - sections == set(), sorted(tools - sections)
    assert sections - tools == set(), sorted(sections - tools)
    assert "## Choosing a tool" in text
    table = text.split("## Choosing a tool", 1)[1].split("\n## ", 1)[0]
    assert {t for t in tools if f"`{t}`" in table} == tools, "every tool appears in the Choosing a tool table"


def test_the_changelog_says_what_is_verified_where() -> None:
    text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## 0.1.0" in text
    for heading in ("### Verified live in sandbox", "### Verified live in production", "### Tests only",
                    "### Not done"):
        assert heading in text, heading
    assert "GATE_COUNT" not in text


def test_the_design_doc_states_the_document_opt_in_contract_and_the_hard_expiry_clock() -> None:
    text = (REPO / "docs" / "design.md").read_text(encoding="utf-8")
    section = text.split("### Documents (opt-in)", 1)[1].split("\n### ", 1)[0]
    flat = " ".join(section.split())
    for needle in ("--allow-documents", "QBO_ALLOW_DOCUMENTS", "QBO_ATTACHMENT_HOSTS",
                   "Nothing is pinned by default", "financialdocument.platform.intuit.com",
                   "Production has not"):
        assert needle in flat, needle
    assert "five-year maximum that only a re-consent restarts" in " ".join(text.split())
    assert "CHANGELOG.md" in (REPO / "README.md").read_text(encoding="utf-8")


# ==========================================================================
# The 100-day clock, as observed
# ==========================================================================
#
# Observed in sandbox on 2026-09-26: the inactivity clock restarts when Intuit
# issues a new refresh-token VALUE, which happens at most once per 24 hours
# (Intuit's Authorization FAQ); a refresh inside those 24 hours returns the
# same value and the same expiry. So "every refresh restarts it" is wrong, and
# "assumed" is stale.

#: Phrasings of that wrong claim, in either of its two forms.
STALE_CLOCK_CLAIMS = (
    "restarted by every refresh",
    "every refresh restarts",
    "a refresh restarts it",
    "assumed to restart",
    "restarts it is an assumption",
    "restart is an assumption",
)


def _published_text() -> dict[str, str]:
    """Source, tests and the documents a reader relies on; not this file,
    which lists the old wording."""
    tests = [p for p in sorted((REPO / "tests").rglob("*.py")) if p.name != "test_release_docs.py"]
    docs = sorted((REPO / "docs").glob("*.md"))
    paths = [*sorted((REPO / "src").rglob("*.py")), *tests, *docs, REPO / "README.md", REPO / "CHANGELOG.md"]
    return {str(p.relative_to(REPO)): " ".join(p.read_text(encoding="utf-8").split()) for p in paths}


def test_no_text_says_every_refresh_restarts_the_inactivity_clock() -> None:
    found = [
        (path, phrase)
        for path, text in _published_text().items()
        for phrase in STALE_CLOCK_CLAIMS
        if phrase in text.casefold()
    ]
    assert found == [], found


def _around(path: str, marker: str, span: int = 900) -> str:
    text = " ".join((REPO / path).read_text(encoding="utf-8").split())
    start = text.index(marker)
    return text[start:start + span]


def test_the_three_clock_passages_cite_the_observation() -> None:
    for path, marker in (
        ("src/qbo_multicompany_mcp/store.py", "def days_to_inactivity_expiry"),
        ("src/qbo_multicompany_mcp/keepalive.py", "# Two clocks"),
        ("docs/design.md", "A refresh token has two clocks"),
    ):
        passage = _around(path, marker)
        assert "2026-09-26" in passage, path
        assert "Authorization FAQ" in passage, path
        assert "24 hours" in passage, path
