from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, call

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import agent
import departure_service
import ptv
from models import Departure, DepartureBoard, DepartureWithDisruptions


APP_PATH = Path(__file__).resolve().parents[1] / "streamlit_app.py"
QUESTION = "What are the next trains from Richmond, and are there any disruptions?"
ANSWER = "Retrieved train information and associated notices."


@pytest.fixture
def assistant(monkeypatch):
    answer = Mock(return_value=ANSWER)
    monkeypatch.setattr(agent, "answer_question_from_env", answer)
    return answer


@pytest.fixture(autouse=True)
def services(monkeypatch):
    search = Mock(return_value=[{
        "stop_id": 1, "stop_name": "Richmond", "stop_suburb": "Richmond", "route_type": 0,
    }])
    departures = Mock(return_value=DepartureBoard(stop_id=1, departures=[
        DepartureWithDisruptions(departure=Departure(stop_id=1, route_id=7)),
    ]))
    monkeypatch.setattr(ptv, "search_stations", search)
    monkeypatch.setattr(departure_service, "get_departures_with_disruptions", departures)
    return search, departures


def app():
    return AppTest.from_file(str(APP_PATH), default_timeout=10).run()


def ask(application, question=QUESTION):
    return application.chat_input[0].set_value(question).run()


def test_initial_chat_does_not_call_agent_or_services(assistant, services):
    application = app()
    assert not application.exception
    assert "Hopwise AI Assistant" in [heading.value for heading in application.subheader]
    assert len(application.chat_input) == 1
    assert application.session_state["chat_messages"] == []
    assert not application.chat_message
    assert application.text_input[0].label == "Melbourne train station"
    assistant.assert_not_called()
    for service in services:
        service.assert_not_called()


def test_question_and_final_answer_persist_without_another_agent_call(assistant):
    application = ask(app(), "  " + QUESTION + "  ")
    expected = [{"role": "user", "content": QUESTION},
                {"role": "assistant", "content": ANSWER}]
    assert not application.exception
    assert application.session_state["chat_messages"] == expected
    assert [message.name for message in application.chat_message] == ["user", "assistant"]
    assert [message.markdown[0].value for message in application.chat_message] == [QUESTION, ANSWER]
    application.run()
    assert not application.exception
    assert application.session_state["chat_messages"] == expected
    assert len(application.chat_message) == 2
    assistant.assert_called_once_with(QUESTION)


def test_multiple_questions_keep_display_history_but_are_independent_runs(assistant):
    assistant.side_effect = ["Richmond answer.", "Glen Waverley answer."]
    application = ask(app())
    second_question = "What are the next trains from Glen Waverley?"
    ask(application, second_question)
    assert not application.exception
    assert assistant.call_args_list == [call(QUESTION), call(second_question)]
    assert [message["content"] for message in application.session_state["chat_messages"]] == [
        QUESTION, "Richmond answer.", second_question, "Glen Waverley answer.",
    ]
    assert len(application.chat_message) == 4


@pytest.mark.parametrize("error", [
    agent.AgentError("private-test-secret"),
    ptv.PTVRequestError("private-test-secret"),
    ValueError("private-test-secret"),
])
def test_failures_show_safe_messages_and_do_not_retry_on_rerun(assistant, error):
    assistant.side_effect = error
    application = ask(app())
    assert not application.exception
    friendly_message = application.error[0].value
    assert "couldn't retrieve train information" in friendly_message
    messages = application.session_state["chat_messages"]
    assert messages[-1] == {"role": "assistant", "content": friendly_message, "error": True}
    assert "private-test-secret" not in repr(messages)
    application.run()
    assert not application.exception
    assert application.error[0].value == friendly_message
    assistant.assert_called_once_with(QUESTION)


def test_new_question_can_succeed_after_failure(assistant):
    assistant.side_effect = [agent.AgentError("Unavailable"), ANSWER]
    application = ask(app())
    ask(application)
    assert not application.exception
    assert len(application.chat_message) == 4
    assert application.session_state["chat_messages"][-1] == {
        "role": "assistant", "content": ANSWER,
    }
    assert assistant.call_count == 2


def test_blank_question_does_not_call_agent_or_add_messages(assistant):
    application = ask(app(), "   ")
    assert not application.exception
    assert application.session_state["chat_messages"] == []
    assistant.assert_not_called()


def test_progress_indicator_wraps_agent_execution(monkeypatch, assistant):
    real_spinner = st.spinner
    active_spinners = []

    @contextmanager
    def track_spinner(text, *args, **kwargs):
        active_spinners.append(text)
        try:
            with real_spinner(text, *args, **kwargs):
                yield
        finally:
            active_spinners.pop()

    def answer(question):
        assert active_spinners == ["Checking train information and disruption notices..."]
        return ANSWER

    monkeypatch.setattr(st, "spinner", track_spinner)
    assistant.side_effect = answer
    application = ask(app())
    assert not application.exception
    assert application.session_state["chat_messages"][-1]["content"] == ANSWER
    assert active_spinners == []


def test_chat_and_station_departure_controls_preserve_each_others_state(assistant, services):
    search, departures = services
    application = app()
    application.text_input[0].set_value("Richmond")
    application.button[0].click().run()
    application.selectbox[0].select(1).run()
    assert len(application.dataframe) == 1

    ask(application)
    assert not application.exception
    assert application.session_state["selected_stop_id"] == 1
    assert application.selectbox[0].value == 1
    assert len(application.dataframe) == 1
    search.assert_called_once_with("Richmond")
    departures.assert_called_once_with(1)

    application.button(key="refresh_departures").click().run()
    assert departures.call_count == 2
    assert len(application.chat_message) == 2
    assistant.assert_called_once_with(QUESTION)

    search.return_value = []
    application.text_input[0].set_value("Unknown station")
    application.button[0].click().run()
    assert not application.exception
    assert application.session_state["selected_stop_id"] is None
    assert len(application.chat_message) == 2
    assert application.session_state["chat_messages"][-1]["content"] == ANSWER
    assert not application.dataframe
    assistant.assert_called_once_with(QUESTION)


def test_browser_sessions_do_not_share_chat_history(assistant):
    first = ask(app())
    second = app()
    assert not second.exception
    assert second.session_state["chat_messages"] == []
    assert not second.chat_message
    assert len(first.session_state["chat_messages"]) == 2
    assistant.assert_called_once_with(QUESTION)
