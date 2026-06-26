"""Epistemic Ingestion Pipeline — V2-T5.

Four-phase pipeline that moves a raw candidate fact from isolation through
triangulation, constitutional audit, and final assimilation into the Neo4j
knowledge graph.

Phase 1 — Ingest & Isolate  : write raw fact to janus_sandbox_facts (SQLite)
Phase 2 — Triangulate       : Analyst agent compares against Neo4j graph
Phase 3 — Constitutional Audit : Critic agent checks against core_constitution
Phase 4 — Assimilate        : write node + relationships to Neo4j with α weight
"""

import json
import logging
import uuid
from contextlib import contextmanager
from typing import Any

from neo4j import GraphDatabase

import src.config as config
from src.database import get_connection
from src.llm import query_agent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Neo4j connection
# ---------------------------------------------------------------------------

_driver = None


def _get_driver():
    global _driver
    if _driver is None:
        if not config.NEO4J_URI:
            raise RuntimeError(
                "NEO4J_URI is not configured. Set it in .env or environment."
            )
        _driver = GraphDatabase.driver(
            config.NEO4J_URI,
            auth=(config.NEO4J_USERNAME, config.NEO4J_PASSWORD),
        )
    return _driver


@contextmanager
def _neo4j_session():
    driver = _get_driver()
    session = driver.session(database=config.NEO4J_DATABASE)
    try:
        yield session
    finally:
        session.close()


def close_driver():
    """Close the shared Neo4j driver (call on shutdown)."""
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None


# ---------------------------------------------------------------------------
# Neo4jKnowledgeStore
# ---------------------------------------------------------------------------

class Neo4jKnowledgeStore:
    """Thin wrapper around Neo4j for parameterised Cypher operations.

    All writes use parameterised queries — no string interpolation of
    user/agent-supplied data into Cypher.
    """

    def run(self, cypher: str, **params) -> list[dict]:
        with _neo4j_session() as session:
            result = session.run(cypher, **params)
            return [dict(record) for record in result]

    def upsert_fact_node(
        self,
        fact_id: str,
        fact_text: str,
        source: str,
        confidence_alpha: float,
        extra_props: dict | None = None,
    ) -> str:
        """Merge a Fact node by fact_id; return its neo4j element id."""
        props = {
            "fact_id": fact_id,
            "text": fact_text,
            "source": source,
            "confidence_alpha": confidence_alpha,
        }
        if extra_props:
            props.update(extra_props)

        records = self.run(
            """
            MERGE (f:Fact {fact_id: $fact_id})
            SET f += $props
            RETURN elementId(f) AS eid
            """,
            fact_id=fact_id,
            props=props,
        )
        return records[0]["eid"] if records else ""

    def add_relationship(
        self,
        from_fact_id: str,
        rel_type: str,
        to_fact_id: str,
        props: dict | None = None,
    ) -> None:
        self.run(
            """
            MATCH (a:Fact {fact_id: $from_id}), (b:Fact {fact_id: $to_id})
            MERGE (a)-[r:RELATES {rel_type: $rel_type}]->(b)
            SET r += $props
            """,
            from_id=from_fact_id,
            to_id=to_fact_id,
            rel_type=rel_type,
            props=props or {},
        )

    def find_related_facts(self, fact_text: str, limit: int = 5) -> list[dict]:
        """Return up to `limit` existing Fact nodes for triangulation context."""
        words = [w.strip() for w in fact_text.lower().split() if len(w) > 4][:8]
        if not words:
            return []
        # Simple keyword match via CONTAINS — sufficient without GDS/APOC
        conditions = " OR ".join(f"toLower(f.text) CONTAINS $w{i}" for i, _ in enumerate(words))
        params = {f"w{i}": w for i, w in enumerate(words)}
        params["limit"] = limit
        return self.run(
            f"MATCH (f:Fact) WHERE {conditions} RETURN f LIMIT $limit",
            **params,
        )

    def export_graph(self) -> dict:
        """Export all nodes and relationships as plain dicts (for migration)."""
        nodes = self.run("MATCH (n) RETURN n, labels(n) AS labels, elementId(n) AS eid")
        rels = self.run(
            "MATCH (a)-[r]->(b) "
            "RETURN elementId(a) AS from_eid, type(r) AS rel_type, "
            "properties(r) AS props, elementId(b) AS to_eid"
        )
        return {"nodes": nodes, "relationships": rels}


# ---------------------------------------------------------------------------
# SQLite helpers for janus_sandbox_facts
# ---------------------------------------------------------------------------

