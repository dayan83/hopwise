"""Minimal Hopwise station-search interface."""

import streamlit as st

from ptv import PTVRequestError, search_stations


def main():
    st.set_page_config(page_title="Hopwise")
    st.title("Hopwise")
    st.write("Find Melbourne train stations and select a station to get started.")

    st.session_state.setdefault("station_results", [])
    st.session_state.setdefault("selected_stop_id", None)
    st.session_state.setdefault("search_complete", False)

    with st.form("station_search"):
        query = st.text_input("Melbourne train station", placeholder="e.g. Richmond")
        submitted = st.form_submit_button("Search")

    if submitted:
        # A new search invalidates the previous results and selection, even if it fails.
        st.session_state.station_results = []
        st.session_state.selected_stop_id = None
        st.session_state.search_complete = False
        st.session_state.pop("station_choice", None)
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


if __name__ == "__main__":
    main()
