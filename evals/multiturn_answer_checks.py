"""Narrow final-answer coverage checks, independent of tool execution and calculations.

Recognizes labelled service blocks/list items and simple Markdown tables.
Unrecognized wording requires review; parsed contradictory facts fail verification.
These checks never assign manual factual-accuracy, grounding or relevance scores.
"""

from datetime import datetime
import re
from zoneinfo import ZoneInfo


_CLOCK = r"(?<![\d:])(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?:\s*[ap]m)?(?![\d:])"
_LABELS = {"estimated": r"(?:estimated|estimate|est\.?)", "scheduled": r"(?:scheduled|sched\.?)"}
_ZONE = r"(?:(?:UTC|GMT)?[+-]\d{2}:?\d{2}|(?-i:[A-Z]{1,5})\b)"
_STAMP = r"(?:\b\d{4}-\d{2}-\d{2}[ T])?" + _CLOCK + r"(?:\s*" + _ZONE + ")?"
_LIST_ITEM = re.compile(r"^(\s*)(?:\d+[.)]|[-+])\s+")
_NOTICE_HEADING = re.compile(r"^(?:#{1,6}\s*)?(?:(?:directly associated|broader contextual|contextual|route|general|service)\s+)?(?:disruption\s+)?notices?:\s*$", re.I)


def _parts(text):
    values = []
    for stamp in re.finditer(_STAMP, text, re.I):
        match = re.search(_CLOCK, stamp.group(), re.I)
        value = re.sub(r"\s+", "", match.group()).lower()
        period = value[-2:] if value.endswith(("am", "pm")) else None
        if period:
            value = value[:-2]
        parts = [int(part) for part in value.split(":")]
        hour, minute = parts[:2]
        if period:
            if not 1 <= hour <= 12:
                continue
            hour = hour % 12 + (12 if period == "pm" else 0)
        date = re.match(r"\d{4}-\d{2}-\d{2}", stamp.group())
        zone = stamp.group()[match.end():].strip()
        values.append({"time": f"{hour:02}:{minute:02}:{parts[2] if len(parts) == 3 else 0:02}",
                       "date": date.group() if date else None, "zone": zone or None})
    return values


def _times(text):
    return [value["time"] for value in _parts(text)]


def _labelled_parts(text, label):
    shared = re.search(r"\b(?:estimated\s+and\s+scheduled|scheduled\s+and\s+estimated)\s*(?:(?:at|:)\s*)?(" + _STAMP + ")", text, re.I)
    if shared:
        return _parts(shared.group(1))
    name = _LABELS[label]
    suffix = r"(?:\s+(?:at|time|departure|is)){0,2}\s*[:=\u2013\u2014-]?\s*"
    forward = re.findall(r"\b" + name + suffix + "(" + _STAMP + ")", text, re.I)
    # In "estimated 10:00 (scheduled 09:59)", scheduled labels 09:59, not 10:00.
    backward = re.findall("(" + _STAMP + r")\s*[([]?\s*" + name
                          + r"\b(?!" + suffix + _STAMP + ")", text, re.I)
    values = []
    for match in forward + backward:
        for value in _parts(match):
            if value not in values:
                values.append(value)
    return values


def _mentions(text, name):
    return bool(re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.I))


def _records(answer, destinations):
    records = []
    headers = None
    block = []
    indent = 0
    notice_section = False

    def flush():
        if not block:
            return
        text = " ".join(part.strip() for part in block)
        # List identities come from the heading, never from a notice in its body.
        identity = block[0].strip()
        if (re.search(r"\bplatform\b", text, re.I)
                or any(_mentions(identity, name) for name in destinations)
                or _times(text)):
            records.append({"text": text, "destination": identity,
                            "estimated": _labelled_parts(text, "estimated"),
                            "scheduled": _labelled_parts(text, "scheduled"),
                            "platform": re.findall(r"\b(?:platform|plat\.?)\s*[:#]?\s*(\w+)", text, re.I)})
        block.clear()

    # Preserve star list markers while removing emphasis and inline-code marks.
    plain = "\n".join(re.sub(r"^(\s*)\* ", r"\1- ", line) for line in answer.splitlines())
    for raw in plain.translate(str.maketrans("", "", "*_`")).splitlines():
        line = raw.strip()
        if _NOTICE_HEADING.match(line):
            flush()
            notice_section = True
            continue
        if notice_section:
            if re.match(r"^(?:#{1,6}\s*)?(?:(?:next|upcoming|train)\s+)?departures\b.*:", line, re.I):
                notice_section = False
            else:
                continue
        item = _LIST_ITEM.match(raw)
        if block and raw[:1].isspace() and len(raw) - len(raw.lstrip()) > indent:
            block.append(raw)
            continue
        flush()
        if not line:
            continue
        if item:
            block.append(raw)
            indent = len(item.group(1))
            continue
        if "|" in line:
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            mapping = {}
            for index, cell in enumerate(cells):
                for field, pattern in {"destination": r"destination|^to$|route", "estimated": r"estim|^est\.?$",
                                       "scheduled": r"sched", "platform": r"platform|^plat\.?$"}.items():
                    if re.search(pattern, cell, re.I):
                        mapping[field] = index
            if "destination" in mapping and ("estimated" in mapping or "scheduled" in mapping):
                headers = mapping
                continue
            if headers is not None and _times(line) and max(headers.values()) < len(cells):
                records.append({"text": line, "destination": cells[headers["destination"]],
                                "estimated": _parts(cells[headers["estimated"]]) if "estimated" in headers else [],
                                "scheduled": _parts(cells[headers["scheduled"]]) if "scheduled" in headers else [],
                                "platform": [cells[headers["platform"]]] if "platform" in headers else []})
                continue
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            if _times(sentence) and (any(_mentions(sentence, name) for name in destinations)
                                     or re.search(r"\bplatform\b", sentence, re.I)):
                records.append({"text": sentence, "destination": sentence,
                                "estimated": _labelled_parts(sentence, "estimated"),
                                "scheduled": _labelled_parts(sentence, "scheduled"),
                                "platform": re.findall(r"\b(?:platform|plat\.?)\s*[:#]?\s*(\w+)", sentence, re.I)})
    flush()
    # Keep unsupported time-bearing list items: an extra service must not vanish
    # just because its time labels/platform syntax were not understood.
    return records


