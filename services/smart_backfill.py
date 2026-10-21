"""Smart backfill: fill the year caches from the Lumentree cloud, day by day.

Every day is requested with the per-day API (``aggregator.fetch_day``), which
always answers for the exact date it is asked about. The month endpoint
(getMonthData) is NOT used by default: the cloud ignores its ``month``
parameter for months that are over and returns the current month's numbers
again, so it cannot be trusted to label values with the right date.

Only days the cache is missing are requested, and today / future days are
never requested. A restart with complete history therefore asks the cloud for
nothing, and a normal night adds just yesterday.
"""

from __future__ import annotations

import asyncio
import calendar
import datetime as dt
import logging
import time
from typing import Any

from ..const import today_in_ha
from ..core.api_client import LumentreeHttpApiClient
from . import cache as cache_io

_LOGGER = logging.getLogger(__name__)

# Metrics getMonthData reports, mapped to the cache's daily keys.
_MONTH_KEYS: dict[str, str] = {
    "pv": "pv",
    "grid": "grid",
    "homeload": "load",
    "essentialLoad": "essential",
    "bat": "charge",
    "batF": "discharge",
}
_REQUIRED_API_KEYS = ("pv", "grid", "homeload", "essentialLoad")
_DAY_METRICS = tuple(_MONTH_KEYS.values())

# Ignore differences no larger than API rounding noise.
_PARTIAL_DAY_TOLERANCE = 0.05
_REQUEST_DELAY_SECONDS = 0.1
_MAX_REQUEST_DELAY_SECONDS = 5.0

# Give up on a run after this many per-day requests fail back to back, so a
# cloud that is down is not hammered. The next run (startup / 00:20) retries.
_MAX_CONSECUTIVE_ERRORS = 5

# getMonthData is only ever considered for the month in progress, and only when
# this flag is turned on. Off by default: every value then comes from the
# per-day API and is stored under exactly the date that was requested.
_USE_MONTH_API_FOR_CURRENT_MONTH = False

# Kept for backward compatibility with code that imports it; no longer used.
_MAX_IGNORED_STREAK = 3


class _ParamsIgnoredError(Exception):
    """getMonthData answered a month with another month's numbers."""


def _validate_month_data(
    month_data: dict[str, Any], year: int, month: int
) -> None:
    """Reject truncated/malformed responses before they can damage the cache.

    The four consumption/production series are required. Battery series have
    historically been omitted by some API responses, so those are optional;
    when supplied, however, they must cover the requested month too.
    """
    if not isinstance(month_data, dict):
        raise ValueError("getMonthData did not return a dictionary")

    days_in_month = calendar.monthrange(year, month)[1]
    for key in _REQUIRED_API_KEYS:
        series = month_data.get(key)
        if not isinstance(series, (list, tuple)) or len(series) < days_in_month:
            length = len(series) if isinstance(series, (list, tuple)) else "missing"
            raise ValueError(
                f"getMonthData series {key!r} is incomplete for {year}-{month:02d} "
                f"(got {length}, expected at least {days_in_month})"
            )

    for key in ("bat", "batF"):
        if key not in month_data or month_data[key] is None:
            continue
        series = month_data[key]
        if not isinstance(series, (list, tuple)) or len(series) < days_in_month:
            length = len(series) if isinstance(series, (list, tuple)) else "invalid"
            raise ValueError(
                f"getMonthData series {key!r} is incomplete for {year}-{month:02d} "
                f"(got {length}, expected at least {days_in_month})"
            )


def _day_values(month_data: dict[str, Any], day: int) -> dict[str, float]:
    """Return the day's available metrics in kWh.

    Missing optional battery series are intentionally omitted instead of
    converted to zero, allowing existing cached battery data to be preserved.
    """
    values: dict[str, float] = {}
    for api_key, cache_key in _MONTH_KEYS.items():
        series = month_data.get(api_key)
        if not isinstance(series, (list, tuple)) or day > len(series):
            continue
        try:
            value = float(series[day - 1] or 0.0)
        except (TypeError, ValueError) as err:
            raise ValueError(f"Invalid {api_key} value for day {day}: {series[day - 1]!r}") from err
        if value < 0:
            # Some APIs use negative values for direction; preserve the sign
            # only if the API client has already normalized that metric.
            value = abs(value)
        values[cache_key] = value
    return values


