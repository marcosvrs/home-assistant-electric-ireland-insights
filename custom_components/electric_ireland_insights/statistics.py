"""Import and migrate Electric Ireland external statistics."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Literal, cast

from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder.core import Recorder
from homeassistant.components.recorder.db_schema import Statistics
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.components.recorder.tasks import RecorderTask
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance, session_scope

from .const import DOMAIN, LOOKUP_DAYS, _redact_id
from .types import ElectricIrelandDatapoint

if TYPE_CHECKING:
    from homeassistant.components.energy.data import EnergyPreferences


_LOGGER = logging.getLogger(__name__)


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


class StatisticsMixin(ABC):
    """Recorder statistics behavior shared by refresh and setup flows."""

    hass: HomeAssistant
    _account: str
    _account_hash: str

    @abstractmethod
    def _get_discount_percentage(self) -> int:
        """Return the effective discount configured for the account."""
        raise NotImplementedError

    async def async_migrate_legacy_statistics(self) -> None:
        """Rename raw-account statistic metadata to privacy-safe IDs."""
        recorder = get_instance(self.hass)
        metadata = await recorder.async_add_executor_job(partial(get_metadata, self.hass, statistic_source=DOMAIN))
        legacy_prefix = f"{DOMAIN}:{self._account}_"
        statistic_replacements: dict[str, str] = {}
        hashed_prefix = f"{DOMAIN}:{self._account_hash}_"
        for statistic_id in sorted(metadata):
            if not statistic_id.startswith(hashed_prefix):
                continue

            hashed_metadata_id, hashed_metadata = metadata[statistic_id]
            hashed_name = hashed_metadata.get("name")
            if hashed_name is None or self._account not in hashed_name:
                continue

            sanitized_metadata: StatisticMetaData = {
                **hashed_metadata,
                "name": hashed_name.replace(self._account, self._account_hash),
                "statistic_id": statistic_id,
            }
            await self._async_update_statistic_metadata(
                recorder,
                statistic_id=statistic_id,
                metadata_id=hashed_metadata_id,
                old_metadata=hashed_metadata,
                new_metadata=sanitized_metadata,
            )
            metadata[statistic_id] = (hashed_metadata_id, sanitized_metadata)

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
                    sum_deltas: dict[bool, dict[float | None, float]] = {
                        True: {},
                        False: {},
                    }
                    for is_old, rows in ((True, old_rows), (False, new_rows)):
                        previous_sum: float | None = None
                        for row in rows:
                            if row.sum is not None:
                                if previous_sum is None:
                                    sum_deltas[is_old][row.start_ts] = row.sum
                                else:
                                    sum_deltas[is_old][row.start_ts] = row.sum - previous_sum
                                previous_sum = row.sum

                    merged_rows = [(row, True) for row in old_only_rows] + [(row, False) for row in new_rows]
                    merged_rows.sort(key=lambda item: item[0].start_ts or 0)
                    running_sum = merged_rows[0][0].sum or 0.0
                    for index, (row, is_old) in enumerate(merged_rows):
                        if index:
                            if row.state is not None:
                                running_sum += row.state
                            elif row.sum is not None:
                                running_sum += sum_deltas[is_old].get(row.start_ts, 0.0)
                        row.sum = running_sum
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

    async def _async_update_statistic_metadata(
        self,
        recorder: Recorder,
        *,
        statistic_id: str,
        metadata_id: int,
        old_metadata: StatisticMetaData,
        new_metadata: StatisticMetaData,
    ) -> None:
        """Wait for one recorder-thread statistic metadata update."""

        def _update(instance: Recorder) -> None:
            with session_scope(session=instance.get_session()) as session:
                instance.statistics_meta_manager.update_or_add(
                    session,
                    new_metadata,
                    {statistic_id: (metadata_id, old_metadata)},
                )

        await self._async_run_recorder_task(recorder, _update)

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

    async def async_import_datapoints(
        self,
        datapoints: list[ElectricIrelandDatapoint],
        *,
        discount: int,
    ) -> set[str]:
        """Import aggregate and applicable per-tariff statistics for datapoints."""
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
        for datapoint in datapoints:
            buckets.setdefault(datapoint["tariff_bucket"], []).append(datapoint)

        seen_buckets = set(buckets)
        _LOGGER.debug(
            "Tariff buckets seen: %s (%s)",
            sorted(seen_buckets),
            {bucket: len(bucket_datapoints) for bucket, bucket_datapoints in buckets.items()},
        )
        if seen_buckets - {"flat_rate"}:
            await self._insert_per_tariff_statistics(buckets, discount=discount)
        return seen_buckets

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
