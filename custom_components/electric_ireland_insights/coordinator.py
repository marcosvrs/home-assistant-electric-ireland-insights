"""Coordinator for Electric Ireland Insights."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Literal, cast

import aiohttp
from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder.core import Recorder
from homeassistant.components.recorder.db_schema import Statistics
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.components.recorder.tasks import RecorderTask
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.issue_registry import IssueSeverity, async_create_issue, async_delete_issue
from homeassistant.helpers.recorder import get_instance, session_scope
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.dt import now as dt_now
from homeassistant.util.dt import utcnow

if TYPE_CHECKING:
    from homeassistant.components.energy.data import EnergyPreferences

from .api import ElectricIrelandAPI
from .const import (
    CONF_DISCOUNT_PERCENTAGE,
    DATA_GAP_THRESHOLD_DAYS,
    DEFAULT_DISCOUNT_PERCENTAGE,
    DOMAIN,
    INITIAL_LOOKBACK_DAYS,
    LOOKUP_DAYS,
    SCAN_INTERVAL,
    _get_api_lock,
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


def _replace_statistic_references(
    value: object,
    replacements: Mapping[str, str],
    *,
    legacy_prefix: str,
    hashed_prefix: str,
) -> object:
    """Replace statistic IDs in a nested Energy Dashboard preference."""
    if isinstance(value, str):
        replacement = replacements.get(value)
        if replacement is not None:
            return replacement
        if value.startswith(legacy_prefix):
            return f"{hashed_prefix}{value[len(legacy_prefix) :]}"
        return value
    if isinstance(value, list):
        return [
            _replace_statistic_references(
                item,
                replacements,
                legacy_prefix=legacy_prefix,
                hashed_prefix=hashed_prefix,
            )
            for item in value
        ]
    if isinstance(value, dict):
        return {
            key: _replace_statistic_references(
                item,
                replacements,
                legacy_prefix=legacy_prefix,
                hashed_prefix=hashed_prefix,
            )
            for key, item in value.items()
        }
    return value


@dataclass(slots=True)
class _RecorderCallbackTask(RecorderTask):
    """Run a recorder-thread callback and report its completion."""

    action: Callable[[Recorder], None]
    on_done: Callable[[Exception | None], None]

    def run(self, instance: Recorder) -> None:
        """Run the callback on the recorder thread."""
        try:
            self.action(instance)
        except Exception as err:
            self.on_done(err)
            raise
        else:
            self.on_done(None)


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
        self._api_lock = _get_api_lock()
        self._closed = False

    def _update_cached_meter_ids(self, discovered_ids: MeterIds) -> None:
        """Persist meter identifiers discovered during authentication."""
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

    def _get_discount_percentage(self) -> int:
        """Return the configured discount, including legacy entry data."""
        discount = self._config_entry.options.get(CONF_DISCOUNT_PERCENTAGE)
        if discount is None:
            discount = self._config_entry.data.get(
                CONF_DISCOUNT_PERCENTAGE,
                DEFAULT_DISCOUNT_PERCENTAGE,
            )
        return int(discount)

    async def async_migrate_legacy_statistics(self) -> None:
        """Rename raw-account statistic metadata to privacy-safe IDs."""
        recorder = get_instance(self.hass)
        metadata = await recorder.async_add_executor_job(partial(get_metadata, self.hass, statistic_source=DOMAIN))
        legacy_prefix = f"{DOMAIN}:{self._account}_"
        statistic_replacements: dict[str, str] = {}
        for old_statistic_id in sorted(metadata):
            if not old_statistic_id.startswith(legacy_prefix):
                continue

            new_statistic_id = f"{DOMAIN}:{self._account_hash}_{old_statistic_id[len(legacy_prefix) :]}"
            if new_statistic_id in metadata:
                _LOGGER.warning(
                    "Could not migrate legacy statistic identity for account=%s: "
                    "merged legacy history into hashed data",
                    _redact_id(self._account),
                )
                await self._async_merge_legacy_statistic(
                    recorder,
                    old_statistic_id=old_statistic_id,
                    new_statistic_id=new_statistic_id,
                )
                statistic_replacements[old_statistic_id] = new_statistic_id
                metadata.pop(old_statistic_id)
                continue

            metadata_id, old_metadata = metadata[old_statistic_id]
            old_name = old_metadata.get("name")
            new_metadata: StatisticMetaData = {
                **old_metadata,
                "name": old_name.replace(self._account, self._account_hash) if old_name is not None else None,
                "statistic_id": new_statistic_id,
            }
            await self._async_rename_legacy_statistic(
                recorder,
                old_statistic_id=old_statistic_id,
                new_statistic_id=new_statistic_id,
                metadata_id=metadata_id,
                old_metadata=old_metadata,
                new_metadata=new_metadata,
            )
            statistic_replacements[old_statistic_id] = new_statistic_id
            metadata[new_statistic_id] = metadata.pop(old_statistic_id)

        await self._async_migrate_energy_preferences(statistic_replacements)

    async def _async_merge_legacy_statistic(
        self,
        recorder: Recorder,
        *,
        old_statistic_id: str,
        new_statistic_id: str,
    ) -> None:
        """Merge legacy rows into an existing privacy-safe statistic."""

        def _merge(instance: Recorder) -> None:
            with session_scope(session=instance.get_session()) as session:
                old_metadata = instance.statistics_meta_manager.get(session, old_statistic_id)
                new_metadata = instance.statistics_meta_manager.get(session, new_statistic_id)
                if old_metadata is None or new_metadata is None:
                    return

                old_metadata_id = old_metadata[0]
                new_metadata_id = new_metadata[0]
                current_name = new_metadata[1].get("name")
                if current_name is not None:
                    sanitized_name = current_name.replace(self._account, self._account_hash)
                    if sanitized_name != current_name:
                        instance.statistics_meta_manager.update_or_add(
                            session,
                            {
                                **new_metadata[1],
                                "name": sanitized_name,
                                "statistic_id": new_statistic_id,
                            },
                            {new_statistic_id: (new_metadata_id, new_metadata[1])},
                        )
                old_rows = (
                    session.query(Statistics)
                    .filter(Statistics.metadata_id == old_metadata_id)
                    .order_by(Statistics.start_ts)
                    .all()
                )
                new_rows = (
                    session.query(Statistics)
                    .filter(Statistics.metadata_id == new_metadata_id)
                    .order_by(Statistics.start_ts)
                    .all()
                )
                new_start_ts = {row.start_ts for row in new_rows}
                old_only_rows = [row for row in old_rows if row.start_ts not in new_start_ts]

                if old_only_rows:
                    merged_rows = [(row, True) for row in old_only_rows] + [(row, False) for row in new_rows]
                    merged_rows.sort(key=lambda item: item[0].start_ts or 0)
                    running_sum = merged_rows[0][0].sum or 0.0
                    last_original_sum: dict[bool, float | None] = {
                        True: None,
                        False: None,
                    }
                    for index, (row, is_old) in enumerate(merged_rows):
                        original_sum = row.sum
                        if index:
                            if row.state is not None:
                                running_sum += row.state
                            else:
                                previous_original_sum = last_original_sum[is_old]
                                if original_sum is not None and previous_original_sum is not None:
                                    running_sum += original_sum - previous_original_sum
                        row.sum = running_sum
                        last_original_sum[is_old] = original_sum

                    for row in old_only_rows:
                        row.metadata_id = new_metadata_id
                    session.flush()

                instance.statistics_meta_manager.delete(session, [old_statistic_id])

        await self._async_run_recorder_task(recorder, _merge)

    async def _async_migrate_energy_preferences(
        self,
        replacements: Mapping[str, str],
    ) -> None:
        """Retarget Energy Dashboard statistic references after migration."""
        manager = await async_get_manager(self.hass)
        if manager.data is None:
            return

        preferences = cast(
            "EnergyPreferences",
            _replace_statistic_references(
                deepcopy(manager.data),
                replacements,
                legacy_prefix=f"{DOMAIN}:{self._account}_",
                hashed_prefix=f"{DOMAIN}:{self._account_hash}_",
            ),
        )
        if preferences == manager.data:
            return
        await manager.async_update(preferences)

    async def _async_rename_legacy_statistic(
        self,
        recorder: Recorder,
        *,
        old_statistic_id: str,
        new_statistic_id: str,
        metadata_id: int,
        old_metadata: StatisticMetaData,
        new_metadata: StatisticMetaData,
    ) -> None:
        """Wait for one recorder-thread statistic rename."""

        def _rename(instance: Recorder) -> None:
            with session_scope(session=instance.get_session()) as session:
                instance.statistics_meta_manager.update_statistic_id(
                    session,
                    DOMAIN,
                    old_statistic_id,
                    new_statistic_id,
                )
                instance.statistics_meta_manager.update_or_add(
                    session,
                    new_metadata,
                    {new_statistic_id: (metadata_id, old_metadata)},
                )

        await self._async_run_recorder_task(recorder, _rename)

    async def _async_clear_statistics(self, recorder: Recorder, statistic_ids: list[str]) -> None:
        """Wait for one recorder-thread statistic cleanup."""

        def _clear(instance: Recorder) -> None:
            with session_scope(session=instance.get_session()) as session:
                instance.statistics_meta_manager.delete(session, statistic_ids)

        await self._async_run_recorder_task(recorder, _clear)

    async def _async_run_recorder_task(
        self,
        recorder: Recorder,
        action: Callable[[Recorder], None],
    ) -> None:
        """Wait for one recorder task callback."""
        loop = asyncio.get_running_loop()
        completed = loop.create_future()

        def _on_done(error: Exception | None) -> None:
            def _resolve() -> None:
                if completed.done():
                    return
                if error is None:
                    completed.set_result(None)
                else:
                    completed.set_exception(error)

            loop.call_soon_threadsafe(_resolve)

        recorder.queue_task(_RecorderCallbackTask(action, _on_done))
        await completed

    async def async_clear_discounted_statistics(self) -> None:
        """Remove stale discounted statistics when no discount is configured."""
        discount = self._get_discount_percentage()
        if discount:
            return

        metadata = await get_instance(self.hass).async_add_executor_job(
            partial(get_metadata, self.hass, statistic_source=DOMAIN)
        )
        statistic_prefixes = (
            f"{DOMAIN}:{self._account_hash}_cost",
            f"{DOMAIN}:{self._account}_cost",
        )
        statistic_ids = sorted(
            statistic_id
            for statistic_id in metadata
            if any(statistic_id.startswith(prefix) for prefix in statistic_prefixes)
            and statistic_id.endswith("_discounted")
        )
        if not statistic_ids:
            return

        await self._async_clear_statistics(get_instance(self.hass), statistic_ids)
        _LOGGER.info(
            "Removed %d discounted statistics because the configured discount is 0",
            len(statistic_ids),
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
                async with self._api_lock:
                    meter_ids, discovered_ids = await self._api.authenticate(session, cached_ids)
            except CannotConnect:
                if cached_ids is None:
                    raise
                _LOGGER.warning("Cached meter IDs failed during login, falling back to full discovery")
                session.cookie_jar.clear()
                async with self._api_lock:
                    meter_ids, discovered_ids = await self._api.authenticate(session, None)

            if discovered_ids is not None:
                self._update_cached_meter_ids(discovered_ids)

            bill_period_stale = (
                self._bill_periods_fetched_at is None
                or (utcnow() - self._bill_periods_fetched_at).total_seconds() > 86400
            )
            if bill_period_stale:
                try:
                    async with self._api_lock:
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
                    async with self._api_lock:
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
                    async with self._api_lock:
                        meter_ids, discovered_ids = await self._api.authenticate(
                            session,
                            None,
                        )
                    if discovered_ids is not None:
                        self._update_cached_meter_ids(discovered_ids)
                    async with self._api_lock:
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
            discount = self._get_discount_percentage()
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
        await self._async_tariff_backfill(full_history=full_history)

    async def _async_tariff_backfill(self, *, full_history: bool = False) -> None:
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
                self._update_cached_meter_ids(discovered_ids)

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
                        self._update_cached_meter_ids(discovered_ids)
                    async with self._api_lock:
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
                discount = self._get_discount_percentage()
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

    async def _async_get_base_sum(self, statistic_id: str, overlap_start: datetime) -> float:
        """Return the cumulative sum immediately before the incoming window."""
        statistic_types: set[Literal["change", "last_reset", "max", "mean", "min", "state", "sum"]] = {"sum"}
        search_starts = (
            overlap_start - timedelta(days=LOOKUP_DAYS + 1),
            datetime(1970, 1, 1, tzinfo=UTC),
        )
        for start_time in search_starts:
            existing_before = await get_instance(self.hass).async_add_executor_job(
                partial(
                    statistics_during_period,
                    self.hass,
                    start_time,
                    overlap_start,
                    {statistic_id},
                    "hour",
                    None,
                    statistic_types,
                )
            )
            rows = (existing_before or {}).get(statistic_id, [])
            if rows:
                return rows[-1].get("sum") or 0.0
        return 0.0

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

        base_sum = await self._async_get_base_sum(statistic_id, overlap_start)

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
