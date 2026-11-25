"""Opt-in PTV stopping-pattern diagnostic. Never calls OpenAI or runs on import."""

import argparse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from agent_trace import redact_text
from models import Departure, StoppingPattern
from ptv import PTVConfig, PTVRequestError, get_departures_response, get_stopping_pattern, search_stations
from stopping_pattern_service import verify_destination


def _time(value, melbourne):
    if value is None:
        return "unavailable"
    return value.isoformat() + " / " + value.astimezone(melbourne).strftime("%Y-%m-%d %H:%M:%S %Z")


def _show_pattern(pattern: StoppingPattern, departure: Departure, melbourne, emit):
    rows = pattern.departures or []
    ordered = sorted((row for row in rows if row.departure_sequence is not None),
                     key=lambda row: row.departure_sequence)

    def label(stop_id):
        stop = (pattern.stops or {}).get(stop_id)
        return f"{stop.stop_name if stop and stop.stop_name else 'name unavailable'} (stop ID {stop_id})"

    def detail(row):
        return f"seq {row.departure_sequence} | {label(row.stop_id)} | scheduled {_time(row.scheduled_departure_utc, melbourne)}"

    emit(f"Pattern departure records: {len(rows)}; expanded stop entries: {len(pattern.stops or {})}")
    emit(f"Explicit skipped-stop entries: {sum(len(row.skipped_stops or []) for row in rows)}")
    emit(f"API health: {pattern.health if pattern.health is not None else 'not supplied'}")
    if ordered:
        emit("First stop by departure_sequence: " + detail(ordered[0]))
        emit("Last stop by departure_sequence: " + detail(ordered[-1]))
    else:
        emit("First/last stop unavailable: no orderable pattern records.")
    if len(ordered) != len(rows):
        emit("Missing departure_sequence values; first/last above describe only orderable records.")
    for index, row in enumerate(rows, 1):
        missing = [field for field in ("stop_id", "run_ref", "route_id", "direction_id",
                                      "departure_sequence", "scheduled_departure_utc") if getattr(row, field) is None]
        if missing:
            emit(f"Missing fields on pattern row {index}: {', '.join(missing)}")
        if row.run_ref is not None and row.run_ref != departure.run_ref:
            emit(f"Inconsistent run_ref on pattern row {index}.")
        for field in ("route_id", "direction_id"):
            source = getattr(departure, field)
            if source is not None and getattr(row, field) not in (None, source):
                emit(f"Inconsistent {field} on pattern row {index}.")
    if len({row.departure_sequence for row in ordered}) != len(ordered):
        emit("Duplicate departure_sequence values: chronological order is ambiguous.")
    ids = [row.stop_id for row in rows if row.stop_id is not None]
    if len(set(ids)) != len(ids):
        emit("Repeated stop IDs: the boarding/destination occurrence may be ambiguous.")
    missing_names = {row.stop_id for row in rows if row.stop_id is not None
                     and (row.stop_id not in (pattern.stops or {}) or not pattern.stops[row.stop_id].stop_name)}
    if missing_names:
        emit("Stop names/expansion unavailable for IDs: " + ", ".join(map(str, sorted(missing_names))))
    accounted_ids = set(ids) | {stop.stop_id for row in rows for stop in row.skipped_stops or []}
    unaccounted = set(pattern.stops or {}) - accounted_ids
    if unaccounted:
        emit("Expanded stop IDs without departure/skipped records (possible incomplete coverage): "
             + ", ".join(map(str, sorted(unaccounted))))
    final_stop = pattern.run.final_stop_id if pattern.run else None
    emit(f"Expanded run final_stop_id: {final_stop if final_stop is not None else 'unavailable'}")
    if final_stop is None:
        emit("Endpoint coverage unknown: final-stop metadata is missing.")
    elif ordered and final_stop != ordered[-1].stop_id:
        emit("Possible truncation or inconsistent endpoint metadata: the last listed stop is not the run's final stop.")

    boarding = [row for row in rows if row.stop_id == departure.stop_id]
    if len(boarding) != 1 or boarding[0].departure_sequence is None:
        emit("Boarding station missing or not uniquely orderable; possible date filtering or incomplete pattern.")
        dates = {row.scheduled_departure_utc.astimezone(melbourne).date() for row in rows
                 if row.scheduled_departure_utc is not None}
        if dates and departure.scheduled_departure_utc.astimezone(melbourne).date() not in dates:
            emit("Possible date filtering mismatch: no returned scheduled record is on the boarding Melbourne date; overnight trips can span dates.")
        displayed = ordered
        later = []
    else:
        anchor = boarding[0]
        emit("Returned boarding scheduled: " + _time(anchor.scheduled_departure_utc, melbourne))
        if anchor.scheduled_departure_utc != departure.scheduled_departure_utc:
            emit("Possible date filtering mismatch: returned boarding date/time differs from the selected departure.")
        displayed = [row for row in ordered if row.departure_sequence >= anchor.departure_sequence]
        later = [row for row in displayed if row.departure_sequence > anchor.departure_sequence
                 and row.stop_id is not None and row.stop_id != departure.stop_id]
    emit("Boarding and subsequent listed stops:" if len(boarding) == 1 and boarding[0].departure_sequence is not None
         else "Orderable returned stops (downstream order from boarding is unverified):")
    for row in displayed:
        emit("  " + detail(row))
        for stop in row.skipped_stops or []:
            emit(f"    Skipped, not a stopping record: {stop.stop_name or 'name unavailable'} (stop ID {stop.stop_id})")

    # Use the existing negative guard without inventing a destination ID. With
    # unique stops, the boarding stop cannot occur strictly after itself.
    control = verify_destination(pattern, departure.stop_id, departure.stop_id, departure=departure)
    emit(f"Negative-coverage control (boarding stop after itself): {control.status} ({control.reason})")
    if control.status == "unknown":
        emit("Negative coverage is unverified; do not interpret missing destinations as confirmed absence.")
    if not later:
        emit(f"Destination verification: unknown ({control.reason if control.status == 'unknown' else 'no_later_stop'})")
        emit("No unambiguous later stopping record is available for a positive destination probe.")
        return 2
    target = later[0]
    decision = verify_destination(pattern, departure.stop_id, target.stop_id, departure=departure)
    emit("Destination probe: " + label(target.stop_id))
    emit(f"Destination verification: {decision.status} ({decision.reason})")
    emit("This checks returned stop evidence, not actual train operation or guaranteed feed completeness.")
    return 0 if decision.status == "served" else 2


