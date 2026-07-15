"""Issue #108 audit: enumerates each agent_registry row's effective LLM
endpoint under the current env. Read-only — prints a table to stdout for
manual transcription into docs/threat_model.md's routing posture section
(and, separately and manually, into a comment on GitHub issue #108 — that
step is a human-confirmed action, not automated by this script).

Usage:
    python scripts/audit_agent_routing.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.database import init_db  # noqa: E402
from src.llm import get_agent_routing_audit  # noqa: E402


def main():
    init_db()
    findings = get_agent_routing_audit()

    print(f"{'agent_id':<12} {'model':<30} {'resolved_endpoint':<40} {'allow_offbox':<13} would_violate")
    for f in findings:
        print(
            f"{f['agent_id']:<12} {str(f['model']):<30} {f['resolved_endpoint']:<40} "
            f"{str(f['allow_offbox']):<13} {f['would_violate']}"
        )

    violations = [f for f in findings if f["would_violate"]]
    if violations:
        print(f"\n{len(violations)} agent(s) would raise OffboxRoutingViolationError at call time.")


if __name__ == "__main__":
    main()
