import stat

import src.config as config
from src.config import run_config_check, validate_config


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