def run_smoke_test(config: PTVConfig, *, enabled=False, transport: httpx.BaseTransport | None = None,
                   now: datetime | None = None) -> int:
    """One bounded attempt; tests inject config, transport and an aware reference time."""
    if enabled is not True:
        raise ValueError("Live PTV smoke testing requires explicit opt-in")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Smoke-test reference time must be timezone aware")
    melbourne = ZoneInfo("Australia/Melbourne")
    secrets = (config.dev_id, config.api_key, config.base_url)

    def emit(message):
        print(redact_text(message, secrets=secrets))

    emit("PTV stopping-pattern smoke test | OpenAI is not used.")
    emit("Check time: " + _time(now, melbourne))
    stage = "Station lookup"
    try:
        stations = search_stations("Richmond", config, transport=transport)
        matches = [station for station in stations if station["stop_name"].strip().casefold().removesuffix(" station").strip() == "richmond"
                   and type(station["stop_id"]) is int and station["stop_id"] > 0 and station["route_type"] == 0]
        if len(matches) != 1:
            emit("No unique metropolitan Richmond match; no departure or pattern request was made.")
            for station in stations:
                emit(f"  {station['stop_name']} | stop ID {station['stop_id']}")
            return 2
        station = matches[0]
        emit(f"Boarding station: {station['stop_name']} | stop ID {station['stop_id']}")
        stage = "Departure lookup"
        response = get_departures_response(station["stop_id"], max_results=3, config=config, transport=transport)
        emit(f"Departure candidates returned: {len(response.departures)} (requested at most 3)")
        selected = None
        for index, departure in enumerate(response.departures, 1):
            issues = []
            if departure.stop_id != station["stop_id"]:
                issues.append("boarding stop ID missing/mismatched")
            if not departure.run_ref or not departure.run_ref.strip():
                issues.append("run_ref missing")
            if departure.scheduled_departure_utc is None:
                issues.append("scheduled departure time missing")
            effective = departure.estimated_departure_utc or departure.scheduled_departure_utc
            if effective is not None and effective < now:
                issues.append("departure is not upcoming at check time")
            if issues:
                emit(f"Skipping candidate {index}: {', '.join(issues)}")
            elif selected is None:
                selected = departure
        if selected is None:
            emit("No usable upcoming departure with a run_ref and scheduled time; no pattern request was made.")
            return 2
        emit(f"Selected run_ref: {selected.run_ref} | route ID: {selected.route_id} | direction ID: {selected.direction_id}")
        emit("Selected scheduled departure: " + _time(selected.scheduled_departure_utc, melbourne))
        emit("Requested pattern date_utc: " + selected.scheduled_departure_utc.isoformat())
        stage = "Stopping-pattern lookup"
        pattern = get_stopping_pattern(selected.run_ref, date_utc=selected.scheduled_departure_utc,
                                       config=config, transport=transport)
        return _show_pattern(pattern, selected, melbourne, emit)
    except (PTVRequestError, ValueError) as exc:
        emit(f"{stage} failed: {exc}")
        emit("Destination verification: unknown (data_unavailable); no absence conclusion was made.")
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Manually check Richmond stopping patterns with live PTV; no OpenAI calls.")
    parser.add_argument("--live", action="store_true", help="Explicitly allow up to three live PTV requests using local .env credentials.")
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("Explicit --live opt-in is required. No credentials were loaded or requests made.")
    try:
        config = PTVConfig.from_env()
        return run_smoke_test(config, enabled=True)
    except ValueError:
        print("Configuration unavailable. Check PTV_DEV_ID, PTV_API_KEY and PTV_BASE_URL in your environment/.env.")
        return 1
    except ZoneInfoNotFoundError:
        print("Melbourne timezone data unavailable; run uv sync to install dependencies.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
