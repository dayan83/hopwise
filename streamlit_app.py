"""Hopwise station search and upcoming departures."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import streamlit as st

from departure_service import departure_delay
from models import Departure
from ptv import PTVRequestError, get_departures_response, search_stations


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
            with st.spinner("Loading departures..."):
                response = get_departures_response(stop_id)
            st.session_state.departure_response = response
            st.session_state.departures_refreshed_at = datetime.now(timezone.utc)
            st.session_state.departures_error = None
        except (ValueError, PTVRequestError) as exc:
            st.session_state.departures_error = f"Unable to load departures: {exc}"

    if st.session_state.departures_error:
        st.error(st.session_state.departures_error)
        if st.session_state.departure_response is not None:
            st.warning("Showing previously fetched departures; the latest refresh failed.")
    response = st.session_state.departure_response
    if response is None:
        return
    st.caption("Last refreshed: " + _local_time(st.session_state.departures_refreshed_at, melbourne))
    if not response.departures:
        st.info("No upcoming departures returned for this station.")
        return
    rows = [{
        "Scheduled": _local_time(departure.scheduled_departure_utc, melbourne),
        "Estimated": (_local_time(departure.estimated_departure_utc, melbourne)
                      if departure.estimated_departure_utc is not None else "No estimate available"),
        "Route": departure.route_name or (
            f"Route {departure.route_id}" if departure.route_id is not None else "Not provided"),
        "Destination": departure.destination_name or "Not provided",
        "Platform": departure.platform_number or "Not provided",
        "Timing": _timing(departure),
    } for departure in response.departures]
    st.caption("Times are in Australia/Melbourne. Timing compares PTV's estimate with the schedule.")
    st.dataframe(rows, hide_index=True)


def main():
    st.set_page_config(page_title="Hopwise")
    st.title("Hopwise")
    st.write("Find a Melbourne train station and view its upcoming departures.")

    st.session_state.setdefault("station_results", [])
    st.session_state.setdefault("selected_stop_id", None)
    st.session_state.setdefault("search_complete", False)
    if "departures_stop_id" not in st.session_state:
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
