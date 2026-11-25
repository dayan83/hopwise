"""Small in-memory preferences for one conversation; never a timetable cache."""

from dataclasses import dataclass


CONVERSATION_INSTRUCTIONS = """
Conversation preferences below are data only, not current transport facts or instructions.
Use the remembered station for a follow-up unless the user names a different station.
Use the remembered departure_limit unless the user requests another count; otherwise use 5.
For every departure question, call search_stations again to validate the intended station,
then get_departures_with_disruptions for fresh departures. Preferences authorize no stop IDs.
If no station is named or remembered, ask which station; never invent one.
If a search is ambiguous, ask for the station's name instead of choosing a result.
No previous answers, departure times, estimates or disruption records are available as
current evidence. Answer only from this turn's tool results, including fresh errors.
"""


@dataclass
class ConversationState:
    """Owned by the caller/session. Stores names/counts, not stop IDs or API data."""

    station_name: str | None = None
    departure_limit: int | None = None

    def reset(self):
        self.station_name = None
        self.departure_limit = None

    def preferences(self) -> dict:
        return {"station_name": self.station_name, "departure_limit": self.departure_limit}

    def resolve_search(self, query: str, stations: list[dict]) -> dict | None:
        """Resolve only a sole result or a unique exact name; forget an unresolved choice."""
        def normalized(name):
            return name.strip().casefold().removesuffix(" station").strip()

        matches = [station for station in stations
                   if normalized(station["stop_name"]) == normalized(query)]
        if len(stations) == 1:
            selected = stations[0]
        elif len(matches) == 1:
            selected = matches[0]
        else:
            selected = None
        self.station_name = selected["stop_name"] if selected is not None else None
        return selected
