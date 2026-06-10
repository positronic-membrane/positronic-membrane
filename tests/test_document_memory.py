"""
Tests for Janus Document Memory (janus_documents table + helper functions + dynamic skill).

All tests use the established tmp_path / src.config.DB_PATH isolation pattern.
Mocking respects the GEMINI.md rule: mock in the module where imported/used.
"""
import pytest
import json
import src.config
import src.memory
from unittest.mock import patch
from src.database import (
    init_db,
    get_connection,
    create_document,
    get_document,
    update_document,
    delete_document,
    list_documents,
)
from src.skills import DynamicSkillExecutor


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
        "INSERT INTO parties (id, name, role, public_key) VALUES ('contrib1', 'Alice', 'contributor', 'key1');"
    )
    conn.commit()
    conn.close()

    yield
    src.config.DB_PATH = orig_db_path


@pytest.fixture(autouse=True)
def setup_test_vector_db(tmp_path):
    """Isolate ChromaDB persistent directory."""
    orig_path = src.config.VECTOR_DB_PATH
    src.config.VECTOR_DB_PATH = str(tmp_path / "test_chromadb")
    src.memory._chroma_client = None
    src.memory._collections = {}
    yield
    src.config.VECTOR_DB_PATH = orig_path


# ---------------------------------------------------------------------------
# Helper Function Tests
# ---------------------------------------------------------------------------

class TestCreateDocument:
    def test_create_returns_integer_id(self):
        doc_id = create_document("My First Doc", "Hello world")
        assert isinstance(doc_id, int)
        assert doc_id > 0

    def test_create_with_tags(self):
        create_document("Tagged Doc", "Content here", tags=["work", "important"])
        doc = get_document("Tagged Doc")
        assert doc is not None
        assert "work" in doc["tags"]
        assert "important" in doc["tags"]

    def test_create_without_tags_defaults_to_empty_list(self):
        create_document("No Tags Doc", "Body text")
        doc = get_document("No Tags Doc")
        assert doc["tags"] == []

    def test_duplicate_title_raises(self):
        create_document("Unique Title", "First version")
        with pytest.raises(Exception):
            create_document("Unique Title", "Second version")


class TestGetDocument:
    def test_get_existing_document(self):
        create_document("Spec Doc", "Specification content", tags=["spec"])
        doc = get_document("Spec Doc")
        assert doc is not None
        assert doc["title"] == "Spec Doc"
        assert doc["content"] == "Specification content"
        assert doc["tags"] == ["spec"]
        assert "id" in doc
        assert "created_at" in doc
        assert "updated_at" in doc

    def test_get_nonexistent_returns_none(self):
        result = get_document("Does Not Exist")
        assert result is None


class TestUpdateDocument:
    def test_update_content(self):
        create_document("Updatable", "Old content")
        result = update_document("Updatable", content="New content")
        assert result is True
        doc = get_document("Updatable")
        assert doc["content"] == "New content"

    def test_update_tags(self):
        create_document("Tag Me", "Some text", tags=["old"])
        result = update_document("Tag Me", tags=["new", "fresh"])
        assert result is True
        doc = get_document("Tag Me")
        assert "new" in doc["tags"]
        assert "old" not in doc["tags"]

    def test_update_content_and_tags_together(self):
        create_document("Both", "initial", tags=["a"])
        update_document("Both", content="revised", tags=["b", "c"])
        doc = get_document("Both")
        assert doc["content"] == "revised"
        assert doc["tags"] == ["b", "c"]

    def test_update_nonexistent_returns_false(self):
        result = update_document("Ghost Doc", content="anything")
        assert result is False


class TestDeleteDocument:
    def test_delete_existing_document(self):
        create_document("To Delete", "Temporary")
        result = delete_document("To Delete")
        assert result is True
        assert get_document("To Delete") is None

    def test_delete_nonexistent_returns_false(self):
        result = delete_document("Never Existed")
        assert result is False


class TestListDocuments:
    def test_list_empty(self):
        docs = list_documents()
        assert docs == []

    def test_list_all_documents(self):
        create_document("Alpha", "Content A", tags=["cat1"])
        create_document("Beta", "Content B", tags=["cat2"])
        docs = list_documents()
        assert len(docs) == 2
        titles = {d["title"] for d in docs}
        assert "Alpha" in titles
        assert "Beta" in titles

    def test_list_with_tag_filter(self):
        create_document("Filtered In", "text", tags=["finance"])
        create_document("Filtered Out", "text", tags=["personal"])
        docs = list_documents(tag_filter="finance")
        assert len(docs) == 1
        assert docs[0]["title"] == "Filtered In"

    def test_list_without_matching_tag_returns_empty(self):
        create_document("Only One", "text", tags=["design"])
        docs = list_documents(tag_filter="nonexistent-tag")
        assert docs == []


