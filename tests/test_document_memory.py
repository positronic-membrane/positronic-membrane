"""
Tests for Janus Document Memory and Drafting Workspace.
Validates the new gitignored drafts folder workflow, DB sync skills, and persona command routing.

Mocking respects the GEMINI.md rule: mock in the module where imported/used.
"""
from unittest.mock import patch

import pytest

import src.config
import src.memory
from src.database import (
    create_document,
    delete_document,
    get_connection,
    get_document,
    init_db,
    update_document,
)
from src.persona import handle_docs_command
from src.skills import DynamicSkillExecutor, SafeDocuments

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Isolate SQLite DB for each test."""
    temp_db = tmp_path / "test_janus_docs.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    init_db()

    # Create a contributor party so DynamicSkillExecutor role checks pass
    conn = get_connection(read_only_constitution=False)
    conn.execute(
        "INSERT OR REPLACE INTO parties (id, name, role, public_key) VALUES ('contrib1', 'Alice', 'contributor', 'key1');"
    )
    conn.commit()
    conn.close()

    yield
    src.config.DB_PATH = orig_db_path


@pytest.fixture(autouse=True)
def mock_workspace_root(tmp_path):
    """Isolate workspace filesystem for SafeFS and config."""
    # Create the docs/drafts folder inside the temp path
    (tmp_path / "docs" / "drafts").mkdir(parents=True, exist_ok=True)

    # Mock get_effective_workspace_root in config
    with patch("src.config.get_effective_workspace_root", return_value=tmp_path):
        yield


# ---------------------------------------------------------------------------
# SafeDocuments Wrapper Class Tests
# ---------------------------------------------------------------------------

class TestSafeDocumentsWrapper:
    def test_upsert_and_get(self):
        sd = SafeDocuments()
        assert sd.upsert("Doc1", "Hello content", ["tag1"]) is True
        doc = sd.get("Doc1")
        assert doc is not None
        assert doc["title"] == "Doc1"
        assert doc["content"] == "Hello content"
        assert doc["tags"] == ["tag1"]
        # purpose/metadata default to 'memory' / {} when not specified
        assert doc["purpose"] == "memory"
        assert doc["metadata"] == {}

    def test_upsert_with_knowledge_purpose_and_metadata(self):
        sd = SafeDocuments()
        sd.upsert(
            "Curated Spec", "Important roadmap", purpose="knowledge",
            metadata={"source_url": "https://example.com/spec", "confidence": 0.9}
        )
        doc = sd.get("Curated Spec")
        assert doc["purpose"] == "knowledge"
        assert doc["metadata"] == {"source_url": "https://example.com/spec", "confidence": 0.9}

    def test_upsert_rejects_invalid_purpose(self):
        sd = SafeDocuments()
        with pytest.raises(ValueError):
            sd.upsert("Bad Doc", "content", purpose="archived")

    def test_list_and_filter(self):
        sd = SafeDocuments()
        sd.upsert("Doc A", "Content A", ["finance"])
        sd.upsert("Doc B", "Content B", ["personal"])

        all_docs = sd.list()
        # Cleaned DB might have other seeded docs, so >= 2
        assert len(all_docs) >= 2

        filtered = sd.list(tag_filter="finance")
        assert len(filtered) == 1
        assert filtered[0]["title"] == "Doc A"

    def test_list_filter_by_purpose(self):
        sd = SafeDocuments()
        sd.upsert("Memory Doc", "ephemeral note")
        sd.upsert("Knowledge Doc", "curated content", purpose="knowledge")

        knowledge_docs = sd.list(purpose="knowledge")
        assert len(knowledge_docs) == 1
        assert knowledge_docs[0]["title"] == "Knowledge Doc"

        memory_docs = sd.list(purpose="memory")
        assert any(d["title"] == "Memory Doc" for d in memory_docs)
        assert not any(d["title"] == "Knowledge Doc" for d in memory_docs)

    def test_delete(self):
        sd = SafeDocuments()
        sd.upsert("Doc Del", "Content")
        assert sd.delete("Doc Del") is True
        assert sd.get("Doc Del") is None


# ---------------------------------------------------------------------------
# Database Helper Function Tests (Retained for Backwards Compatibility)
# ---------------------------------------------------------------------------

class TestDatabaseHelpers:
    def test_create_and_get_document(self):
        doc_id = create_document("Helper Doc", "Test content", tags=["test"])
        assert isinstance(doc_id, int)

        doc = get_document("Helper Doc")
        assert doc is not None
        assert doc["title"] == "Helper Doc"
        assert doc["content"] == "Test content"
        assert doc["tags"] == ["test"]

    def test_update_document(self):
        create_document("Updatable", "old")
        assert update_document("Updatable", content="new", tags=["tag1"]) is True
        doc = get_document("Updatable")
        assert doc["content"] == "new"
        assert doc["tags"] == ["tag1"]

    def test_update_document_preserves_purpose_when_not_specified(self):
        """A content-only update must not silently downgrade a 'knowledge' doc back to 'memory'."""
        create_document("Curated Doc", "v1", purpose="knowledge", metadata={"confidence": 0.8})
        assert update_document("Curated Doc", content="v2") is True
        doc = get_document("Curated Doc")
        assert doc["content"] == "v2"
        assert doc["purpose"] == "knowledge"
        assert doc["metadata"] == {"confidence": 0.8}

    def test_update_document_can_change_purpose_explicitly(self):
        create_document("Promotable Doc", "draft note")
        assert update_document("Promotable Doc", purpose="knowledge", metadata={"promoted": True}) is True
        doc = get_document("Promotable Doc")
        assert doc["purpose"] == "knowledge"
        assert doc["metadata"] == {"promoted": True}

    def test_delete_document(self):
        create_document("Delete Me", "temp")
        assert delete_document("Delete Me") is True
        assert get_document("Delete Me") is None


# ---------------------------------------------------------------------------
# Draft Filesystem Skills Tests
# ---------------------------------------------------------------------------

class TestDraftFilesystemSkills:
    def test_write_and_read_draft_file(self):
        # Write
        res_write = DynamicSkillExecutor.execute(
            "write_draft_file",
            {"filename": "test_draft.md", "content": "Hello drafting world!"},
            party_id="contrib1"
        )
        assert res_write["success"]
        assert "saved to" in res_write["result"].lower()

        # Read
        res_read = DynamicSkillExecutor.execute(
            "read_draft_file",
            {"filename": "test_draft.md"},
            party_id="contrib1"
        )
        assert res_read["success"]
        assert res_read["result"] == "Hello drafting world!"

    def test_list_draft_files(self):
        DynamicSkillExecutor.execute(
            "write_draft_file",
            {"filename": "a_draft.md", "content": "A"},
            party_id="contrib1"
        )
        DynamicSkillExecutor.execute(
            "write_draft_file",
            {"filename": "b_draft.md", "content": "B"},
            party_id="contrib1"
        )

        res = DynamicSkillExecutor.execute("list_draft_files", {}, party_id="contrib1")
        assert res["success"]
        assert "a_draft.md" in res["result"]
        assert "b_draft.md" in res["result"]

    def test_delete_draft_file(self):
        DynamicSkillExecutor.execute(
            "write_draft_file",
            {"filename": "kill_draft.md", "content": "die"},
            party_id="contrib1"
        )
        # Delete
        res = DynamicSkillExecutor.execute(
            "delete_draft_file",
            {"filename": "kill_draft.md"},
            party_id="contrib1"
        )
        assert res["success"]
        assert "deleted" in res["result"].lower()

        # Try to read
        res_read = DynamicSkillExecutor.execute(
            "read_draft_file",
            {"filename": "kill_draft.md"},
            party_id="contrib1"
        )
        assert "[Error]" in res_read["result"]


# ---------------------------------------------------------------------------
# Database Sync Skills Tests
# ---------------------------------------------------------------------------

class TestDatabaseSyncSkills:
    def test_commit_draft_to_db(self):
        # 1. Write local draft file
        DynamicSkillExecutor.execute(
            "write_draft_file",
            {"filename": "my_roadmap.md", "content": "# Roadmap V1\n- Task 1\n- Task 2"},
            party_id="contrib1"
        )

        # 2. Commit it to SQLite DB
        res = DynamicSkillExecutor.execute(
            "commit_draft_to_db",
            {"filename": "my_roadmap.md", "doc_title": "Project Roadmap", "tags": ["v1", "active"]},
            party_id="contrib1"
        )
        assert res["success"]
        assert "successfully committed" in res["result"].lower()

        # 3. Verify in database
        doc = get_document("Project Roadmap")
        assert doc is not None
        assert doc["content"] == "# Roadmap V1\n- Task 1\n- Task 2"
        assert doc["tags"] == ["v1", "active"]
        assert doc["purpose"] == "memory"

    def test_commit_draft_to_db_with_knowledge_purpose(self):
        DynamicSkillExecutor.execute(
            "write_draft_file",
            {"filename": "architecture.md", "content": "# Architecture\nCurated and authoritative."},
            party_id="contrib1"
        )

        res = DynamicSkillExecutor.execute(
            "commit_draft_to_db",
            {
                "filename": "architecture.md",
                "doc_title": "Architecture Doc",
                "purpose": "knowledge",
                "metadata": {"source": "design review"},
            },
            party_id="contrib1"
        )
        assert res["success"]
        assert "knowledge" in res["result"].lower()

        doc = get_document("Architecture Doc")
        assert doc["purpose"] == "knowledge"
        assert doc["metadata"] == {"source": "design review"}

    def test_checkout_db_to_draft(self):
        # 1. Seed database document
        create_document("Remote Document", "Text from DB", tags=["db"])

        # 2. Checkout to draft file
        res = DynamicSkillExecutor.execute(
            "checkout_db_to_draft",
            {"doc_title": "Remote Document", "filename": "checked_out.md"},
            party_id="contrib1"
        )
        assert res["success"]
        assert "successfully checked out" in res["result"].lower()

        # 3. Read local draft and assert
        res_read = DynamicSkillExecutor.execute(
            "read_draft_file",
            {"filename": "checked_out.md"},
            party_id="contrib1"
        )
        assert res_read["success"]
        assert res_read["result"] == "Text from DB"

    def test_delete_db_document(self):
        create_document("Ghost Doc", "Content")
        res = DynamicSkillExecutor.execute(
            "delete_db_document",
            {"doc_title": "Ghost Doc"},
            party_id="contrib1"
        )
        assert res["success"]
        assert "successfully deleted" in res["result"].lower()
        assert get_document("Ghost Doc") is None


# ---------------------------------------------------------------------------
# Read-Only Document Memory Skill Tests
# ---------------------------------------------------------------------------

class TestDocumentMemorySkillReadOnly:
    def test_skill_list(self):
        create_document("Doc A", "aaa", tags=["test_tag"])
        create_document("Doc B", "bbb", tags=["other"])

        res = DynamicSkillExecutor.execute(
            "document_memory",
            {"action": "list"},
            party_id="contrib1"
        )
        assert res["success"]
        assert "Doc A" in res["result"]
        assert "Doc B" in res["result"]

    def test_skill_list_filtered_by_purpose(self):
        create_document("Memory Note", "aaa", purpose="memory")
        create_document("Knowledge Doc", "bbb", purpose="knowledge")

        res = DynamicSkillExecutor.execute(
            "document_memory",
            {"action": "list", "purpose": "knowledge"},
            party_id="contrib1"
        )
        assert res["success"]
        assert "Knowledge Doc" in res["result"]
        assert "Memory Note" not in res["result"]

    def test_skill_get(self):
        create_document("Doc Get", "Body of doc", tags=["view"])
        res = DynamicSkillExecutor.execute(
            "document_memory",
            {"action": "get", "title": "Doc Get"},
            party_id="contrib1"
        )
        assert res["success"]
        assert "Doc Get" in res["result"]
        assert "Body of doc" in res["result"]
        assert "memory" in res["result"].lower()

    def test_skill_unsupported_actions_raise(self):
        # Old mutate actions should raise ValueError
        for action in ("create", "update", "delete"):
            res = DynamicSkillExecutor.execute(
                "document_memory",
                {"action": action, "title": "Fail", "content": "Fail"},
                party_id="contrib1"
            )
            assert not res["success"] or "[Error]" in str(res.get("result", ""))


# ---------------------------------------------------------------------------
# Persona CLI Handler `/docs` Commands Tests
# ---------------------------------------------------------------------------

class TestPersonaDocsCommands:
    @patch("src.persona.get_session_party_id", return_value="contrib1")
    def test_docs_create_command(self, mock_party):
        res = handle_docs_command("/docs create Setup Instructions")
        assert "saved to" in res.lower()

        # Verify draft file was written
        res_read = DynamicSkillExecutor.execute(
            "read_draft_file",
            {"filename": "Setup_Instructions.md"},
            party_id="contrib1"
        )
        assert res_read["success"]
        assert "# Setup Instructions" in res_read["result"]

    @patch("src.persona.get_session_party_id", return_value="contrib1")
    def test_docs_commit_command(self, mock_party):
        # 1. Create a draft
        DynamicSkillExecutor.execute(
            "write_draft_file",
            {"filename": "manual.md", "content": "Read me text"},
            party_id="contrib1"
        )

        # 2. Commit via command
        res = handle_docs_command("/docs commit manual.md | System Manual #docs #sys")
        assert "successfully committed" in res.lower()

        # 3. Verify DB document content
        doc = get_document("System Manual")
        assert doc is not None
        assert doc["content"] == "Read me text"
        assert doc["tags"] == ["docs", "sys"]

    @patch("src.persona.get_session_party_id", return_value="contrib1")
    def test_docs_get_command(self, mock_party):
        create_document("Archived Spec", "Database Content")

        # Checkout DB document to local draft file
        res = handle_docs_command("/docs get Archived Spec")
        assert "successfully checked out" in res.lower()

        # Verify draft file exists and matches DB content
        res_read = DynamicSkillExecutor.execute(
            "read_draft_file",
            {"filename": "Archived_Spec.md"},
            party_id="contrib1"
        )
        assert res_read["success"]
        assert res_read["result"] == "Database Content"

    @patch("src.persona.get_session_party_id", return_value="contrib1")
    def test_docs_list_command(self, mock_party):
        create_document("Specs A", "Content A")
        res = handle_docs_command("/docs list")
        assert "Specs A" in res

    @patch("src.persona.get_session_party_id", return_value="contrib1")
    def test_docs_delete_command(self, mock_party):
        create_document("Trash Doc", "Content")
        res = handle_docs_command("/docs delete Trash Doc")
        assert "deleted" in res.lower()
        assert get_document("Trash Doc") is None

    @patch("src.persona.get_session_party_id", return_value="contrib1")
    def test_docs_drafts_list_command(self, mock_party):
        DynamicSkillExecutor.execute(
            "write_draft_file",
            {"filename": "test_list_draft.md", "content": "123"},
            party_id="contrib1"
        )
        res = handle_docs_command("/docs drafts")
        assert "test_list_draft.md" in res
