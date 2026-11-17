from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from streamlit.testing.v1 import AppTest

import departure_service
import ptv
from models import Departure, DepartureBoard, DepartureWithDisruptions, Disruption


APP_PATH = Path(__file__).resolve().parents[1] / "streamlit_app.py"


@pytest.fixture
def search(monkeypatch):
    search = Mock(return_value=[{
        "stop_id": 1, "stop_name": "Richmond", "stop_suburb": "Richmond", "route_type": 0}])
    monkeypatch.setattr(ptv, "search_stations", search)
    return search


@pytest.fixture
def fetch(monkeypatch):
    fetch = Mock(return_value=DepartureBoard(stop_id=1))
    monkeypatch.setattr(departure_service, "get_departures_with_disruptions", fetch)
    return fetch


def selected_app():
    application = AppTest.from_file(str(APP_PATH), default_timeout=10).run()
    application.text_input[0].set_value("Richmond")
    application.button[0].click().run()
    return application.selectbox[0].select(1).run()


def notice(disruption_id=101, **overrides):
    return Disruption.model_validate({
        "disruption_id": disruption_id, "title": "Service notice", "disruption_status": "Current",
        "description": "Detailed PTV notice text.", "disruption_type": "Service alteration",
        "from_date": "2026-11-17T00:00:00Z", "to_date": None,
        "url": "https://ptv.example/notices/101", **overrides,
    })


def entry(ids, notices=(), unresolved=(), **overrides):
    departure = Departure.model_validate({
        "stop_id": 1, "route_id": 9, "run_ref": "run-A", "disruption_ids": ids,
        "scheduled_departure_utc": "2026-11-17T00:00:00Z", **overrides,
    })
    return DepartureWithDisruptions(departure=departure, associated_disruptions=list(notices),
                                    unresolved_disruption_ids=list(unresolved))


def test_shared_notice_displayed_once_with_references_for_each_departure(search, fetch):
    shared = notice()
    fetch.return_value = DepartureBoard(stop_id=1, departures=[
        entry([101], [shared]), entry([101], [shared], run_ref="run-B",
                                    estimated_departure_utc="2026-11-17T00:00:00Z")])
    application = selected_app()
    assert not application.exception
    rows = application.dataframe[0].value
    assert list(rows["Notices"]) == ["Notice 101", "Notice 101"]
    assert list(rows["Timing"]) == ["No estimate available", "On time (estimated)"]
    assert len(application.expander) == 1
    assert application.expander[0].label == "Notice 101: Service notice"
    assert not application.expander[0].proto.expanded
    text = "\n".join(element.value for element in application.markdown)
    assert text.count("Detailed PTV notice text.") == 1
    assert "PTV status: Current" in text
    assert any("associations alone do not establish a train delay" in caption.value
               for caption in application.caption)
    application.run()
    fetch.assert_called_once_with(1)


def test_resolved_and_unresolved_notice_ids_are_distinguished(search, fetch):
    fetch.return_value = DepartureBoard(stop_id=1, departures=[entry([101, 999], [notice()], [999])])
    application = selected_app()
    assert not application.exception
    assert application.dataframe[0].value.iloc[0]["Notices"] == "Notice 101; Details unavailable for IDs 999"
    assert len(application.expander) == 1
    assert not application.error


def test_only_unresolved_ids_do_not_produce_empty_notice_cards(search, fetch):
    fetch.return_value = DepartureBoard(stop_id=1, departures=[entry([999], unresolved=[999])])
    application = selected_app()
    assert not application.exception
    assert application.dataframe[0].value.iloc[0]["Notices"] == "Details unavailable for IDs 999"
    assert not application.expander


@pytest.mark.parametrize("ids,expected", [(None, "Association data not supplied"), ([], "No linked notices listed")])
def test_missing_and_empty_association_information_remain_distinct(search, fetch, ids, expected):
    fetch.return_value = DepartureBoard(stop_id=1, departures=[entry(ids)])
    application = selected_app()
    assert not application.exception
    assert application.dataframe[0].value.iloc[0]["Notices"] == expected
    assert not application.expander


