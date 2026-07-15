import hashlib

from src.database import get_connection, register_helper_agent
from src.llm import get_agent_settings
from src.prompt_registry import get_prompt, list_prompt_names, list_versions, rollback_prompt, update_prompt


def test_seed_creates_active_version_one_for_core_agents():
    """init_db() (run by the global autouse fixture) migrates the six core agent
    prompts from agent_registry into prompt_templates as active version 1."""
    names = list_prompt_names()
    for agent_id in ("proposer", "critic", "explorer", "archivist", "analyst", "persona"):
        assert agent_id in names

    tpl = get_prompt("persona")
    assert tpl is not None
    assert tpl["version"] == 1
    assert tpl["is_active"] == 1
    assert "Persona surface of Project Janus" in tpl["content"]


def test_get_prompt_returns_none_for_unknown_name():
    assert get_prompt("does_not_exist") is None


def test_update_prompt_creates_new_version_and_deactivates_old():
    old = get_prompt("persona")

    new_version = update_prompt("persona", "New persona behavior.", change_reason="test update", created_by="tester")
    assert new_version == old["version"] + 1

    active = get_prompt("persona")
    assert active["version"] == new_version
    assert active["content"] == "New persona behavior."
    assert active["change_reason"] == "test update"
    assert active["created_by"] == "tester"

    # The old version is preserved, just no longer active.
    old_row = get_prompt("persona", version=old["version"])
    assert old_row is not None
    assert old_row["is_active"] == 0
    assert old_row["content"] == old["content"]


def test_rollback_prompt_reactivates_old_version():
    original = get_prompt("persona")
    update_prompt("persona", "Second version content.", change_reason="v2", created_by="tester")

    ok = rollback_prompt("persona", original["version"], created_by="tester")
    assert ok is True

    active = get_prompt("persona")
    assert active["version"] == original["version"]
    assert active["content"] == original["content"]

    # The superseded (rolled-back-from) version is still queryable, not deleted.
    v2 = get_prompt("persona", version=original["version"] + 1)
    assert v2 is not None
    assert v2["is_active"] == 0
    assert v2["content"] == "Second version content."


def test_rollback_nonexistent_version_returns_false():
    before = get_prompt("persona")
    ok = rollback_prompt("persona", 9999, created_by="tester")
    assert ok is False

    after = get_prompt("persona")
    assert after["version"] == before["version"]
    assert after["content"] == before["content"]


def test_list_versions_orders_newest_first_and_marks_active():
    update_prompt("persona", "v2 content", change_reason="v2", created_by="tester")
    versions = list_versions("persona")

    assert [v["version"] for v in versions] == sorted((v["version"] for v in versions), reverse=True)
    active_versions = [v for v in versions if v["is_active"] == 1]
    assert len(active_versions) == 1
    assert active_versions[0]["version"] == max(v["version"] for v in versions)


def test_get_agent_settings_reflects_prompt_registry_content():
    settings = get_agent_settings("persona")
    assert settings is not None
    _, system_prompt, _ = settings
    active = get_prompt("persona")
    assert system_prompt == active["content"]

    update_prompt("persona", "Overlay check content.", change_reason="overlay test", created_by="tester")
    settings_after = get_agent_settings("persona")
    assert settings_after[1] == "Overlay check content."


def test_get_agent_settings_falls_back_when_no_template_row():
    """A helper agent registered directly into agent_registry with no matching
    prompt_templates row should behave exactly as before this feature existed."""
    conn = get_connection(read_only_constitution=False)
    conn.execute("""
    INSERT INTO agent_registry (agent_id, agent_name, system_prompt, target_model)
    VALUES ('helper_no_template', 'Helper', 'Raw agent_registry prompt.', NULL);
    """)
    conn.commit()
    conn.close()

    assert get_prompt("helper_no_template") is None
    settings = get_agent_settings("helper_no_template")
    assert settings is not None
    assert settings[1] == "Raw agent_registry prompt."


def test_register_helper_agent_stays_in_sync_with_prompt_registry():
    """register_helper_agent() (the SafeSwarm.register_agent SDK write path) must
    keep prompt_templates in sync for any agent_id that already has an active
    template row, or get_agent_settings()'s overlay silently shadows this write."""
    original = get_prompt("persona")

    register_helper_agent("persona", "Persona Interface", "Registered via helper agent path.", None)

    active = get_prompt("persona")
    assert active["version"] == original["version"] + 1
    assert active["content"] == "Registered via helper agent path."
    assert active["created_by"] == "system"

    settings = get_agent_settings("persona")
    assert settings[1] == "Registered via helper agent path."


def test_register_helper_agent_does_not_churn_version_on_identical_content():
    """Re-registering with unchanged content should not create a redundant version."""
    active_before = get_prompt("persona")
    register_helper_agent("persona", "Persona Interface", active_before["content"], None)
    active_after = get_prompt("persona")
    assert active_after["version"] == active_before["version"]


def test_register_helper_agent_creates_template_for_new_agent_id():
    """A brand-new agent_id with no prior prompt_templates row gets one seeded
    as version 1 the first time it's registered via this path."""
    assert get_prompt("brand_new_helper") is None
    register_helper_agent("brand_new_helper", "Brand New Helper", "Helper prompt content.", None)
    tpl = get_prompt("brand_new_helper")
    assert tpl is not None
    assert tpl["version"] == 1
    assert tpl["content"] == "Helper prompt content."


def test_rollback_changes_llm_cache_hash():
    """Maintainer caveat on issue #67: llm_cache keys on sha256(system + prompt),
    so a rollback that changes prompt content must naturally produce a different
    cache key rather than continuing to serve a newer version's cached response."""
    settings_v_current = get_agent_settings("persona")
    original = get_prompt("persona")

    update_prompt("persona", "Distinct content for hash comparison.", change_reason="hash test", created_by="tester")
    settings_v2 = get_agent_settings("persona")

    hash_v1 = hashlib.sha256((settings_v_current[1] + "same user prompt").encode("utf-8")).hexdigest()
    hash_v2 = hashlib.sha256((settings_v2[1] + "same user prompt").encode("utf-8")).hexdigest()
    assert hash_v1 != hash_v2

    rollback_prompt("persona", original["version"], created_by="tester")
    settings_rolled_back = get_agent_settings("persona")
    hash_rolled_back = hashlib.sha256((settings_rolled_back[1] + "same user prompt").encode("utf-8")).hexdigest()

    assert hash_rolled_back == hash_v1
    assert hash_rolled_back != hash_v2
