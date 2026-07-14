"""
Agent Status Sync (issue #70). Polls GitHub issues carrying agent:* labels for
structured status comments posted by external coding agents, so the operator
gets visibility into their progress/blockers without manually checking a
terminal or the issue by hand.

Public repo hazard (issue #107): a status comment is an unauthenticated write
channel into the daemon's episodic memory and the pending_escalations queue
that reaches the operator's next conversation turn. Every comment is gated by
is_trusted_github_author() before it is parsed at all — the same allowlist
mechanism src/pr_review.py already uses, not a new one — and any blocker text
that does reach a prompt is quarantine_wrap()-ed and length-capped.
"""
import json
import logging
import re
import time
from typing import Optional

import src.config
from src.database import enqueue_escalation, get_connection, log_episodic_memory
from src.middleware import is_trusted_github_author, quarantine_wrap
from src.skills import SafeGitHub

logger = logging.getLogger("JanusAgentSync")

_STATUS_COMMENT_RE = re.compile(r"<!--\s*agent-status\s*(\{.*?\})\s*-->", re.DOTALL)
_VALID_STATUSES = {"in-progress", "blocked", "review-ready", "abandoned"}
_BLOCKER_TEXT_CHAR_LIMIT = 500

# name -> (color, description). Applied to a repo once (ensure_agent_status_labels)
# and documented verbatim in every handoff bundle (src/agent_handoff.py).
STATUS_LABELS = {
    "agent:in-progress": ("fbca04", "Agent is actively working this issue"),
    "agent:blocked": ("d73a4a", "Agent is blocked and needs operator input"),
    "agent:review-ready": ("0e8a16", "Agent believes work is ready for review"),
    "agent:abandoned": ("6a737d", "Agent stopped work without completing"),
}


def _parse_status_comment(body: str) -> Optional[dict]:
    """Extracts and validates the hidden agent-status JSON block from a comment
    body. Returns None (never raises) for anything malformed or absent — the
    same "no update" posture as a comment with no status block at all."""
    if not body:
        return None
    match = _STATUS_COMMENT_RE.search(body)
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    status = payload.get("status")
    if status not in _VALID_STATUSES:
        return None

    progress = payload.get("progress")
    if progress is not None:
        try:
            progress = int(progress)
        except (TypeError, ValueError):
            progress = None
        else:
            if not (0 <= progress <= 100):
                progress = None

    blocker = payload.get("blocker")
    if blocker is not None and not isinstance(blocker, str):
        blocker = str(blocker)

    agent = payload.get("agent")
    if agent is not None and not isinstance(agent, str):
        agent = None

    return {"status": status, "progress": progress, "blocker": blocker, "agent": agent}


_LABEL_RETRY_BACKOFF_SECONDS = 86400  # 1 day


def ensure_agent_status_labels(gh: SafeGitHub, repo: str) -> bool:
    """Idempotently creates the agent:* labels on `repo`. Short-circuits to
    zero API calls once system_config['agent_sync.labels_ensured'] is '1' —
    GitHub API calls share a 50/hr budget with everything else SafeGitHub
    does, so this must not cost 4 calls on every poll forever. If the token
    permanently lacks label-write permission, every attempt would otherwise
    fail and retry all 4 calls on every single poll (up to 48/hr at the
    default cadence, starving the actual issue/comment scanning this feature
    exists for) — back off to at most one retry attempt per day instead."""
    from src.explorer import _get_config_str

    if _get_config_str("agent_sync.labels_ensured", "0") == "1":
        return True

    last_attempted_raw = _get_config_str("agent_sync.labels_last_attempted_at", "")
    now = time.time()
    if last_attempted_raw:
        try:
            if now - float(last_attempted_raw) < _LABEL_RETRY_BACKOFF_SECONDS:
                return False
        except ValueError:
            pass

    conn = get_connection(read_only_constitution=True)
    try:
        conn.execute(
            "UPDATE system_config SET config_value = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE config_key = 'agent_sync.labels_last_attempted_at';",
            (str(now),),
        )
        conn.commit()
    finally:
        conn.close()

    all_ok = True
    for name, (color, description) in STATUS_LABELS.items():
        try:
            gh.create_label(repo, name, color=color, description=description)
        except RuntimeError as e:
            if "422" not in str(e):
                all_ok = False
                logger.warning(f"Failed to create label '{name}' on {repo}: {e}")
            # 422 == label already exists — treat as success.
        except Exception as e:
            all_ok = False
            logger.warning(f"Failed to create label '{name}' on {repo}: {e}")

    if all_ok:
        conn = get_connection(read_only_constitution=True)
        try:
            conn.execute(
                "UPDATE system_config SET config_value = '1', updated_at = CURRENT_TIMESTAMP "
                "WHERE config_key = 'agent_sync.labels_ensured';"
            )
            conn.commit()
        finally:
            conn.close()
    return all_ok