def _create_staging_fact(
    fact_text: str,
    source: str,
    source_url: str | None,
    raw_metadata: dict,
) -> int:
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO janus_sandbox_facts
                (fact_text, source, source_url, raw_metadata, status)
            VALUES (?, ?, ?, ?, 'pending')
            """,
            (fact_text, source, source_url, json.dumps(raw_metadata)),
        )
        conn.commit()
        return cursor.lastrowid


def _update_staging_fact(fact_row_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = "datetime('now')"
    set_clause = ", ".join(
        f"{k} = datetime('now')" if v == "datetime('now')" else f"{k} = ?"
        for k, v in fields.items()
    )
    values = [v for v in fields.values() if v != "datetime('now')"]
    with get_connection() as conn:
        conn.execute(
            f"UPDATE janus_sandbox_facts SET {set_clause} WHERE id = ?",
            (*values, fact_row_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Pipeline phases
# ---------------------------------------------------------------------------

def _phase1_ingest(
    fact_text: str,
    source: str,
    source_url: str | None,
    raw_metadata: dict,
) -> int:
    """Write raw fact to janus_sandbox_facts. Returns row id."""
    row_id = _create_staging_fact(fact_text, source, source_url, raw_metadata)
    logger.info("Epistemic P1: staged fact id=%d source=%s", row_id, source)
    return row_id


def _phase2_triangulate(
    row_id: int,
    fact_text: str,
    store: Neo4jKnowledgeStore,
) -> tuple[str, float, str]:
    """Analyst agent compares fact against existing graph nodes.

    Returns (verdict, confidence, reasoning).
    verdict is one of: 'reinforce', 'contradict', 'gap'.
    """
    related = store.find_related_facts(fact_text)
    graph_context = (
        "\n".join(r["f"]["text"] for r in related if "f" in r)
        if related
        else "(no related facts in graph yet)"
    )

    prompt = (
        f"Candidate fact:\n{fact_text}\n\n"
        f"Existing knowledge graph context:\n{graph_context}\n\n"
        "Classify this fact as 'reinforce', 'contradict', or 'gap' relative to "
        "the graph context. Respond with JSON only: "
        '{"verdict": "...", "confidence": 0.0-1.0, "reasoning": "..."}'
    )
    raw = query_agent("analyst", prompt)

    try:
        parsed: dict[str, Any] = json.loads(raw)
        verdict = str(parsed.get("verdict", "gap")).lower()
        if verdict not in ("reinforce", "contradict", "gap"):
            verdict = "gap"
        confidence = float(parsed.get("confidence", 0.5))
        reasoning = str(parsed.get("reasoning", ""))
    except (json.JSONDecodeError, ValueError):
        verdict, confidence, reasoning = "gap", 0.5, raw

    _update_staging_fact(
        row_id,
        status="triangulated",
        analyst_verdict=verdict,
        analyst_confidence=confidence,
        analyst_reasoning=reasoning,
    )
    logger.info("Epistemic P2: fact id=%d verdict=%s confidence=%.2f", row_id, verdict, confidence)
    return verdict, confidence, reasoning


def _phase3_audit(row_id: int, fact_text: str) -> tuple[str, str]:
    """Critic agent audits the fact against core_constitution.

    Returns (verdict, reasoning). verdict is 'approve' or 'reject'.
    """
    prompt = (
        f"Fact under audit:\n{fact_text}\n\n"
        "Does this fact violate any rule in the core constitution? "
        "Respond with JSON only: "
        '{"verdict": "approve"|"reject", "reasoning": "..."}'
    )
    raw = query_agent("critic", prompt)

    try:
        parsed = json.loads(raw)
        verdict = str(parsed.get("verdict", "approve")).lower()
        if verdict not in ("approve", "reject"):
            verdict = "approve"
        reasoning = str(parsed.get("reasoning", ""))
    except (json.JSONDecodeError, ValueError):
        verdict, reasoning = "approve", raw

    _update_staging_fact(
        row_id,
        status="audited",
        critic_verdict=verdict,
        critic_reasoning=reasoning,
    )
    logger.info("Epistemic P3: fact id=%d critic=%s", row_id, verdict)
    return verdict, reasoning


def _phase4_assimilate(
    row_id: int,
    fact_text: str,
    source: str,
    analyst_confidence: float,
    store: Neo4jKnowledgeStore,
) -> str:
    """Write fact node to Neo4j. Returns the neo4j element id."""
    fact_id = str(uuid.uuid4())
    # Confidence weight α: blend analyst confidence with a fixed prior of 0.5
    alpha = round(0.5 * analyst_confidence + 0.5 * 0.5, 4)

    neo4j_eid = store.upsert_fact_node(
        fact_id=fact_id,
        fact_text=fact_text,
        source=source,
        confidence_alpha=alpha,
    )
    _update_staging_fact(
        row_id,
        status="assimilated",
        neo4j_node_id=fact_id,
        confidence_alpha=alpha,
    )
    logger.info("Epistemic P4: fact id=%d assimilated neo4j_eid=%s α=%.4f", row_id, neo4j_eid, alpha)
    return fact_id


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_epistemic_pipeline(
    fact_text: str,
    source: str = "manual",
    source_url: str | None = None,
    raw_metadata: dict | None = None,
    store: Neo4jKnowledgeStore | None = None,
) -> dict:
    """Run the full 4-phase epistemic ingestion pipeline.

    Returns a dict summarising the outcome of each phase.
    Pass `store` explicitly in tests to inject a mock.
    """
    if store is None:
        store = Neo4jKnowledgeStore()
    metadata = raw_metadata or {}

    # Phase 1
    row_id = _phase1_ingest(fact_text, source, source_url, metadata)

    # Phase 2
    analyst_verdict, analyst_confidence, analyst_reasoning = _phase2_triangulate(
        row_id, fact_text, store
    )

    if analyst_verdict == "contradict":
        _update_staging_fact(row_id, status="rejected")
        return {
            "row_id": row_id,
            "outcome": "rejected",
            "phase": "triangulation",
            "reason": f"Analyst flagged contradiction (confidence={analyst_confidence:.2f}): {analyst_reasoning}",
        }

    # Phase 3
    critic_verdict, critic_reasoning = _phase3_audit(row_id, fact_text)

    if critic_verdict == "reject":
        _update_staging_fact(row_id, status="rejected")
        return {
            "row_id": row_id,
            "outcome": "rejected",
            "phase": "constitutional_audit",
            "reason": f"Critic blocked assimilation: {critic_reasoning}",
        }

    # Phase 4
    neo4j_fact_id = _phase4_assimilate(
        row_id, fact_text, source, analyst_confidence, store
    )

    return {
        "row_id": row_id,
        "outcome": "assimilated",
        "neo4j_fact_id": neo4j_fact_id,
        "analyst_verdict": analyst_verdict,
        "analyst_confidence": analyst_confidence,
        "critic_verdict": critic_verdict,
    }
