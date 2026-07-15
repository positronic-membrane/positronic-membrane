import stat

import src.config as config
from src.config import (
    run_agent_routing_check,
    run_config_check,
    validate_agent_routing_policy,
    validate_config,
)


def test_clean_config_has_no_errors_or_warnings():
    result = validate_config()
    assert result.ok
    assert result.errors == []


def test_postgres_missing_database_url_is_error(monkeypatch):
    monkeypatch.setattr(config, "DB_TYPE", "postgres")
    monkeypatch.setattr(config, "DATABASE_URL", "")
    result = validate_config()
    assert not result.ok
    assert any("DATABASE_URL" in e for e in result.errors)


def test_postgres_uppercase_db_type_missing_database_url_is_error(monkeypatch):
    monkeypatch.setattr(config, "DB_TYPE", "Postgres")
    monkeypatch.setattr(config, "DATABASE_URL", "")
    result = validate_config()
    assert not result.ok
    assert any("DATABASE_URL" in e for e in result.errors)


def test_postgres_with_database_url_is_not_an_error(monkeypatch):
    monkeypatch.setattr(config, "DB_TYPE", "postgres")
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://user:pass@host:5432/db")
    result = validate_config()
    assert not any("DATABASE_URL" in e for e in result.errors)


def test_sqlite_path_unwritable_parent_is_error(monkeypatch, tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(stat.S_IREAD | stat.S_IEXEC)
    try:
        monkeypatch.setattr(config, "DB_TYPE", "sqlite")
        monkeypatch.setattr(config, "DB_PATH", str(locked / "sub" / "janus.db"))
        result = validate_config()
        assert not result.ok
        assert any("DB_PATH" in e for e in result.errors)
    finally:
        locked.chmod(stat.S_IRWXU)


def test_require_auth_with_env_keys_present_no_error(monkeypatch):
    monkeypatch.setenv("JWT_PRIVATE_KEY", "dummy-private")
    monkeypatch.setenv("JWT_PUBLIC_KEY", "dummy-public")
    monkeypatch.setattr(config, "REQUIRE_AUTH", True)
    result = validate_config()
    assert not any("JWT" in e for e in result.errors)


def test_require_auth_no_keys_and_keys_dir_unwritable_is_error(monkeypatch, tmp_path):
    monkeypatch.delenv("JWT_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("JWT_PUBLIC_KEY", raising=False)

    import src.auth as auth

    locked_parent = tmp_path / "locked_home"
    locked_parent.mkdir()
    fake_keys_dir = locked_parent / ".keys"
    locked_parent.chmod(stat.S_IREAD | stat.S_IEXEC)
    try:
        monkeypatch.setattr(auth, "KEYS_DIR", fake_keys_dir)
        monkeypatch.setattr(auth, "PRIVATE_KEY_PATH", fake_keys_dir / "jwt_private.pem")
        monkeypatch.setattr(auth, "PUBLIC_KEY_PATH", fake_keys_dir / "jwt_public.pem")
        monkeypatch.setattr(config, "REQUIRE_AUTH", True)

        result = validate_config()
        assert not result.ok
        assert any("JWT" in e for e in result.errors)
    finally:
        locked_parent.chmod(stat.S_IRWXU)


def test_require_auth_false_skips_jwt_check(monkeypatch, tmp_path):
    monkeypatch.delenv("JWT_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("JWT_PUBLIC_KEY", raising=False)
    monkeypatch.setattr(config, "REQUIRE_AUTH", False)
    result = validate_config()
    assert not any("JWT" in e for e in result.errors)


def test_github_enabled_no_tokens_is_warning(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_ENABLED", True)
    monkeypatch.setattr(config, "GITHUB_ACCESS_TOKEN", "")
    monkeypatch.setattr(config, "GITHUB_PM_TOKEN", "")
    result = validate_config()
    assert result.ok
    assert any("GITHUB" in w for w in result.warnings)


def test_github_enabled_with_token_no_warning(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_ENABLED", True)
    monkeypatch.setattr(config, "GITHUB_ACCESS_TOKEN", "ghp_dummy")
    monkeypatch.setattr(config, "GITHUB_PM_TOKEN", "")
    result = validate_config()
    assert not any("GITHUB" in w for w in result.warnings)


def test_external_agents_enabled_without_encryption_key_is_error(monkeypatch):
    monkeypatch.setattr(config, "EXTERNAL_AGENTS_ENABLED", True)
    monkeypatch.setattr(config, "JANUS_ENCRYPTION_KEY", "")
    result = validate_config()
    assert not result.ok
    assert any("JANUS_ENCRYPTION_KEY" in e for e in result.errors)


def test_external_agents_enabled_with_encryption_key_no_error(monkeypatch):
    monkeypatch.setattr(config, "EXTERNAL_AGENTS_ENABLED", True)
    monkeypatch.setattr(config, "JANUS_ENCRYPTION_KEY", "some-secret-value")
    result = validate_config()
    assert not any("JANUS_ENCRYPTION_KEY" in e for e in result.errors)


def test_external_agents_disabled_skips_encryption_key_check(monkeypatch):
    monkeypatch.setattr(config, "EXTERNAL_AGENTS_ENABLED", False)
    monkeypatch.setattr(config, "JANUS_ENCRYPTION_KEY", "")
    result = validate_config()
    assert result.ok


def test_openrouter_model_without_key_is_warning(monkeypatch):
    monkeypatch.setattr(config, "LLM_MODEL", "openai/gpt-4o")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    result = validate_config()
    assert result.ok
    assert any("OPENROUTER" in w for w in result.warnings)


def test_local_model_without_openrouter_key_no_warning(monkeypatch):
    monkeypatch.setattr(config, "LLM_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    result = validate_config()
    assert not any("OPENROUTER" in w for w in result.warnings)


def test_unknown_sandbox_provider_is_warning(monkeypatch):
    monkeypatch.setattr(config, "SANDBOX_PROVIDER", "kubernetes")
    result = validate_config()
    assert any("SANDBOX_PROVIDER" in w for w in result.warnings)


def test_e2b_provider_is_boot_blocking_error(monkeypatch):
    """E2B_API_KEY presence must not matter — the executor itself is unimplemented."""
    monkeypatch.setattr(config, "SANDBOX_PROVIDER", "e2b")
    monkeypatch.setattr(config, "E2B_API_KEY", "dummy-key")
    result = validate_config()
    assert not result.ok
    assert any("SANDBOX_PROVIDER" in e for e in result.errors)


def test_e2b_provider_uppercase_is_still_a_boot_blocking_error(monkeypatch):
    """Case must not let SANDBOX_PROVIDER=E2B slip past into the warning branch."""
    monkeypatch.setattr(config, "SANDBOX_PROVIDER", "E2B")
    result = validate_config()
    assert not result.ok
    assert any("SANDBOX_PROVIDER" in e for e in result.errors)


def test_local_provider_disallowed_is_warning(monkeypatch):
    monkeypatch.setattr(config, "SANDBOX_PROVIDER", "local")
    monkeypatch.setattr(config, "ALLOW_LOCAL_SANDBOX_EXEC", False)
    result = validate_config()
    assert any("ALLOW_LOCAL_SANDBOX_EXEC" in w for w in result.warnings)


def test_unknown_spawn_provider_is_warning(monkeypatch):
    monkeypatch.setattr(config, "SPAWN_PROVIDER", "kubernetes")
    result = validate_config()
    assert any("SPAWN_PROVIDER" in w for w in result.warnings)


def test_neo4j_uri_without_credentials_is_warning(monkeypatch):
    monkeypatch.setattr(config, "NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setattr(config, "NEO4J_USERNAME", "")
    monkeypatch.setattr(config, "NEO4J_PASSWORD", "")
    result = validate_config()
    assert any("NEO4J" in w for w in result.warnings)


def test_run_config_check_returns_0_when_clean():
    assert run_config_check() == 0


def test_run_config_check_returns_1_and_logs_on_critical(monkeypatch, caplog):
    monkeypatch.setattr(config, "DB_TYPE", "postgres")
    monkeypatch.setattr(config, "DATABASE_URL", "")
    with caplog.at_level("CRITICAL"):
        assert run_config_check() == 1
    assert "DATABASE_URL" in caplog.text


def test_run_config_check_warns_but_returns_0(monkeypatch, caplog):
    monkeypatch.setattr(config, "GITHUB_ENABLED", True)
    monkeypatch.setattr(config, "GITHUB_ACCESS_TOKEN", "")
    monkeypatch.setattr(config, "GITHUB_PM_TOKEN", "")
    with caplog.at_level("WARNING"):
        assert run_config_check() == 0
    assert "GITHUB" in caplog.text


def test_unknown_log_level_is_warning(monkeypatch):
    monkeypatch.setattr(config, "LOG_LEVEL", "VERBOSE")
    result = validate_config()
    assert any("LOG_LEVEL" in w for w in result.warnings)


def test_unknown_log_format_is_warning(monkeypatch):
    monkeypatch.setattr(config, "LOG_FORMAT", "xml")
    result = validate_config()
    assert any("LOG_FORMAT" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Per-agent off-box LLM routing policy (issue #108)
# ---------------------------------------------------------------------------

def test_validate_agent_routing_policy_clean_db_has_no_findings(monkeypatch):
    """Freshly-seeded agent_registry rows have target_model=None, which
    resolves to the local LLM_BASE_URL fallback — no violation. Explicitly
    neutralizes OPENROUTER_API_KEY/LLM_MODEL so this doesn't depend on the
    developer's real .env (which may set both to off-box-routing values)."""
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(config, "LLM_MODEL", "qwen2.5-coder:7b")
    result = validate_agent_routing_policy()
    assert result.ok
    assert result.warnings == []


def _seed_offbox_violation(monkeypatch):
    from src.database import get_connection

    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-or-v1-testkey")
    conn = get_connection()
    conn.cursor().execute(
        "UPDATE agent_registry SET target_model = 'google/gemini-2.5-flash' WHERE agent_id = 'proposer';"
    )
    conn.commit()
    conn.close()


def test_validate_agent_routing_policy_warns_by_default(monkeypatch):
    monkeypatch.setattr(config, "STRICT_OFFBOX_VALIDATION", False)
    _seed_offbox_violation(monkeypatch)

    result = validate_agent_routing_policy()
    assert result.ok  # warning, not an error
    assert any("proposer" in w and "OffboxRoutingViolationError" in w for w in result.warnings)


def test_validate_agent_routing_policy_errors_when_strict(monkeypatch):
    monkeypatch.setattr(config, "STRICT_OFFBOX_VALIDATION", True)
    _seed_offbox_violation(monkeypatch)

    result = validate_agent_routing_policy()
    assert not result.ok
    assert any("proposer" in e for e in result.errors)


def test_run_agent_routing_check_returns_1_when_strict(monkeypatch, caplog):
    monkeypatch.setattr(config, "STRICT_OFFBOX_VALIDATION", True)
    _seed_offbox_violation(monkeypatch)

    with caplog.at_level("CRITICAL"):
        assert run_agent_routing_check() == 1
    assert "agent routing policy" in caplog.text.lower()


def test_run_agent_routing_check_returns_0_by_default(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(config, "LLM_MODEL", "qwen2.5-coder:7b")
    assert run_agent_routing_check() == 0


def test_validate_agent_routing_policy_degrades_to_warning_on_missing_table(monkeypatch, tmp_path):
    """If agent_registry doesn't exist yet (e.g. the standalone janus-server
    entrypoint runs before init_db()), this must warn, not crash."""
    empty_db = tmp_path / "no_schema.db"
    empty_db.touch()
    monkeypatch.setattr(config, "DB_PATH", str(empty_db))

    result = validate_agent_routing_policy()
    assert result.ok
    assert len(result.warnings) == 1
    assert "agent_registry" in result.warnings[0]


def test_validate_agent_routing_policy_genuine_error_respects_strict_mode(monkeypatch):
    """A non-'missing table' failure (a real bug, not the expected pre-init_db()
    condition) must still escalate to an error under STRICT_OFFBOX_VALIDATION,
    instead of being unconditionally folded into a warning like a missing table."""
    import src.llm

    def _boom():
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr(src.llm, "get_agent_routing_audit", _boom)

    monkeypatch.setattr(config, "STRICT_OFFBOX_VALIDATION", False)
    result = validate_agent_routing_policy()
    assert result.ok
    assert any("connection reset by peer" in w for w in result.warnings)

    monkeypatch.setattr(config, "STRICT_OFFBOX_VALIDATION", True)
    result = validate_agent_routing_policy()
    assert not result.ok
    assert any("connection reset by peer" in e for e in result.errors)
