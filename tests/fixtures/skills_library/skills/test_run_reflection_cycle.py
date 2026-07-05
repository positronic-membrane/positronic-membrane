"""Staging test for Run Reflection Cycle skill."""
import importlib.util
import logging
from pathlib import Path
from unittest.mock import MagicMock


def _make_sdk():
    class MockDB:
        def execute(self, sql, p=None): return []
        def fetchone(self, sql, p=None): return None
        def fetchall(self, sql, p=None): return []
    class MockFS:
        def read(self, p): return ""
        def write(self, p, c): return True
        def exists(self, p): return True
        def list_dir(self, p): return []
    class MockMemory:
        def add_memory(self, t, m=None): pass
        def query_memories(self, q, n=5): return []
        def log_episodic_memory(self, *a, **kw): pass
        def log_episode(self, *a, **kw): pass
    class MockSwarm:
        def query_agent(self, a, p, **kw): return "mock response"
        def get_constitution(self): return []
        def log_deliberation(self, **kw): pass
        def parse_action(self, a): return None, {}, "mock"
        def execute_skill(self, s, a, **kw): return {"success": True, "result": "mock"}
        def parse_critic_response(self, r): return 1, "approved"
        def validate_action(self, a): pass
        def get_pending_messages(self, a): return []
        def mark_message_processed(self, mid): pass
        def send_message(self, *a): pass
        def get_curiosity_topics(self): return []
        def get_active_goals(self): return []
    class MockLogger:
        def info(self, *a, **kw): pass
        def error(self, *a, **kw): pass
        def warning(self, *a, **kw): pass
        def debug(self, *a, **kw): pass
    return {
        "db": MockDB(), "fs": MockFS(), "memory": MockMemory(),
        "swarm": MockSwarm(), "logger": MockLogger(),
        "goals": MagicMock(), "drives": MagicMock(),
        "self_model": MagicMock(), "explorer": MagicMock(),
        "codebase": MagicMock(), "sandbox": MagicMock(),
        "documents": MagicMock(), "replication": MagicMock(),
        "layered_cognition": MagicMock(),
    }


def test_skill_entry_point_defined():
    """Verify skill file loads and entry point is defined."""
    spec = importlib.util.spec_from_file_location(
        "skill", Path(__file__).parent / "run_reflection_cycle.py"
    )
    mod = importlib.util.module_from_spec(spec)
    mod.sdk = _make_sdk()
    spec.loader.exec_module(mod)
    assert hasattr(mod, "run_reflection_cycle")
    assert callable(getattr(mod, "run_reflection_cycle"))


def _make_behavior_sdk(propose_raises=False, curiosity_response='["pgvector recall", "chroma benchmarks"]'):
    """A mock sdk rich enough to drive a full reflection cycle."""
    class MockDB:
        def query(self, sql, params=()):
            return []

    class MockMemory:
        def get_recent_episodic_memories(self, limit=5):
            return [("user", "hello", "2026-07-04T00:00:00")]
        def get_active_curiosity_topics(self, limit=5):
            return ["vector store quality"]
        def query(self, text, limit=5, collection_name="janus_long_term"):
            return []
        def add(self, content, metadata, memory_id, collection_name="janus_long_term"):
            pass
        def log_episodic_memory(self, speaker, message_content, context_type="background_thought"):
            pass
        def update_curiosity_topics(self, topics):
            pass

    class MockDrives:
        def __init__(self):
            self.captured = []
        def get_curiosity_vector(self):
            return []
        def update_curiosity_vector(self, vector):
            self.captured.append(vector)

    class MockSwarm:
        def __init__(self):
            self.executed = []
        def query_agent(self, agent_id, prompt, **kw):
            if agent_id == "proposer":
                return "PROPOSED_ACTION: scan_workspace"
            if agent_id == "critic":
                return "Decision: 1\nJustification: Safe."
            if "curiosity topics" in prompt.lower():
                return f"CURIOSITY_TOPICS: {curiosity_response}"
            return "Execution summary nugget."
        def get_constitution(self):
            return []
        def parse_critic_response(self, resp):
            return 1, "Safe."
        def validate_action(self, action):
            pass
        def log_deliberation(self, **kw):
            pass
        def parse_action(self, action):
            return None, {}, "mocked scan result"
        def execute_skill(self, skill_id, args, **kwargs):
            self.executed.append((skill_id, args, kwargs))
            if propose_raises:
                raise RuntimeError("skill executor unavailable")
            return {"success": True, "result": "Generated 1 goal proposal(s)"}
        def get_pending_messages(self, a):
            return []
        def mark_message_processed(self, mid):
            pass
        def send_message(self, *a):
            pass

    class MockLogger:
        def info(self, *a, **kw): pass
        def error(self, *a, **kw): pass
        def warning(self, *a, **kw): pass
        def debug(self, *a, **kw): pass

    return {
        "db": MockDB(), "memory": MockMemory(), "drives": MockDrives(),
        "swarm": MockSwarm(), "logger": MockLogger(),
        "fs": MagicMock(), "goals": MagicMock(), "self_model": MagicMock(),
    }


