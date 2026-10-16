"""Fetch a current Electric Ireland data window."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.util.dt import utcnow

from .api import ElectricIrelandAPI
from .const import _redact_id
from .exceptions import CachedIdsInvalid, CannotConnect
from .types import BillPeriod, ElectricIrelandDatapoint, MeterIds

if TYPE_CHECKING:
    from . import ElectricIrelandConfigEntry

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RefreshResult:
    """Data collected for one scheduled polling cycle."""

    datapoints: list[ElectricIrelandDatapoint]
    bill_periods_available: int


class RefreshWorkflow:
    """Own current-window authentication, billing-period selection, and fetching."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ElectricIrelandConfigEntry,
        api: ElectricIrelandAPI,
        session: aiohttp.ClientSession,
        api_lock: asyncio.Lock,
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._hass = hass
        self._config_entry = config_entry
        self._api = api
        self._session = session
        self._api_lock = api_lock
        self._now = now
        self._bill_periods: list[BillPeriod] = []
        self._bill_periods_fetched_at: datetime | None = None

    def update_cached_meter_ids(self, discovered_ids: MeterIds) -> None:
        """Persist discovered meter IDs and invalidate the billing-period cache."""
        new_data = {
            **dict(self._config_entry.data),
            "partner_id": discovered_ids["partner"],
            "contract_id": discovered_ids["contract"],
            "premise_id": discovered_ids["premise"],
        }
        self._hass.config_entries.async_update_entry(self._config_entry, data=new_data)
        self._bill_periods = []
        self._bill_periods_fetched_at = None
        _LOGGER.debug(
            "Updated cached meter IDs: partner=%s",
            _redact_id(discovered_ids["partner"]),
        )

    async def async_fetch(self, lookback_days: int) -> RefreshResult:
        """Authenticate, select dates within the lookback, and fetch them sequentially."""
        entry_data = self._config_entry.data
        cached_ids: MeterIds | None = None
        if entry_data.get("partner_id") and entry_data.get("contract_id") and entry_data.get("premise_id"):
            cached_ids = {
                "partner": entry_data["partner_id"],
                "contract": entry_data["contract_id"],
                "premise": entry_data["premise_id"],
            }

        try:
            async with self._api_lock:
                meter_ids, discovered_ids = await self._api.authenticate(self._session, cached_ids)
        except CannotConnect:
            if cached_ids is None:
                raise
            _LOGGER.warning("Cached meter IDs failed during login, falling back to full discovery")
            self._session.cookie_jar.clear()
            async with self._api_lock:
                meter_ids, discovered_ids = await self._api.authenticate(self._session, None)

        if discovered_ids is not None:
            self.update_cached_meter_ids(discovered_ids)

        bill_period_stale = (
            self._bill_periods_fetched_at is None or (utcnow() - self._bill_periods_fetched_at).total_seconds() > 86400
        )
        if bill_period_stale:
            try:
                async with self._api_lock:
                    self._bill_periods = await self._api.get_bill_periods(self._session, meter_ids)
                self._bill_periods_fetched_at = utcnow()
            except CannotConnect:
                _LOGGER.warning("Failed to fetch bill periods, falling back to full lookback window")
                self._bill_periods = []
                self._bill_periods_fetched_at = None

        yesterday = (self._now() - timedelta(days=1)).date()
        all_lookback_dates = {yesterday - timedelta(days=i) for i in range(lookback_days)}
        if self._bill_periods:
            dates_in_periods: set[date] = set()
            for period in self._bill_periods:
                period_start = date.fromisoformat(period["startDate"][:10])
                period_end = date.fromisoformat(period["endDate"][:10])
                current = period_start
                while current <= period_end:
                    dates_in_periods.add(current)
                    current += timedelta(days=1)
            dates_to_fetch = dates_in_periods & all_lookback_dates
            if len(dates_to_fetch) < len(all_lookback_dates):
                _LOGGER.debug(
                    "Billing periods cover %d of %d lookback days; skipping %d dates outside periods",
                    len(dates_to_fetch),
                    len(all_lookback_dates),
                    len(all_lookback_dates) - len(dates_to_fetch),
                )
        else:
            dates_to_fetch = all_lookback_dates

        datapoints: list[ElectricIrelandDatapoint] = []
        failed_dates: list[date] = []
        for target_date in sorted(dates_to_fetch):  # Sequential to avoid provider rate limiting.
            try:
                async with self._api_lock:
                    day_data = await self._api.get_hourly_usage(self._session, meter_ids, target_date)
                datapoints.extend(day_data)
            except CannotConnect:
                _LOGGER.warning(
                    "Failed to fetch hourly usage for %s (transient connection error), will retry on next poll",
                    target_date,
                )
                failed_dates.append(target_date)
            except CachedIdsInvalid:
                _LOGGER.warning("Cached meter IDs failed during data fetch, re-authenticating")
                self._session.cookie_jar.clear()
                async with self._api_lock:
                    meter_ids, discovered_ids = await self._api.authenticate(self._session, None)
                if discovered_ids is not None:
                    self.update_cached_meter_ids(discovered_ids)
                async with self._api_lock:
                    day_data = await self._api.get_hourly_usage(self._session, meter_ids, target_date)
                datapoints.extend(day_data)

        if failed_dates and not datapoints:
            raise CannotConnect(f"All {len(failed_dates)} lookback day(s) failed with connection errors")

        return RefreshResult(datapoints, len(self._bill_periods))
