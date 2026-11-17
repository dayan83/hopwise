"""Hopwise station search and upcoming departures."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import streamlit as st

from departure_service import departure_delay, get_departures_with_disruptions
from models import Departure, DepartureBoard, DepartureWithDisruptions, Disruption
from ptv import PTVRequestError, search_stations


def _clear_departures():
    st.session_state.departures_stop_id = None
    st.session_state.departure_response = None
    st.session_state.departures_refreshed_at = None
    st.session_state.departures_error = None


def _local_time(value: datetime | None, melbourne: ZoneInfo) -> str:
    return "Not available" if value is None else value.astimezone(melbourne).strftime("%Y-%m-%d %H:%M:%S %Z")


def _timing(departure: Departure) -> str:
    if departure.estimated_departure_utc is None:
        return "No estimate available"
    delay = departure_delay(departure)
    if delay is None:
        return "Delay unavailable (no scheduled time)"
    seconds = delay.total_seconds()
    if seconds == 0:
        return "On time (estimated)"
    magnitude = abs(seconds)
    duration = f"{magnitude:g} sec" if magnitude < 60 else f"{magnitude / 60:g} min"
    return f"{duration} {'late' if seconds > 0 else 'early'} (estimated)"


def _notice_summary(entry: DepartureWithDisruptions) -> str:
    parts = [f"Notice {notice.disruption_id}" for notice in entry.associated_disruptions]
    if entry.unresolved_disruption_ids:
        parts.append("Details unavailable for IDs " + ", ".join(map(str, entry.unresolved_disruption_ids)))
    if parts:
        return "; ".join(parts)
    return ("Association data not supplied" if entry.departure.disruption_ids is None
            else "No linked notices listed")


def _show_notice(notice: Disruption, melbourne: ZoneInfo, contexts: list[str] | None = None):
    label = f"Notice {notice.disruption_id}" if notice.disruption_id is not None else "Notice without an ID"
    with st.expander(f"{label}: {notice.title or 'Title not supplied'}", expanded=False):
        if contexts:
            st.caption("Returned in: " + "; ".join(contexts))
        st.write("PTV status: " + (notice.disruption_status or "Not supplied"))
        st.write("Type: " + (notice.disruption_type or "Not supplied"))
        st.write(notice.description or "Description not supplied.")
        end = _local_time(notice.to_date, melbourne) if notice.to_date is not None else "Unknown"
        st.caption(f"From: {_local_time(notice.from_date, melbourne)} | To: {end}")
        st.caption("Last updated: " + _local_time(notice.last_updated, melbourne))
        if notice.source_groups:
            st.caption("PTV groups: " + ", ".join(notice.source_groups))
        if notice.url:
            st.write("Source: " + notice.url)


def _show_disruptions(board: DepartureBoard, melbourne: ZoneInfo):
    # Deduplicate presentation only; the service has already determined every association.
    linked = {}
    for entry in board.departures:
        for notice in entry.associated_disruptions:
            linked.setdefault(notice.disruption_id, notice)
    if linked:
        st.subheader("Linked disruption notices")
        st.caption("Use the notice IDs in each departure row to find details below. Shared notices are shown once.")
        for notice in linked.values():
            _show_notice(notice, melbourne)

    contextual = {}
    sources = [("Departures response", board.contextual_notices)]
    sources.extend((f"Route {route_id}", notices) for route_id, notices in board.route_notices.items())
    for context, notices in sources:
        for notice in notices:
            if notice.disruption_id is not None and notice.disruption_id in linked:
                continue
            # Missing IDs cannot establish that two notices are the same record.
            key = notice.disruption_id if notice.disruption_id is not None else (context, id(notice))
            if key not in contextual:
                contextual[key] = (notice, [])
            if context not in contextual[key][1]:
                contextual[key][1].append(context)
    if contextual:
        st.subheader("Other route/station notices")
        st.caption("These notices are context only; no direct association to a displayed departure was supplied.")
        for notice, contexts in contextual.values():
            _show_notice(notice, melbourne, contexts)


def _show_departures(stop_id: int):
    st.subheader("Upcoming departures")
    try:
        melbourne = ZoneInfo("Australia/Melbourne")
    except ZoneInfoNotFoundError:
        st.error("Melbourne timezone data is missing. Run uv sync to install dependencies.")
        return

    changed_station = st.session_state.departures_stop_id != stop_id
    if changed_station:
        _clear_departures()
        st.session_state.departures_stop_id = stop_id
    refresh = st.button("Refresh departures", key="refresh_departures")
    if changed_station or refresh:
        try:
            with st.spinner("Loading departures and notices..."):
                response = get_departures_with_disruptions(stop_id)
            st.session_state.departure_response = response
            st.session_state.departures_refreshed_at = datetime.now(timezone.utc)
            st.session_state.departures_error = None
        except (ValueError, PTVRequestError) as exc:
            st.session_state.departures_error = f"Unable to load departures: {exc}"

    if st.session_state.departures_error:
        st.error(st.session_state.departures_error)
        if st.session_state.departure_response is not None:
            st.warning("Showing previously fetched departures and notices; the latest refresh failed.")
    response = st.session_state.departure_response
    if response is None:
        return
    st.caption("Last refreshed: " + _local_time(st.session_state.departures_refreshed_at, melbourne))
    if not response.departures:
        st.info("No upcoming departures returned for this station.")
        return
    rows = [{
        "Scheduled": _local_time(entry.departure.scheduled_departure_utc, melbourne),
        "Estimated": (_local_time(entry.departure.estimated_departure_utc, melbourne)
                      if entry.departure.estimated_departure_utc is not None else "No estimate available"),
        "Route": entry.departure.route_name or (
            f"Route {entry.departure.route_id}" if entry.departure.route_id is not None else "Not provided"),
        "Destination": entry.departure.destination_name or "Not provided",
        "Platform": entry.departure.platform_number or "Not provided",
        "Timing": _timing(entry.departure),
        "Notices": _notice_summary(entry),
    } for entry in response.departures]
    st.caption("Times are in Australia/Melbourne. Timing compares PTV's estimate with the schedule.")
    st.dataframe(rows, hide_index=True)
    st.caption("Notice associations alone do not establish a train delay or its cause. Timing uses departure estimates.")
    _show_disruptions(response, melbourne)


def main():
    st.set_page_config(page_title="Hopwise")
    st.title("Hopwise")
    st.write("Find a Melbourne train station and view its upcoming departures.")

    st.session_state.setdefault("station_results", [])
    st.session_state.setdefault("selected_stop_id", None)
    st.session_state.setdefault("search_complete", False)
    if ("departures_stop_id" not in st.session_state
            or (st.session_state.departure_response is not None
                and not isinstance(st.session_state.departure_response, DepartureBoard))):
        # Discard cached responses from the earlier departure-only UI when hot reloading.
        _clear_departures()

    with st.form("station_search"):
        query = st.text_input("Melbourne train station", placeholder="e.g. Richmond")
        submitted = st.form_submit_button("Search")

    if submitted:
        # A new search invalidates the previous results and selection, even if it fails.
        st.session_state.station_results = []
        st.session_state.selected_stop_id = None
        st.session_state.search_complete = False
        st.session_state.pop("station_choice", None)
        _clear_departures()
        term = query.strip()
        if not term:
            st.warning("Enter a station name to search.")
        else:
            try:
                with st.spinner("Searching stations..."):
                    st.session_state.station_results = search_stations(term)
            except ValueError as exc:
                st.error(str(exc))
            except PTVRequestError as exc:
                st.error(f"Station search failed: {exc}")
            else:
                st.session_state.search_complete = True

    stations = st.session_state.station_results
    if st.session_state.search_complete and not stations:
        st.info("No matching metropolitan train stations found. Try another station name.")
    if stations:
        by_id = {station["stop_id"]: station for station in stations}

        def station_label(stop_id):
            station = by_id[stop_id]
            suburb = station["stop_suburb"] or "Suburb not specified"
            return f"{station['stop_name']} — {suburb} (stop ID: {stop_id})"

        selected = st.selectbox(
            "Select a station", options=list(by_id), index=None,
            format_func=station_label, key="station_choice",
            placeholder="Choose a matching station",
        )
        st.session_state.selected_stop_id = selected
        if selected is not None:
            st.success(f"Selected: {station_label(selected)}")
            _show_departures(selected)
        else:
            _clear_departures()


if __name__ == "__main__":
    main()
