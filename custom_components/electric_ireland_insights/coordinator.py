"""Coordinator for Electric Ireland Insights."""

from __future__ import annotations

import inspect
import logging
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import Literal

import aiohttp
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
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
    DEFAULT_DISCOUNT_PERCENTAGE,
    DOMAIN,
    INITIAL_LOOKBACK_DAYS,
    LOOKUP_DAYS,
    SCAN_INTERVAL,
    _redact_id,
    hash_account_id,
)
from .exceptions import CachedIdsInvalid, CannotConnect, InvalidAuth
from .types import (
    BillPeriod,
    CoordinatorData,
    ElectricIrelandDatapoint,
    MeterIds,
)

_LOGGER = logging.getLogger(__name__)


async def _close_session(session: aiohttp.ClientSession) -> None:
    """Close an aiohttp session, tolerating non-awaitable test mocks."""
    close_result = session.close()
    if inspect.isawaitable(close_result):
        await close_result


TARIFF_BUCKET_MAP_DISPLAY: dict[str, str] = {
    "flat_rate": "Flat Rate",
    "off_peak": "Off-Peak",
    "mid_peak": "Mid-Peak",
    "on_peak": "On-Peak",
}


class ElectricIrelandCoordinator(DataUpdateCoordinator[CoordinatorData]):
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
        self._bill_periods: list[BillPeriod] = []
        self._bill_periods_fetched_at: datetime | None = None
        self._session = async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar())
        self._closed = False

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
        session = self._session
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

            entry_data = self._config_entry.data
            cached_ids: MeterIds | None = None
            if entry_data.get("partner_id") and entry_data.get("contract_id") and entry_data.get("premise_id"):
                cached_ids = {
                    "partner": entry_data["partner_id"],
                    "contract": entry_data["contract_id"],
                    "premise": entry_data["premise_id"],
                }

            try:
                meter_ids, discovered_ids = await self._api.authenticate(session, cached_ids)
            except CannotConnect:
                if cached_ids is None:
                    raise
                _LOGGER.warning("Cached meter IDs failed during login, falling back to full discovery")
                session.cookie_jar.clear()
                meter_ids, discovered_ids = await self._api.authenticate(session, None)

            if discovered_ids is not None:
                new_data = {
                    **dict(self._config_entry.data),
                    "partner_id": discovered_ids["partner"],
                    "contract_id": discovered_ids["contract"],
                    "premise_id": discovered_ids["premise"],
                }
                self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
                self._bill_periods = []
                self._bill_periods_fetched_at = None
                _LOGGER.debug(
                    "Updated cached meter IDs: partner=%s",
                    _redact_id(discovered_ids["partner"]),
                )

            bill_period_stale = (
                self._bill_periods_fetched_at is None
                or (utcnow() - self._bill_periods_fetched_at).total_seconds() > 86400
            )
            if bill_period_stale:
                try:
                    self._bill_periods = await self._api.get_bill_periods(session, meter_ids)
                    self._bill_periods_fetched_at = utcnow()
                except CannotConnect:
                    _LOGGER.warning("Failed to fetch bill periods, falling back to full lookback window")
                    self._bill_periods = []
                    self._bill_periods_fetched_at = None

            yesterday = (dt_now() - timedelta(days=1)).date()
            all_lookback_dates = {yesterday - timedelta(days=i) for i in range(lookback)}

            if self._bill_periods:
                dates_in_periods: set[date] = set()
                for period in self._bill_periods:
                    period_start = date.fromisoformat(period["startDate"][:10])
                    period_end = date.fromisoformat(period["endDate"][:10])
                    d = period_start
                    while d <= period_end:
                        dates_in_periods.add(d)
                        d += timedelta(days=1)

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
            for target_date in sorted(dates_to_fetch):  # SEQUENTIAL — never parallel
                try:
                    day_data = await self._api.get_hourly_usage(
                        session,
                        meter_ids,
                        target_date,
                    )
                    datapoints.extend(day_data)
                except CannotConnect:
                    _LOGGER.warning(
                        "Failed to fetch hourly usage for %s (transient connection error), will retry on next poll",
                        target_date,
                    )
                    failed_dates.append(target_date)
                except CachedIdsInvalid:
                    _LOGGER.warning(
                        "Cached meter IDs failed during data fetch, re-authenticating",
                    )
                    session.cookie_jar.clear()
                    meter_ids, discovered_ids = await self._api.authenticate(
                        session,
                        None,
                    )
                    if discovered_ids is not None:
                        new_data = {
                            **dict(self._config_entry.data),
                            "partner_id": discovered_ids["partner"],
                            "contract_id": discovered_ids["contract"],
                            "premise_id": discovered_ids["premise"],
                        }
                        self.hass.config_entries.async_update_entry(
                            self._config_entry,
                            data=new_data,
                        )
                        self._bill_periods = []
                        self._bill_periods_fetched_at = None
                    day_data = await self._api.get_hourly_usage(
                        session,
                        meter_ids,
                        target_date,
                    )
                    datapoints.extend(day_data)

            if failed_dates and not datapoints:
                raise CannotConnect(f"All {len(failed_dates)} lookback day(s) failed with connection errors")

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

            await self._insert_statistics(
                datapoints,
                "consumption",
                f"{DOMAIN}:{self._account_hash}_consumption",
                UnitOfEnergy.KILO_WATT_HOUR,
            )
            await self._insert_statistics(
                datapoints,
                "cost",
                f"{DOMAIN}:{self._account_hash}_cost",
                "EUR",
            )
            discount = self._config_entry.options.get(CONF_DISCOUNT_PERCENTAGE, DEFAULT_DISCOUNT_PERCENTAGE)
            if discount:
                await self._insert_statistics(
                    datapoints,
                    "cost",
                    f"{DOMAIN}:{self._account_hash}_cost_discounted",
                    "EUR",
                    name_override=f"Electric Ireland Cost Discounted ({self._account_hash})",
                    discount=discount,
                )
            _LOGGER.debug(
                "Imported %d aggregate datapoints for account=%s",
                len(datapoints),
                _redact_id(self._account),
            )

            buckets: dict[str, list[ElectricIrelandDatapoint]] = {}
            for dp in datapoints:
                buckets.setdefault(dp["tariff_bucket"], []).append(dp)

            seen_buckets = set(buckets.keys())
            _LOGGER.debug(
                "Tariff buckets seen: %s (%s)",
                sorted(seen_buckets),
                {k: len(v) for k, v in buckets.items()},
            )
            if await self._should_import_per_tariff_statistics(seen_buckets):
                await self._insert_per_tariff_statistics(buckets, discount=discount)

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
                    "bill_periods_available": len(self._bill_periods),
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
                meter_ids, _ = await self._api.authenticate(session, None)
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

            if full_history:
                try:
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
                        meter_ids, _ = await self._api.authenticate(session, None)
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
                    day_data = await self._api.get_hourly_usage(session, meter_ids, target_date)
                    datapoints.extend(day_data)

            if datapoints:
                await self._insert_statistics(
                    datapoints,
                    "consumption",
                    f"{DOMAIN}:{self._account_hash}_consumption",
                    UnitOfEnergy.KILO_WATT_HOUR,
                )
                await self._insert_statistics(
                    datapoints,
                    "cost",
                    f"{DOMAIN}:{self._account_hash}_cost",
                    "EUR",
                )
                discount = self._config_entry.options.get(CONF_DISCOUNT_PERCENTAGE, DEFAULT_DISCOUNT_PERCENTAGE)
                if discount:
                    await self._insert_statistics(
                        datapoints,
                        "cost",
                        f"{DOMAIN}:{self._account_hash}_cost_discounted",
                        "EUR",
                        name_override=f"Electric Ireland Cost Discounted ({self._account_hash})",
                        discount=discount,
                    )

                buckets: dict[str, list[ElectricIrelandDatapoint]] = {}
                for dp in datapoints:
                    buckets.setdefault(dp["tariff_bucket"], []).append(dp)

                seen_buckets = set(buckets.keys())
                if await self._should_import_per_tariff_statistics(seen_buckets):
                    await self._insert_per_tariff_statistics(buckets, discount=discount)

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

    async def _should_import_per_tariff_statistics(self, seen_buckets: set[str]) -> bool:
        return bool(seen_buckets - {"flat_rate"})

    async def _insert_per_tariff_statistics(
        self,
        buckets: dict[str, list[ElectricIrelandDatapoint]],
        *,
        discount: int = 0,
    ) -> None:
        for bucket_name, bucket_dps in buckets.items():
            display = TARIFF_BUCKET_MAP_DISPLAY.get(bucket_name, bucket_name.replace("_", " ").title())
            await self._insert_statistics(
                bucket_dps,
                "consumption",
                f"{DOMAIN}:{self._account_hash}_consumption_{bucket_name}",
                UnitOfEnergy.KILO_WATT_HOUR,
                name_override=f"Electric Ireland Consumption {display} ({self._account_hash})",
            )
            await self._insert_statistics(
                bucket_dps,
                "cost",
                f"{DOMAIN}:{self._account_hash}_cost_{bucket_name}",
                "EUR",
                name_override=f"Electric Ireland Cost {display} ({self._account_hash})",
            )
            if discount:
                await self._insert_statistics(
                    bucket_dps,
                    "cost",
                    f"{DOMAIN}:{self._account_hash}_cost_{bucket_name}_discounted",
                    "EUR",
                    name_override=f"Electric Ireland Cost {display} Discounted ({self._account_hash})",
                    discount=discount,
                )

    async def _insert_statistics(
        self,
        datapoints: list[ElectricIrelandDatapoint],
        metric: Literal["consumption", "cost"],
        statistic_id: str,
        unit: str,
        *,
        name_override: str | None = None,
        discount: int = 0,
    ) -> None:
        filtered = []
        for dp in datapoints:
            value = dp.get(metric)
            if value is None:
                continue
            if metric == "cost" and discount:
                value = float(value) * (1 - discount / 100)
            start_ts = dp["start"]
            start = datetime.fromtimestamp(start_ts, tz=UTC).replace(minute=0, second=0, microsecond=0)
            filtered.append((start, float(value)))

        if not filtered:
            return

        filtered.sort(key=lambda x: x[0])
        overlap_start = filtered[0][0]

        stat_types: set[Literal["change", "last_reset", "max", "mean", "min", "state", "sum"]] = {"sum"}
        existing_before = await get_instance(self.hass).async_add_executor_job(
            partial(
                statistics_during_period,
                self.hass,
                datetime(1970, 1, 1, tzinfo=UTC),
                overlap_start,
                {statistic_id},
                "hour",
                None,
                stat_types,
            )
        )

        base_sum = 0.0
        rows = (existing_before or {}).get(statistic_id, [])
        if rows:
            base_sum = rows[-1].get("sum") or 0.0

        statistics: list[StatisticData] = []
        current_sum = base_sum
        for start, value in filtered:
            current_sum += value
            statistics.append(
                StatisticData(
                    start=start,
                    state=value,
                    sum=current_sum,
                )
            )

        default_name = f"Electric Ireland {'Consumption' if metric == 'consumption' else 'Cost'} ({self._account_hash})"
        stat_name = name_override or default_name
        metadata = StatisticMetaData(
            has_sum=True,
            mean_type=StatisticMeanType.NONE,
            name=stat_name,
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_of_measurement=unit,
            unit_class="energy" if metric == "consumption" else None,
        )

        async_add_external_statistics(self.hass, metadata, statistics)
