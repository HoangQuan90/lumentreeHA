"""Monthly coordinator: use the official Lumentree monthly API directly."""

from __future__ import annotations

import calendar
import datetime as dt
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from ..const import (
    DEFAULT_MONTHLY_INTERVAL,
    DEFAULT_TARIFF_VND_PER_KWH,
    KEY_MONTHLY_CHARGE_KWH,
    KEY_MONTHLY_DISCHARGE_KWH,
    KEY_MONTHLY_ESSENTIAL_KWH,
    KEY_MONTHLY_GRID_IN_KWH,
    KEY_MONTHLY_LOAD_KWH,
    KEY_MONTHLY_PV_KWH,
    KEY_MONTHLY_SAVED_KWH,
    KEY_MONTHLY_SAVINGS_VND,
    KEY_MONTHLY_TOTAL_LOAD_KWH,
    get_timezone,
)
from ..services.aggregator import StatsAggregator

_LOGGER = logging.getLogger(__name__)


class MonthlyStatsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Expose monthly statistics from the vendor's getMonthData endpoint.

    The monthly API is the single source of truth here.  No daily cache,
    daily coordinator, or local aggregation is added to the API values, so the
    current day is not double-counted and the result matches Lumentree's own
    monthly statistics.
    """

    __slots__ = ("aggregator", "device_sn", "_entry_id", "_last_month")

    def __init__(
        self,
        hass: HomeAssistant,
        aggregator: StatsAggregator,
        device_sn: str,
        entry_id: str | None = None,
    ) -> None:
        self.aggregator = aggregator
        self.device_sn = device_sn
        self._entry_id = entry_id
        self._last_month: tuple[int, int] | None = None

        super().__init__(
            hass,
            _LOGGER,
            name="lumentree_monthly",
            update_interval=dt.timedelta(seconds=DEFAULT_MONTHLY_INTERVAL),
            always_update=False,
        )

    @staticmethod
    def _sum(values: Any) -> float:
        """Sum a vendor array while ignoring malformed values."""
        if not isinstance(values, list):
            return 0.0
        total = 0.0
        for value in values:
            try:
                total += float(value)
            except (TypeError, ValueError):
                continue
        return total

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            timezone = get_timezone(self.hass)
            now = dt_util.now(timezone)
            year = now.year
            month = now.month
            device_id = self.aggregator._device_id

            _LOGGER.debug(
                "Monthly coordinator: fetching official getMonthData for %s @ %04d-%02d",
                device_id,
                year,
                month,
            )

            # IMPORTANT: direct official Lumentree API only.
            # Do not merge cache/current-day data here.
            api_data = await self.aggregator._api.get_month_data(
                device_id,
                year,
                month,
            )

            days_in_month = calendar.monthrange(year, month)[1]

            daily_pv = list(api_data.get("pv", []))
            daily_grid = list(api_data.get("grid", []))
            daily_load = list(api_data.get("homeload", []))
            daily_essential = list(api_data.get("essentialLoad", []))
            daily_charge = list(api_data.get("bat", []))
            daily_discharge = list(api_data.get("batF", []))

            # The API returns one value per day.  Pad missing future days with
            # zero so chart/entity consumers keep the existing 1..N shape.
            def pad(values: list[Any]) -> list[float]:
                result: list[float] = []
                for index in range(days_in_month):
                    if index < len(values):
                        try:
                            result.append(float(values[index]))
                        except (TypeError, ValueError):
                            result.append(0.0)
                    else:
                        result.append(0.0)
                return result

            daily_pv = pad(daily_pv)
            daily_grid = pad(daily_grid)
            daily_load = pad(daily_load)
            daily_essential = pad(daily_essential)
            daily_charge = pad(daily_charge)
            daily_discharge = pad(daily_discharge)

            daily_total_load = [
                load + essential
                for load, essential in zip(daily_load, daily_essential, strict=False)
            ]
            daily_saved_kwh = [
                max(0.0, total - grid)
                for total, grid in zip(daily_total_load, daily_grid, strict=False)
            ]
            daily_savings_vnd = [
                saved * DEFAULT_TARIFF_VND_PER_KWH for saved in daily_saved_kwh
            ]

            monthly_pv = self._sum(daily_pv)
            monthly_grid = self._sum(daily_grid)
            monthly_load = self._sum(daily_load)
            monthly_essential = self._sum(daily_essential)
            monthly_total_load = self._sum(daily_total_load)
            monthly_charge = self._sum(daily_charge)
            monthly_discharge = self._sum(daily_discharge)
            monthly_saved_kwh = self._sum(daily_saved_kwh)
            monthly_savings_vnd = monthly_saved_kwh * DEFAULT_TARIFF_VND_PER_KWH

            self._last_month = (year, month)

            _LOGGER.debug(
                "Monthly API result %s-%02d: PV=%.3f grid=%.3f load=%.3f "
                "charge=%.3f discharge=%.3f",
                year,
                month,
                monthly_pv,
                monthly_grid,
                monthly_load,
                monthly_charge,
                monthly_discharge,
            )

            return {
                KEY_MONTHLY_PV_KWH: monthly_pv,
                KEY_MONTHLY_GRID_IN_KWH: monthly_grid,
                KEY_MONTHLY_LOAD_KWH: monthly_load,
                KEY_MONTHLY_ESSENTIAL_KWH: monthly_essential,
                KEY_MONTHLY_TOTAL_LOAD_KWH: monthly_total_load,
                KEY_MONTHLY_CHARGE_KWH: monthly_charge,
                KEY_MONTHLY_DISCHARGE_KWH: monthly_discharge,
                KEY_MONTHLY_SAVED_KWH: monthly_saved_kwh,
                KEY_MONTHLY_SAVINGS_VND: monthly_savings_vnd,
                "daily_pv": daily_pv,
                "daily_charge": daily_charge,
                "daily_discharge": daily_discharge,
                "daily_grid": daily_grid,
                "daily_load": daily_load,
                "daily_essential": daily_essential,
                "daily_total_load": daily_total_load,
                "daily_saved_kwh": daily_saved_kwh,
                "daily_savings_vnd": daily_savings_vnd,
                "days_in_month": days_in_month,
                "year": year,
                "month": month,
            }

        except TimeoutError as err:
            raise UpdateFailed("Timeout monthly") from err
        except Exception as err:
            _LOGGER.exception("Unexpected monthly update error")
            raise UpdateFailed(f"Unexpected error: {err}") from err
