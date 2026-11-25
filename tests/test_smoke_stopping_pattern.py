from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
from zoneinfo import ZoneInfoNotFoundError

import httpx
import httpx2
import openai
import pytest

from evals.fixture_transport import FixtureTransport
import smoke_stopping_pattern as smoke


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Diagnostic tests must not load credentials, construct OpenAI or use live networking")

    monkeypatch.setattr(smoke.PTVConfig, "from_env", forbidden)
    monkeypatch.setattr(openai, "OpenAI", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def data(name="stopping_pattern.json"):
    fixture = json.loads((Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8"))
    fixture["responses"][:0] = [
        {"request": {"method": "GET", "path": "/v3/search/Richmond", "query": {
            "route_types": "0", "include_outlets": "false", "match_stop_by_suburb": "false", "match_route_by_suburb": "false"}},
         "response": {"status_code": 200, "body": {"stops": [
             {"stop_name": "East Richmond", "stop_id": 1199, "stop_suburb": "Cremorne", "route_type": 0},
             {"stop_name": "Richmond Station", "stop_id": 1101, "stop_suburb": "Richmond", "route_type": 0}]}}},
        {"request": {"method": "GET", "path": "/v3/departures/route_type/0/stop/1101",
                     "query": {"max_results": "3", "expand": ["5", "2", "3"]}},
         "response": {"status_code": 200, "body": {"departures": [deepcopy(fixture["boarding_departure"])]}}},
    ]
    return fixture


def pattern_body(fixture):
    return fixture["responses"][2]["response"]["body"]


def run(fixture, *, now=None):
    transport = FixtureTransport(fixture)
    status = smoke.run_smoke_test(transport.config, enabled=True, transport=transport,
                                  now=now or transport.as_of_utc)
    return status, transport


@pytest.mark.parametrize("argv, exit_code", [([], 2), (["--help"], 0)])
def test_cli_without_live_opt_in_never_loads_credentials_or_requests(argv, exit_code):
    with pytest.raises(SystemExit) as caught:
        smoke.main(argv)
    assert caught.value.code == exit_code


def test_programmatic_diagnostic_also_requires_explicit_opt_in():
    fixture = FixtureTransport(data())
    with pytest.raises(ValueError, match="explicit opt-in"):
        smoke.run_smoke_test(fixture.config, transport=fixture)
    assert fixture.match_counts == (0, 0, 0)


def test_live_cli_path_runs_offline_with_fixed_http_and_existing_verification(monkeypatch, capsys):
    transport = FixtureTransport(data())
    actual_run = smoke.run_smoke_test
    monkeypatch.setattr(smoke.PTVConfig, "from_env", lambda: transport.config)
    monkeypatch.setattr(smoke, "run_smoke_test", lambda config, **kwargs:
                        actual_run(config, transport=transport, now=transport.as_of_utc, **kwargs))
    verified = []
    verify = smoke.verify_destination

    def observe(pattern, boarding_id, destination_id, **kwargs):
        verified.append((boarding_id, destination_id, kwargs["departure"].run_ref))
        return verify(pattern, boarding_id, destination_id, **kwargs)

    monkeypatch.setattr(smoke, "verify_destination", observe)
    assert smoke.main(["--live"]) == 0
    assert transport.match_counts == (1, 1, 1) and transport.miss_count == 0
    assert verified == [(1101, 1101, "synthetic-GW-001"), (1101, 1104, "synthetic-GW-001")]
    output = capsys.readouterr().out
    assert "Boarding station: Richmond Station | stop ID 1101" in output
    assert "Selected run_ref: synthetic-GW-001" in output
    assert "Pattern departure records: 4; expanded stop entries: 4" in output
    assert "First stop by departure_sequence: seq 100 | Flinders Street" in output
    assert "Last stop by departure_sequence: seq 900 | Glen Waverley" in output
    assert "2026-11-17T22:58:00+00:00 / 2026-11-18 09:58:00 AEDT" in output
    downstream = output.split("Boarding and subsequent listed stops:")[1]
    assert downstream.index("seq 200") < downstream.index("seq 350") < downstream.index("seq 900")
    assert "seq 100" not in downstream
    assert "Destination verification: served (destination_after_boarding)" in output


def test_skipped_stations_are_labelled_and_not_used_as_the_destination_probe(capsys):
    status, transport = run(data("stopping_pattern_skipped.json"))
    assert status == 0 and transport.match_counts == (1, 1, 1)
    output = capsys.readouterr().out
    assert "Skipped, not a stopping record: Burnley (stop ID 1104)" in output
    assert "Destination probe: Glen Waverley (stop ID 1103)" in output


def test_truncation_is_reported_even_when_one_later_stop_is_directly_verified(capsys):
    fixture = data()
    pattern_body(fixture)["departures"] = pattern_body(fixture)["departures"][1:]
    status, _ = run(fixture)
    assert status == 0  # Positive probe only; negative coverage remains unverified.
    output = capsys.readouterr().out
    assert "Possible truncation or inconsistent endpoint metadata" in output
    assert "Expanded stop IDs without departure/skipped records" in output
    assert "unknown (incomplete_pattern)" in output
    assert "do not interpret missing destinations as confirmed absence" in output
    assert "Destination verification: served" in output


@pytest.mark.parametrize("problem, expected", [
    ("wrong_date", "Possible date filtering mismatch"),
    ("missing_boarding", "Boarding station missing or not uniquely orderable"),
    ("missing_sequence", "Missing departure_sequence values"),
    ("duplicate_sequence", "Duplicate departure_sequence values"),
    ("repeated_boarding", "Repeated stop IDs"),
])
def test_problem_patterns_are_inconclusive_without_retrying_or_claiming_absence(problem, expected, capsys):
    fixture = data()
    rows = pattern_body(fixture)["departures"]
    if problem == "wrong_date":
        for row in rows:
            row["scheduled_departure_utc"] = row["scheduled_departure_utc"].replace("2026-11-17", "2026-11-18")
    elif problem == "missing_boarding":
        pattern_body(fixture)["departures"] = [row for row in rows if row["stop_id"] != 1101]
    elif problem == "missing_sequence":
        rows[0].pop("departure_sequence")
    elif problem == "duplicate_sequence":
        rows[0]["departure_sequence"] = 350
    else:
        duplicate = deepcopy(rows[2])
        duplicate["departure_sequence"] = 950
        rows.append(duplicate)
    status, transport = run(fixture)
    assert status == 2 and transport.match_counts == (1, 1, 1)
    output = capsys.readouterr().out
    assert expected in output and "Destination verification: unknown" in output
    assert "Destination verification: not_served" not in output


def test_missing_optional_stop_names_do_not_invent_names_or_negative_coverage(capsys):
    fixture = data()
    pattern_body(fixture).pop("stops")
    status, _ = run(fixture)
    assert status == 0
    output = capsys.readouterr().out
    assert "Stop names/expansion unavailable" in output
    assert "Destination probe: name unavailable (stop ID 1104)" in output
    assert "Negative coverage is unverified" in output


@pytest.mark.parametrize("terminal", [False, True])
def test_empty_pattern_or_no_later_stop_is_inconclusive(terminal, capsys):
    fixture = data()
    payload = pattern_body(fixture)
    if terminal:
        payload["departures"] = [payload["departures"][2]]
        payload["stops"] = {"1101": payload["stops"]["1101"]}
        payload["runs"]["synthetic-GW-001"]["final_stop_id"] = 1101
    else:
        payload["departures"] = []
    status, transport = run(fixture)
    assert status == 2 and transport.match_counts == (1, 1, 1)
    output = capsys.readouterr().out
    assert "Destination verification: unknown" in output
    assert "No unambiguous later stopping record" in output


@pytest.mark.parametrize("matches", [[], [
    {"stop_name": "Richmond", "stop_id": 1101, "route_type": 0},
    {"stop_name": "Richmond Station", "stop_id": 1199, "route_type": 0},
]])
def test_empty_or_ambiguous_station_search_stops_before_departures(matches, capsys):
    fixture = data()
    fixture["responses"][0]["response"]["body"]["stops"] = matches
    status, transport = run(fixture)
    assert status == 2 and transport.match_counts == (1, 0, 0)
    assert "No unique metropolitan Richmond match" in capsys.readouterr().out


@pytest.mark.parametrize("problem", ["empty", "missing_run", "missing_stop", "missing_time", "past"])
def test_no_usable_departure_never_fetches_a_pattern(problem, capsys):
    fixture = data()
    departures = fixture["responses"][1]["response"]["body"]["departures"]
    if problem == "empty":
        departures.clear()
    elif problem == "past":
        departures[0]["scheduled_departure_utc"] = "2026-11-17T21:58:00Z"
    else:
        departures[0][{"missing_run": "run_ref", "missing_stop": "stop_id", "missing_time": "scheduled_departure_utc"}[problem]] = None
    status, transport = run(fixture)
    assert status == 2 and transport.match_counts == (1, 1, 0)
    assert "No usable upcoming departure" in capsys.readouterr().out


def test_skips_bad_candidate_then_fetches_exactly_one_valid_run():
    fixture = data()
    departures = fixture["responses"][1]["response"]["body"]["departures"]
    departures.insert(0, {**departures[0], "run_ref": None})
    status, transport = run(fixture)
    assert status == 0 and transport.match_counts == (1, 1, 1)


def test_delayed_departure_with_past_schedule_but_future_estimate_can_be_selected():
    fixture = data()
    fixture["responses"][1]["response"]["body"]["departures"][0]["estimated_departure_utc"] = "2026-11-17T23:05:00Z"
    status, transport = run(fixture, now=datetime(2026, 11, 17, 23, 0, tzinfo=timezone.utc))
    assert status == 0 and transport.match_counts == (1, 1, 1)


@pytest.mark.parametrize("stage", [0, 1, 2])
def test_ptv_error_at_each_stage_is_controlled_without_fallback(stage, capsys):
    fixture = data()
    fixture["responses"][stage]["response"] = {"status_code": 503, "body": {"message": "Synthetic unavailable"}}
    status, transport = run(fixture)
    assert status == 1 and transport.match_counts == tuple(1 if index <= stage else 0 for index in range(3))
    output = capsys.readouterr().out
    assert "failed: PTV request failed (HTTP 503)" in output
    assert "Destination verification: unknown (data_unavailable)" in output


def test_names_errors_and_logs_do_not_expose_credentials_or_authenticated_urls(capsys, caplog):
    fixture = data()
    transport = FixtureTransport(fixture)
    pattern_body(fixture)["stops"]["1104"]["stop_name"] = (
        f"Burnley {transport.config.api_key} {transport.config.dev_id} "
        f"{transport.config.base_url}/v3/private?signature=secret-signature")
    with caplog.at_level("DEBUG"):
        assert run(fixture)[0] == 0
    captured = capsys.readouterr()
    diagnostic = captured.out + captured.err + caplog.text
    for secret in (transport.config.api_key, transport.config.dev_id, transport.config.base_url, "signature=", "secret-signature"):
        assert secret not in diagnostic
    assert "[REDACTED]" in diagnostic


def test_missing_credentials_are_reported_without_raw_exception_details(monkeypatch, capsys):
    def missing():
        raise ValueError("private-config-diagnostics")

    monkeypatch.setattr(smoke.PTVConfig, "from_env", missing)
    assert smoke.main(["--live"]) == 1
    output = capsys.readouterr().out
    assert "Check PTV_DEV_ID, PTV_API_KEY and PTV_BASE_URL" in output
    assert "private-config-diagnostics" not in output


def test_missing_timezone_is_a_controlled_setup_failure(monkeypatch, capsys):
    monkeypatch.setattr(smoke.PTVConfig, "from_env", lambda: FixtureTransport(data()).config)

    def missing(*args):
        raise ZoneInfoNotFoundError("missing-data")

    monkeypatch.setattr(smoke, "ZoneInfo", missing)
    assert smoke.main(["--live"]) == 1
    assert "uv sync" in capsys.readouterr().out