def _validate_not_future(
    month_data: dict[str, Any], year: int, month: int, today: dt.date
) -> None:
    """Reject a current-month answer that reports energy for future days.

    The current month is always merged and its API values are authoritative, so
    a wrong answer here would overwrite good cache data. Days after today cannot
    have production; if they do, the API answered a different month. One day of
    grace covers a cloud clock that is ahead of Home Assistant's timezone.
    """
    if (year, month) != (today.year, today.month):
        return
    last_ok = today + dt.timedelta(days=1)
    for day in range(1, calendar.monthrange(year, month)[1] + 1):
        if dt.date(year, month, day) <= last_ok:
            continue
        values = _day_values(month_data, day)
        if any(value > _PARTIAL_DAY_TOLERANCE for value in values.values()):
            raise _ParamsIgnoredError(
                f"{year}-{month:02d} reports energy for future day {day}"
            )


def _has_data(values: dict[str, Any]) -> bool:
    return any(float(values.get(key, 0.0) or 0.0) > 0.0 for key in _DAY_METRICS)


def _month_is_complete(cache: dict[str, Any], year: int, month: int, today: dt.date) -> bool:
    """True when every finished day of the month is cached or known empty."""
    daily = cache.get("daily", {})
    empty = set(cache.get("meta", {}).get("empty_dates", []))
    for day in range(1, calendar.monthrange(year, month)[1] + 1):
        date = dt.date(year, month, day)
        if date >= today:
            return False
        date_str = date.isoformat()
        if date_str not in daily and date_str not in empty:
            return False
    return True


def _month_has_cached_data(cache: dict[str, Any], year: int, month: int) -> bool:
    prefix = f"{year}-{month:02d}-"
    return any(
        date_str.startswith(prefix) and _has_data(values)
        for date_str, values in cache.get("daily", {}).items()
    )


def drop_unfinished_days(cache: dict[str, Any], today: dt.date) -> bool:
    """Remove entries for ``today`` and later; return True if any were removed."""
    cutoff = today.isoformat()
    daily = cache.get("daily", {})
    stale = [date_str for date_str in daily if date_str >= cutoff]
    for date_str in stale:
        del daily[date_str]
    meta = cache.get("meta", {})
    empties = meta.get("empty_dates")
    if isinstance(empties, list):
        kept = [date_str for date_str in empties if date_str < cutoff]
        if len(kept) != len(empties):
            meta["empty_dates"] = kept
            stale.append("empty_dates")
    return bool(stale)


def merge_month(
    cache: dict[str, Any],
    year: int,
    month: int,
    month_data: dict[str, Any],
    today: dt.date,
) -> tuple[int, int, bool]:
    """Merge a validated month response into the daily cache.

    Only finished days are written. For metrics actually returned by the API,
    the API value is authoritative in either direction, so stale high values
    are corrected as well as incomplete low values. Missing battery metrics do
    not overwrite the cached battery values.

    Returns (days_added, days_updated, month_has_data).
    """
    _validate_month_data(month_data, year, month)

    days_added = 0
    days_updated = 0
    month_has_data = False
    daily = cache.setdefault("daily", {})
    empty = set(cache.get("meta", {}).get("empty_dates", []))

    for day in range(1, calendar.monthrange(year, month)[1] + 1):
        date = dt.date(year, month, day)
        if date >= today:
            break
        date_str = date.isoformat()
        api_values = _day_values(month_data, day)
        api_has_data = _has_data(api_values)
        month_has_data = month_has_data or api_has_data

        existing = daily.get(date_str)
        if existing is None:
            if not api_has_data:
                if date_str not in empty:
                    cache_io.mark_empty(cache, date_str)
                    days_added += 1
                continue
            # New entries need every cache metric; absent optional battery
            # fields default to zero only when no previous value exists.
            complete_values = {key: float(api_values.get(key, 0.0) or 0.0) for key in _DAY_METRICS}
            cache_io.update_daily(cache, date_str, complete_values)
            days_added += 1
            continue

        merged = {key: float(existing.get(key, 0.0) or 0.0) for key in _DAY_METRICS}
        changed = False
        for key, api_value in api_values.items():
            if abs(float(api_value) - merged[key]) > _PARTIAL_DAY_TOLERANCE:
                merged[key] = float(api_value)
                changed = True
        if changed:
            cache_io.update_daily(cache, date_str, merged)
            days_updated += 1

    return days_added, days_updated, month_has_data


