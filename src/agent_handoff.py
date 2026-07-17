import logging
import re
from pathlib import Path
from typing import List, Optional

import src.config
from src.codebase import generate_file_summary
from src.middleware import UNTRUSTED_DATA_NOTICE, is_trusted_github_author, quarantine_wrap
from src.skills import SafeDocuments, SafeGitHub

logger = logging.getLogger("JanusAgentHandoff")

DEFAULT_TEMPLATE = "generic"

_AGENT_TEMPLATES = {
    "claude_code": (
        "> Handoff bundle for **Claude Code**. You have direct file-system and shell "
        "access to this repository — use the Context Files section below as a "
        "starting point, but feel free to search or read further files as needed."
    ),
    "codex": (
        "> Handoff bundle for **Codex**. Treat this bundle as your full task context — "
        "it may not persist across sessions, so do not assume access to prior "
        "conversation history beyond what is included here."
    ),
    "generic": "",
}


def _extract_section(body: str, heading: str) -> Optional[str]:
    if not body:
        return None
    heading_re = re.compile(
        rf"^#{{2,3}}\s*{re.escape(heading)}\s*$", re.MULTILINE | re.IGNORECASE
    )
    match = heading_re.search(body)
    if not match:
        return None

    # Scan line-by-line rather than a single regex over the raw remainder so
    # that a '#'-prefixed comment inside a fenced code block (```...```) quoted
    # from the issue body isn't mistaken for the next markdown heading.
    lines = body[match.end():].split("\n")
    end_line_idx = len(lines)
    in_fence = False
    for idx, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and re.match(r"^#{1,6}\s", line):
            end_line_idx = idx
            break
    return "\n".join(lines[:end_line_idx]).strip() or None


def parse_target_files(issue_body: str) -> List[str]:
    section = _extract_section(issue_body, "Target Files")
    if not section:
        return []
    paths: List[str] = []
    seen = set()
    for line in section.splitlines():
        # Each bullet names its path in the first backtick span; any further
        # backtick spans on the same line (e.g. "`src/x.py` — `func()` note")
        # are description text, not additional paths.
        match = re.search(r"`([^`]+)`", line)
        if match:
            path = match.group(1)
            if path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


