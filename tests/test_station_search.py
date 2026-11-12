import hashlib
import hmac
import traceback

import httpx
import pytest

from ptv import PTVConfig, PTVRequestError, search_stations


@pytest.fixture
def config():
    return PTVConfig("fake-id", "fake-key", "https://ptv.example")


def stop(name="Richmond", stop_id=1, route_type=0):
    return {"stop_name": name, "stop_suburb": "Richmond",
            "stop_id": stop_id, "route_type": route_type, "routes": []}


@pytest.mark.parametrize("term", ["Richmond", "Flinders Street", "Glen Waverley", "A/B & C?#"])
def test_search_request_and_signature(config, term):
    def handler(request):
        from urllib.parse import quote

        assert request.method == "GET"
        assert request.url.raw_path.split(b"?")[0].decode() == "/v3/search/" + quote(term, safe="")
        assert request.url.params["route_types"] == "0"
        assert request.url.params["include_outlets"] == "false"
        assert request.url.params["match_stop_by_suburb"] == "false"
        assert request.url.params["devid"] == "fake-id"
        unsigned, signature = request.url.raw_path.rsplit(b"&signature=", 1)
        assert signature.decode() == hmac.new(b"fake-key", unsigned, hashlib.sha1).hexdigest().upper()
        return httpx.Response(200, json={"stops": [stop(term)]})

    assert search_stations("  " + term + "  ", config, transport=httpx.MockTransport(handler)) == [
        {"stop_name": term, "stop_suburb": "Richmond", "stop_id": 1, "route_type": 0}]


def test_ambiguous_names_and_mode_filter(config):
    matches = [stop(), stop("East Richmond", 2), stop("Richmond tram", 3, 1),
               stop("Richmond bus", 4, 2), stop("Regional station", 5, 3)]
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, json={"stops": matches, "routes": [{"route_name": "Richmond"}]}))
    results = search_stations("Richmond", config, transport=transport)
    assert [station["stop_id"] for station in results] == [1, 2]


@pytest.mark.parametrize("stops", [[], [stop(route_type=2)]])
def test_no_matches(config, stops):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"stops": stops}))
    assert search_stations("Unknown station", config, transport=transport) == []


@pytest.mark.parametrize("term", ["", "  ", "Ri", "1234"])
def test_invalid_term_never_requests(config, term):
    def handler(request):
        pytest.fail("Invalid search must not send a request")
    with pytest.raises(ValueError, match="Station name"):
        search_stations(term, config, transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("failure", ["http", "network", "json", "shape"])
def test_search_errors_are_safe(config, failure, caplog):
    def handler(request):
        if failure == "network":
            raise httpx.ConnectError(str(request.url), request=request)
        if failure == "json":
            return httpx.Response(200, text="invalid")
        if failure == "shape":
            return httpx.Response(200, json={"stops": None})
        return httpx.Response(403)
    with caplog.at_level("DEBUG"), pytest.raises(PTVRequestError) as caught:
        search_stations("Richmond", config, transport=httpx.MockTransport(handler))
    output = "".join(traceback.format_exception(caught.value)) + caplog.text
    for secret in (config.api_key, config.dev_id, "signature="):
        assert secret not in output