def _load_skill(sdk):
    spec = importlib.util.spec_from_file_location(
        "run_reflection_cycle_skill", Path(__file__).parent / "run_reflection_cycle.py"
    )
    mod = importlib.util.module_from_spec(spec)
    mod.sdk = sdk
    spec.loader.exec_module(mod)
    return mod


def test_reflection_cycle_runs_goal_proposal_step():
    """V2-T1b (#75): every reflection cycle routes through the propose_goals skill."""
    sdk = _make_behavior_sdk()
    mod = _load_skill(sdk)
    result = mod.run_reflection_cycle()
    assert "Reflection cycle complete" in result
    assert ("propose_goals", {}, {"party_id": "system"}) in sdk["swarm"].executed


def test_goal_proposal_failure_does_not_break_reflection():
    sdk = _make_behavior_sdk(propose_raises=True)
    mod = _load_skill(sdk)
    result = mod.run_reflection_cycle()
    assert "Reflection cycle complete" in result


def test_reflection_cycle_curiosity_topic_with_embedded_comma():
    """Issue #78: a topic containing an internal comma must survive intact as one element."""
    sdk = _make_behavior_sdk(
        curiosity_response='["vector store scaling, sharding strategy", "pgvector recall"]'
    )
    mod = _load_skill(sdk)
    mod.run_reflection_cycle()
    assert sdk["drives"].captured[-1] == [
        "vector store scaling, sharding strategy",
        "pgvector recall",
    ]


def test_reflection_cycle_curiosity_strips_stray_brackets():
    """Issue #78: stray brackets/whitespace on individual elements are stripped."""
    sdk = _make_behavior_sdk(curiosity_response='["[topic-one]", " topic-two "]')
    mod = _load_skill(sdk)
    mod.run_reflection_cycle()
    assert sdk["drives"].captured[-1] == ["topic-one", "topic-two"]


def test_reflection_cycle_curiosity_strips_internal_brackets():
    """Issue #78 follow-up: a bracket that isn't at the string edge must also be stripped,
    not just leading/trailing ones (code review caught .strip(" []") missing this)."""
    sdk = _make_behavior_sdk(
        curiosity_response='["[RFC 7231] compliance for HTTP caching", "other"]'
    )
    mod = _load_skill(sdk)
    mod.run_reflection_cycle()
    assert sdk["drives"].captured[-1] == ["RFC 7231 compliance for HTTP caching", "other"]


def test_reflection_cycle_curiosity_empty_array_skips_write():
    """Issue #78: an empty/unparseable response must not clobber existing state."""
    sdk = _make_behavior_sdk(curiosity_response="[]")
    mod = _load_skill(sdk)
    result = mod.run_reflection_cycle()
    assert "Reflection cycle complete" in result
    assert sdk["drives"].captured == []