def _resolve_target_file(rel_path: str, root: Path) -> Optional[Path]:
    candidate = (root / rel_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def _build_context_files_section(target_paths: List[str]) -> str:
    if not target_paths:
        return (
            '*No "Target Files" section found in the issue body — codebase context was '
            "not auto-populated. Add a `## Target Files` list to the issue, or reference "
            "relevant files manually.*"
        )
    root = src.config.get_effective_workspace_root().resolve()
    parts = []
    for rel_path in target_paths:
        resolved = _resolve_target_file(rel_path, root)
        if resolved is None:
            parts.append(
                f"### {rel_path}\n*(File does not exist yet — target for new creation.)*"
            )
        elif src.config.path_has_protected_secret(resolved.relative_to(root).parts):
            # Never surface secrets (.env*/.keys) in a handoff packet (issue #147).
            parts.append(f"### {rel_path}\n*(Protected secret path — not included.)*")
        else:
            parts.append(f"### {rel_path}\n{generate_file_summary(resolved)}")
    return "\n\n".join(parts)


def _build_status_protocol_section() -> str:
    """Static text describing the structured status-comment format (issue #70),
    so an agent receiving a handoff packet is told the reporting protocol as
    part of receiving work, not just agents that happen to already know it."""
    from src.agent_sync import STATUS_LABELS

    labels_list = "\n".join(f"- `{name}`" for name in STATUS_LABELS)
    return (
        "Apply one of the following labels to this issue to reflect your current state:\n\n"
        f"{labels_list}\n\n"
        "Then post a status comment on the issue using this exact hidden-comment format "
        "(a JSON object inside an HTML comment) so the system can parse it:\n\n"
        "```\n<!-- agent-status\n"
        '{"status": "in-progress", "progress": 60, "blocker": null, "agent": "your-registered-name"}\n'
        "-->\n```\n\n"
        "`status` must be one of `in-progress`, `blocked`, `review-ready`, `abandoned`. "
        "`progress` is an integer 0-100 (optional). `blocker` is a short description of what's "
        "blocking you, required when `status` is `blocked`. `agent` should match your registration "
        "name in the external_agents table, if you have one. Only comments from repo "
        "collaborators/members/owners are honored — comments from unverified accounts are ignored."
    )


def _build_conventions_section() -> str:
    contributing_path = src.config.ROOT_DIR / "CONTRIBUTING.md"
    try:
        return contributing_path.read_text(encoding="utf-8").strip()
    except OSError as e:
        return f"*Failed to read CONTRIBUTING.md: {e}*"


def _filter_untrusted_authors_enabled() -> bool:
    """Reads system_config['handoff.filter_untrusted_authors'], default True
    (filter on) if the row is missing/unreadable."""
    from src.explorer import _get_config_str
    value = _get_config_str("handoff.filter_untrusted_authors", "1")
    return value.strip().lower() not in ("0", "false", "no", "off")


def _build_discussion_section(comments: list) -> str:
    if not comments:
        return "*No comments on this issue.*"

    filter_untrusted = _filter_untrusted_authors_enabled()
    entries = [UNTRUSTED_DATA_NOTICE]
    for comment in comments:
        # GitHub returns "user": null for comments from deleted/ghost accounts.
        author = (comment.get("user") or {}).get("login", "unknown")
        created_at = comment.get("created_at", "")
        trusted = is_trusted_github_author(comment)

        if not trusted and filter_untrusted:
            body = (
                "*(comment from an unverified author omitted — set "
                "`handoff.filter_untrusted_authors=0` to display it)*"
            )
        else:
            body = comment.get("body", "")

        entries.append(
            f"**{author}** ({created_at}):\n"
            + quarantine_wrap(body, source="github-comment", author=author, trusted=trusted, include_notice=False)
        )
    return "\n\n".join(entries)


def generate_handoff(
    issue_number: int,
    repo: Optional[str] = None,
    agent_template: Optional[str] = None,
    party_id: Optional[str] = None,
    commit_db: bool = False,
) -> str:
    """Fetches issue_number from GitHub and assembles a self-contained markdown
    context bundle for an external coding agent, saving it to docs/drafts/."""
    repo = repo or src.config.GITHUB_REPO
    if not repo:
        raise ValueError("No repo configured: set GITHUB_REPO or pass repo=.")

    template_name = (agent_template or src.config.AGENT_HANDOFF_TEMPLATE or DEFAULT_TEMPLATE).lower()
    if template_name not in _AGENT_TEMPLATES:
        logger.warning(
            f"Unknown agent_template '{template_name}', falling back to '{DEFAULT_TEMPLATE}'."
        )
        template_name = DEFAULT_TEMPLATE
    preamble = _AGENT_TEMPLATES[template_name]

    gh = SafeGitHub(party_id=party_id)
    issue = gh.get_issue(repo, issue_number)
    comments = gh.list_issue_comments(repo, issue_number)

    title = issue.get("title", "")
    body = issue.get("body") or ""
    state = issue.get("state", "")
    labels = ", ".join(label.get("name", "") for label in (issue.get("labels") or []))
    issue_author = (issue.get("user") or {}).get("login", "unknown")
    issue_trusted = is_trusted_github_author(issue)

    target_paths = parse_target_files(body)

    acceptance_criteria = _extract_section(body, "Acceptance Criteria") or (
        "*No explicit Acceptance Criteria section in the issue — see Issue body above.*"
    )
    test_plan = _extract_section(body, "Test Plan") or (
        "*No explicit Test Plan section in the issue. Follow repository conventions: "
        "pytest, JANUS_TEST_MODE=1, mirror existing test file naming (test_<module>.py).*"
    )

    # Title is issue-author content too — kept out of the H1 heading (the very
    # first line of the bundle) and quarantined alongside the body instead.
    quarantined_issue = quarantine_wrap(
        f"**Title:** {title}\n\n{body}", source="github-issue-body", author=issue_author, trusted=issue_trusted
    )

    sections = [
        f"# Agent Handoff: Issue #{issue_number}",
        "## Issue\n\n"
        f"**Repo:** {repo}\n**Number:** #{issue_number}\n**State:** {state}\n"
        f"**Labels:** {labels or 'none'}\n\n{quarantined_issue}",
        f"## Discussion\n\n{_build_discussion_section(comments)}",
        f"## Context Files\n\n{_build_context_files_section(target_paths)}",
        "## Architecture Notes\n\n"
        "See CLAUDE.md for full architecture context: multi-party governance model, "
        "the Safe* SDK wrapper pattern, and the sandbox session lifecycle.",
        f"## Acceptance Criteria\n\n{acceptance_criteria}",
        f"## Test Plan\n\n{test_plan}",
        f"## Conventions\n\n{_build_conventions_section()}",
        f"## Status Reporting Protocol\n\n{_build_status_protocol_section()}",
    ]

    bundle = "\n\n".join(sections)
    if preamble:
        bundle = f"{preamble}\n\n{bundle}"

    draft_dir = src.config.ROOT_DIR / "docs" / "drafts"
    draft_dir.mkdir(parents=True, exist_ok=True)
    draft_path = draft_dir / f"issue_{issue_number}_handoff.md"
    draft_path.write_text(bundle, encoding="utf-8")

    if commit_db:
        SafeDocuments().upsert(
            title=f"Handoff: Issue #{issue_number}",
            content=bundle,
            tags=["handoff"],
            purpose="memory",
        )

    return bundle
