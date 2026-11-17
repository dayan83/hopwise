from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from streamlit.testing.v1 import AppTest

import ptv


APP_PATH = Path(__file__).resolve().parents[1] / "streamlit_app.py"
STATIONS = [
    {"stop_id": 1, "stop_name": "Richmond", "stop_suburb": "Richmond", "route_type": 0},
    {"stop_id": 2, "stop_name": "East Richmond", "stop_suburb": "Cremorne", "route_type": 0},
]


@pytest.fixture
def search(monkeypatch):
    search = Mock(return_value=STATIONS)
    monkeypatch.setattr(ptv, "search_stations", search)
    return search


def app():
    return AppTest.from_file(str(APP_PATH), default_timeout=10).run()


def submit(application, term="Richmond"):
    application.text_input[0].set_value(term)
    return application.button[0].click().run()


def test_initial_page_does_not_search(search):
    application = app()
    assert not application.exception
    assert application.title[0].value == "Hopwise"
    assert application.session_state["selected_stop_id"] is None
    assert not application.selectbox
    search.assert_not_called()


def test_search_displays_matches_without_choosing_the_first(search):
    application = submit(app(), "  Richmond  ")
    assert not application.exception
    search.assert_called_once_with("Richmond")
    assert application.selectbox[0].options == [
        "Richmond — Richmond (stop ID: 1)", "East Richmond — Cremorne (stop ID: 2)"]
    assert application.selectbox[0].value is None
    assert application.session_state["selected_stop_id"] is None


def test_selection_persists_across_reruns_without_another_request(search):
    application = submit(app())
    application.selectbox[0].select(2).run()
    assert not application.exception
    assert application.session_state["selected_stop_id"] == 2
    assert "East Richmond" in application.success[0].value
    application.run()
    assert application.session_state["selected_stop_id"] == 2
    search.assert_called_once_with("Richmond")


def test_new_search_resets_previous_selection(search):
    application = submit(app())
    application.selectbox[0].select(2).run()
    search.return_value = [{"stop_id": 3, "stop_name": "Flinders Street",
                            "stop_suburb": "Melbourne City", "route_type": 0}]
    submit(application, "Flinders Street")
    assert not application.exception
    assert application.session_state["selected_stop_id"] is None
    assert application.selectbox[0].value is None
    assert "Flinders Street" in application.selectbox[0].options[0]
    application.selectbox[0].select(3).run()
    assert application.session_state["selected_stop_id"] == 3
    assert search.call_count == 2


def test_blank_search_clears_selection_without_a_request(search):
    application = submit(app())
    application.selectbox[0].select(1).run()
    submit(application, "   ")
    assert not application.exception
    assert application.warning[0].value == "Enter a station name to search."
    assert application.session_state["selected_stop_id"] is None
    assert not application.selectbox
    search.assert_called_once_with("Richmond")


def test_no_matches_clears_previous_results_and_selection(search):
    application = submit(app())
    application.selectbox[0].select(1).run()
    search.return_value = []
    submit(application, "Unknown station")
    assert not application.exception
    assert "No matching metropolitan train stations" in application.info[0].value
    assert application.session_state["selected_stop_id"] is None
    assert not application.selectbox
    application.run()
    assert application.info
    assert search.call_count == 2


@pytest.mark.parametrize("error", [
    ptv.PTVRequestError("PTV request failed (HTTP 403)"),
    ptv.PTVRequestError("PTV request failed due to a network error"),
    ValueError("Missing required environment variables: PTV_API_KEY"),
])
def test_search_errors_are_displayed_without_stale_selection(search, error):
    application = submit(app())
    application.selectbox[0].select(1).run()
    search.side_effect = error
    submit(application)
    assert not application.exception
    assert str(error) in application.error[0].value
    assert application.session_state["selected_stop_id"] is None
    assert application.session_state["station_results"] == []
    assert not application.selectbox
    assert not application.info


@pytest.mark.parametrize("term", ["Ri", "1234"])
def test_invalid_names_use_existing_client_validation(monkeypatch, term):
    def unexpected_config():
        pytest.fail("Invalid search should be rejected before loading credentials")

    monkeypatch.setattr(ptv.PTVConfig, "from_env", unexpected_config)
    application = submit(app(), term)
    assert not application.exception
    assert "Station name must contain at least three characters" in application.error[0].value
    assert application.session_state["selected_stop_id"] is None


def test_existing_client_integration_uses_only_station_search(monkeypatch):
    paths = []
    real_search = ptv.search_stations
    config = ptv.PTVConfig("fake-id", "fake-key", "https://ptv.example")

    def handler(request):
        paths.append(request.url.path)
        assert request.url.path == "/v3/search/Richmond"
        assert request.url.params["devid"] == config.dev_id
        assert "signature" in request.url.params
        return httpx.Response(200, json={"stops": STATIONS + [
            {"stop_id": 99, "stop_name": "Richmond tram stop", "route_type": 1}]})

    monkeypatch.setattr(ptv, "search_stations", lambda term: real_search(
        term, config, transport=httpx.MockTransport(handler)))
    application = submit(app())
    assert not application.exception
    assert len(application.selectbox[0].options) == 2
    application.selectbox[0].select(1).run()
    assert application.session_state["selected_stop_id"] == 1
    assert paths == ["/v3/search/Richmond"]


def test_browser_sessions_do_not_share_selection(search):
    first = submit(app())
    first.selectbox[0].select(1).run()
    second = app()
    assert second.session_state["selected_stop_id"] is None
    assert second.session_state["station_results"] == []
    assert first.session_state["selected_stop_id"] == 1
    search.assert_called_once_with("Richmond")
