"""Yearly coordinator: use the official Lumentree yearly API directly."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from ..const import (
    DEFAULT_TARIFF_VND_PER_KWH,
    DEFAULT_YEARLY_INTERVAL,
    KEY_YEARLY_CHARGE_KWH,
    KEY_YEARLY_DISCHARGE_KWH,
    KEY_YEARLY_ESSENTIAL_KWH,
    KEY_YEARLY_GRID_IN_KWH,
    KEY_YEARLY_LOAD_KWH,
    KEY_YEARLY_PV_KWH,
    KEY_YEARLY_SAVED_KWH,
    KEY_YEARLY_SAVINGS_VND,
    KEY_YEARLY_TOTAL_LOAD_KWH,
    get_timezone,
)
from ..services.aggregator import StatsAggregator

_LOGGER = logging.getLogger(__name__)


class YearlyStatsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Expose yearly statistics from the vendor's getYearData endpoint.

    The official yearly API is the only source used for yearly values.  The
    current month is NOT replaced with cache/daily data, because doing so can
    mix two different sources and double-count the current day.
    """

    __slots__ = ("aggregator", "device_sn", "_entry_id", "_last_year")

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
        self._last_year: int | None = None

        super().__init__(
            hass,
            _LOGGER,
            name="lumentree_yearly",
            update_interval=dt.timedelta(seconds=DEFAULT_YEARLY_INTERVAL),
            always_update=False,
        )

    @staticmethod
    def _normalize(values: Any, length: int = 12) -> list[float]:
        """Return exactly 12 finite-ish numeric monthly values."""
        if not isinstance(values, list):
            values = []
        result: list[float] = []
        for index in range(length):
            if index < len(values):
                try:
                    result.append(float(values[index]))
                except (TypeError, ValueError):
                    result.append(0.0)
            else:
                result.append(0.0)
        return result

    @staticmethod
    def _sum(values: list[float]) -> float:
        return sum(values)

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            timezone = get_timezone(self.hass)
            now = dt_util.now(timezone)
            year = now.year
            device_id = self.aggregator._device_id

            _LOGGER.debug(
                "Yearly coordinator: fetching official getYearData for %s @ %04d",
                device_id,
                year,
            )

            # IMPORTANT: direct official Lumentree API only.
            # No cache, summarize_year(), summarize_month(), or current-month
            # replacement is used for the final yearly values.
            api_data = await self.aggregator._api.get_year_data(
                device_id,
                year,
            )

            monthly_pv = self._normalize(api_data.get("pv"))
            monthly_grid = self._normalize(api_data.get("grid"))
            monthly_load = self._normalize(api_data.get("homeload"))
            monthly_essential = self._normalize(api_data.get("essentialLoad"))
            monthly_charge = self._normalize(api_data.get("bat"))
            monthly_discharge = self._normalize(api_data.get("batF"))

            monthly_total_load = [
                load + essential
                for load, essential in zip(monthly_load, monthly_essential, strict=False)
            ]
            monthly_saved_kwh = [
                max(0.0, total - grid)
                for total, grid in zip(monthly_total_load, monthly_grid, strict=False)
            ]
            monthly_savings_vnd = [
                saved * DEFAULT_TARIFF_VND_PER_KWH for saved in monthly_saved_kwh
            ]

            yearly_pv = self._sum(monthly_pv)
            yearly_grid = self._sum(monthly_grid)
            yearly_load = self._sum(monthly_load)
            yearly_essential = self._sum(monthly_essential)
            yearly_total_load = self._sum(monthly_total_load)
            yearly_charge = self._sum(monthly_charge)
            yearly_discharge = self._sum(monthly_discharge)
            yearly_saved_kwh = self._sum(monthly_saved_kwh)
            yearly_savings_vnd = yearly_saved_kwh * DEFAULT_TARIFF_VND_PER_KWH

            self._last_year = year

            _LOGGER.debug(
                "Yearly API result %04d: PV=%.3f grid=%.3f load=%.3f "
                "charge=%.3f discharge=%.3f",
                year,
                yearly_pv,
                yearly_grid,
                yearly_load,
                yearly_charge,
                yearly_discharge,
            )

            return {
                KEY_YEARLY_PV_KWH: yearly_pv,
                KEY_YEARLY_GRID_IN_KWH: yearly_grid,
                KEY_YEARLY_LOAD_KWH: yearly_load,
                KEY_YEARLY_ESSENTIAL_KWH: yearly_essential,
                KEY_YEARLY_TOTAL_LOAD_KWH: yearly_total_load,
                KEY_YEARLY_CHARGE_KWH: yearly_charge,
                KEY_YEARLY_DISCHARGE_KWH: yearly_discharge,
                KEY_YEARLY_SAVED_KWH: yearly_saved_kwh,
                KEY_YEARLY_SAVINGS_VND: yearly_savings_vnd,
                "monthly_pv": monthly_pv,
                "monthly_grid": monthly_grid,
                "monthly_load": monthly_load,
                "monthly_essential": monthly_essential,
                "monthly_total_load": monthly_total_load,
                "monthly_charge": monthly_charge,
                "monthly_discharge": monthly_discharge,
                "monthly_saved_kwh": monthly_saved_kwh,
                "monthly_savings_vnd": monthly_savings_vnd,
                "year": year,
            }

        except TimeoutError as err:
            raise UpdateFailed("Timeout yearly") from err
        except Exception as err:
            _LOGGER.exception("Unexpected yearly update error")
            raise UpdateFailed(f"Unexpected error: {err}") from err
