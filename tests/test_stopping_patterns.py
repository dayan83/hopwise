from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import socket
import traceback

import httpx
import httpx2
import pytest

from evals.fixture_transport import FixtureMiss, FixtureTransport
from models import Departure
from ptv import PTVConfig, PTVRequestError, get_stopping_pattern
from stopping_pattern_service import get_destination_verification, verify_destination


FIXTURES = Path(__file__).parent / "fixtures"
BOARDING_TIME = datetime(2026, 11, 17, 22, 58, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Stopping-pattern tests must not use credentials or live network transports")

    monkeypatch.setattr(PTVConfig, "from_env", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def load_fixture(name="stopping_pattern.json"):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def body(data):
    return data["responses"][0]["response"]["body"]


def execute(data, destination=1103, **departure_updates):
    transport = FixtureTransport(data)
    departure = Departure.model_validate({**data["boarding_departure"], **departure_updates})
    result = get_destination_verification(departure, destination, config=transport.config, transport=transport)
    return result, transport


def test_pattern_reuses_auth_and_parses_run_ref_stops_sequences_and_aware_times():
    data = load_fixture()
    fixture = FixtureTransport(data)
    config = PTVConfig("fake-dev", "fake-key", "https://ptv.example")
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.path == "/v3/pattern/run/synthetic-GW-001/route_type/0"
        assert request.url.params.get_list("expand") == ["1", "3"]
        assert request.url.params["include_skipped_stops"] == "true"
        assert request.url.params["include_advertised_interchange"] == "false"
        assert request.url.params["date_utc"] == "2026-11-17T22:58:00+00:00"
        assert "stop_id" not in request.url.params
        unsigned, signature = request.url.raw_path.rsplit(b"&signature=", 1)
        assert signature.decode() == hmac.new(b"fake-key", unsigned, hashlib.sha1).hexdigest().upper()
        return fixture.handle_request(request)

    pattern = get_stopping_pattern("synthetic-GW-001", date_utc=BOARDING_TIME,
                                   config=config, transport=httpx.MockTransport(handler))
    assert len(requests) == 1 and fixture.match_counts == (1,)
    assert pattern.run_ref == "synthetic-GW-001" and pattern.route_type == 0
    assert pattern.date_utc == BOARDING_TIME
    assert pattern.stops[1101].stop_name == "Richmond"
    assert pattern.run.final_stop_id == 1103
    assert [row.stop_id for row in pattern.departures] == [1103, 1102, 1101, 1104]
    assert pattern.departures[2].scheduled_departure_utc == BOARDING_TIME
    assert pattern.departures[2].estimated_departure_utc is None
    assert pattern.departures[2].scheduled_departure_utc.tzinfo is timezone.utc


@pytest.mark.parametrize("destination, status, reason", [
    (1103, "served", "destination_after_boarding"),
    (1601, "not_served", "destination_absent"),
    (1102, "not_served", "destination_not_after_boarding"),
    (1101, "not_served", "destination_not_after_boarding"),
])
def test_destination_decisions_use_stop_ids_after_boarding(destination, status, reason):
    result, transport = execute(load_fixture(), destination)
    assert result.status == status and result.reason == reason
    assert result.boarding_sequence == 200
    assert transport.match_counts == (1,) and transport.miss_count == 0


def test_nonconsecutive_departure_sequences_order_independently_of_response_and_stop_order():
    data = load_fixture()
    for stop in body(data)["stops"].values():
        stop["stop_sequence"] = 9999 - stop["stop_sequence"]
    result, _ = execute(data)
    assert result.status == "served"
    assert result.boarding_sequence == 200 and result.destination_sequence == 900


def test_local_aware_boarding_time_is_normalized_before_request_and_identity_matching():
    result, transport = execute(load_fixture(), scheduled_departure_utc="2026-11-18T09:58:00+11:00")
    assert result.status == "served" and transport.match_counts == (1,)


def test_explicit_skipped_stop_is_not_a_served_departure_or_invented_sequence():
    data = load_fixture("stopping_pattern_skipped.json")
    result, transport = execute(data, 1104)
    assert result.status == "not_served" and result.reason == "destination_skipped"
    assert result.skipped_after_sequence == 200 and result.destination_sequence is None
    pattern = get_stopping_pattern("synthetic-GW-express", date_utc=BOARDING_TIME,
                                   config=transport.config, transport=transport)
    assert pattern.stops[1104].stop_name == "Burnley"
    assert 1104 not in [row.stop_id for row in pattern.departures]
    assert pattern.departures[2].skipped_stops[0].stop_id == 1104
    assert verify_destination(pattern, 1101, 1103).status == "served"


def test_names_and_final_destination_never_substitute_for_a_destination_stop_record():
    data = load_fixture()
    body(data)["runs"]["synthetic-GW-001"]["destination_name"] = "Alamein"
    body(data)["routes"] = {"2101": {"route_id": 2101, "route_name": "Alamein"}}
    assert execute(data, 1601)[0].status == "not_served"
    # A missing terminal record makes absence uncertain, never a positive match.
    body(data)["runs"]["synthetic-GW-001"]["final_stop_id"] = 1601
    assert execute(data, 1601)[0].status == "unknown"


@pytest.mark.parametrize("missing", ["departures", "empty_departures", "sequence", "stop_id", "run_ref", "route_id", "skipped_id"])
def test_incomplete_pattern_does_not_become_a_negative_answer(missing):
    data = load_fixture()
    payload = body(data)
    if missing == "departures":
        payload.pop("departures")
    elif missing == "empty_departures":
        payload["departures"] = []
    elif missing == "skipped_id":
        payload["departures"][2]["skipped_stops"] = [{"stop_name": "Unidentified station"}]
    else:
        payload["departures"][0].pop("departure_sequence" if missing == "sequence" else missing)
    result, _ = execute(data)
    assert result.status == "unknown" and result.reason in {"missing_pattern", "incomplete_pattern"}


@pytest.mark.parametrize("missing", ["run", "terminal", "stop_expansion", "stop_identity", "unaccounted_stop"])
def test_absence_requires_endpoint_and_stop_coverage_evidence(missing):
    data = load_fixture()
    payload = body(data)
    if missing == "run":
        payload.pop("runs")
    elif missing == "terminal":
        payload["runs"]["synthetic-GW-001"].pop("final_stop_id")
    elif missing == "stop_expansion":
        payload.pop("stops")
    elif missing == "stop_identity":
        payload["stops"]["1101"].pop("stop_id")
    else:
        payload["stops"]["1601"] = {"stop_id": 1601, "stop_name": "Alamein", "route_type": 0}
    result, _ = execute(data, 1601)
    assert result.status == "unknown" and result.reason == "incomplete_pattern"


def test_explicit_positive_evidence_does_not_require_optional_display_metadata():
    data = load_fixture()
    body(data).pop("stops")
    body(data).pop("runs")
    result, _ = execute(data)
    assert result.status == "served" and result.destination_sequence == 900


@pytest.mark.parametrize("ambiguity, reason", [
    ("duplicate_sequence", "ambiguous_sequence"),
    ("repeated_stop", "ambiguous_stop_occurrences"),
    ("served_and_skipped", "ambiguous_stop_occurrences"),
    ("wrong_run", "run_identity_mismatch"),
    ("wrong_route", "run_identity_mismatch"),
    ("wrong_direction", "run_identity_mismatch"),
    ("wrong_mode", "run_identity_mismatch"),
    ("offline", "api_unhealthy"),
])
def test_ambiguous_or_inconsistent_run_data_stays_unknown(ambiguity, reason):
    data = load_fixture()
    payload = body(data)
    if ambiguity == "duplicate_sequence":
        payload["departures"][0]["departure_sequence"] = 200
    elif ambiguity == "repeated_stop":
        duplicate = deepcopy(payload["departures"][2])
        duplicate["departure_sequence"] = 950
        payload["departures"].append(duplicate)
    elif ambiguity == "served_and_skipped":
        payload["departures"][2]["skipped_stops"] = [payload["stops"]["1103"]]
    elif ambiguity == "offline":
        payload["status"]["health"] = 0
    elif ambiguity == "wrong_mode":
        payload["stops"]["1101"]["route_type"] = 1
    else:
        field, value = {"wrong_run": ("run_ref", "foreign-run"), "wrong_route": ("route_id", 999),
                        "wrong_direction": ("direction_id", 999)}[ambiguity]
        payload["departures"][0][field] = value
    result, _ = execute(data)
    assert result.status == "unknown" and result.reason == reason


def test_boarding_time_binds_the_pattern_to_the_selected_departure():
    # A different date cannot match this fixed request or fall back to live PTV.
    with pytest.raises(FixtureMiss, match="fixture_miss"):
        execute(load_fixture(), scheduled_departure_utc="2026-11-18T22:58:00Z")


def test_stale_pattern_or_different_boarding_direction_is_not_accepted():
    data = load_fixture()
    body(data)["departures"][2]["scheduled_departure_utc"] = "2026-11-18T22:58:00Z"
    assert execute(data)[0].reason == "boarding_departure_mismatch"
    result, _ = execute(load_fixture(), direction_id=4102)
    assert result.status == "unknown" and result.reason == "boarding_departure_mismatch"


@pytest.mark.parametrize("where, flag, reason", [(2, "DOO", "boarding_drop_off_only"), (0, "PUO", "destination_pick_up_only")])
def test_explicit_pickup_dropoff_restrictions_do_not_claim_a_usable_service(where, flag, reason):
    data = load_fixture()
    body(data)["departures"][where]["flags"] = flag
    result, _ = execute(data)
    assert result.status == "unknown" and result.reason == reason


@pytest.mark.parametrize("bad", ["sequence_string", "sequence_boolean", "sequence_negative", "stop_string", "stop_boolean",
                                  "mismatched_stop_key", "mismatched_run_key", "naive_time", "wrong_shape"])
def test_malformed_response_is_safely_rejected_by_client_and_unknown_in_service(bad):
    data = load_fixture()
    payload = body(data)
    if bad.startswith("sequence_"):
        payload["departures"][0]["departure_sequence"] = {"sequence_string": "900", "sequence_boolean": True,
                                                           "sequence_negative": -1}[bad]
    elif bad.startswith("stop_"):
        payload["departures"][0]["stop_id"] = "1103" if bad == "stop_string" else True
    elif bad == "mismatched_stop_key":
        payload["stops"]["1101"]["stop_id"] = 1102
    elif bad == "mismatched_run_key":
        payload["runs"]["synthetic-GW-001"]["run_ref"] = "foreign-run"
    elif bad == "naive_time":
        payload["departures"][0]["scheduled_departure_utc"] = "2026-11-18T10:25:00"
    else:
        payload["departures"] = {"stop_id": 1101}
    transport = FixtureTransport(data)
    with pytest.raises(PTVRequestError, match="invalid stopping pattern data"):
        get_stopping_pattern("synthetic-GW-001", date_utc=BOARDING_TIME, config=transport.config, transport=transport)
    result, _ = execute(data)
    assert result.status == "unknown" and result.reason == "pattern_unavailable"


def test_realistic_http_error_is_unknown_and_not_a_successful_empty_pattern():
    data = load_fixture("stopping_pattern_api_failure.json")
    result, transport = execute(data)
    assert result.status == "unknown" and result.reason == "pattern_unavailable"
    assert transport.match_counts == (1,)


@pytest.mark.parametrize("failure", ["http", "network", "json", "model"])
def test_client_failure_diagnostics_never_expose_signed_urls_or_credentials(failure, caplog):
    config = PTVConfig("private-dev", "private-key", "https://ptv.example")

    def handler(request):
        if failure == "network":
            raise httpx.ConnectError(str(request.url), request=request)
        if failure == "json":
            return httpx.Response(200, text=str(request.url))
        if failure == "model":
            return httpx.Response(200, json={"departures": [{"stop_id": str(request.url)}]})
        return httpx.Response(503, text=str(request.url))

    with caplog.at_level("DEBUG"), pytest.raises(PTVRequestError) as caught:
        get_stopping_pattern("synthetic-run", config=config, transport=httpx.MockTransport(handler))
    diagnostic = "".join(traceback.format_exception(caught.value)) + caplog.text
    for secret in (config.dev_id, config.api_key, "signature=", config.base_url):
        assert secret not in diagnostic


@pytest.mark.parametrize("arguments", [
    {"run_ref": ""}, {"run_ref": None}, {"run_ref": 123}, {"run_ref": " .. "}, {"run_ref": ".."},
    {"run_ref": "run\nname"}, {"route_type": True}, {"route_type": -1},
    {"date_utc": datetime(2026, 11, 17)}, {"date_utc": "2026-11-17T22:58:00Z"},
])
def test_invalid_request_arguments_never_send_http(arguments):
    with pytest.raises(ValueError):
        get_stopping_pattern(**{"run_ref": "synthetic-run", **arguments})


@pytest.mark.parametrize("field", ["run_ref", "stop_id", "scheduled_departure_utc"])
def test_missing_source_departure_details_do_not_trigger_a_request(field):
    result, transport = execute(load_fixture(), **{field: None})
    assert result.status == "unknown" and result.reason == "missing_boarding_details"
    assert transport.match_counts == (0,)


@pytest.mark.parametrize("stop_id", [0, -1, True, "1103", None])
def test_invalid_destination_identifiers_are_rejected_before_configuration_or_http(stop_id):
    with pytest.raises(ValueError, match="positive integer"):
        get_destination_verification(Departure(), stop_id)
