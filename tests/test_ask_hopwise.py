import json
from unittest.mock import MagicMock, Mock

import pytest

import ask_hopwise
import agent
from agent import AgentError


@pytest.fixture(autouse=True)
def isolate_configuration(monkeypatch):
    monkeypatch.setattr(agent, "load_dotenv", Mock())
    monkeypatch.setattr("sys.argv", ["ask_hopwise.py"])
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)


def test_missing_key_stops_before_creating_client(monkeypatch):
    client = Mock()
    monkeypatch.setattr(agent, "OpenAI", client)
    with pytest.raises(SystemExit, match="OPENAI_API_KEY"):
        ask_hopwise.main()
    client.assert_not_called()


def test_missing_model_is_explicit(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-key")
    with pytest.raises(SystemExit, match="OPENAI_MODEL"):
        ask_hopwise.main()


def test_terminal_question_uses_env_configuration_without_printing_it(monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setattr("sys.argv", ["ask_hopwise.py", "What are the next trains from Richmond?"])
    factory = MagicMock()
    client = factory.return_value.__enter__.return_value
    answer = Mock(return_value="Retrieved train information.")
    monkeypatch.setattr(agent, "OpenAI", factory)
    monkeypatch.setattr(agent, "answer_question", answer)
    ask_hopwise.main()
    factory.assert_called_once_with(timeout=30.0, max_retries=0)
    answer.assert_called_once_with("What are the next trains from Richmond?", client=client, model="test-model")
    output = capsys.readouterr()
    assert output.out == "Retrieved train information.\n"
    assert output.err == ""


def test_agent_failure_is_a_clean_terminal_error(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setattr(ask_hopwise, "answer_question_from_env", Mock(side_effect=AgentError("OpenAI request failed")))
    with pytest.raises(SystemExit, match="OpenAI request failed"):
        ask_hopwise.main()


def test_trace_flag_prints_json_separately_from_the_answer(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ask_hopwise.py", "--trace", "Next Richmond trains?"])

    def answer(question, *, trace):
        trace.reset()
        trace.record_tool("search_stations", "call-1", {"query": "Richmond"},
                          {"ok": True, "stations": []})
        trace.status = "completed"
        return "Retrieved train information."

    run = Mock(side_effect=answer)
    monkeypatch.setattr(ask_hopwise, "answer_question_from_env", run)
    ask_hopwise.main()
    output = capsys.readouterr()
    assert output.out == "Retrieved train information.\n"
    captured = json.loads(output.err)
    assert captured["status"] == "completed"
    assert captured["tool_calls"][0]["call_id"] == "call-1"
    assert run.call_args.args == ("Next Richmond trains?",)


def test_environment_entry_point_forwards_trace(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    factory = MagicMock()
    run = Mock(return_value="Answer.")
    monkeypatch.setattr(agent, "OpenAI", factory)
    monkeypatch.setattr(agent, "answer_question", run)
    trace = agent.AgentTrace()
    assert agent.answer_question_from_env("Richmond?", trace=trace) == "Answer."
    run.assert_called_once_with("Richmond?", client=factory.return_value.__enter__.return_value,
                                model="test-model", trace=trace)


def test_environment_entry_point_forwards_optional_conversation(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    factory = MagicMock()
    run = Mock(return_value="Answer.")
    monkeypatch.setattr(agent, "OpenAI", factory)
    monkeypatch.setattr(agent, "answer_question", run)
    conversation = agent.ConversationState("Richmond", 2)
    assert agent.answer_question_from_env("Next two?", conversation=conversation) == "Answer."
    run.assert_called_once_with("Next two?", client=factory.return_value.__enter__.return_value,
                                model="test-model", conversation=conversation)


def test_trace_is_printed_for_missing_configuration_without_creating_client(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ask_hopwise.py", "--trace"])
    factory = Mock()
    monkeypatch.setattr(agent, "OpenAI", factory)
    with pytest.raises(SystemExit, match="OPENAI_API_KEY"):
        ask_hopwise.main()
    captured = json.loads(capsys.readouterr().err)
    assert captured == {"status": "failed", "error_code": "missing_configuration", "tool_calls": []}
    factory.assert_not_called()
