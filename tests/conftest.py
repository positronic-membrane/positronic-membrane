import json
import os
from pathlib import Path

import pytest

import src.config
from src.database import get_connection, init_db

# Ensure skill library boot sync is skipped for all tests (always override, not setdefault —
# setdefault would leave a pre-existing JANUS_TEST_MODE=0 in place and trigger a real git clone)
os.environ["JANUS_TEST_MODE"] = "1"

_FIXTURE_SKILLS_DIR = Path(__file__).parent / "fixtures" / "skills_library"


def _seed_skills_from_fixture(conn) -> None:
    """Insert test-fixture skills directly into agent_skills, bypassing the staging harness.

    These blobs are dev-authored and trusted; running pytest for each one on every test
    would make the suite prohibitively slow.  INSERT OR REPLACE so fixture versions win
    over any minimal bootstrap rows already seeded by init_db().
    """
    registry_path = _FIXTURE_SKILLS_DIR / "registry.json"
    if not registry_path.exists():
        return
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    for skill in registry.get("skills", []):
        skill_file = _FIXTURE_SKILLS_DIR / skill["file"]
        if not skill_file.exists():
            continue
        code_blob = skill_file.read_text(encoding="utf-8")
        conn.execute(
            """INSERT OR REPLACE INTO agent_skills (
                   skill_id, name, description, parameters_schema, code_blob,
                   entry_point_function, required_role, trigger_type, trigger_config
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                skill["skill_id"],
                skill.get("name", skill["skill_id"]),
                skill.get("description", ""),
                skill.get("parameters_schema", "{}"),
                code_blob,
                skill.get("entry_point_function", "run"),
                skill.get("required_role", "contributor"),
                skill.get("trigger_type", "manual"),
                skill.get("trigger_config", "{}"),
            ),
        )
    conn.commit()


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """
    Global auto-use fixture that isolates DB_PATH for every test execution.
    This guarantees that tests never read/write the production database.
    Local test-file fixtures with the same name override this one; that is expected.
    """
    temp_db = tmp_path / "test_janus.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)

    init_db()

    yield

    src.config.DB_PATH = orig_db_path


@pytest.fixture(autouse=True)
def _no_live_neo4j(monkeypatch):
    """Blank NEO4J_URI for every test so the suite never reaches a live Neo4j
    instance (or spends LLM budget) through the epistemic ingestion hooks —
    the developer's .env may configure a real Aura endpoint. Tests exercising
    ingestion re-set src.config.NEO4J_URI themselves and mock the pipeline."""
    monkeypatch.setattr(src.config, "NEO4J_URI", "")


@pytest.fixture(autouse=True)
def _seed_test_skills(setup_test_db):
    """Seed library skills into the test DB after the schema is initialised.

    Depends on setup_test_db (the local override when present, otherwise the conftest
    default) so that init_db() and DB_PATH isolation always run first.  Uses a unique
    underscore-prefixed name so no test file accidentally overrides it.
    """
    conn = get_connection()
    try:
        _seed_skills_from_fixture(conn)
    finally:
        conn.close()
