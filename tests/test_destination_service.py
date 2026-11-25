from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import socket

import httpx
import httpx2
import openai
import pytest

import destination_service as service
from evals.fixture_transport import FixtureTransport
from ptv import PTVConfig


ORIGIN, DESTINATION = 1162, 9002
A, B, C, D = "synthetic-BL-early", "synthetic-GW-nonmatch", "synthetic-BL-next", "synthetic-BL-failure"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Destination service tests must not load credentials, call OpenAI or use live networking")

    monkeypatch.setattr(PTVConfig, "from_env", forbidden)
    monkeypatch.setattr(openai, "OpenAI", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def fixture():
    return json.loads((Path(__file__).parent / "fixtures" / "destination_departures.json").read_text(encoding="utf-8"))


def source(data):
    return data["responses"][0]["response"]["body"]


def select(data, *runs):
    source(data)["departures"] = [row for row in source(data)["departures"] if row["run_ref"] in runs]


def pattern(data, run):
    return next(entry for entry in data["responses"] if entry["request"]["path"].startswith(f"/v3/pattern/run/{run}/"))


def execute(data, *, clock=None, **kwargs):
    data["responses"][0]["request"]["query"]["max_results"] = str(kwargs.get("candidate_limit", 20))
    transport = FixtureTransport(data)
    result = service.get_departures_to_destination(ORIGIN, DESTINATION, config=transport.config,
                                                   transport=transport, clock=clock or (lambda: transport.as_of_utc), **kwargs)
    return result, transport


def test_mixed_results_preserve_disruptions_timing_and_explicit_unknowns():
    result, transport = execute(fixture())
    assert result.status == "partial" and result.reason == "fewer_confirmed_matches_within_search"
    assert [row.departure.run_ref for row in result.matches] == [C, A]
    assert [row.departure.run_ref for row in result.candidates] == [B, C, D, A]
    assert result.candidates[0].verification.status == "not_served"
    assert result.candidates[2].verification.status == "unknown"
    assert result.candidates[2].verification.reason == "pattern_unavailable"
    assert result.metadata.model_dump() == {
        "candidate_limit": 20, "max_pattern_requests": 10, "candidates_received": 4, "candidates_examined": 4,
        "pattern_requests": 4, "patterns_fetched": 3, "pattern_cache_hits": 0, "pattern_budget_skips": 0,
        "confirmed_matches": 2, "confirmed_non_matches": 1, "unknown_verifications": 1,
        "past_excluded": 0, "duplicates_excluded": 0, "matches_returned": 2,
        "candidate_limit_reached": False, "pattern_request_limit_reached": False, "search_limit_reached": False}
    first, second = result.matches
    assert first.departure.estimated_departure_utc is None and first.time_source == "scheduled"
    assert first.delay_seconds is None and first.delay_status == "unknown"
    assert first.passenger_delay_description == "Timing comparison unavailable: a scheduled time or real-time estimate is missing."
    assert first.unresolved_disruption_ids == [7999]
    assert second.departure.scheduled_departure_utc.isoformat() == "2026-11-25T06:05:00+00:00"
    assert second.departure.estimated_departure_utc.isoformat() == "2026-11-25T06:07:00+00:00"
    assert second.departure.platform_number == "10"
    assert second.departure.route_name == "Lilydale" and second.departure.destination_name == "Ringwood"
    assert second.delay_seconds == 120 and second.delay_status == "late"
    assert second.delay_description == "Estimated 120 seconds late relative to the schedule."
    assert second.passenger_delay_description == "About 2 minutes late (estimated)."
    assert [notice.disruption_id for notice in second.associated_disruptions] == [7001]
    assert [notice.disruption_id for notice in result.contextual_notices] == [7002]
    assert [notice.disruption_id for notice in result.route_notices[2101]] == [7003]
    assert transport.match_counts == (1, 1, 1, 1, 1, 1)


@pytest.mark.parametrize("run, status, reason", [(A, "served", "requested_matches_found"),
                                                (B, "not_served", "no_confirmed_matches_within_search")])
def test_one_run_matching_or_confirmed_nonmatching(run, status, reason):
    data = fixture()
    select(data, run)
    result, _ = execute(data, limit=1)
    assert result.status == "ok" and result.reason == reason
    assert result.candidates[0].verification.status == status
    assert len(result.matches) == (1 if status == "served" else 0)


def test_only_requested_number_of_matches_returned_but_all_outcomes_remain_visible():
    data = fixture()
    select(data, A, C)
    result, _ = execute(data, limit=1)
    assert [row.departure.run_ref for row in result.matches] == [C]
    assert result.metadata.confirmed_matches == 2 and result.metadata.matches_returned == 1
    assert len(result.candidates) == 2


def test_names_never_create_a_destination_match():
    data = fixture()
    select(data, B)
    source(data)["routes"]["2102"]["route_name"] = "Blackburn"
    source(data)["runs"][B]["destination_name"] = "Blackburn"
    result, _ = execute(data)
    assert result.matches == [] and result.candidates[0].verification.status == "not_served"


def test_destination_before_boarding_does_not_match():
    data = fixture()
    select(data, B)
    payload = pattern(data, B)["response"]["body"]
    earlier = deepcopy(next(row for row in payload["departures"] if row["stop_id"] == ORIGIN))
    earlier.update(stop_id=DESTINATION, departure_sequence=150, scheduled_departure_utc="2026-11-25T06:01:00Z")
    payload["departures"].append(earlier)
    payload["stops"][str(DESTINATION)] = {"stop_id": DESTINATION, "stop_name": "Blackburn", "route_type": 0}
    result, _ = execute(data)
    assert result.matches == []
    assert result.candidates[0].verification.reason == "destination_not_after_boarding"
    assert result.metadata.confirmed_non_matches == 1


@pytest.mark.parametrize("problem", ["missing", "incomplete", "malformed", "http"])
def test_pattern_failures_and_incomplete_patterns_remain_unknown(problem):
    data = fixture()
    select(data, A)
    response = pattern(data, A)["response"]
    if problem == "missing":
        response["body"] = {}
    elif problem == "incomplete":
        response["body"]["departures"][0].pop("departure_sequence")
    elif problem == "malformed":
        response["body"]["departures"][0]["departure_sequence"] = "900"
    else:
        response.update(status_code=503, body={"message": "Synthetic failure"})
    result, _ = execute(data)
    assert result.matches == [] and result.status == "partial"
    assert result.reason == "no_confirmed_matches_unverifiable"
    assert result.metadata.confirmed_non_matches == 0 and result.metadata.unknown_verifications == 1


def test_partial_pattern_failure_keeps_other_confirmed_services_and_uncertainty():
    data = fixture()
    select(data, A, D)
    result, _ = execute(data, limit=1)
    assert [row.departure.run_ref for row in result.matches] == [A]
    assert result.status == "partial" and result.reason == "requested_matches_found"
    assert result.metadata.unknown_verifications == 1 and result.metadata.pattern_requests == 2


def test_no_confirmed_matches_with_unknowns_is_not_a_confirmed_absence():
    data = fixture()
    select(data, B, D)
    result, _ = execute(data)
    assert result.matches == [] and result.reason == "no_confirmed_matches_unverifiable"
    assert result.metadata.confirmed_non_matches == result.metadata.unknown_verifications == 1
    assert result.status == "partial"


def test_more_requested_matches_than_candidate_window_finds_is_explicit():
    result, transport = execute(fixture(), limit=3, candidate_limit=2)
    assert result.metadata.candidates_examined == 2 and result.metadata.pattern_requests == 2
    assert result.metadata.candidate_limit_reached and result.metadata.search_limit_reached
    assert result.reason == "fewer_confirmed_matches_within_search"
    assert len(result.matches) == 1
    assert transport.match_counts[4:] == (0, 0)


@pytest.mark.parametrize("budget", [0, 1, 2])
def test_pattern_request_budget_counts_failures_and_preserves_unknowns(budget):
    result, transport = execute(fixture(), max_pattern_requests=budget)
    assert result.metadata.pattern_requests == budget
    assert sum(transport.match_counts[2:]) == budget
    assert result.metadata.pattern_request_limit_reached and result.metadata.search_limit_reached
    assert result.metadata.pattern_budget_skips == 4 - budget
    assert result.metadata.unknown_verifications >= 4 - budget
    assert result.status == "partial"


def test_failed_request_consumes_budget_so_later_candidate_is_not_queried():
    data = fixture()
    select(data, A, D)
    result, transport = execute(data, max_pattern_requests=1)
    assert result.metadata.pattern_requests == 1 and result.metadata.patterns_fetched == 0
    assert result.metadata.unknown_verifications == 2
    assert transport.match_counts[2] == 0 and transport.match_counts[5] == 1


def change_a_time(data, scheduled, estimated):
    record = next(row for row in source(data)["departures"] if row["run_ref"] == A)
    record.update(scheduled_departure_utc=scheduled, estimated_departure_utc=estimated)
    entry = pattern(data, A)
    entry["request"]["query"]["date_utc"] = scheduled.replace("Z", "+00:00") if scheduled else "unused"
    for row in entry["response"]["body"]["departures"]:
        if row["stop_id"] == ORIGIN:
            row["scheduled_departure_utc"] = scheduled
        elif row["departure_sequence"] < 200:
            row["scheduled_departure_utc"] = "2026-11-25T05:57:00Z"


def test_past_schedule_with_future_estimate_is_still_upcoming():
    data = fixture()
    select(data, A)
    change_a_time(data, "2026-11-25T06:00:00Z", "2026-11-25T06:03:00Z")
    result, _ = execute(data)
    assert len(result.matches) == 1 and result.matches[0].time_source == "estimated"
    assert result.matches[0].best_departure_utc.isoformat() == "2026-11-25T06:03:00+00:00"
    assert result.matches[0].departure.scheduled_departure_utc.isoformat() == "2026-11-25T06:00:00+00:00"
    assert result.checked_at_utc.isoformat() == "2026-11-25T06:01:59+00:00"


@pytest.mark.parametrize("scheduled, estimated", [
    ("2026-11-25T06:00:00Z", None),
    ("2026-11-25T06:00:00Z", "2026-11-25T06:01:00Z"),
    ("2026-11-25T06:05:00Z", "2026-11-25T06:01:00Z"),
])
def test_past_best_time_is_excluded_without_pattern_lookup(scheduled, estimated):
    data = fixture()
    select(data, A)
    change_a_time(data, scheduled, estimated)
    result, transport = execute(data)
    assert result.matches == [] and result.reason == "no_upcoming_candidates"
    assert result.metadata.past_excluded == 1 and result.metadata.pattern_requests == 0
    assert result.candidates[0].exclusion_reason == "past_departure"
    assert sum(transport.match_counts[2:]) == 0


def test_departure_that_becomes_past_during_retrieval_is_not_returned():
    data = fixture()
    select(data, A)
    times = iter([datetime(2026, 11, 25, 6, 1, 59, tzinfo=timezone.utc),
                  datetime(2026, 11, 25, 6, 8, tzinfo=timezone.utc)])
    result, _ = execute(data, clock=lambda: next(times))
    assert result.matches == [] and result.metadata.past_excluded == 1
    assert result.candidates[0].verification.status == "served"  # Membership remains true, eligibility changed.
    assert result.candidates[0].upcoming_status == "past"


def test_equal_reference_time_is_eligible_and_offset_clock_is_normalized():
    data = fixture()
    select(data, C)
    result, _ = execute(data, clock=lambda: datetime.fromisoformat("2026-11-25T17:04:00+11:00"))
    assert len(result.matches) == 1 and result.checked_at_utc.tzinfo is timezone.utc


def test_identical_duplicate_runs_reuse_pattern_without_inventing_extra_trains():
    data = fixture()
    select(data, A)
    source(data)["departures"] *= 2
    result, transport = execute(data, max_pattern_requests=1)
    assert len(result.matches) == 1 and result.metadata.confirmed_matches == 1
    assert result.metadata.duplicates_excluded == 1 and result.metadata.candidates_examined == 2
    assert result.metadata.pattern_requests == result.metadata.pattern_cache_hits == 1
    assert transport.match_counts[2] == 1
    assert result.metadata.pattern_budget_skips == 0


def test_failed_patterns_are_cached_within_the_request_without_retries():
    data = fixture()
    select(data, D)
    source(data)["departures"] *= 2
    result, transport = execute(data)
    assert result.metadata.pattern_requests == result.metadata.pattern_cache_hits == 1
    assert result.metadata.patterns_fetched == 0 and result.metadata.unknown_verifications == 1
    assert transport.match_counts[5] == 1


def test_conflicting_duplicate_records_remain_unknown():
    data = fixture()
    select(data, A)
    second = deepcopy(source(data)["departures"][0])
    second["platform_number"] = "99"
    source(data)["departures"].append(second)
    result, _ = execute(data)
    assert result.matches == [] and result.metadata.unknown_verifications == 2
    assert all(row.verification.reason == "conflicting_duplicate_departures" for row in result.candidates)
    assert result.metadata.pattern_requests == 1 and result.metadata.pattern_cache_hits == 1


@pytest.mark.parametrize("second_time", ["2026-11-26T06:05:00Z", "2026-11-25T06:06:00Z"])
def test_same_run_on_another_date_or_occurrence_uses_a_different_pattern(second_time):
    data = fixture()
    select(data, A)
    second = deepcopy(source(data)["departures"][0])
    second.update(scheduled_departure_utc=second_time, estimated_departure_utc=None)
    source(data)["departures"].append(second)
    entry = deepcopy(pattern(data, A))
    entry["request"]["query"]["date_utc"] = second_time.replace("Z", "+00:00")
    offset = datetime.fromisoformat(second_time) - datetime.fromisoformat("2026-11-25T06:05:00Z")
    for row in entry["response"]["body"]["departures"]:
        row["scheduled_departure_utc"] = (datetime.fromisoformat(row["scheduled_departure_utc"]) + offset).isoformat()
    data["responses"].append(entry)
    result, transport = execute(data)
    assert len(result.matches) == 2 and result.metadata.pattern_requests == 2
    assert result.metadata.pattern_cache_hits == 0 and transport.match_counts[-1] == 1


def test_cache_is_not_shared_between_separate_searches():
    data = fixture()
    select(data, A)
    for _ in range(2):
        result, transport = execute(deepcopy(data))
        assert result.metadata.pattern_requests == 1 and result.metadata.pattern_cache_hits == 0
        assert transport.match_counts[2] == 1


@pytest.mark.parametrize("missing", ["run_ref", "scheduled_departure_utc", "both_times", "origin"])
def test_unverifiable_source_fields_are_not_nonmatches_and_cost_no_pattern_request(missing):
    data = fixture()
    select(data, A)
    row = source(data)["departures"][0]
    if missing == "both_times":
        row.update(scheduled_departure_utc=None, estimated_departure_utc=None)
    elif missing == "origin":
        row["stop_id"] = 9999
    else:
        row[missing] = None
    result, _ = execute(data)
    assert result.metadata.unknown_verifications == 1 and result.metadata.confirmed_non_matches == 0
    assert result.metadata.pattern_requests == 0 and result.matches == []


def test_empty_success_is_distinct_from_source_api_failure():
    data = fixture()
    source(data)["departures"] = []
    result, _ = execute(data)
    assert result.status == "ok" and result.reason == "no_candidates_returned"
    assert result.metadata.candidates_received == 0


@pytest.mark.parametrize("failure", ["departures", "route_fallback"])
def test_failed_source_board_is_unavailable_not_a_successful_empty_search(failure):
    data = fixture()
    data["responses"][0 if failure == "departures" else 1]["response"] = {"status_code": 503, "body": {"message": "Unavailable"}}
    result, transport = execute(data)
    assert result.status == "unavailable" and result.reason == "departure_board_unavailable"
    assert result.metadata.candidates_received is None and result.metadata.candidates_examined == 0
    assert result.matches == [] and sum(transport.match_counts[2:]) == 0


def test_invalid_source_estimate_is_not_silently_replaced_with_the_schedule():
    data = fixture()
    select(data, A)
    source(data)["departures"][0]["estimated_departure_utc"] = "not-a-timestamp"
    result, transport = execute(data)
    assert result.status == "unavailable" and result.metadata.candidates_received is None
    assert sum(transport.match_counts[2:]) == 0


def test_errors_and_logs_never_expose_credentials_or_authenticated_urls(caplog):
    data = fixture()
    select(data, D)
    config = FixtureTransport(data).config
    pattern(data, D)["response"]["body"] = {"message": f"{config.api_key} {config.dev_id} {config.base_url}/v3/test?signature=secret"}
    with caplog.at_level("DEBUG"):
        result, _ = execute(data)
    diagnostic = result.model_dump_json() + caplog.text
    for secret in (config.api_key, config.dev_id, config.base_url, "signature="):
        assert secret not in diagnostic


@pytest.mark.parametrize("arguments", [
    {"origin_stop_id": 0}, {"origin_stop_id": True}, {"destination_stop_id": -1}, {"destination_stop_id": "9002"},
    {"limit": 0}, {"limit": 11}, {"limit": True}, {"candidate_limit": 0}, {"candidate_limit": 51},
    {"max_pattern_requests": -1}, {"max_pattern_requests": 21}, {"max_pattern_requests": True},
])
def test_invalid_parameters_fail_before_credentials_or_http(arguments):
    with pytest.raises(ValueError):
        service.get_departures_to_destination(**{"origin_stop_id": ORIGIN, "destination_stop_id": DESTINATION, **arguments})


@pytest.mark.parametrize("now", [datetime(2026, 11, 25), "2026-11-25T06:00:00Z"])
def test_clock_must_return_an_aware_datetime_before_any_http(now):
    with pytest.raises(ValueError, match="timezone-aware"):
        service.get_departures_to_destination(ORIGIN, DESTINATION, clock=lambda: now)


def test_backwards_clock_cannot_produce_a_successful_recommendation():
    data = fixture()
    select(data, A)
    times = iter([datetime(2026, 11, 25, 6, 1, 59, tzinfo=timezone.utc),
                  datetime(2026, 11, 25, 6, 1, 58, tzinfo=timezone.utc)])
    with pytest.raises(ValueError, match="backwards"):
        execute(data, clock=lambda: next(times))
