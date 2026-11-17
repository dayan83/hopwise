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
    assert capsys.readouterr().out == "Retrieved train information.\n"


def test_agent_failure_is_a_clean_terminal_error(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setattr(ask_hopwise, "answer_question_from_env", Mock(side_effect=AgentError("OpenAI request failed")))
    with pytest.raises(SystemExit, match="OpenAI request failed"):
        ask_hopwise.main()
