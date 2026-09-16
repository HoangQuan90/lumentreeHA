"""HTTP API client for Lumentree integration."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Iterable
from typing import Any

import aiohttp
from aiohttp import ClientConnectorError, ServerConnectionError
from aiohttp.client import ClientTimeout

from ..const import (
    BASE_URL,
    DEFAULT_HEADERS,
    URL_DEVICE_MANAGE,
    URL_GET_ALL_DAY_DATA,
    URL_GET_BAT_DAY_DATA,
    URL_GET_MONTH_DATA,
    URL_GET_OTHER_DAY_DATA,
    URL_GET_PV_DAY_DATA,
    URL_GET_SERVER_TIME,
    URL_GET_YEAR_DATA,
    URL_SHARE_DEVICES,
)
from .exceptions import ApiException, AuthException

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = ClientTimeout(total=30)
AUTH_RETRY_DELAY = 0.5
AUTH_MAX_RETRIES = 3

API_MAX_RETRIES = 3
API_RETRY_BASE_DELAY = 1.0
API_RETRY_MAX_DELAY = 10.0

RETURN_VALUE_ENDPOINT_MISSING = 998


def _finite_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


class LumentreeHttpApiClient:
    """HTTP API client for Lumentree cloud services."""

    __slots__ = ("_session", "_token", "_device_info_cache", "_all_day_data_absent")

    _CACHE_TIMEOUT = 3600

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._token: str | None = None
        self._device_info_cache: dict[str, tuple[dict[str, Any], float]] = {}
        self._all_day_data_absent = False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_float_list(vals: Any) -> list[float]:
        if not isinstance(vals, list):
            return []
        result: list[float] = []
        for value in vals:
            number = _finite_or_none(value)
            if number is not None:
                result.append(number)
        return result

    @staticmethod
    def _series_5min_kwh(series_w: list[tuple[int, float]]) -> list[tuple[int, float]]:
        factor = (5.0 / 60.0) / 1000.0
        return [(slot, value * factor) for slot, value in series_w]

    @staticmethod
    def _series_hour_kwh(series_kwh5: list[tuple[int, float]]) -> list[float]:
        if not series_kwh5:
            return []
        hours = [0.0] * 24
        for slot, value in series_kwh5:
            hour = slot // 12
            if 0 <= hour < 24:
                hours[hour] += value
        return hours

    @staticmethod
    def _values(frame: list[tuple[int, float]]) -> list[float]:
        if not frame:
            return []
        values = [0.0] * (max(slot for slot, _ in frame) + 1)
        for slot, value in frame:
            values[slot] = value
        return values

    @staticmethod
    def _sum(series: list[float]) -> float:
        return sum(series) if series else 0.0

    @staticmethod
    def _metric_total_kwh(metric: Any) -> float | None:
        if not isinstance(metric, dict):
            return None
        number = _finite_or_none(metric.get("tableValue"))
        return None if number is None else number / 10.0

    @staticmethod
    def _slot_readings(metric: Any) -> list[tuple[int, float]]:
        if not isinstance(metric, dict):
            return []
        values = metric.get("tableValueInfo")
        if not isinstance(values, list):
            return []
        result: list[tuple[int, float]] = []
        for slot, value in enumerate(values):
            number = _finite_or_none(value)
            if number is not None:
                result.append((slot, number))
        return result

    @staticmethod
    def _slot_sum(
        left: list[tuple[int, float]],
        right: list[tuple[int, float]],
    ) -> list[tuple[int, float]]:
        a = dict(left)
        b = dict(right)
        result: list[tuple[int, float]] = []
        for slot in range(max([*a, *b], default=-1) + 1):
            x = a.get(slot)
            y = b.get(slot)
            if x is None and y is None:
                continue
            result.append((slot, float(x or 0.0) + float(y or 0.0)))
        return result

    @staticmethod
    def _slot_difference(
        charge_slots: list[tuple[int, float]],
        discharge_slots: list[tuple[int, float]],
    ) -> list[tuple[int, float]]:
        charge = dict(charge_slots)
        discharge = dict(discharge_slots)
        result: list[tuple[int, float]] = []
        for slot in range(max([*charge, *discharge], default=-1) + 1):
            c = charge.get(slot)
            d = discharge.get(slot)
            if c is not None and d is not None:
                result.append((slot, c - d))
            elif d is not None:
                result.append((slot, -d))
            elif c is not None:
                result.append((slot, c))
        return result

    @classmethod
    def _build_pv_result(cls, metric: Any) -> dict[str, Any]:
        result: dict[str, Any] = {"pv_today": cls._metric_total_kwh(metric)}
        series = cls._slot_readings(metric)
        if series:
            series_kwh5 = cls._series_5min_kwh(series)
            result.update(
                {
                    "pv_series_5min_w": cls._values(series),
                    "pv_series_5min_kwh": cls._values(series_kwh5),
                    "pv_series_hour_kwh": cls._series_hour_kwh(series_kwh5),
                    "pv_sum_kwh": cls._sum(cls._values(series_kwh5)),
                }
            )
        return result

    @classmethod
    def _build_grid_result(cls, metric: Any) -> dict[str, Any]:
        result: dict[str, Any] = {"grid_in_today": cls._metric_total_kwh(metric)}
        series = cls._slot_readings(metric)
        if series:
            series_kwh5 = cls._series_5min_kwh(series)
            result.update(
                {
                    "grid_series_5min_w": cls._values(series),
                    "grid_series_5min_kwh": cls._values(series_kwh5),
                    "grid_series_hour_kwh": cls._series_hour_kwh(series_kwh5),
                }
            )
        return result

    @classmethod
    def _build_load_result(
        cls, load_metric: Any, essential_metric: Any
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}

        load_total = cls._metric_total_kwh(load_metric)
        essential_total = cls._metric_total_kwh(essential_metric)

        if load_total is not None:
            result["load_today"] = load_total
        if essential_total is not None:
            result["essential_today"] = essential_total
        if load_total is not None or essential_total is not None:
            result["total_load_today"] = float(load_total or 0.0) + float(
                essential_total or 0.0
            )

        load_series = cls._slot_readings(load_metric)
        essential_series = cls._slot_readings(essential_metric)

        if load_series:
            series_kwh5 = cls._series_5min_kwh(load_series)
            result.update(
                {
                    "load_series_5min_w": cls._values(load_series),
                    "load_series_5min_kwh": cls._values(series_kwh5),
                    "load_series_hour_kwh": cls._series_hour_kwh(series_kwh5),
                }
            )

        if essential_series:
            series_kwh5 = cls._series_5min_kwh(essential_series)
            result.update(
                {
                    "essential_series_5min_w": cls._values(essential_series),
                    "essential_series_5min_kwh": cls._values(series_kwh5),
                    "essential_series_hour_kwh": cls._series_hour_kwh(series_kwh5),
                }
            )

        if load_series and essential_series:
            total = cls._slot_sum(load_series, essential_series)
            total_kwh5 = cls._series_5min_kwh(total)
            result.update(
                {
                    "total_load_series_5min_w": cls._values(total),
                    "total_load_series_5min_kwh": cls._values(total_kwh5),
                    "total_load_series_hour_kwh": cls._series_hour_kwh(total_kwh5),
                }
            )
            if "total_load_today" not in result:
                result["total_load_today"] = cls._sum(cls._values(total_kwh5))

        return result

    @classmethod
    def _build_battery_result(
        cls,
        series_slots: list[tuple[int, float]],
        charge_today: float | None,
        discharge_today: float | None,
    ) -> dict[str, Any]:
        """Build battery metrics. Positive signed series means charging."""
        result: dict[str, Any] = {
            "charge_today": charge_today,
            "discharge_today": discharge_today,
        }
        if not series_slots:
            return result

        factor = (5.0 / 60.0) / 1000.0
        charge_kwh5 = [
            (slot, value * factor if value > 0 else 0.0)
            for slot, value in series_slots
        ]
        discharge_kwh5 = [
            (slot, abs(value) * factor if value < 0 else 0.0)
            for slot, value in series_slots
        ]

        result.update(
            {
                "battery_series_5min_w": cls._values(series_slots),
                "battery_charge_series_hour_kwh": cls._series_hour_kwh(charge_kwh5),
                "battery_discharge_series_hour_kwh": cls._series_hour_kwh(
                    discharge_kwh5
                ),
            }
        )
        return result

    @staticmethod
    def _drop_none_scalars(stats: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in stats.items()
            if isinstance(value, list) or value is not None
        }

    # ------------------------------------------------------------------
    # Authentication / HTTP
    # ------------------------------------------------------------------

    def set_token(self, token: str | None) -> None:
        self._token = token

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        requires_auth: bool = True,
        max_retries: int = API_MAX_RETRIES,
    ) -> dict[str, Any]:
        if isinstance(endpoint, str) and (
            endpoint.startswith("http://") or endpoint.startswith("https://")
        ):
            url = endpoint
        else:
            url = f"{BASE_URL}{endpoint}"

        headers = DEFAULT_HEADERS.copy()
        if extra_headers:
            headers.update(extra_headers)

        if requires_auth:
            if self._token:
                headers["Authorization"] = self._token
            else:
                raise AuthException("Token required")

        if data and method.upper() == "POST":
            headers["Content-Type"] = headers.get(
                "Content-Type", "application/x-www-form-urlencoded"
            )

        last_exc: Exception | None = None
        delay = API_RETRY_BASE_DELAY

        for attempt in range(max_retries):
            try:
                async with self._session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    data=data,
                    timeout=DEFAULT_TIMEOUT,
                ) as response:
                    resp_text = await response.text()
                    resp_text_short = resp_text[:300]

                    try:
                        resp_json = await response.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError) as exc:
                        raise ApiException(
                            f"Invalid JSON: {resp_text_short}"
                        ) from exc

                    if not response.ok and not resp_json:
                        response.raise_for_status()

                    return_value = resp_json.get("returnValue")

                    if endpoint == URL_GET_SERVER_TIME and "data" in resp_json:
                        return resp_json

                    if return_value != 1:
                        msg = resp_json.get("msg", "Unknown")
                        if return_value == 203 or response.status in (401, 403):
                            raise AuthException(
                                f"Auth failed (code={return_value}, status={response.status}): {msg}"
                            )
                        raise ApiException(
                            f"API error: {msg} (code={return_value})",
                            code=return_value,
                        )

                    return resp_json

            except (AuthException, ApiException):
                raise
            except (TimeoutError, ClientConnectorError, ServerConnectionError) as exc:
                last_exc = exc
                if attempt < max_retries - 1:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, API_RETRY_MAX_DELAY)
            except aiohttp.ClientResponseError as exc:
                if exc.status in (401, 403):
                    raise AuthException(
                        f"Auth error ({exc.status}): {exc.message}"
                    ) from exc
                if 500 <= exc.status < 600 and attempt < max_retries - 1:
                    last_exc = exc
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, API_RETRY_MAX_DELAY)
                else:
                    raise ApiException(f"HTTP error: {exc.status}") from exc
            except aiohttp.ClientError as exc:
                last_exc = exc
                if attempt < max_retries - 1:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, API_RETRY_MAX_DELAY)
            except Exception as exc:
                _LOGGER.exception("Unexpected HTTP error %s", url)
                raise ApiException(f"Unexpected error: {exc}") from exc

        if last_exc:
            if isinstance(last_exc, (ClientConnectorError, ServerConnectionError)):
                raise ApiException(
                    f"Connection failed after {max_retries} attempts"
                ) from last_exc
            if isinstance(last_exc, asyncio.TimeoutError):
                raise ApiException(
                    f"Request timeout after {max_retries} attempts"
                ) from last_exc
            raise ApiException(
                f"Request failed after {max_retries} attempts: {last_exc}"
            ) from last_exc

        raise ApiException(
            f"Request failed after {max_retries} attempts (unknown error)"
        )

    async def _get_server_time(self) -> int | None:
        try:
            resp = await self._request(
                "GET", URL_GET_SERVER_TIME, requires_auth=False
            )
            server_time = resp.get("data", {}).get("serverTime")
            return int(server_time) if server_time else None
        except Exception as exc:
            _LOGGER.exception("Failed to get server time: %s", exc)
            return None

    async def _get_token(self, device_id: str, server_time: int) -> str | None:
        try:
            payload = {
                "deviceIds": device_id,
                "serverTime": str(server_time),
            }
            headers = {
                "source": "2",
                "Content-Type": "application/x-www-form-urlencoded",
            }
            resp = await self._request(
                "POST",
                URL_SHARE_DEVICES,
                data=payload,
                extra_headers=headers,
                requires_auth=False,
            )
            token = resp.get("data", {}).get("token")
            return token if token else None
        except Exception as exc:
            _LOGGER.exception("Failed to get token: %s", exc)
            return None

    async def authenticate_device(self, device_id: str) -> str:
        _LOGGER.info("Authenticating device %s", device_id)
        last_exc: Exception | None = None

        for attempt in range(AUTH_MAX_RETRIES):
            try:
                server_time = await self._get_server_time()
                if not server_time:
                    raise ApiException("Failed to get server time")

                token = await self._get_token(device_id, server_time)
                if not token:
                    raise AuthException(
                        f"Failed to get token (attempt {attempt + 1})"
                    )

                self.set_token(token)
                _LOGGER.info("Authentication successful for %s", device_id)
                return token

            except (ApiException, AuthException) as exc:
                last_exc = exc
                _LOGGER.warning(
                    "Auth attempt %s failed: %s", attempt + 1, exc
                )
            except Exception as exc:
                last_exc = AuthException(f"Unexpected error: {exc}")
                _LOGGER.exception(
                    "Unexpected auth error (attempt %s)", attempt + 1
                )

            if attempt < AUTH_MAX_RETRIES - 1:
                await asyncio.sleep(AUTH_RETRY_DELAY)

        if last_exc:
            raise last_exc
        raise AuthException("Authentication failed (unknown reason)")

    # ------------------------------------------------------------------
    # Device information
    # ------------------------------------------------------------------

    async def get_device_info(self, device_id: str) -> dict[str, Any]:
        if not device_id:
            return {"_error": "Device ID missing"}

        current_time = time.time()

        expired_keys = [
            key
            for key, (_, cache_time) in self._device_info_cache.items()
            if current_time - cache_time >= self._CACHE_TIMEOUT
        ]
        for key in expired_keys:
            self._device_info_cache.pop(key, None)

        if device_id in self._device_info_cache:
            cached_data, cache_time = self._device_info_cache[device_id]
            if current_time - cache_time < self._CACHE_TIMEOUT:
                return cached_data

        try:
            response_json = await self._request(
                "POST",
                URL_DEVICE_MANAGE,
                params={"page": "1", "snName": device_id},
                requires_auth=True,
            )
            response_data = response_json.get("data", {})
            devices_list = (
                response_data.get("devices")
                if isinstance(response_data, dict)
                else None
            )

            if isinstance(devices_list, list) and devices_list:
                device_info = devices_list[0]
                if isinstance(device_info, dict):
                    self._device_info_cache[device_id] = (
                        device_info,
                        current_time,
                    )
                    return device_info

            return {"_error": "Device not found"}

        except (ApiException, AuthException):
            raise
        except Exception as exc:
            _LOGGER.exception(
                "Unexpected error getting device info for %s", device_id
            )
            return {"_error": f"Unexpected error: {exc}"}

    # ------------------------------------------------------------------
    # Daily statistics
    # ------------------------------------------------------------------

    async def get_daily_stats(
        self, device_identifier: str, query_date: str
    ) -> dict[str, Any]:
        """Get daily statistics with a battery-specific legacy fallback.

        The combined endpoint is kept for PV/grid/load. If it omits either
        battery total, getBatDayData is queried and only the battery fields
        are replaced. This fixes missing discharge_today without abandoning
        the faster combined endpoint.
        """
        if self._all_day_data_absent:
            combined: dict[str, Any] = {}
        else:
            combined = await self.get_all_day_data(
                device_identifier, query_date
            )

        if combined:
            combined_charge = combined.get("charge_today")
            combined_discharge = combined.get("discharge_today")

            if combined_discharge is not None and combined_charge is not None:
                return combined

            _LOGGER.info(
                "Combined day data for %s @ %s has incomplete battery "
                "statistics (charge=%s, discharge=%s); fetching "
                "getBatDayData fallback",
                device_identifier,
                query_date,
                combined_charge,
                combined_discharge,
            )

            battery = await self._fetch_battery_data(
                {
                    "deviceId": device_identifier,
                    "queryDate": query_date,
                }
            )

            # Replace only battery fields. PV/grid/load stay on the new API.
            for key in (
                "charge_today",
                "discharge_today",
                "battery_series_5min_w",
                "battery_charge_series_hour_kwh",
                "battery_discharge_series_hour_kwh",
            ):
                if key in battery and battery[key] is not None:
                    combined[key] = battery[key]

            return self._drop_none_scalars(combined)

        _LOGGER.info(
            "Combined day endpoint returned no usable data for %s @ %s; "
            "falling back to legacy endpoints",
            device_identifier,
            query_date,
        )

        base_params = {
            "deviceId": device_identifier,
            "queryDate": query_date,
        }

        results = await asyncio.gather(
            self._fetch_pv_data(base_params),
            self._fetch_battery_data(base_params),
            self._fetch_other_data(base_params),
            return_exceptions=True,
        )
        return self._merge_stats_results(results)

    async def get_year_data(
        self, device_identifier: str, year: int
    ) -> dict[str, Any]:
        try:
            params = {
                "deviceId": device_identifier,
                "year": str(year),
            }
            resp = await self._request(
                "GET",
                URL_GET_YEAR_DATA,
                params=params,
                requires_auth=True,
            )

            if not resp or resp.get("returnValue") != 1:
                raise ValueError(f"API returned invalid response: {resp}")

            data = resp.get("data", {})
            if not data:
                raise ValueError("API returned empty data")

            result: dict[str, Any] = {}
            for key in (
                "pv",
                "grid",
                "homeload",
                "essentialLoad",
                "bat",
                "batF",
            ):
                item = data.get(key)
                if isinstance(item, dict):
                    values = self._to_float_list(
                        item.get("tableValueInfo", [])
                    )
                    result[key] = (
                        [value / 10.0 for value in values]
                        if values
                        else [0.0] * 12
                    )
                else:
                    result[key] = [0.0] * 12

            return result

        except Exception as exc:
            _LOGGER.error(
                "Error fetching year data for %s @ %s: %s",
                device_identifier,
                year,
                exc,
            )
            raise

    async def get_month_data(
        self,
        device_identifier: str,
        year: int,
        month: int,
    ) -> dict[str, Any]:
        try:
            params = {
                "deviceId": device_identifier,
                "year": str(year),
                "month": str(month),
            }
            resp = await self._request(
                "GET",
                URL_GET_MONTH_DATA,
                params=params,
                requires_auth=True,
            )
            data = resp.get("data", {})

            result: dict[str, Any] = {}
            for key in (
                "pv",
                "grid",
                "homeload",
                "essentialLoad",
                "bat",
            ):
                item = data.get(key)
                if isinstance(item, dict):
                    values = self._to_float_list(
                        item.get("tableValueInfo", [])
                    )
                    result[key] = [value / 10.0 for value in values]
                else:
                    result[key] = []

            return result

        except Exception as exc:
            _LOGGER.error(
                "Error fetching month data for %s @ %s-%02d: %s",
                device_identifier,
                year,
                month,
                exc,
            )
            return {
                "pv": [],
                "grid": [],
                "homeload": [],
                "essentialLoad": [],
                "bat": [],
            }

    # ------------------------------------------------------------------
    # Legacy daily endpoints
    # ------------------------------------------------------------------

    async def _fetch_pv_data(
        self, base_params: dict[str, str]
    ) -> dict[str, Any]:
        try:
            resp = await self._request(
                "GET",
                URL_GET_PV_DAY_DATA,
                params=base_params,
                requires_auth=True,
            )
            return self._build_pv_result(
                (resp.get("data") or {}).get("pv")
            )
        except (ApiException, AuthException) as exc:
            _LOGGER.warning(
                "Failed PV stats (%s): %s",
                type(exc).__name__,
                exc,
            )
            return {"pv_today": None}
        except Exception:
            _LOGGER.exception("Unexpected PV stats error")
            return {"pv_today": None}

    async def _fetch_battery_data(
        self, base_params: dict[str, str]
    ) -> dict[str, Any]:
        """Fetch battery charge/discharge from the legacy endpoint."""
        try:
            resp = await self._request(
                "GET",
                URL_GET_BAT_DAY_DATA,
                params=base_params,
                requires_auth=True,
            )
            data = resp.get("data") or {}
            bats_data = data.get("bats", [])

            charge_today: float | None = None
            discharge_today: float | None = None

            if isinstance(bats_data, list):
                if len(bats_data) > 0 and isinstance(bats_data[0], dict):
                    charge_today = self._metric_total_kwh(bats_data[0])

                if len(bats_data) > 1 and isinstance(bats_data[1], dict):
                    discharge_today = self._metric_total_kwh(bats_data[1])

            # Legacy battery series: positive = discharge.
            # Shared convention: positive = charge.
            series_slots = [
                (slot, -value)
                for slot, value in self._slot_readings(data)
            ]

            return self._build_battery_result(
                series_slots,
                charge_today,
                discharge_today,
            )

        except (ApiException, AuthException) as exc:
            _LOGGER.warning(
                "Failed battery stats (%s): %s",
                type(exc).__name__,
                exc,
            )
            return {
                "charge_today": None,
                "discharge_today": None,
            }
        except Exception:
            _LOGGER.exception("Unexpected battery stats error")
            return {
                "charge_today": None,
                "discharge_today": None,
            }

    async def _fetch_other_data(
        self, base_params: dict[str, str]
    ) -> dict[str, Any]:
        try:
            resp = await self._request(
                "GET",
                URL_GET_OTHER_DAY_DATA,
                params=base_params,
                requires_auth=True,
            )
            data = resp.get("data") or {}

            result = self._build_grid_result(data.get("grid"))
            result.update(
                self._build_load_result(
                    data.get("homeload"),
                    data.get("essentialLoad"),
                )
            )
            return result

        except (ApiException, AuthException) as exc:
            _LOGGER.warning(
                "Failed other stats (%s): %s",
                type(exc).__name__,
                exc,
            )
            return {
                "grid_in_today": None,
                "load_today": None,
            }
        except Exception:
            _LOGGER.exception("Unexpected other stats error")
            return {
                "grid_in_today": None,
                "load_today": None,
            }

    # ------------------------------------------------------------------
    # Combined daily endpoint
    # ------------------------------------------------------------------

    @classmethod
    def _merge_all_day_payload(
        cls, payload: Any
    ) -> dict[str, Any]:
        if not isinstance(payload, dict) or not payload:
            return {}

        data = payload

        result = cls._build_pv_result(data.get("pv"))
        result.update(cls._build_grid_result(data.get("grid")))
        result.update(
            cls._build_load_result(
                data.get("homeload"),
                data.get("essentialLoad"),
            )
        )

        signed_slots = cls._slot_difference(
            cls._slot_readings(data.get("bat")),
            cls._slot_readings(data.get("batF")),
        )

        charge_today = cls._metric_total_kwh(data.get("bat"))
        discharge_today = cls._metric_total_kwh(data.get("batF"))

        # IMPORTANT: do not turn missing batF into 0 here.
        # get_daily_stats() uses the missing value to trigger the reliable
        # getBatDayData fallback.
        result.update(
            cls._build_battery_result(
                signed_slots,
                charge_today,
                discharge_today,
            )
        )

        return cls._drop_none_scalars(result)

    async def get_all_day_data(
        self,
        device_identifier: str,
        query_date: str,
    ) -> dict[str, Any]:
        try:
            resp = await self._request(
                "GET",
                URL_GET_ALL_DAY_DATA,
                params={
                    "deviceId": device_identifier,
                    "queryDate": query_date,
                },
                requires_auth=True,
            )
            return self._merge_all_day_payload(resp.get("data"))

        except (ApiException, AuthException) as exc:
            if getattr(exc, "code", None) == RETURN_VALUE_ENDPOINT_MISSING:
                self._all_day_data_absent = True

            _LOGGER.warning(
                "Failed all-day stats (%s): %s",
                type(exc).__name__,
                exc,
            )
            return {}

        except Exception:
            _LOGGER.exception("Unexpected all-day stats error")
            return {}

    # ------------------------------------------------------------------
    # Merge
    # ------------------------------------------------------------------

    def _merge_stats_results(
        self, results: Iterable[Any]
    ) -> dict[str, Any]:
        merged: dict[str, Any] = {}

        for result in results:
            if isinstance(result, dict):
                merged.update(result)
            elif isinstance(result, Exception):
                _LOGGER.warning("API call failed: %s", result)

        return self._drop_none_scalars(merged)
