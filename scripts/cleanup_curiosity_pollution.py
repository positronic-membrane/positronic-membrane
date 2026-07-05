"""One-time cleanup for the curiosity-topic pollution described in issue #78.

Prior to the fix in `run_reflection_cycle` (positronic-membrane/janus-skills-library#13),
curiosity topics were parsed with a bracket-inviting prompt + bare comma split, which left
bracket-polluted and comma-shredded fragments in the `janus_curiosity` vector collection and
in `drive_state.curiosity_vector_json`. This script cleans up data already written before the
fix landed. It does NOT need to run again after this one-time pass.

- `janus_curiosity` docs containing a stray `[` or `]` are retired (metadata `resolved: "true"`)
  rather than edited/deleted, since the VectorStoreAdapter interface only supports metadata
  updates, not document text changes or deletion.
- Comma-shredded fragments were already split into separate vector rows at write time and can't
  be losslessly reconstructed, so they are only flagged for human review, never auto-retired.
- `drive_state.curiosity_vector_json` entries are regex-cleaned of stray brackets in place.

Usage:
    python scripts/cleanup_curiosity_pollution.py            # dry run (default)
    python scripts/cleanup_curiosity_pollution.py --apply    # write changes
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.database import get_curiosity_vector, update_curiosity_vector  # noqa: E402
from src.memory import get_collection  # noqa: E402


def clean_curiosity_collection(apply: bool) -> None:
    coll = get_collection("janus_curiosity")
    results = coll.get(where={"resolved": "false"})
    docs = results.get("documents") or []
    metas = results.get("metadatas") or []
    ids = results.get("ids") or []

    retire_ids, retire_metas = [], []
    for doc, _meta, doc_id in zip(docs, metas, ids, strict=True):
        if "[" in doc or "]" in doc:
            print(f"RETIRE (bracket-polluted): {doc_id} -> {doc!r}")
            retire_ids.append(doc_id)
            # Send only the changed keys (not a copy of the fetched metadata) — both
            # backends merge on update, so re-sending a snapshot fetched moments ago
            # would clobber any concurrent field changes (e.g. relevance_count bumped
            # by a live reflection cycle) with stale values.
            retire_metas.append({"resolved": "true", "retired_reason": "issue_78_cleanup"})

    print(f"janus_curiosity: {len(retire_ids)} bracket-polluted doc(s) to retire "
          f"out of {len(docs)} active. Comma-shredded fragments (no brackets) can't be "
          f"reliably auto-detected — review remaining active janus_curiosity entries "
          f"manually if pollution persists after this pass.")
    if apply and retire_ids:
        coll.update(ids=retire_ids, metadatas=retire_metas)
        print(f"janus_curiosity: retired {len(retire_ids)} doc(s).")


def clean_drive_vector(apply: bool) -> None:
    vector = get_curiosity_vector()
    cleaned = []
    seen = set()
    for t in vector:
        t = re.sub(r"[\[\]]", "", t).strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            cleaned.append(t)

    if cleaned == vector:
        print("drive_state.curiosity_vector_json: no cleanup needed.")
        return

    print(f"drive_state.curiosity_vector_json: {vector!r} -> {cleaned!r}")
    if apply:
        update_curiosity_vector(cleaned)
        print("drive_state.curiosity_vector_json: updated.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run).")
    args = parser.parse_args()

    if not args.apply:
        print("Dry run (pass --apply to write changes).\n")

    clean_curiosity_collection(args.apply)
    print()
    clean_drive_vector(args.apply)


if __name__ == "__main__":
    main()
