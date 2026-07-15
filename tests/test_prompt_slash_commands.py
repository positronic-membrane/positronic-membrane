import pytest

from src.persona import handle_web_slash_command


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
async def test_prompt_history_command():
    res = await handle_web_slash_command("/prompt history persona")
    assert "v1" in res
    assert "(active)" in res


@pytest.mark.asyncio
async def test_prompt_update_command_creates_new_version():
    res = await handle_web_slash_command("/prompt update persona | You are now extremely terse.")
    assert "[✔]" in res
    assert "version 2" in res

    show_res = await handle_web_slash_command("/prompt show persona")
    assert "You are now extremely terse." in show_res
    assert "v2" in show_res


@pytest.mark.asyncio
async def test_prompt_update_command_invalid_format():
    res = await handle_web_slash_command("/prompt update persona no pipe here")
    assert "[Error] Usage: /prompt update" in res


@pytest.mark.asyncio
async def test_prompt_rollback_command_happy_path():
    await handle_web_slash_command("/prompt update persona | Temporary new behavior.")
    res = await handle_web_slash_command("/prompt rollback persona 1")
    assert "[✔]" in res
    assert "rolled back to version 1" in res

    show_res = await handle_web_slash_command("/prompt show persona")
    assert "v1" in show_res


@pytest.mark.asyncio
async def test_prompt_rollback_command_not_found():
    res = await handle_web_slash_command("/prompt rollback persona 9999")
    assert "[Error]" in res
    assert "not found" in res


@pytest.mark.asyncio
async def test_prompt_rollback_command_malformed_usage():
    res = await handle_web_slash_command("/prompt rollback persona")
    assert "[Error] Usage: /prompt rollback" in res


@pytest.mark.asyncio
async def test_prompt_unknown_subcommand():
    res = await handle_web_slash_command("/prompt bogus persona")
    assert "[Error] Unknown /prompt subcommand" in res
