import pytest
import src.config
from src.database import init_db, get_constitution, add_constitution_rule

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Isolate DB for testing."""
    temp_db = tmp_path / "test_janus.db"
    orig_db_path = src.config.DB_PATH
    src.config.DB_PATH = str(temp_db)
    init_db()
    yield
    src.config.DB_PATH = orig_db_path

def test_add_constitutional_amendment():
    """Verify that rules can be added or updated in the core constitution via amendments."""
    # Ensure starting with empty/default database
    initial_rules = get_constitution()
    
    # 1. Propose and add a new rule
    add_constitution_rule("test_framework", "Swarm must obey human commands.")
    
    # 2. Verify rule in DB
    rules = get_constitution()
    assert len(rules) > len(initial_rules)
    
    rule_keys = [r[0] for r in rules]
    assert "TEST_FRAMEWORK" in rule_keys
    
    rule_text = [r[1] for r in rules if r[0] == "TEST_FRAMEWORK"][0]
    assert rule_text == "Swarm must obey human commands."

def test_delete_constitutional_rule():
    """Verify that rules can be deleted from the core constitution."""
    add_constitution_rule("delete_me", "This rule should be deleted.")
    
    rules = get_constitution()
    rule_keys = [r[0] for r in rules]
    assert "DELETE_ME" in rule_keys
    
    from src.database import delete_constitution_rule
    delete_constitution_rule("delete_me")
    
    rules_after = get_constitution()
    rule_keys_after = [r[0] for r in rules_after]
    assert "DELETE_ME" not in rule_keys_after
