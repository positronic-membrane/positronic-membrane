"""Tests for src/epistemic.py — all Neo4j and LLM calls are mocked."""
import json
from unittest.mock import MagicMock, patch

import pytest

from src.database import get_connection, init_db
from src.epistemic import (
    Neo4jKnowledgeStore,
    _phase1_ingest,
    _phase2_triangulate,
    _phase3_audit,
    _phase4_assimilate,
    run_epistemic_pipeline,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(related_facts=None, upsert_eid="neo4j-eid-123"):
    store = MagicMock(spec=Neo4jKnowledgeStore)
    store.find_related_facts.return_value = related_facts or []
    store.upsert_fact_node.return_value = upsert_eid
    return store


def _analyst_response(verdict="gap", confidence=0.8, reasoning="no conflict"):
    return json.dumps({"verdict": verdict, "confidence": confidence, "reasoning": reasoning})


def _critic_response(verdict="approve", reasoning="no constitution violation"):
    return json.dumps({"verdict": verdict, "reasoning": reasoning})


# ---------------------------------------------------------------------------
# Phase 1: ingest
# ---------------------------------------------------------------------------

def test_phase1_creates_staging_row():
    row_id = _phase1_ingest("The sky is blue.", "manual", None, {})
    assert isinstance(row_id, int) and row_id > 0

    with get_connection() as conn:
        row = conn.execute(
            "SELECT fact_text, status FROM janus_sandbox_facts WHERE id = ?", (row_id,)
        ).fetchone()
    assert row[0] == "The sky is blue."
    assert row[1] == "pending"


def test_phase1_stores_source_url():
    row_id = _phase1_ingest("Water boils at 100°C.", "web_research", "https://example.com", {})
    with get_connection() as conn:
        row = conn.execute(
            "SELECT source_url FROM janus_sandbox_facts WHERE id = ?", (row_id,)
        ).fetchone()
    assert row[0] == "https://example.com"


# ---------------------------------------------------------------------------
# Phase 2: triangulate
# ---------------------------------------------------------------------------

def test_phase2_gap_verdict():
    row_id = _phase1_ingest("Helium is lighter than air.", "manual", None, {})
    store = _make_store()

    with patch("src.epistemic.query_agent", return_value=_analyst_response("gap", 0.9)):
        verdict, confidence, reasoning = _phase2_triangulate(row_id, "Helium is lighter than air.", store)

    assert verdict == "gap"
    assert confidence == 0.9

    with get_connection() as conn:
        row = conn.execute(
            "SELECT status, analyst_verdict, analyst_confidence FROM janus_sandbox_facts WHERE id = ?",
            (row_id,),
        ).fetchone()
    assert row[0] == "triangulated"
    assert row[1] == "gap"
    assert abs(row[2] - 0.9) < 1e-6


def test_phase2_contradict_verdict():
    row_id = _phase1_ingest("The sky is green.", "manual", None, {})
    store = _make_store(related_facts=[{"f": {"text": "The sky is blue."}}])

    with patch("src.epistemic.query_agent", return_value=_analyst_response("contradict", 0.95)):
        verdict, confidence, _ = _phase2_triangulate(row_id, "The sky is green.", store)

    assert verdict == "contradict"
    assert confidence == 0.95


def test_phase2_malformed_json_falls_back_to_gap():
    row_id = _phase1_ingest("Some fact.", "manual", None, {})
    store = _make_store()

    with patch("src.epistemic.query_agent", return_value="not json at all"):
        verdict, confidence, reasoning = _phase2_triangulate(row_id, "Some fact.", store)

    assert verdict == "gap"
    assert confidence == 0.5
    assert reasoning == "not json at all"


# ---------------------------------------------------------------------------
# Phase 3: audit
# ---------------------------------------------------------------------------

def test_phase3_approve():
    row_id = _phase1_ingest("Gold is a metal.", "manual", None, {})

    with patch("src.epistemic.query_agent", return_value=_critic_response("approve")):
        verdict, reasoning = _phase3_audit(row_id, "Gold is a metal.")

    assert verdict == "approve"
    with get_connection() as conn:
        row = conn.execute(
            "SELECT status, critic_verdict FROM janus_sandbox_facts WHERE id = ?", (row_id,)
        ).fetchone()
    assert row[0] == "audited"
    assert row[1] == "approve"


def test_phase3_reject():
    row_id = _phase1_ingest("Harmful fact.", "manual", None, {})

    with patch(
        "src.epistemic.query_agent",
        return_value=_critic_response("reject", "violates constitution rule X"),
    ):
        verdict, reasoning = _phase3_audit(row_id, "Harmful fact.")

    assert verdict == "reject"
    assert "constitution" in reasoning


def test_phase3_malformed_json_defaults_approve():
    row_id = _phase1_ingest("Safe fact.", "manual", None, {})

    with patch("src.epistemic.query_agent", return_value="LGTM"):
        verdict, _ = _phase3_audit(row_id, "Safe fact.")

    assert verdict == "approve"


# ---------------------------------------------------------------------------
# Phase 4: assimilate
# ---------------------------------------------------------------------------

def test_phase4_writes_to_neo4j_and_updates_row():
    row_id = _phase1_ingest("Iron is magnetic.", "manual", None, {})
    store = _make_store(upsert_eid="eid-abc")

    neo4j_fact_id = _phase4_assimilate(row_id, "Iron is magnetic.", "manual", 0.9, store)

    assert neo4j_fact_id  # non-empty UUID
    store.upsert_fact_node.assert_called_once()
    call_kwargs = store.upsert_fact_node.call_args.kwargs
    assert call_kwargs["fact_text"] == "Iron is magnetic."
    assert 0 < call_kwargs["confidence_alpha"] <= 1.0

    with get_connection() as conn:
        row = conn.execute(
            "SELECT status, neo4j_node_id FROM janus_sandbox_facts WHERE id = ?", (row_id,)
        ).fetchone()
    assert row[0] == "assimilated"
    assert row[1] == neo4j_fact_id


# ---------------------------------------------------------------------------
# Full pipeline — spec's verification test
# ---------------------------------------------------------------------------

def test_pipeline_assimilates_high_confidence_fact():
    """High-confidence, constitution-clean fact reaches Neo4j."""
    store = _make_store()

    with patch("src.epistemic.query_agent") as mock_agent:
        mock_agent.side_effect = [
            _analyst_response("gap", 0.85),
            _critic_response("approve"),
        ]
        result = run_epistemic_pipeline(
            "Copper conducts electricity.", source="web_research", store=store
        )

    assert result["outcome"] == "assimilated"
    assert "neo4j_fact_id" in result
    store.upsert_fact_node.assert_called_once()


def test_pipeline_analyst_blocks_contradiction():
    """Analyst verdict 'contradict' stops the pipeline before the Critic."""
    store = _make_store(related_facts=[{"f": {"text": "The sky is blue."}}])

    with patch("src.epistemic.query_agent") as mock_agent:
        mock_agent.return_value = _analyst_response("contradict", 0.95)
        result = run_epistemic_pipeline("The sky is green.", store=store)

    assert result["outcome"] == "rejected"
    assert result["phase"] == "triangulation"
    # Critic must NOT have been called
    assert mock_agent.call_count == 1

    with get_connection() as conn:
        row = conn.execute(
            "SELECT status FROM janus_sandbox_facts WHERE id = ?", (result["row_id"],)
        ).fetchone()
    assert row[0] == "rejected"


def test_pipeline_critic_blocks_constitutional_violation():
    """Critic rejection stops assimilation even after Analyst approves."""
    store = _make_store()

    with patch("src.epistemic.query_agent") as mock_agent:
        mock_agent.side_effect = [
            _analyst_response("gap", 0.7),
            _critic_response("reject", "violates banned boundary rule"),
        ]
        result = run_epistemic_pipeline("Problematic fact.", store=store)

    assert result["outcome"] == "rejected"
    assert result["phase"] == "constitutional_audit"
    store.upsert_fact_node.assert_not_called()

    with get_connection() as conn:
        row = conn.execute(
            "SELECT status FROM janus_sandbox_facts WHERE id = ?", (result["row_id"],)
        ).fetchone()
    assert row[0] == "rejected"


def test_pipeline_reinforce_verdict_still_assimilates():
    """'reinforce' is not a contradiction — fact should be assimilated."""
    store = _make_store()

    with patch("src.epistemic.query_agent") as mock_agent:
        mock_agent.side_effect = [
            _analyst_response("reinforce", 0.9),
            _critic_response("approve"),
        ]
        result = run_epistemic_pipeline("Water is H2O.", store=store)

    assert result["outcome"] == "assimilated"
    store.upsert_fact_node.assert_called_once()


def test_pipeline_metadata_stored_in_staging():
    store = _make_store()

    with patch("src.epistemic.query_agent") as mock_agent:
        mock_agent.side_effect = [
            _analyst_response("gap", 0.8),
            _critic_response("approve"),
        ]
        result = run_epistemic_pipeline(
            "Carbon has 6 protons.",
            source="web_research",
            source_url="https://example.com/carbon",
            raw_metadata={"confidence": 0.95},
            store=store,
        )

    with get_connection() as conn:
        row = conn.execute(
            "SELECT source, source_url, raw_metadata FROM janus_sandbox_facts WHERE id = ?",
            (result["row_id"],),
        ).fetchone()
    assert row[0] == "web_research"
    assert row[1] == "https://example.com/carbon"
    assert json.loads(row[2]) == {"confidence": 0.95}
