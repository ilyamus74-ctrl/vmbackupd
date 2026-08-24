"""Calendar schedule for FULL + INCREMENTAL backup chains."""

# Architecture: NEW

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _require_time(value: str) -> tuple[int, int]:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise ValueError("schedule time must be HH:MM")
    hh, mm = value[:2], value[3:]
    if not hh.isdigit() or not mm.isdigit():
        raise ValueError("schedule time must be HH:MM")
    hour, minute = int(hh), int(mm)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("schedule time must be valid HH:MM")
    return hour, minute


def validate_chain_schedule(timezone_name: str, full_weekday: int, full_time: str,
                            incremental_times) -> dict:
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise ValueError("chain schedule requires timezone")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError("chain schedule requires valid IANA timezone") from exc
    if isinstance(full_weekday, bool):
        raise ValueError("full_weekday must be 0..6")
    full_weekday = int(full_weekday)
    if not 0 <= full_weekday <= 6:
        raise ValueError("full_weekday must be 0..6")
    _require_time(full_time)
    if not isinstance(incremental_times, (list, tuple)) or not 1 <= len(incremental_times) <= 4:
        raise ValueError("incremental_times must contain 1..4 HH:MM values")
    normalized = []
    for value in incremental_times:
        _require_time(value)
        if value not in normalized:
            normalized.append(value)
    if not normalized:
        raise ValueError("incremental_times must not be empty")
    normalized.sort()
    return {
        "timezone": timezone_name.strip(),
        "full_weekday": full_weekday,
        "full_time": full_time,
        "incremental_times": normalized,
    }


def _local_candidate(day, time_text: str, zone: ZoneInfo) -> datetime:
    hour, minute = _require_time(time_text)
    naive = datetime(day.year, day.month, day.day, hour, minute)
    candidates = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        round_trip = candidate.astimezone(timezone.utc).astimezone(zone)
        if round_trip.replace(tzinfo=None) == naive:
            candidates.append(candidate)
    if candidates:
        return min(candidates, key=lambda value: value.astimezone(timezone.utc))
    # DST spring-forward gap: first real minute after requested wall time.
    for offset in range(1, 24 * 60 + 1):
        probe = naive + timedelta(minutes=offset)
        for fold in (0, 1):
            candidate = probe.replace(tzinfo=zone, fold=fold)
            round_trip = candidate.astimezone(timezone.utc).astimezone(zone)
            if round_trip.replace(tzinfo=None) == probe:
                return candidate
    raise ValueError("cannot resolve schedule wall-clock time")


def next_full_after(value: datetime, schedule: dict) -> datetime:
    if value.tzinfo is None:
        raise ValueError("schedule cursor must be timezone-aware")
    zone = ZoneInfo(schedule["timezone"])
    local = value.astimezone(zone)
    target_weekday = int(schedule["full_weekday"])
    for delta in range(0, 15):
        day = local.date() + timedelta(days=delta)
        if day.weekday() != target_weekday:
            continue
        candidate = _local_candidate(day, schedule["full_time"], zone)
        if candidate.astimezone(timezone.utc) > value.astimezone(timezone.utc):
            return candidate
    raise ValueError("cannot determine next FULL schedule slot")


def next_incremental_after(value: datetime, schedule: dict) -> datetime:
    if value.tzinfo is None:
        raise ValueError("schedule cursor must be timezone-aware")
    zone = ZoneInfo(schedule["timezone"])
    local = value.astimezone(zone)
    for delta in range(0, 3700):
        day = local.date() + timedelta(days=delta)
        for time_text in schedule["incremental_times"]:
            candidate = _local_candidate(day, time_text, zone)
            if candidate.astimezone(timezone.utc) > value.astimezone(timezone.utc):
                return candidate
    raise ValueError("cannot determine next INCREMENTAL schedule slot")


def initialize_chain_schedule(now: datetime, timezone_name: str, full_weekday: int,
                              full_time: str, incremental_times) -> dict:
    base = validate_chain_schedule(timezone_name, full_weekday, full_time, incremental_times)
    base.update({
        "enabled": True,
        "next_full_at": next_full_after(now, base).isoformat(),
        "next_incremental_at": next_incremental_after(now, base).isoformat(),
    })
    return base


def advance_cursor(cursor: datetime, now: datetime, *, kind: str, schedule: dict) -> tuple[int, datetime]:
    if cursor.tzinfo is None or now.tzinfo is None:
        raise ValueError("schedule times must be timezone-aware")
    if cursor.astimezone(timezone.utc) > now.astimezone(timezone.utc):
        raise ValueError("schedule cursor is not due")
    represented = 0
    current = cursor
    step = next_full_after if kind == "FULL" else next_incremental_after
    while current.astimezone(timezone.utc) <= now.astimezone(timezone.utc):
        represented += 1
        if represented > 100000:
            raise ValueError("schedule backlog is unreasonably large")
        current = step(current, schedule)
    return represented, current