def _fingerprint(month_data: dict[str, Any]) -> tuple[tuple[float, ...], ...]:
    """Stable fingerprint of a month response (kept for backward compatibility)."""
    result: list[tuple[float, ...]] = []
    for key in _MONTH_KEYS:
        series = month_data.get(key)
        if not isinstance(series, (list, tuple)):
            result.append(())
            continue
        try:
            result.append(tuple(float(value or 0.0) for value in series))
        except (TypeError, ValueError):
            result.append(tuple())
    return tuple(result)


async def _fill_month_from_daily_api(
    aggregator, cache: dict[str, Any], year: int, month: int, today: dt.date
) -> tuple[dict[str, Any], int, bool, int, bool]:
    """Fill the finished days of one month that the cache is missing.

    Each missing day is requested with the per-day API, which always answers
    for the date it was asked about. Days that are cached or already known to
    be empty are skipped, and today / future days are never requested.

    Returns (cache, days_added, month_has_data, errors, gave_up). ``gave_up``
    is True when the cloud kept failing and the run should stop.
    """
    days_added = 0
    month_has_data = False
    errors = 0
    consecutive_errors = 0
    gave_up = False
    delay = _REQUEST_DELAY_SECONDS
    known_empty = set(cache.get("meta", {}).get("empty_dates", []))

    for day in range(1, calendar.monthrange(year, month)[1] + 1):
        date = dt.date(year, month, day)
        if date >= today:
            break
        date_str = date.isoformat()

        existing = cache.setdefault("daily", {}).get(date_str)
        if existing is not None:
            month_has_data = month_has_data or _has_data(existing)
            continue
        if date_str in known_empty:
            continue

        try:
            fetched = await aggregator.fetch_day(date_str)
        except Exception as err:
            errors += 1
            consecutive_errors += 1
            delay = min(delay * 2, _MAX_REQUEST_DELAY_SECONDS)
            _LOGGER.debug("Per-day request for %s failed: %s", date_str, err)
            if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                gave_up = True
                break
            await asyncio.sleep(delay)
            continue

        consecutive_errors = 0
        delay = _REQUEST_DELAY_SECONDS
        values = {key: float(fetched.get(key, 0.0) or 0.0) for key in _DAY_METRICS}
        if _has_data(values):
            cache, _metrics = cache_io.update_daily(cache, date_str, values)
            month_has_data = True
        else:
            cache_io.mark_empty(cache, date_str)
        days_added += 1
        await asyncio.sleep(delay)

    return cache, days_added, month_has_data, errors, gave_up


async def _persist_year(hass, device_id: str, year: int, cache: dict[str, Any]) -> None:
    """Write a year cache to disk (blocking I/O runs in the executor)."""

    def _write() -> None:
        cache_io.save_year(device_id, year, cache_io.recompute_aggregates(cache))

    await hass.async_add_executor_job(_write)


