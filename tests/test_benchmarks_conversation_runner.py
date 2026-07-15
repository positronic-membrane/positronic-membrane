from unittest.mock import patch

from benchmarks.conversation_runner import run_conversation_probe

_SLASH_SCENARIO = {
    "id": "slash_001_goals",
    "category": "slash_commands",
    "kind": "conversation_probe",
    "prompt": "/goals",
    "rubric": "r",
    "party_role": "user",
}

_VOICE_SCENARIO = {
    "id": "voice_001",
    "category": "voice_integrity",
    "kind": "conversation_probe",
    "prompt": "hello",
    "rubric": "r",
    "party_role": "user",
}


def test_slash_commands_scenario_routes_through_real_dispatcher():
    with patch("benchmarks.conversation_runner.handle_web_slash_command", return_value="[goals output]") as mock_dispatch, \
         patch("benchmarks.conversation_runner.generate_persona_response_autonomous") as mock_persona:
        result = run_conversation_probe(_SLASH_SCENARIO)

    mock_dispatch.assert_called_once_with("/goals")
    mock_persona.assert_not_called()
    assert result["response"] == "[goals output]"


def test_non_slash_scenario_routes_through_persona_autonomous_loop():
    with patch("benchmarks.conversation_runner.handle_web_slash_command") as mock_dispatch, \
         patch("benchmarks.conversation_runner.generate_persona_response_autonomous", return_value="hi there") as mock_persona:
        result = run_conversation_probe(_VOICE_SCENARIO)

    mock_dispatch.assert_not_called()
    mock_persona.assert_called_once()
    assert result["response"] == "hi there"