def _resolve_agent_id(agent_name: Optional[str]) -> Optional[int]:
    if not agent_name:
        return None
    conn = get_connection(read_only_constitution=True)
    try:
        row = conn.execute(
            "SELECT id FROM external_agents WHERE name = ?;", (agent_name,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _get_existing_status_row(repo: str, issue_number: int, github_login: str) -> Optional[dict]:
    conn = get_connection(read_only_constitution=True)
    try:
        row = conn.execute(
            "SELECT id, status, last_comment_id FROM agent_work_status "
            "WHERE repo = ? AND issue_number = ? AND github_login = ?;",
            (repo, issue_number, github_login),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        return {"id": row["id"], "status": row["status"], "last_comment_id": row["last_comment_id"]}
    except (TypeError, IndexError, KeyError):
        return {"id": row[0], "status": row[1], "last_comment_id": row[2]}


def _upsert_status_row(
    *, repo: str, issue_number: int, github_login: str, agent_id: Optional[int],
    parsed: dict, comment_id: int, comment_url: str, existing_id: Optional[int],
) -> None:
    blocker_text = (parsed.get("blocker") or "")[:_BLOCKER_TEXT_CHAR_LIMIT] or None
    conn = get_connection(read_only_constitution=True)
    try:
        if existing_id is not None:
            conn.execute(
                "UPDATE agent_work_status SET agent_id = ?, status = ?, progress = ?, "
                "blocker_text = ?, last_comment_id = ?, last_comment_url = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?;",
                (agent_id, parsed["status"], parsed.get("progress"), blocker_text,
                 comment_id, comment_url, existing_id),
            )
        else:
            conn.execute(
                "INSERT INTO agent_work_status "
                "(agent_id, repo, issue_number, github_login, status, progress, "
                "blocker_text, last_comment_id, last_comment_url) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);",
                (agent_id, repo, issue_number, github_login, parsed["status"],
                 parsed.get("progress"), blocker_text, comment_id, comment_url),
            )
        conn.commit()
    finally:
        conn.close()


def _throttle_ok() -> bool:
    """Reads/updates agent_sync.poll_interval_seconds and .last_poll_time.
    Writes the new last_poll_time BEFORE any GitHub API work happens, so a
    mid-poll failure doesn't cause an immediate retry storm on the next tick."""
    from src.explorer import _get_config_int, _get_config_str

    interval = _get_config_int("agent_sync.poll_interval_seconds", 300)
    last_poll_raw = _get_config_str("agent_sync.last_poll_time", "")
    now = time.time()
    if last_poll_raw:
        try:
            if now - float(last_poll_raw) < interval:
                return False
        except ValueError:
            pass

    conn = get_connection(read_only_constitution=True)
    try:
        conn.execute(
            "UPDATE system_config SET config_value = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE config_key = 'agent_sync.last_poll_time';",
            (str(now),),
        )
        conn.commit()
    finally:
        conn.close()
    return True


def poll_agent_status(repo: Optional[str] = None, party_id: Optional[str] = None) -> dict:
    """Scans open issues carrying agent:* labels for trusted status comments,
    persists the latest verified status per (repo, issue, commenter), logs
    every processed update to episodic memory, and enqueues an escalation on
    any new transition into 'blocked'. Self-throttled independently of
    whatever cadence calls it (see _throttle_ok)."""
    repo = repo or src.config.GITHUB_REPO
    if not repo:
        return {"skipped": "no_repo"}

    if not _throttle_ok():
        return {"skipped": "throttled"}

    effective_party_id = party_id or "system"
    gh = SafeGitHub(party_id=effective_party_id)
    ensure_agent_status_labels(gh, repo)

    labeled_issue_numbers: set = set()
    for label_name in STATUS_LABELS:
        try:
            issues = gh.list_open_issues(repo, label=label_name)
        except Exception as e:
            logger.error(f"Failed to list issues labeled '{label_name}' on {repo}: {e}")
            continue
        for issue in issues or []:
            number = issue.get("number")
            if number is not None:
                labeled_issue_numbers.add(number)

    status_updates = 0
    new_blockers = 0
    skipped_untrusted = 0

    for issue_number in labeled_issue_numbers:
        try:
            comments = gh.list_issue_comments(repo, issue_number)
        except Exception as e:
            logger.error(f"Failed to list comments for {repo}#{issue_number}: {e}")
            continue

        # Track the latest trusted+parsed status PER commenter, not a single
        # overall latest — multiple agents can legitimately work the same
        # issue concurrently (agent_work_status is keyed on github_login), so
        # an earlier comment from agent-a must not be discarded just because
        # agent-b commented more recently.
        latest_by_login: dict = {}
        for comment in comments or []:
            if not is_trusted_github_author(comment):
                skipped_untrusted += 1
                continue
            parsed = _parse_status_comment(comment.get("body", ""))
            if parsed is None:
                continue
            login = (comment.get("user") or {}).get("login", "unknown")
            latest_by_login[login] = (comment, parsed)

        for github_login, (comment, parsed) in latest_by_login.items():
            comment_id = comment.get("id")
            comment_url = comment.get("html_url", "")

            existing = _get_existing_status_row(repo, issue_number, github_login)
            if existing and existing["last_comment_id"] == comment_id:
                continue  # already processed this exact comment on a prior poll

            is_new_blocker = parsed["status"] == "blocked" and (
                existing is None or existing["status"] != "blocked"
            )
            agent_id = _resolve_agent_id(parsed.get("agent"))
            _upsert_status_row(
                repo=repo, issue_number=issue_number, github_login=github_login,
                agent_id=agent_id, parsed=parsed, comment_id=comment_id,
                comment_url=comment_url, existing_id=existing["id"] if existing else None,
            )
            status_updates += 1

            who = parsed.get("agent") or github_login
            summary_line = f"Agent status: {repo}#{issue_number} ({who}) -> {parsed['status']}"
            if parsed.get("progress") is not None:
                summary_line += f" ({parsed['progress']}%)"
            log_episodic_memory(speaker="system", message_content=summary_line, context_type="background_thought")

            if is_new_blocker:
                new_blockers += 1
                blocker_text = (parsed.get("blocker") or "")[:_BLOCKER_TEXT_CHAR_LIMIT]
                detail = quarantine_wrap(
                    blocker_text or "(no blocker description provided)",
                    source="github-issue-comment", author=github_login, trusted=True,
                )
                enqueue_escalation(
                    source="agent_status_blocked",
                    summary=f"Agent '{who}' reported a BLOCKER on {repo}#{issue_number}",
                    detail=detail,
                )

    return {
        "issues_scanned": len(labeled_issue_numbers),
        "labeled_issues": sorted(labeled_issue_numbers),
        "status_updates": status_updates,
        "new_blockers": new_blockers,
        "skipped_untrusted": skipped_untrusted,
    }
