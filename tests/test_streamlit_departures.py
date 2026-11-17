from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import httpx
import pytest
from streamlit.testing.v1 import AppTest

import ptv
from models import Departure, DeparturesResponse


APP_PATH = Path(__file__).resolve().parents[1] / "streamlit_app.py"
STATIONS = [{"stop_id": stop_id, "stop_name": name, "stop_suburb": "Richmond", "route_type": 0}
            for stop_id, name in [(1, "Richmond"), (2, "East Richmond")]]


def response(**overrides):
    data = {"stop_id": 1, "route_id": 9, "route_name": "Glen Waverley", "run_ref": "run-A",
            "destination_name": "Flinders Street", "platform_number": "2",
            "scheduled_departure_utc": "2026-11-17T00:00:00Z",
            "estimated_departure_utc": "2026-11-17T00:02:00Z"}
    data.update(overrides)
    return DeparturesResponse(departures=[Departure.model_validate(data)])


@pytest.fixture
def search(monkeypatch):
    search = Mock(return_value=STATIONS)
    monkeypatch.setattr(ptv, "search_stations", search)
    return search


@pytest.fixture
def fetch(monkeypatch):
    fetch = Mock(return_value=response())
    monkeypatch.setattr(ptv, "get_departures_response", fetch)
    return fetch


def selected_app():
    application = AppTest.from_file(str(APP_PATH), default_timeout=10).run()
    application.text_input[0].set_value("Richmond")
    application.button[0].click().run()
    application.selectbox[0].select(1).run()
    return application


def refresh(application):
    return application.button(key="refresh_departures").click().run()


def test_station_selection_loads_typed_departures_once(search, fetch):
    application = selected_app()
    assert not application.exception
    fetch.assert_called_once_with(1)
    row = application.dataframe[0].value.iloc[0]
    assert row.to_dict() == {
        "Scheduled": "2026-11-17 11:00:00 AEDT", "Estimated": "2026-11-17 11:02:00 AEDT",
        "Route": "Glen Waverley", "Destination": "Flinders Street", "Platform": "2",
        "Timing": "2 min late (estimated)",
    }
    timestamp = application.session_state["departures_refreshed_at"]
    assert timestamp.utcoffset().total_seconds() == 0
    local = timestamp.astimezone(ZoneInfo("Australia/Melbourne")).strftime("%Y-%m-%d %H:%M:%S %Z")
    assert any(caption.value == "Last refreshed: " + local for caption in application.caption)
    application.run()
    fetch.assert_called_once_with(1)
    assert application.session_state["departures_refreshed_at"] == timestamp
    search.assert_called_once_with("Richmond")


def test_refresh_replaces_data_and_updates_success_timestamp(search, fetch):
    application = selected_app()
    previous = application.session_state["departures_refreshed_at"]
    fetch.return_value = response(platform_number="5")
    refresh(application)
    assert not application.exception
    assert fetch.call_count == 2
    assert application.dataframe[0].value.iloc[0]["Platform"] == "5"
    assert application.session_state["departures_refreshed_at"] > previous
    search.assert_called_once_with("Richmond")


@pytest.mark.parametrize("estimate,scheduled,expected", [
    (None, "2026-11-17T00:00:00Z", "No estimate available"),
    ("2026-11-17T00:00:00Z", "2026-11-17T00:00:00Z", "On time (estimated)"),
    ("2026-11-16T23:59:00Z", "2026-11-17T00:00:00Z", "1 min early (estimated)"),
    ("2026-11-17T00:00:15Z", "2026-11-17T00:00:00Z", "15 sec late (estimated)"),
    ("2026-11-17T00:00:00Z", None, "Delay unavailable (no scheduled time)"),
])
def test_timing_distinguishes_missing_estimates_from_on_time(search, fetch, estimate, scheduled, expected):
    fetch.return_value = response(estimated_departure_utc=estimate, scheduled_departure_utc=scheduled)
    application = selected_app()
    assert not application.exception
    row = application.dataframe[0].value.iloc[0]
    assert row["Timing"] == expected
    if estimate is None:
        assert row["Estimated"] == "No estimate available"


