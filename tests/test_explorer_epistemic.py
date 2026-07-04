"""Tests for the explorer → epistemic pipeline wiring (issue #74).

All LLM and pipeline calls are mocked at the call site
(src.explorer.query_agent / src.explorer.run_epistemic_pipeline), per GEMINI.md.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

import src.config
from src.database import get_connection
from src.explorer import (
    extract_candidate_facts,
    get_max_facts_per_cycle,
    ingest_discoveries,
)
from src.skills import SafeExplorer


def _set_cap(value: str):
    with get_connection() as conn:
        conn.execute(
            "UPDATE system_config SET config_value = ? WHERE config_key = 'epistemic.max_facts_per_cycle';",
            (value,),
        )
        conn.commit()


@pytest.fixture
def neo4j_configured(monkeypatch):
    monkeypatch.setattr(src.config, "NEO4J_URI", "bolt://localhost:7687")


@pytest.fixture
def neo4j_unconfigured(monkeypatch):
    monkeypatch.setattr(src.config, "NEO4J_URI", "")


# ---------------------------------------------------------------------------
# Fact extraction
# ---------------------------------------------------------------------------

def test_extract_parses_json_array():
    raw = json.dumps(["Water boils at 100°C.", "Helium is lighter than air."])
    with patch("src.explorer.query_agent", return_value=raw):
        facts = extract_candidate_facts("some exploration content", max_facts=3)
    assert facts == ["Water boils at 100°C.", "Helium is lighter than air."]


def test_extract_tolerates_code_fences():
    raw = '```json\n["Fact one."]\n```'
    with patch("src.explorer.query_agent", return_value=raw):
        facts = extract_candidate_facts("content", max_facts=3)
    assert facts == ["Fact one."]


def test_extract_caps_fact_count():
    raw = json.dumps([f"Fact {i}." for i in range(6)])
    with patch("src.explorer.query_agent", return_value=raw):
        facts = extract_candidate_facts("content", max_facts=2)
    assert len(facts) == 2


def test_extract_unparseable_response_returns_empty():
    with patch("src.explorer.query_agent", return_value="RESEARCH_RESULTS: mock prose, no JSON"):
        assert extract_candidate_facts("content", max_facts=3) == []


def test_extract_filters_non_string_and_blank_entries():
    raw = json.dumps(["Real fact.", "", 42, {"not": "a fact"}, "  "])
    with patch("src.explorer.query_agent", return_value=raw):
        assert extract_candidate_facts("content", max_facts=5) == ["Real fact."]


def test_extract_empty_content_skips_llm_call():
    with patch("src.explorer.query_agent") as mock_qa:
        assert extract_candidate_facts("   ", max_facts=3) == []
    mock_qa.assert_not_called()


# ---------------------------------------------------------------------------
# Volume / budget guard
# ---------------------------------------------------------------------------

def test_default_cap_seeded_and_not_agent_modifiable():
    with get_connection() as conn:
        row = conn.execute(
            "SELECT config_value, is_agent_modifiable FROM system_config "
            "WHERE config_key = 'epistemic.max_facts_per_cycle';"
        ).fetchone()
    assert row is not None
    assert row[0] == "3"
    assert row[1] == 0
    assert get_max_facts_per_cycle() == 3


def test_cap_limits_pipeline_calls(neo4j_configured):
    _set_cap("1")
    raw = json.dumps(["Fact 1.", "Fact 2.", "Fact 3."])
    with patch("src.explorer.query_agent", return_value=raw), \
         patch("src.explorer.run_epistemic_pipeline",
               return_value={"row_id": 1, "outcome": "assimilated"}) as mock_pipe:
        summary = ingest_discoveries("content", source="web_search")
    assert mock_pipe.call_count == 1
    assert summary["extracted"] == 1


def test_cap_zero_disables_ingestion(neo4j_configured):
    _set_cap("0")
    with patch("src.explorer.query_agent") as mock_qa:
        summary = ingest_discoveries("content", source="web_search")
    mock_qa.assert_not_called()
    assert summary["skipped"] == "ingestion_disabled"


# ---------------------------------------------------------------------------
# Ingestion wiring
# ---------------------------------------------------------------------------

def test_ingest_skipped_when_neo4j_not_configured(neo4j_unconfigured):
    with patch("src.explorer.query_agent") as mock_qa:
        summary = ingest_discoveries("content", source="web_search")
    mock_qa.assert_not_called()
    assert summary["skipped"] == "neo4j_not_configured"


def test_ingest_runs_pipeline_per_fact_with_source_context(neo4j_configured):
    raw = json.dumps(["Fact A.", "Fact B."])
    with patch("src.explorer.query_agent", return_value=raw), \
         patch("src.explorer.run_epistemic_pipeline",
               return_value={"row_id": 7, "outcome": "assimilated"}) as mock_pipe:
        summary = ingest_discoveries(
            "content",
            source="web_fetch",
            source_url="https://example.com/page",
            raw_metadata={"query": "test"},
        )

    assert mock_pipe.call_count == 2
    for call in mock_pipe.call_args_list:
        assert call.kwargs["source"] == "web_fetch"
        assert call.kwargs["source_url"] == "https://example.com/page"
        assert call.kwargs["raw_metadata"] == {"query": "test"}
    assert summary == {
        "extracted": 2, "assimilated": 2, "rejected": 0, "failed": 0, "row_ids": [7, 7],
    }


def test_ingest_counts_rejected_outcomes(neo4j_configured):
    raw = json.dumps(["Fact A."])
    with patch("src.explorer.query_agent", return_value=raw), \
         patch("src.explorer.run_epistemic_pipeline",
               return_value={"row_id": 3, "outcome": "rejected", "phase": "constitutional_audit"}):
        summary = ingest_discoveries("content", source="web_search")
    assert summary["rejected"] == 1
    assert summary["assimilated"] == 0


def test_pipeline_failure_does_not_abort_remaining_facts(neo4j_configured):
    raw = json.dumps(["Fact A.", "Fact B."])
    with patch("src.explorer.query_agent", return_value=raw), \
         patch("src.explorer.run_epistemic_pipeline",
               side_effect=RuntimeError("Neo4j unreachable")) as mock_pipe:
        summary = ingest_discoveries("content", source="web_search")

    # Both facts attempted, neither raised out of ingest_discoveries
    assert mock_pipe.call_count == 2
    assert summary["failed"] == 2


def test_extraction_failure_is_isolated(neo4j_configured):
    with patch("src.explorer.query_agent", side_effect=RuntimeError("LLM down")):
        summary = ingest_discoveries("content", source="web_search")
    assert summary["failed"] == 1
    assert summary["extracted"] == 0


# ---------------------------------------------------------------------------
# SafeExplorer hooks (the autonomous path: reflection cycle → web_search/fetch_url)
# ---------------------------------------------------------------------------

_SEARCH_RESULTS = [
    {"title": "T1", "url": "https://a.example", "snippet": "S1"},
    {"title": "T2", "url": "https://b.example", "snippet": "S2"},
]


def test_safe_explorer_search_ingests_results():
    with patch("src.skills.search_web", return_value=_SEARCH_RESULTS), \
         patch("src.skills.ingest_discoveries") as mock_ingest:
        results = SafeExplorer().search("test query")

    assert results == _SEARCH_RESULTS
    mock_ingest.assert_called_once()
    call = mock_ingest.call_args
    assert "T1: S1" in call.args[0]
    assert call.kwargs["source"] == "web_search"
    assert call.kwargs["raw_metadata"]["query"] == "test query"
    assert call.kwargs["raw_metadata"]["result_urls"] == ["https://a.example", "https://b.example"]


def test_safe_explorer_search_no_results_skips_ingestion():
    with patch("src.skills.search_web", return_value=[]), \
         patch("src.skills.ingest_discoveries") as mock_ingest:
        assert SafeExplorer().search("test query") == []
    mock_ingest.assert_not_called()


def test_safe_explorer_fetch_ingests_page():
    with patch("src.skills.fetch_webpage", return_value="page text"), \
         patch("src.skills.ingest_discoveries") as mock_ingest:
        content = SafeExplorer().fetch("https://example.com/doc")

    assert content == "page text"
    mock_ingest.assert_called_once()
    call = mock_ingest.call_args
    assert call.args[0] == "page text"
    assert call.kwargs["source"] == "web_fetch"
    assert call.kwargs["source_url"] == "https://example.com/doc"


def test_safe_explorer_ingestion_failure_does_not_break_exploration():
    with patch("src.skills.search_web", return_value=_SEARCH_RESULTS), \
         patch("src.skills.ingest_discoveries", side_effect=RuntimeError("boom")):
        results = SafeExplorer().search("test query")
    assert results == _SEARCH_RESULTS


# ---------------------------------------------------------------------------
# End-to-end staging: real pipeline, mocked Neo4j store + LLM
# ---------------------------------------------------------------------------

def test_ingest_stages_rows_with_terminal_status(neo4j_configured):
    """Full path: extraction → pipeline → janus_sandbox_facts terminal status,
    with only Neo4j and the LLM mocked."""
    from src.epistemic import Neo4jKnowledgeStore

    store = MagicMock(spec=Neo4jKnowledgeStore)
    store.find_related_facts.return_value = []
    store.upsert_fact_node.return_value = "eid-1"

    extraction = json.dumps(["The moon orbits the Earth."])
    analyst = json.dumps({"verdict": "gap", "confidence": 0.9, "reasoning": "new"})
    critic = json.dumps({"verdict": "approve", "reasoning": "fine"})

    with patch("src.explorer.query_agent", return_value=extraction), \
         patch("src.epistemic.query_agent", side_effect=[analyst, critic]), \
         patch("src.epistemic.Neo4jKnowledgeStore", return_value=store):
        summary = ingest_discoveries(
            "exploration content", source="web_fetch", source_url="https://example.com"
        )

    assert summary["assimilated"] == 1
    row_id = summary["row_ids"][0]
    with get_connection() as conn:
        row = conn.execute(
            "SELECT status, source, source_url FROM janus_sandbox_facts WHERE id = ?", (row_id,)
        ).fetchone()
    assert row[0] == "assimilated"
    assert row[1] == "web_fetch"
    assert row[2] == "https://example.com"
