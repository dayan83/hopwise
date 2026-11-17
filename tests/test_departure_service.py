import traceback

import httpx
import pytest

from departure_service import get_departures_with_disruptions
from ptv import PTVConfig, PTVRequestError


@pytest.fixture
def config():
    return PTVConfig("fake-id", "fake-key", "https://ptv.example")


def departure(run_ref="run-A", **overrides):
    result = {"stop_id": 1, "route_id": 9, "run_ref": run_ref,
              "scheduled_departure_utc": "2026-11-17T00:00:00Z", "disruption_ids": [101]}
    result.update(overrides)
    return result


def notice(disruption_id=101, **overrides):
    return {"disruption_id": disruption_id, "title": "Works", "disruption_status": "Planned", **overrides}


def test_expanded_details_shared_without_extra_calls(config):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        assert request.url.path == "/v3/departures/route_type/0/stop/1"
        return httpx.Response(200, json={"departures": [departure(), departure("run-B")],
                                        "disruptions": {"101": notice()}})

    board = get_departures_with_disruptions(1, config=config, transport=httpx.MockTransport(handler))
    assert len(paths) == 1
    assert board.stop_id == 1
    for entry in board.departures:
        assert [item.disruption_id for item in entry.associated_disruptions] == [101]
        assert entry.unresolved_disruption_ids == []
        assert entry.departure.estimated_departure_utc is None
    assert board.contextual_notices == []
    assert board.route_notices == {}


def test_fallback_once_per_route_and_only_ids_create_associations(config):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.startswith("/v3/departures/"):
            return httpx.Response(200, json={"departures": [
                departure(disruption_ids=[101, 101]), departure("run-B", disruption_ids=[101, 102]),
                departure("run-C", disruption_ids=[])]})
        assert request.url.path == "/v3/disruptions/route/9"
        return httpx.Response(200, json={"disruptions": {
            "metro_train": [notice(disruption_status="Current"), notice(102)],
            "general": [notice(103, title="Route-level notice")]}})

    board = get_departures_with_disruptions(1, config=config, transport=httpx.MockTransport(handler))
    assert paths == ["/v3/departures/route_type/0/stop/1", "/v3/disruptions/route/9"]
    assert [[item.disruption_id for item in entry.associated_disruptions]
            for entry in board.departures] == [[101], [101, 102], []]
    assert board.departures[0].associated_disruptions[0].disruption_status == "Current"
    assert [item.disruption_id for item in board.route_notices[9]] == [103]
    assert board.route_notices[9][0].source_groups == ["general"]


def test_details_found_on_one_route_reused_for_another(config):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.startswith("/v3/departures/"):
            return httpx.Response(200, json={"departures": [departure(), departure("run-B", route_id=10)]})
        assert request.url.path == "/v3/disruptions/route/9"
        return httpx.Response(200, json={"disruptions": {"general": [notice()]}})

    board = get_departures_with_disruptions(1, config=config, transport=httpx.MockTransport(handler))
    assert len(paths) == 2
    assert [entry.associated_disruptions[0].disruption_id for entry in board.departures] == [101, 101]
    assert board.departures[0].associated_disruptions[0].source_groups == ["general"]


@pytest.mark.parametrize("ids", [None, []])
def test_no_linked_ids_do_not_trigger_route_calls(config, ids):
    def handler(request):
        assert request.url.path.startswith("/v3/departures/")
        return httpx.Response(200, json={"departures": [departure(disruption_ids=ids)],
                                        "disruptions": {"102": notice(102, routes=[{"route_id": 9}])}})

    board = get_departures_with_disruptions(1, config=config, transport=httpx.MockTransport(handler))
    assert board.departures[0].departure.disruption_ids == ids
    assert board.departures[0].associated_disruptions == []
    assert board.departures[0].unresolved_disruption_ids == []
    assert [item.disruption_id for item in board.contextual_notices] == [102]
    assert board.route_notices == {}


def test_missing_details_remain_explicit_after_successful_fallback(config):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.startswith("/v3/departures/"):
            return httpx.Response(200, json={"departures": [departure(), departure("run-B")],
                                            "disruptions": {"101": None}})
        return httpx.Response(200, json={"disruptions": {}})

    board = get_departures_with_disruptions(1, config=config, transport=httpx.MockTransport(handler))
    assert len(paths) == 2
    assert all(entry.unresolved_disruption_ids == [101] for entry in board.departures)
    assert all(entry.associated_disruptions == [] for entry in board.departures)


def test_missing_route_id_cannot_trigger_a_guessed_lookup(config):
    def handler(request):
        assert request.url.path.startswith("/v3/departures/")
        return httpx.Response(200, json={"departures": [departure(route_id=None)]})

    board = get_departures_with_disruptions(1, config=config, transport=httpx.MockTransport(handler))
    assert board.departures[0].unresolved_disruption_ids == [101]


def test_empty_departures_require_no_other_requests(config):
    def handler(request):
        assert request.url.path.startswith("/v3/departures/")
        return httpx.Response(200, json={"departures": []})

    board = get_departures_with_disruptions(1, config=config, transport=httpx.MockTransport(handler))
    assert board.departures == []
    assert board.route_notices == {}


@pytest.mark.parametrize("failure", ["initial", "fallback", "network"])
def test_api_failures_propagate_safely(config, failure, caplog):
    def handler(request):
        if failure == "network":
            raise httpx.ConnectError(str(request.url), request=request)
        if failure == "initial" or request.url.path.startswith("/v3/disruptions/"):
            return httpx.Response(403, text=str(request.url))
        return httpx.Response(200, json={"departures": [departure()]})

    with caplog.at_level("DEBUG"), pytest.raises(PTVRequestError) as caught:
        get_departures_with_disruptions(1, config=config, transport=httpx.MockTransport(handler))
    output = "".join(traceback.format_exception(caught.value)) + caplog.text
    for secret in (config.dev_id, config.api_key, "signature="):
        assert secret not in output