def _local(timestamp):
    return datetime.fromisoformat(timestamp).astimezone(ZoneInfo("Australia/Melbourne"))


def _zone_minutes(zone):
    if zone in {"AEDT", "AEST", "UTC", "GMT", "Z"}:
        return {"AEDT": 660, "AEST": 600, "UTC": 0, "GMT": 0, "Z": 0}[zone]
    offset = re.fullmatch(r"(?:UTC|GMT)?([+-])(\d{2}):?(\d{2})", zone)
    if offset and int(offset[2]) < 24 and int(offset[3]) < 60:
        return (1 if offset[1] == "+" else -1) * (int(offset[2]) * 60 + int(offset[3]))
    return zone  # Unknown timezone syntax is reviewable, not silently discarded.


def check_answer(spec, answer, *, known_destinations=()):
    """Compare independently authored public facts in the final answer, not the trace."""
    checks = []

    def add(name, expected, actual, evidence=None, *, understood=True):
        status = "verified" if expected == actual else ("factual_mismatch" if understood else "unrecognized_format")
        checks.append({"id": name, "expected": expected, "actual": actual,
                       "passed": status == "verified", "status": status, "evidence": evidence})

    answer = answer if isinstance(answer, str) else ""
    expected = spec["expected_facts"].get("departures", [])
    if "expected_answer" in spec:
        add("clarification_contract", spec["expected_answer"], answer, understood=False)
    elif expected:
        station = spec["expected_state_after"]["station_name"]
        add("boarding_station_mentioned", True, _mentions(answer, station), understood=False)
        destinations = set(known_destinations) | {row["destination_name"] for row in expected}
        records = _records(answer, destinations)
        complete = bool(records) and all(record["estimated"] and record["scheduled"] and record["platform"] for record in records)
        add("recognized_service_count", len(expected), len(records), understood=complete)
        for row in expected:
            prefix = f"answer.{row['run_ref']}"
            matches = [record for record in records if _mentions(record["destination"], row["destination_name"])]
            add(prefix + ".unique_service_row", 1, len(matches), understood=complete)
            record = matches[0] if len(matches) == 1 else {}
            evidence = record.get("text")
            for field in ("scheduled", "estimated"):
                timestamp = row[field + "_departure_utc"]
                if timestamp is not None:
                    local = _local(timestamp)
                    parts = record.get(field, [])
                    add(prefix + "." + field + "_local", [local.strftime("%H:%M:%S")],
                        [part["time"] for part in parts], evidence, understood=bool(parts))
                    for index, part in enumerate(parts):
                        if part["date"] is not None:
                            add(prefix + f".{field}_date.{index}", local.strftime("%Y-%m-%d"), part["date"], evidence)
                        if part["zone"] is not None:
                            zone = _zone_minutes(part["zone"].upper())
                            add(prefix + f".{field}_timezone.{index}", int(local.utcoffset().total_seconds() / 60),
                                zone, evidence, understood=isinstance(zone, int))
                else:
                    add(prefix + ".no_invented_" + field, [], record.get(field, []), evidence)
                    add(prefix + ".estimate_unavailable_acknowledged", True,
                        bool(re.search(r"(?:estimate.*(?:unavailable|not available)|no (?:real.time )?estimate)", evidence or "", re.I)), evidence)
            if row.get("platform_number") is not None:
                add(prefix + ".platform", [row["platform_number"]], record.get("platform", []), evidence,
                    understood=bool(record.get("platform")))
    else:
        add("nonempty_answer", True, bool(answer.strip()))
    status = ("factual_mismatch" if any(item["status"] == "factual_mismatch" for item in checks)
              else "unrecognized_format" if any(not item["passed"] for item in checks) else "verified")
    return {"passed": status == "verified", "status": status, "checks": checks,
            "scope": "Recognized final-answer facts only; unrecognized formats require review. This is not complete factual accuracy or grounding.",
            "manual_review_required": True}
