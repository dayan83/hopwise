from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from models import Departure, Disruption


def test_departure_preserves_identifiers_and_aware_times():
    departure = Departure.model_validate({
        "stop_id": 1, "route_id": 9, "run_ref": "run-A", "direction_id": 0,
        "scheduled_departure_utc": "2026-11-17T11:00:00+11:00",
        "estimated_departure_utc": None, "platform_number": "2", "disruption_ids": [101],
    })
    assert (departure.stop_id, departure.route_id, departure.run_ref, departure.direction_id) == (1, 9, "run-A", 0)
    assert departure.scheduled_departure_utc == datetime(2026, 11, 17, tzinfo=timezone.utc)
    assert departure.scheduled_departure_utc.tzinfo is timezone.utc
    assert departure.estimated_departure_utc is None
    assert departure.platform_number == "2"
    assert departure.disruption_ids == [101]


def test_missing_association_information_is_different_from_empty_ids():
    assert Departure().disruption_ids is None
    assert Departure(disruption_ids=[]).disruption_ids == []
    assert Departure(estimated_departure_utc="").estimated_departure_utc is None


@pytest.mark.parametrize("data", [
    {"scheduled_departure_utc": "2026-11-17T00:00:00"},
    {"estimated_departure_utc": datetime(2026, 11, 17)},
    {"scheduled_departure_utc": 1234567890},
    {"route_id": True}, {"disruption_ids": ["101"]},
])
def test_invalid_types_and_naive_times_rejected(data):
    with pytest.raises(ValidationError):
        Departure.model_validate(data)


def test_disruption_keeps_original_status_and_optional_data():
    notice = Disruption.model_validate({
        "disruption_id": 101, "disruption_status": "Planned", "to_date": None,
        "from_date": "2026-11-18T00:00:00Z",
        "routes": [{"route_id": 9, "direction": {"direction_id": 0}}],
    })
    assert notice.disruption_status == "Planned"
    assert notice.to_date is None
    assert notice.from_date.tzinfo is timezone.utc
    assert notice.routes[0]["direction"]["direction_id"] == 0
    assert notice.title is None
