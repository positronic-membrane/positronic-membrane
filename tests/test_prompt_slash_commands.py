import pytest

from src.database import get_connection
from src.persona import handle_web_slash_command


def _seed_party(role: str, party_id: str = "test_party"):
    """Insert a single party with the given role (and nothing higher), so
    get_session_party_id()'s admin->contributor->user priority scan resolves
    to this exact party rather than falling back to the 'system' bypass."""
    conn = get_connection(read_only_constitution=False)
    conn.execute(
        "INSERT INTO parties (id, name, role, public_key) VALUES (?, ?, ?, 'key');",
        (party_id, "Test Party", role),
    )
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_prompt_list_command():
    res = await handle_web_slash_command("/prompt list")
    assert "persona" in res
    assert "proposer" in res


@pytest.mark.asyncio
async def test_prompt_show_command():
    res = await handle_web_slash_command("/prompt show persona")
    assert "v1" in res
    assert "Persona surface of Project Janus" in res


@pytest.mark.asyncio
async def test_prompt_show_unknown_name():
    res = await handle_web_slash_command("/prompt show does_not_exist")
    assert "[Error]" in res


@pytest.mark.asyncio
async def test_prompt_show_is_case_insensitive():
    res = await handle_web_slash_command("/prompt show PERSONA")
    assert "v1" in res
    assert "Persona surface of Project Janus" in res


@pytest.mark.asyncio
async def test_prompt_history_command():
    res = await handle_web_slash_command("/prompt history persona")
    assert "v1" in res
    assert "(active)" in res


@pytest.mark.asyncio
async def test_prompt_update_blocked_for_user_role():
    _seed_party("user")
    res = await handle_web_slash_command("/prompt update persona | Should not be allowed.")
    assert "[Error]" in res
    assert "'contributor' role" in res

    show_res = await handle_web_slash_command("/prompt show persona")
    assert "Should not be allowed." not in show_res


@pytest.mark.asyncio
async def test_prompt_rollback_blocked_for_user_role():
    _seed_party("user")
    res = await handle_web_slash_command("/prompt rollback persona 1")
    assert "[Error]" in res
    assert "'contributor' role" in res


@pytest.mark.asyncio
async def test_prompt_update_command_creates_new_version():
    _seed_party("contributor")
    res = await handle_web_slash_command("/prompt update persona | You are now extremely terse.")
    assert "[✔]" in res
    assert "version 2" in res

    show_res = await handle_web_slash_command("/prompt show persona")
    assert "You are now extremely terse." in show_res
    assert "v2" in show_res


@pytest.mark.asyncio
async def test_prompt_update_command_is_case_insensitive_on_name():
    _seed_party("contributor")
    res = await handle_web_slash_command("/prompt update PERSONA | Case-insensitive update.")
    assert "[✔]" in res

    show_res = await handle_web_slash_command("/prompt show persona")
    assert "Case-insensitive update." in show_res


@pytest.mark.asyncio
async def test_prompt_update_command_invalid_format():
    _seed_party("contributor")
    res = await handle_web_slash_command("/prompt update persona no pipe here")
    assert "[Error] Usage: /prompt update" in res


@pytest.mark.asyncio
async def test_prompt_rollback_command_happy_path():
    _seed_party("contributor")
    await handle_web_slash_command("/prompt update persona | Temporary new behavior.")
    res = await handle_web_slash_command("/prompt rollback persona 1")
    assert "[✔]" in res
    assert "rolled back to version 1" in res

    show_res = await handle_web_slash_command("/prompt show persona")
    assert "v1" in show_res


@pytest.mark.asyncio
async def test_prompt_rollback_command_not_found():
    _seed_party("contributor")
    res = await handle_web_slash_command("/prompt rollback persona 9999")
    assert "[Error]" in res
    assert "not found" in res


@pytest.mark.asyncio
async def test_prompt_rollback_command_malformed_usage():
    _seed_party("contributor")
    res = await handle_web_slash_command("/prompt rollback persona")
    assert "[Error] Usage: /prompt rollback" in res


@pytest.mark.asyncio
async def test_prompt_unknown_subcommand():
    res = await handle_web_slash_command("/prompt bogus persona")
    assert "[Error] Unknown /prompt subcommand" in res