# ---------------------------------------------------------------------------
# Dynamic Skill Tests (via DynamicSkillExecutor)
# ---------------------------------------------------------------------------

class TestDocumentMemorySkill:
    """Runs all five actions through the dynamic skill executor."""

    def test_skill_create(self):
        res = DynamicSkillExecutor.execute(
            "document_memory",
            {"action": "create", "title": "Skill Doc", "content": "Created via skill"},
            party_id="contrib1",
        )
        assert res["success"], res.get("error")
        assert "created" in res["result"].lower()

    def test_skill_get(self):
        create_document("Skill Get", "Readable content", tags=["test"])
        res = DynamicSkillExecutor.execute(
            "document_memory",
            {"action": "get", "title": "Skill Get"},
            party_id="contrib1",
        )
        assert res["success"], res.get("error")
        assert "Skill Get" in res["result"]
        assert "Readable content" in res["result"]

    def test_skill_get_not_found(self):
        res = DynamicSkillExecutor.execute(
            "document_memory",
            {"action": "get", "title": "Missing"},
            party_id="contrib1",
        )
        assert res["success"]  # returns [Error] message, not an exception
        assert "[Error]" in res["result"]

    def test_skill_list_empty(self):
        res = DynamicSkillExecutor.execute(
            "document_memory",
            {"action": "list"},
            party_id="contrib1",
        )
        assert res["success"], res.get("error")
        assert "no documents" in res["result"].lower()

    def test_skill_list_with_documents(self):
        create_document("List Doc A", "aaa", tags=["x"])
        create_document("List Doc B", "bbb", tags=["y"])
        res = DynamicSkillExecutor.execute(
            "document_memory",
            {"action": "list"},
            party_id="contrib1",
        )
        assert res["success"], res.get("error")
        assert "List Doc A" in res["result"]
        assert "List Doc B" in res["result"]

    def test_skill_list_with_tag_filter(self):
        create_document("Tagged Alpha", "aaa", tags=["alpha"])
        create_document("Tagged Beta", "bbb", tags=["beta"])
        res = DynamicSkillExecutor.execute(
            "document_memory",
            {"action": "list", "tag_filter": "alpha"},
            party_id="contrib1",
        )
        assert res["success"], res.get("error")
        assert "Tagged Alpha" in res["result"]
        assert "Tagged Beta" not in res["result"]

    def test_skill_update(self):
        create_document("Update Me", "old text")
        res = DynamicSkillExecutor.execute(
            "document_memory",
            {"action": "update", "title": "Update Me", "content": "new text"},
            party_id="contrib1",
        )
        assert res["success"], res.get("error")
        assert "updated" in res["result"].lower()
        doc = get_document("Update Me")
        assert doc["content"] == "new text"

    def test_skill_update_not_found(self):
        res = DynamicSkillExecutor.execute(
            "document_memory",
            {"action": "update", "title": "Phantom", "content": "irrelevant"},
            party_id="contrib1",
        )
        assert res["success"]
        assert "[Error]" in res["result"]

    def test_skill_delete(self):
        create_document("Kill Me", "soon gone")
        res = DynamicSkillExecutor.execute(
            "document_memory",
            {"action": "delete", "title": "Kill Me"},
            party_id="contrib1",
        )
        assert res["success"], res.get("error")
        assert "deleted" in res["result"].lower()
        assert get_document("Kill Me") is None

    def test_skill_delete_not_found(self):
        res = DynamicSkillExecutor.execute(
            "document_memory",
            {"action": "delete", "title": "Unknown"},
            party_id="contrib1",
        )
        assert res["success"]
        assert "[Error]" in res["result"]

    def test_skill_create_duplicate_returns_error_message(self):
        """Duplicate titles should return an [Error] string, not a Python exception."""
        DynamicSkillExecutor.execute(
            "document_memory",
            {"action": "create", "title": "Dupe", "content": "first"},
            party_id="contrib1",
        )
        res = DynamicSkillExecutor.execute(
            "document_memory",
            {"action": "create", "title": "Dupe", "content": "second"},
            party_id="contrib1",
        )
        assert res["success"]
        assert "[Error]" in res["result"]

    def test_skill_unknown_action_raises(self):
        res = DynamicSkillExecutor.execute(
            "document_memory",
            {"action": "teleport"},
            party_id="contrib1",
        )
        assert not res["success"]
        assert "ValueError" in res["error"] or "Unknown" in res["error"]
