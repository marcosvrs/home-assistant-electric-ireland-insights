"""Coordinator for Electric Ireland Insights."""

from __future__ import annotations

import inspect
import logging
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import Literal

import aiohttp
from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.issue_registry import IssueSeverity, async_create_issue, async_delete_issue
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.dt import now as dt_now
from homeassistant.util.dt import utcnow

from .api import ElectricIrelandAPI
from .const import (
    CONF_DISCOUNT_PERCENTAGE,
    DATA_GAP_THRESHOLD_DAYS,
    DOMAIN,
    INITIAL_LOOKBACK_DAYS,
    LOOKUP_DAYS,
    SCAN_INTERVAL,
    _get_api_lock,
    _redact_id,
    configured_discount_percentage,
    hash_account_id,
)
from .exceptions import CachedIdsInvalid, CannotConnect, InvalidAuth
from .refresh import RefreshWorkflow
from .statistics import StatisticsMixin
from .types import CoordinatorData, ElectricIrelandDatapoint

_LOGGER = logging.getLogger(__name__)


async def _close_session(session: aiohttp.ClientSession) -> None:
    """Close an aiohttp session, tolerating non-awaitable test mocks."""
    close_result = session.close()
    if inspect.isawaitable(close_result):
        await close_result


class ElectricIrelandCoordinator(StatisticsMixin, DataUpdateCoordinator[CoordinatorData]):
    """Coordinator to fetch EI data and import external statistics."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
            always_update=True,
        )
        self._config_entry = config_entry
        self._account = config_entry.data["account_number"]
        self._account_hash = hash_account_id(self._account)
        self._api = ElectricIrelandAPI(
            config_entry.data["username"],
            config_entry.data["password"],
            self._account,
        )
        self._last_update_success = True
        self._has_imported_before = False
        self._session = async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar())
        self._api_lock = _get_api_lock()
        self._closed = False
        self._refresh = RefreshWorkflow(
            hass,
            config_entry,
            self._api,
            self._session,
            self._api_lock,
            now=dt_now,
        )

    def _get_discount_percentage(self) -> int:
        """Return the effective discount, including legacy entry data."""
        return configured_discount_percentage(
            self._config_entry.options.get(CONF_DISCOUNT_PERCENTAGE),
            self._config_entry.data.get(CONF_DISCOUNT_PERCENTAGE),
        )

    def _check_data_gap(self, result: CoordinatorData) -> None:
        latest_ts = result.get("latest_data_timestamp")
        if latest_ts is not None:
            gap_days = (utcnow() - latest_ts).total_seconds() / 86400
            if gap_days > DATA_GAP_THRESHOLD_DAYS:
                async_create_issue(
                    self.hass,
                    DOMAIN,
                    f"data_gap_{self._account_hash}",
                    is_fixable=False,
                    severity=IssueSeverity.WARNING,
                    translation_key="data_gap",
                    translation_placeholders={
                        "account": self._account_hash,
                        "days": str(round(gap_days, 1)),
                    },
                )
                _LOGGER.debug(
                    "Created repair issue: data_gap_%s (%.1f days stale)",
                    _redact_id(self._account),
                    gap_days,
                )
            else:
                async_delete_issue(self.hass, DOMAIN, f"data_gap_{self._account_hash}")

    async def _async_update_data(self) -> CoordinatorData:
        was_successful = self._last_update_success

        def _mark_success(result: CoordinatorData) -> CoordinatorData:
            self._last_update_success = True
            if not was_successful:
                _LOGGER.info("Connection restored — data import resumed")
            self._check_data_gap(result)
            return result

        try:
            stat_id = f"{DOMAIN}:{self._account_hash}_consumption"
            statistic_types: set[Literal["last_reset", "max", "mean", "min", "state", "sum"]] = {"sum"}
            existing = await get_instance(self.hass).async_add_executor_job(
                partial(get_last_statistics, self.hass, 1, stat_id, True, statistic_types)
            )
            lookback = LOOKUP_DAYS

            if not self._has_imported_before:
                self._has_imported_before = bool(existing)
                _LOGGER.debug(
                    "Statistics check: existing=%s, lookback=%d days",
                    bool(existing),
                    lookback,
                )

            refresh_result = await self._refresh.async_fetch(lookback)
            datapoints = refresh_result.datapoints
            bill_periods_available = refresh_result.bill_periods_available

            if not datapoints:
                if self._has_imported_before:
                    if self.data is not None:
                        return _mark_success(self.data)
                    return _mark_success(
                        {
                            "last_import": None,
                            "datapoint_count": 0,
                            "latest_data_timestamp": None,
                            "import_error": "No new data available",
                            "appliance_count": 0,
                            "bill_periods_available": 0,
                            "tariff_buckets_seen": 0,
                        }
                    )
                return _mark_success(
                    {
                        "last_import": utcnow(),
                        "datapoint_count": 0,
                        "latest_data_timestamp": None,
                        "import_error": None,
                        "appliance_count": 0,
                        "bill_periods_available": 0,
                        "tariff_buckets_seen": 0,
                    }
                )

            self._has_imported_before = True

            discount = self._get_discount_percentage()
            seen_buckets = await self.async_import_datapoints(datapoints, discount=discount)
            last_ts = max((dp["start"] for dp in datapoints), default=None)
            latest_data_ts = datetime.fromtimestamp(last_ts, tz=UTC) if last_ts else None

            self.hass.bus.async_fire(
                f"{DOMAIN}_data_imported",
                {
                    "account": self._account_hash,
                    "datapoint_count": len(datapoints),
                    "latest_data_timestamp": latest_data_ts.isoformat() if latest_data_ts else None,
                    "tariff_buckets": sorted(seen_buckets),
                },
            )
            _LOGGER.debug(
                "Fired %s_data_imported event: %d datapoints, latest=%s",
                DOMAIN,
                len(datapoints),
                latest_data_ts,
            )

            return _mark_success(
                {
                    "last_import": utcnow(),
                    "datapoint_count": len(datapoints),
                    "latest_data_timestamp": latest_data_ts,
                    "import_error": None,
                    "appliance_count": 0,
                    "bill_periods_available": bill_periods_available,
                    "tariff_buckets_seen": len(seen_buckets),
                }
            )

        except InvalidAuth as err:
            self._last_update_success = False
            _LOGGER.error("Authentication failed")
            raise ConfigEntryAuthFailed from err
        except CannotConnect as err:
            if was_successful:
                _LOGGER.warning("Connection lost — data import paused")
            self._last_update_success = False
            raise UpdateFailed("Connection error") from err
        except (ConfigEntryAuthFailed, UpdateFailed):
            self._last_update_success = False
            raise
        except Exception as err:
            self._last_update_success = False
            _LOGGER.exception("Unexpected error during update")
            raise UpdateFailed("Unexpected error") from err

    async def async_close(self) -> None:
        """Close the coordinator's aiohttp session."""
        if self._closed:
            return
        self._closed = True
        await _close_session(self._session)

    async def async_tariff_backfill(self, *, full_history: bool = False) -> None:
        """Background backfill of historical data.

        When full_history is True, uses all available bill periods (6-13 months).
        When full_history is False, fetches the last INITIAL_LOOKBACK_DAYS (30 days).
        Skips if tariff_stats_initialized is set and full_history is False.
        """
        if not full_history and self._config_entry.data.get("tariff_stats_initialized"):
            return

        session = async_create_clientsession(self.hass, cookie_jar=aiohttp.CookieJar())
        try:
            try:
                async with self._api_lock:
                    meter_ids, discovered_ids = await self._api.authenticate(session, None)
            except InvalidAuth:
                _LOGGER.warning("Background backfill failed due to invalid auth")
                async_create_issue(
                    self.hass,
                    DOMAIN,
                    f"backfill_auth_failed_{self._account_hash}",
                    is_fixable=False,
                    severity=IssueSeverity.ERROR,
                    translation_key="backfill_auth_failed",
                    translation_placeholders={"account": self._account_hash},
                )
                return
            except CannotConnect:
                _LOGGER.warning("Background backfill failed due to connection error")
                async_create_issue(
                    self.hass,
                    DOMAIN,
                    f"backfill_connection_failed_{self._account_hash}",
                    is_fixable=False,
                    severity=IssueSeverity.WARNING,
                    translation_key="backfill_connection_failed",
                    translation_placeholders={"account": self._account_hash},
                )
                return

            if discovered_ids is not None:
                self._refresh.update_cached_meter_ids(discovered_ids)

            if full_history:
                try:
                    async with self._api_lock:
                        bill_periods = await self._api.get_bill_periods(session, meter_ids)
                except CannotConnect:
                    _LOGGER.warning("Full history backfill: cannot fetch bill periods, will retry")
                    async_create_issue(
                        self.hass,
                        DOMAIN,
                        f"backfill_connection_failed_{self._account_hash}",
                        is_fixable=False,
                        severity=IssueSeverity.WARNING,
                        translation_key="backfill_connection_failed",
                        translation_placeholders={"account": self._account_hash},
                    )
                    return
                if not bill_periods:
                    _LOGGER.warning("Full history requested but no bill periods available; will retry")
                    return
            else:
                try:
                    async with self._api_lock:
                        bill_periods = await self._api.get_bill_periods(session, meter_ids)
                except CannotConnect:
                    bill_periods = []

            yesterday = (dt_now() - timedelta(days=1)).date()

            if bill_periods:
                dates_in_periods: set[date] = set()
                for period in bill_periods:
                    period_start = date.fromisoformat(period["startDate"][:10])
                    period_end = date.fromisoformat(period["endDate"][:10])
                    if period_start > yesterday:
                        continue
                    period_end = min(period_end, yesterday)
                    d = period_start
                    while d <= period_end:
                        dates_in_periods.add(d)
                        d += timedelta(days=1)

                if full_history:
                    all_dates = dates_in_periods
                else:
                    all_lookback_dates = {yesterday - timedelta(days=i) for i in range(INITIAL_LOOKBACK_DAYS)}
                    all_dates = dates_in_periods & all_lookback_dates
            else:
                all_dates = {yesterday - timedelta(days=i) for i in range(INITIAL_LOOKBACK_DAYS)}

            if not all_dates:
                _LOGGER.warning("No backfill dates available within bill periods")
                return

            _LOGGER.info(
                "Starting background backfill (%d days, %s to %s)",
                len(all_dates),
                min(all_dates),
                max(all_dates),
            )

            dates = sorted(all_dates)

            datapoints: list[ElectricIrelandDatapoint] = []
            failed_dates: list[date] = []
            for target_date in dates:
                try:
                    async with self._api_lock:
                        day_data = await self._api.get_hourly_usage(session, meter_ids, target_date)
                    datapoints.extend(day_data)
                except CannotConnect:
                    _LOGGER.warning(
                        "Backfill: failed to fetch hourly usage for %s (transient connection error)",
                        target_date,
                    )
                    failed_dates.append(target_date)
                except CachedIdsInvalid:
                    _LOGGER.debug("Backfill: CachedIdsInvalid on %s, re-authenticating", target_date)
                    session.cookie_jar.clear()
                    try:
                        async with self._api_lock:
                            meter_ids, discovered_ids = await self._api.authenticate(session, None)
                    except (InvalidAuth, CannotConnect):
                        _LOGGER.warning(
                            "Backfill: re-authentication failed on %s, aborting backfill",
                            target_date,
                        )
                        async_create_issue(
                            self.hass,
                            DOMAIN,
                            f"backfill_auth_failed_{self._account_hash}",
                            is_fixable=False,
                            severity=IssueSeverity.ERROR,
                            translation_key="backfill_auth_failed",
                            translation_placeholders={"account": self._account_hash},
                        )
                        return
                    if discovered_ids is not None:
                        self._refresh.update_cached_meter_ids(discovered_ids)
                    async with self._api_lock:
                        day_data = await self._api.get_hourly_usage(session, meter_ids, target_date)
                    datapoints.extend(day_data)

            if datapoints:
                discount = self._get_discount_percentage()
                await self.async_import_datapoints(datapoints, discount=discount)
            new_data = {**dict(self._config_entry.data)}
            if datapoints:
                new_data["tariff_stats_initialized"] = True
            if full_history and not failed_dates:
                new_data["import_full_history"] = False
            self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
            async_delete_issue(self.hass, DOMAIN, f"backfill_auth_failed_{self._account_hash}")
            if failed_dates:
                _LOGGER.warning(
                    "Background backfill completed with %d failed day(s)",
                    len(failed_dates),
                )
                async_create_issue(
                    self.hass,
                    DOMAIN,
                    f"backfill_connection_failed_{self._account_hash}",
                    is_fixable=False,
                    severity=IssueSeverity.WARNING,
                    translation_key="backfill_connection_failed",
                    translation_placeholders={"account": self._account_hash},
                )
            else:
                _LOGGER.info("Background backfill complete (%d datapoints)", len(datapoints))
                async_delete_issue(self.hass, DOMAIN, f"backfill_connection_failed_{self._account_hash}")
            async_delete_issue(self.hass, DOMAIN, f"backfill_failed_{self._account_hash}")

        except Exception:
            _LOGGER.exception("Unexpected error during background backfill")
            async_create_issue(
                self.hass,
                DOMAIN,
                f"backfill_failed_{self._account_hash}",
                is_fixable=False,
                severity=IssueSeverity.WARNING,
                translation_key="backfill_failed",
                translation_placeholders={"account": self._account_hash},
            )
        finally:
            await _close_session(session)
