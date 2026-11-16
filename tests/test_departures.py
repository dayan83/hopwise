import hashlib
import hmac
import json
import traceback

import httpx
import pytest

from ptv import PTVConfig, PTVRequestError, get_departures


@pytest.fixture
def config():
    return PTVConfig("fake-id", "fake-key", "https://ptv.example")


def departure(**overrides):
    result = {
        "route_id": 9, "direction_id": 0,
        "scheduled_departure_utc": "2026-11-14T23:00:00Z",
        "estimated_departure_utc": "2026-11-14T23:02:00Z",
        "platform_number": "2",
    }
    result.update(overrides)
    return result


@pytest.mark.parametrize("route_type,max_results", [(0, 5), (0, 2), (1, 3)])
def test_departures_request_and_signature(config, route_type, max_results):
    expected = departure()

    def handler(request):
        assert request.method == "GET"
        assert request.url.path == f"/v3/departures/route_type/{route_type}/stop/123"
        assert request.url.params["max_results"] == str(max_results)
        assert request.url.params["devid"] == config.dev_id
        unsigned, signature = request.url.raw_path.rsplit(b"&signature=", 1)
        assert signature.decode() == hmac.new(
            b"fake-key", unsigned, hashlib.sha1).hexdigest().upper()
        return httpx.Response(200, json={"departures": [dict(expected, run_ref="ignored")]})

    assert get_departures(123, route_type, max_results, config=config,
                          transport=httpx.MockTransport(handler)) == [expected]


@pytest.mark.parametrize("estimate", [None, "missing"])
def test_no_real_time_estimate(config, estimate):
    record = departure(estimated_departure_utc=None)
    if estimate == "missing":
        record.pop("estimated_departure_utc")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"departures": [record]}))
    result = get_departures(123, config=config, transport=transport)[0]
    assert result["estimated_departure_utc"] is None
    assert result["scheduled_departure_utc"] == "2026-11-14T23:00:00Z"


def test_missing_fields(config):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"departures": [{}]}))
    assert get_departures(123, config=config, transport=transport) == [{
        "route_id": None, "direction_id": None, "scheduled_departure_utc": None,
        "estimated_departure_utc": None, "platform_number": None,
    }]


def test_empty_departures(config):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"departures": []}))
    assert get_departures(123, config=config, transport=transport) == []


def test_result_limit_preserves_response_order(config):
    records = [departure(route_id=route_id) for route_id in (9, 3, 5)]
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"departures": records}))
    result = get_departures(123, max_results=2, config=config, transport=transport)
    assert [record["route_id"] for record in result] == [9, 3]


@pytest.mark.parametrize("kwargs", [
    {"stop_id": 0}, {"stop_id": -1}, {"stop_id": True}, {"stop_id": "123"},
    {"route_type": -1}, {"route_type": False}, {"route_type": 0.5},
    {"max_results": 0}, {"max_results": -1}, {"max_results": 1.5}, {"max_results": True},
])
def test_invalid_arguments_never_request(config, kwargs):
    def handler(request):
        pytest.fail("Invalid arguments must not send a request")

    args = {"stop_id": 123, **kwargs}
    with pytest.raises(ValueError, match="must be an integer"):
        get_departures(**args, config=config, transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("payload", [None, [], {}, {"departures": None},
                                         {"departures": {}}, {"departures": [None]}])
def test_invalid_response(config, payload):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=json.dumps(payload)))
    with pytest.raises(PTVRequestError, match="invalid departures response"):
        get_departures(123, config=config, transport=transport)


@pytest.mark.parametrize("failure", ["http", "network", "json"])
def test_safe_departure_errors(config, failure, caplog):
    def handler(request):
        if failure == "network":
            raise httpx.ConnectError(str(request.url), request=request)
        if failure == "json":
            return httpx.Response(200, text="invalid JSON")
        return httpx.Response(403, text=str(request.url))

    with caplog.at_level("DEBUG"), pytest.raises(PTVRequestError) as caught:
        get_departures(123, config=config, transport=httpx.MockTransport(handler))
    output = "".join(traceback.format_exception(caught.value)) + caplog.text
    for secret in (config.dev_id, config.api_key, "signature="):
        assert secret not in output