async def smart_backfill(
    hass, aggregator, max_years: int = 10, optimize_cache: bool = True
) -> dict[str, Any]:
    """Fill year caches, newest month first, one day at a time.

    Each missing finished day is requested with the per-day API, so its values
    are stored under the exact date asked for. Progress is written after every
    month, so an interrupted run resumes where it stopped.
    """
    device_id = aggregator._device_id
    api_client: LumentreeHttpApiClient = aggregator._api
    today = today_in_ha(hass)

    start_time = time.monotonic()
    stats: dict[str, Any] = {
        "years_processed": 0,
        "months_processed": 0,
        "days_added": 0,
        "days_updated": 0,
        "days_from_daily_api": 0,
        "years_with_data": [],
        "errors": 0,
        "ignored_months": [],
    }
    _LOGGER.info("Starting smart backfill for device %s (max_years=%s)", device_id, max_years)

    aborted = False

    for year in range(today.year, today.year - max(1, max_years), -1):
        cache = await hass.async_add_executor_job(cache_io.load_year, device_id, year, False)
        dirty = drop_unfinished_days(cache, today) if year == today.year else False
        year_has_data = False
        year_errors = 0
        year_written = False
        last_month = today.month if year == today.year else 12

        for month in range(last_month, 0, -1):
            if _month_is_complete(cache, year, month, today):
                year_has_data = year_has_data or _month_has_cached_data(cache, year, month)
                continue

            added = 0
            updated = 0
            month_has_data = False
            handled_by_month_api = False

            # getMonthData is never used for finished months, and for the month
            # in progress only when explicitly enabled (off by default).
            if _USE_MONTH_API_FOR_CURRENT_MONTH and (year, month) == (today.year, today.month):
                try:
                    month_data = await api_client.get_month_data(device_id, year, month)
                    _validate_month_data(month_data, year, month)
                    _validate_not_future(month_data, year, month, today)
                    added, updated, month_has_data = merge_month(
                        cache, year, month, month_data, today
                    )
                    handled_by_month_api = True
                except _ParamsIgnoredError as err:
                    _LOGGER.debug(
                        "getMonthData did not answer %s-%02d (%s); using the per-day API",
                        year, month, err,
                    )
                    stats["ignored_months"].append(f"{year}-{month:02d}")
                except Exception as err:
                    _LOGGER.debug(
                        "getMonthData %s-%02d unusable (%s); using the per-day API",
                        year, month, err,
                    )
                await asyncio.sleep(_REQUEST_DELAY_SECONDS)

            if not handled_by_month_api:
                cache, added, month_has_data, errors, gave_up = await _fill_month_from_daily_api(
                    aggregator, cache, year, month, today
                )
                updated = 0
                stats["days_from_daily_api"] += added
                stats["errors"] += errors
                year_errors += errors
                if gave_up:
                    aborted = True
                    _LOGGER.warning(
                        "Lumentree day API stopped answering at %s-%02d; "
                        "smart backfill will continue on the next run",
                        year, month,
                    )

            year_has_data = year_has_data or month_has_data
            if added or updated:
                dirty = True
                stats["days_added"] += added
                stats["days_updated"] += updated
                stats["months_processed"] += 1

            if dirty:
                await _persist_year(hass, device_id, year, cache)
                dirty = False
                year_written = True

            if aborted:
                break

        if dirty:
            await _persist_year(hass, device_id, year, cache)
            year_written = True
        if year_written:
            stats["years_processed"] += 1
        if year_has_data:
            stats["years_with_data"].append(year)

        if aborted:
            break
        if year < today.year and not year_has_data and year_errors == 0:
            _LOGGER.info("No data at all in %s; history starts after it", year)
            break

    if optimize_cache and stats["years_with_data"]:
        try:
            from . import cache_optimizer

            for year in stats["years_with_data"]:
                result = await hass.async_add_executor_job(
                    cache_optimizer.optimize_year_cache, device_id, year, False
                )
                if result["status"] == "optimized":
                    _LOGGER.info(
                        "Optimized year %s: removed %s empty days, size reduction: %.1f%%",
                        year, result["removed"], result["size_reduction_percent"],
                    )
        except Exception as err:
            _LOGGER.warning("Error optimizing cache: %s", err)

    elapsed = time.monotonic() - start_time
    stats["elapsed_seconds"] = round(elapsed, 2)
    _LOGGER.info(
        "Smart backfill completed: %s years written, %s months, %s days added "
        "(%s from the day API), %s days updated, %s errors, took %.1fs",
        stats["years_processed"], stats["months_processed"], stats["days_added"],
        stats["days_from_daily_api"], stats["days_updated"], stats["errors"], elapsed,
    )
    return stats
