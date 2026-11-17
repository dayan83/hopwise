import hashlib
import hmac
import traceback
from datetime import datetime, timezone

import httpx
import pytest

from ptv import PTVConfig, PTVRequestError, get_departures_response


@pytest.fixture
def config():
    return PTVConfig("fake-id", "fake-key", "https://ptv.example")


def test_one_signed_request_preserves_fields_and_expanded_details(config):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        assert request.url.path == "/v3/departures/route_type/0/stop/1"
        assert request.url.params["expand"] == "5"
        assert request.url.params["max_results"] == "5"
        unsigned, signature = request.url.raw_path.rsplit(b"&signature=", 1)
        assert signature.decode() == hmac.new(b"fake-key", unsigned, hashlib.sha1).hexdigest().upper()
        return httpx.Response(200, json={"departures": [{
            "stop_id": 1, "route_id": 9, "run_ref": "run-A", "direction_id": 0,
            "scheduled_departure_utc": "2026-11-17T00:00:00Z",
            "estimated_departure_utc": None, "platform_number": "2", "disruption_ids": [101],
        }], "disruptions": {"101": {"title": "Works", "disruption_status": "Planned"}}})

    result = get_departures_response(1, config=config, transport=httpx.MockTransport(handler))
    assert len(paths) == 1
    departure = result.departures[0]
    assert (departure.stop_id, departure.route_id, departure.run_ref, departure.disruption_ids) == (1, 9, "run-A", [101])
    assert departure.scheduled_departure_utc == datetime(2026, 11, 17, tzinfo=timezone.utc)
    assert departure.estimated_departure_utc is None
    assert result.disruptions[0].disruption_id == 101
    assert result.disruptions[0].disruption_status == "Planned"


@pytest.mark.parametrize("expanded", [None, {}, {"101": None}, {"101": {}}])
def test_missing_expansion_and_placeholders(config, expanded):
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, json={"departures": [{}], "disruptions": expanded}))
    result = get_departures_response(1, config=config, transport=transport)
    assert result.disruptions == []
    assert result.departures[0].run_ref is None
    assert result.departures[0].disruption_ids is None


@pytest.mark.parametrize("expanded", [[], {"101": []}, {"101": {"disruption_id": 102}},
                                      {"unknown": {"title": "No ID"}}])
def test_invalid_expansion_is_not_reported_as_no_disruptions(config, expanded):
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, json={"departures": [], "disruptions": expanded}))
    with pytest.raises(PTVRequestError, match="expanded disruption"):
        get_departures_response(1, config=config, transport=transport)


def test_rich_response_limit(config):
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, json={"departures": [{"run_ref": "A"}, {"run_ref": "B"}]}))
    result = get_departures_response(1, max_results=1, config=config, transport=transport)
    assert [departure.run_ref for departure in result.departures] == ["A"]


def test_model_validation_errors_are_sanitized(config, caplog):
    def handler(request):
        return httpx.Response(200, json={"departures": [{"scheduled_departure_utc": str(request.url)}]})

    with caplog.at_level("DEBUG"), pytest.raises(PTVRequestError) as caught:
        get_departures_response(1, config=config, transport=httpx.MockTransport(handler))
    output = "".join(traceback.format_exception(caught.value)) + caplog.text
    for secret in (config.dev_id, config.api_key, "signature="):
        assert secret not in output


def test_route_and_destination_names_reused_from_same_response(config):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        assert request.url.params.get_list("expand") == ["5", "2", "3"]
        return httpx.Response(200, json={
            "departures": [{"route_id": 9, "run_ref": "run-A"}, {"route_id": 10, "run_ref": "run-B"}],
            "routes": {"9": {"route_name": "Glen Waverley"}},
            # Resolve via run_ref even when the lookup key is an older numeric identifier.
            "runs": {"123": {"run_ref": "run-A", "destination_name": "Flinders Street"}},
        })

    result = get_departures_response(1, config=config, transport=httpx.MockTransport(handler))
    assert len(calls) == 1
    assert result.departures[0].route_name == "Glen Waverley"
    assert result.departures[0].destination_name == "Flinders Street"
    assert result.departures[1].route_name is None
    assert result.departures[1].destination_name is None


@pytest.mark.parametrize("field,value", [("routes", []), ("runs", "invalid"), ("runs", {"A": []})])
def test_invalid_expanded_service_details_are_safe_errors(config, field, value):
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, json={"departures": [], field: value}))
    with pytest.raises(PTVRequestError, match="expanded route or run details"):
        get_departures_response(1, config=config, transport=transport)