def test_planned_status_and_optional_notice_fields_preserved(search, fetch):
    planned = notice(disruption_status="Planned", description=None, title=None, source_groups=["general"])
    fetch.return_value = DepartureBoard(stop_id=1, departures=[entry([101], [planned])])
    application = selected_app()
    assert not application.exception
    assert application.expander[0].label == "Notice 101: Title not supplied"
    text = "\n".join(element.value for element in application.markdown)
    assert "PTV status: Planned" in text
    assert "Description not supplied." in text
    assert any("2026-11-17 11:00:00 AEDT | To: Unknown" in caption.value
               for caption in application.caption)
    assert any(caption.value == "PTV groups: general" for caption in application.caption)
    assert any(heading.value == "Linked disruption notices" for heading in application.subheader)
    assert all(heading.value != "Other route/station notices" for heading in application.subheader)


def test_contextual_notices_are_separate_and_deduplicated_across_sources(search, fetch):
    first, second = notice(202, title="Context A"), notice(303, title="Context B")
    fetch.return_value = DepartureBoard(
        stop_id=1, departures=[entry([])], contextual_notices=[first],
        route_notices={9: [first, second], 10: [second]})
    application = selected_app()
    assert not application.exception
    assert application.dataframe[0].value.iloc[0]["Notices"] == "No linked notices listed"
    assert len(application.expander) == 2
    assert [expander.label for expander in application.expander] == ["Notice 202: Context A", "Notice 303: Context B"]
    assert any(heading.value == "Other route/station notices" for heading in application.subheader)
    assert all(heading.value != "Linked disruption notices" for heading in application.subheader)
    captions = [caption.value for caption in application.caption]
    assert "Returned in: Departures response; Route 9" in captions
    assert "Returned in: Route 9; Route 10" in captions


def test_idless_context_notices_are_not_collapsed_as_duplicates(search, fetch):
    fetch.return_value = DepartureBoard(stop_id=1, departures=[entry([])], contextual_notices=[
        notice(None, title="First notice"), notice(None, title="Second notice")])
    application = selected_app()
    assert not application.exception
    assert len(application.expander) == 2


def test_refresh_replaces_notices_and_failure_retains_last_successful_board(search, fetch):
    fetch.return_value = DepartureBoard(stop_id=1, departures=[entry([101], [notice()])])
    application = selected_app()
    initial_time = application.session_state["departures_refreshed_at"]
    fetch.side_effect = ptv.PTVRequestError("PTV request failed (HTTP 403)")
    application.button(key="refresh_departures").click().run()
    assert not application.exception
    assert application.expander[0].label == "Notice 101: Service notice"
    assert application.warning
    assert application.session_state["departures_refreshed_at"] == initial_time
    fetch.side_effect = None
    fetch.return_value = DepartureBoard(stop_id=1, departures=[entry([202], [notice(202, title="Updated notice")])])
    application.button(key="refresh_departures").click().run()
    assert not application.exception
    assert [expander.label for expander in application.expander] == ["Notice 202: Updated notice"]
    assert not application.warning
    assert not application.error
    assert fetch.call_count == 3


def test_ui_reuses_real_day5_matching_and_deduplicated_route_fallback(search, monkeypatch):
    paths = []
    service = departure_service.get_departures_with_disruptions
    config = ptv.PTVConfig("fake-id", "fake-key", "https://ptv.example")

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/v3/departures/route_type/0/stop/1":
            return httpx.Response(200, json={"departures": [
                {"stop_id": 1, "route_id": 9, "run_ref": "run-A", "disruption_ids": [101, 101]},
                {"stop_id": 1, "route_id": 9, "run_ref": "run-B", "disruption_ids": [101]},
                {"stop_id": 1, "route_id": 9, "run_ref": "run-C", "disruption_ids": []}]})
        assert request.url.path == "/v3/disruptions/route/9"
        return httpx.Response(200, json={"disruptions": {
            "general": [{"disruption_id": 101, "title": "Explicitly linked general notice",
                         "disruption_status": "Planned"}],
            "metro_train": [{"disruption_id": 202, "title": "Unlinked route notice"}]}})

    monkeypatch.setattr(departure_service, "get_departures_with_disruptions", lambda stop_id: service(
        stop_id, config=config, transport=httpx.MockTransport(handler)))
    application = selected_app()
    assert not application.exception
    assert paths == ["/v3/departures/route_type/0/stop/1", "/v3/disruptions/route/9"]
    assert list(application.dataframe[0].value["Notices"]) == ["Notice 101", "Notice 101", "No linked notices listed"]
    assert [expander.label for expander in application.expander] == [
        "Notice 101: Explicitly linked general notice", "Notice 202: Unlinked route notice"]
    application.run()
    assert len(paths) == 2