def test_missing_names_and_platform_have_explicit_fallbacks(search, fetch):
    fetch.return_value = response(route_name=None, destination_name=None, platform_number=None)
    application = selected_app()
    row = application.dataframe[0].value.iloc[0]
    assert row["Route"] == "Route 9"
    assert row["Destination"] == "Not provided"
    assert row["Platform"] == "Not provided"


def test_winter_times_are_displayed_in_aest(search, fetch):
    fetch.return_value = response(scheduled_departure_utc="2026-07-14T23:00:00Z",
                                 estimated_departure_utc=None)
    application = selected_app()
    assert application.dataframe[0].value.iloc[0]["Scheduled"] == "2026-07-15 09:00:00 AEST"


def test_empty_departures_can_be_refreshed(search, fetch):
    fetch.return_value = DeparturesResponse()
    application = selected_app()
    assert not application.exception
    assert not application.dataframe
    assert "No upcoming departures" in application.info[0].value
    assert application.session_state["departures_refreshed_at"] is not None
    fetch.return_value = response()
    refresh(application)
    assert application.dataframe
    assert not application.info


def test_failed_refresh_keeps_previous_result_and_timestamp_with_warning(search, fetch):
    application = selected_app()
    previous = application.session_state["departures_refreshed_at"]
    fetch.side_effect = ptv.PTVRequestError("PTV request failed (HTTP 403)")
    refresh(application)
    assert not application.exception
    assert "Unable to load departures" in application.error[0].value
    assert "previously fetched departures" in application.warning[0].value
    assert application.dataframe
    assert application.session_state["departures_refreshed_at"] == previous
    application.run()
    assert fetch.call_count == 2
    fetch.side_effect = None
    refresh(application)
    assert not application.error
    assert not application.warning
    assert application.session_state["departures_refreshed_at"] > previous


def test_switching_stations_removes_old_data_even_when_new_request_fails(search, fetch):
    application = selected_app()
    fetch.side_effect = ptv.PTVRequestError("PTV request failed due to a network error")
    application.selectbox[0].select(2).run()
    assert not application.exception
    assert fetch.call_args.args == (2,)
    assert application.session_state["selected_stop_id"] == 2
    assert application.session_state["departure_response"] is None
    assert application.session_state["departures_refreshed_at"] is None
    assert not application.dataframe
    assert application.error


def test_new_search_clears_departures_without_refetching(search, fetch):
    application = selected_app()
    application.text_input[0].set_value("Unknown station")
    search.return_value = []
    application.button[0].click().run()
    assert application.session_state["departure_response"] is None
    assert application.session_state["departures_refreshed_at"] is None
    assert not application.dataframe
    fetch.assert_called_once_with(1)


def test_initial_error_is_retryable_without_automatic_repeated_calls(search, fetch):
    fetch.side_effect = ptv.PTVRequestError("PTV request failed (HTTP 403)")
    application = selected_app()
    assert not application.exception
    assert application.error
    assert application.session_state["departures_refreshed_at"] is None
    application.run()
    fetch.assert_called_once_with(1)
    fetch.side_effect = None
    refresh(application)
    assert not application.error
    assert application.dataframe


def test_ui_uses_real_client_models_and_one_mocked_departure_request(search, monkeypatch):
    paths = []
    real_fetch = ptv.get_departures_response
    config = ptv.PTVConfig("fake-id", "fake-key", "https://ptv.example")

    def handler(request):
        paths.append(request.url.path)
        assert request.url.path == "/v3/departures/route_type/0/stop/1"
        assert request.url.params.get_list("expand") == ["5", "2", "3"]
        assert "signature" in request.url.params
        return httpx.Response(200, json={
            "departures": [{"stop_id": 1, "route_id": 9, "run_ref": "run-A",
                            "scheduled_departure_utc": "2026-11-17T00:00:00Z",
                            "estimated_departure_utc": None}],
            "routes": {"9": {"route_name": "Glen Waverley"}},
            "runs": {"run-A": {"destination_name": "Flinders Street"}},
        })

    monkeypatch.setattr(ptv, "get_departures_response", lambda stop_id: real_fetch(
        stop_id, config=config, transport=httpx.MockTransport(handler)))
    application = selected_app()
    assert not application.exception
    row = application.dataframe[0].value.iloc[0]
    assert row["Route"] == "Glen Waverley"
    assert row["Destination"] == "Flinders Street"
    assert row["Timing"] == "No estimate available"
    application.run()
    assert len(paths) == 1
