import hashlib
import hmac
import json
import traceback
from datetime import datetime, timezone

import httpx
import pytest

from ptv import PTVConfig, PTVRequestError, get_disruptions_for_route


@pytest.fixture
def config():
    return PTVConfig("fake-id", "fake-key", "https://ptv.example")


def disruption(**overrides):
    record = {
        "disruption_id": 123,
        "title": "Track works",
        "description": "Services will run to an altered timetable.",
        "disruption_status": "Planned",
        "disruption_type": "Service alteration",
        "from_date": "2026-11-17T00:00:00Z",
        "to_date": "2026-11-18T00:00:00Z",
        "last_updated": "2026-11-16T00:00:00Z",
        "routes": [{"route_id": 9, "route_name": "Example line", "route_type": 0}],
        "stops": [{"stop_id": 1, "stop_name": "Richmond"}],
        "url": "https://ptv.example/notices/123",
    }
    record.update(overrides)
    return record


def transport_for(groups):
    return httpx.MockTransport(lambda request: httpx.Response(200, json={"disruptions": groups}))


def test_route_disruption_request_and_parsing(config):
    record = disruption()

    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/v3/disruptions/route/9"
        assert request.url.params["devid"] == config.dev_id
        assert "disruption_status" not in request.url.params
        unsigned, signature = request.url.raw_path.rsplit(b"&signature=", 1)
        assert signature.decode() == hmac.new(
            b"fake-key", unsigned, hashlib.sha1).hexdigest().upper()
        return httpx.Response(200, json={"disruptions": {"metro_train": [record]}})

    result = get_disruptions_for_route(9, config=config, transport=httpx.MockTransport(handler))
    expected = {**record, "source_groups": ["metro_train"],
                "from_date": datetime(2026, 11, 17, tzinfo=timezone.utc),
                "to_date": datetime(2026, 11, 18, tzinfo=timezone.utc),
                "last_updated": datetime(2026, 11, 16, tzinfo=timezone.utc)}
    assert result == [expected]


def test_all_statuses_preserved_without_date_filtering(config):
    records = [disruption(disruption_id=i, disruption_status=status,
                          from_date="2020-01-01T00:00:00Z", to_date="2020-01-02T00:00:00Z")
               for i, status in enumerate(("Planned", "Current", "Resolved", "Other status"))]
    result = get_disruptions_for_route(9, config=config,
                                      transport=transport_for({"metro_train": records}))
    assert [item["disruption_status"] for item in result] == [
        "Planned", "Current", "Resolved", "Other status"]
    assert all("active" not in item for item in result)


def test_general_notices_retained_other_modes_excluded(config):
    groups = {"metro_train": [disruption()],
              "general": [disruption(disruption_id=456, routes=None, stops=None)],
              "metro_tram": [disruption(disruption_id=789)],
              "regional_train": [disruption(disruption_id=987)]}
    result = get_disruptions_for_route(9, config=config, transport=transport_for(groups))
    assert [item["disruption_id"] for item in result] == [123, 456]
    assert result[1]["source_groups"] == ["general"]
    assert result[1]["routes"] is None


def test_duplicate_ids_merge_scope_and_affected_entities(config):
    metro = disruption(description=None)
    general = disruption(title="Duplicate headline", stops=[{"stop_id": 2, "stop_name": "Another stop"}])
    groups = {"metro_train": [metro, metro], "general": [general]}
    result = get_disruptions_for_route(9, config=config, transport=transport_for(groups))
    assert len(result) == 1
    assert result[0]["title"] == "Track works"
    assert result[0]["description"] == general["description"]
    assert result[0]["source_groups"] == ["metro_train", "general"]
    assert result[0]["routes"] == metro["routes"]
    assert result[0]["stops"] == metro["stops"] + general["stops"]


def test_missing_fields_and_idless_notices_are_retained(config):
    result = get_disruptions_for_route(9, config=config,
                                      transport=transport_for({"metro_train": [{}, {}]}))
    assert len(result) == 2
    assert result[0] == {
        "disruption_id": None, "title": None, "description": None, "disruption_status": None,
        "disruption_type": None, "from_date": None, "to_date": None, "last_updated": None,
        "routes": None, "stops": None, "url": None, "source_groups": ["metro_train"],
    }


@pytest.mark.parametrize("end", [None, ""])
def test_unknown_end_date(config, end):
    result = get_disruptions_for_route(9, config=config,
        transport=transport_for({"metro_train": [disruption(to_date=end)]}))
    assert result[0]["to_date"] is None
    assert result[0]["disruption_status"] == "Planned"


def test_offset_dates_normalized_to_aware_utc(config):
    result = get_disruptions_for_route(9, config=config, transport=transport_for({
        "metro_train": [disruption(from_date="2026-11-17T11:00:00+11:00")]}))
    assert result[0]["from_date"] == datetime(2026, 11, 17, tzinfo=timezone.utc)
    assert result[0]["from_date"].tzinfo is timezone.utc


@pytest.mark.parametrize("payload", [
    {}, {"disruptions": None}, {"disruptions": {}},
    {"disruptions": {"metro_train": [], "general": []}},
    {"disruptions": {"metro_train": None, "general": None}},
    {"disruptions": {"metro_bus": [disruption()]}},
])
def test_empty_disruptions(config, payload):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    assert get_disruptions_for_route(9, config=config, transport=transport) == []


@pytest.mark.parametrize("payload", [
    None, [], {"disruptions": []}, {"disruptions": "invalid"},
    {"disruptions": {"metro_train": {}}},
    {"disruptions": {"general": [None]}},
    {"disruptions": {"metro_train": [{"routes": "invalid"}]}},
    {"disruptions": {"metro_train": [{"stops": [None]}]}},
    {"disruptions": {"metro_train": [{"disruption_id": []}]}},
    {"disruptions": {"metro_train": [{"disruption_status": {}}]}},
])
def test_invalid_response_is_an_error(config, payload):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=json.dumps(payload)))
    with pytest.raises(PTVRequestError, match="invalid"):
        get_disruptions_for_route(9, config=config, transport=transport)


@pytest.mark.parametrize("value", ["invalid", "2026-11-17T00:00:00", 123])
def test_invalid_or_naive_dates_are_not_assigned_a_timezone(config, value):
    with pytest.raises(PTVRequestError, match="invalid disruption timestamp"):
        get_disruptions_for_route(9, config=config,
            transport=transport_for({"metro_train": [disruption(from_date=value)]}))


@pytest.mark.parametrize("route_id", [-1, True, None, "9", 1.5])
def test_invalid_route_id_never_requests(config, route_id):
    def handler(request):
        pytest.fail("Invalid route IDs must not send a request")

    with pytest.raises(ValueError, match="route_id"):
        get_disruptions_for_route(route_id, config=config, transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("failure", ["http", "network", "json", "timestamp"])
def test_errors_do_not_expose_credentials(config, failure, caplog):
    def handler(request):
        if failure == "network":
            raise httpx.ConnectError(str(request.url), request=request)
        if failure == "json":
            return httpx.Response(200, text="invalid JSON")
        if failure == "timestamp":
            return httpx.Response(200, json={"disruptions": {"metro_train": [
                disruption(from_date=str(request.url))]}})
        return httpx.Response(403, text=str(request.url))

    with caplog.at_level("DEBUG"), pytest.raises(PTVRequestError) as caught:
        get_disruptions_for_route(9, config=config, transport=httpx.MockTransport(handler))
    output = "".join(traceback.format_exception(caught.value)) + caplog.text
    for secret in (config.dev_id, config.api_key, "signature="):
        assert secret not in output
