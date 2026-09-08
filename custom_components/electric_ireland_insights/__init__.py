"""Electric Ireland Insights integration."""

from __future__ import annotations

import logging

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry

from .const import DOMAIN, _redact_id, hash_account_id
from .coordinator import ElectricIrelandCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type ElectricIrelandConfigEntry = ConfigEntry[ElectricIrelandCoordinator]

_LEGACY_DIAGNOSTIC_ENTITY_KEYS = frozenset({"last_import_time", "data_freshness_days"})


def _migrate_legacy_entity_ids(hass: HomeAssistant, entry: ElectricIrelandConfigEntry) -> None:
    """Rename legacy diagnostic entity IDs that exposed the account number."""
    registry = async_get_entity_registry(hass)
    account = entry.data["account_number"]
    account_hash = hash_account_id(account)

    for entity in tuple(registry.entities.values()):
        key = entity.translation_key
        legacy_unique_id = f"{DOMAIN}_{account}_{key}"
        if (
            entity.config_entry_id != entry.entry_id
            or entity.platform != DOMAIN
            or key not in _LEGACY_DIAGNOSTIC_ENTITY_KEYS
            or entity.unique_id != legacy_unique_id
        ):
            continue

        legacy_entity_id = f"sensor.{DOMAIN}_{account}_{key}"
        new_entity_id = f"sensor.{DOMAIN}_{account_hash}_{key}"
        new_unique_id = f"{DOMAIN}_{account_hash}_{key}"
        registered_entity_id = registry.async_get_entity_id("sensor", DOMAIN, new_unique_id)
        if registered_entity_id is not None and registered_entity_id != entity.entity_id:
            registered_entity = registry.async_get(registered_entity_id)
            if registered_entity is not None and registered_entity.config_entry_id == entry.entry_id:
                if entity.entity_id == legacy_entity_id:
                    registry.async_remove(entity.entity_id)
                    _LOGGER.info("Removed duplicate legacy diagnostic entity key=%s", key)
                else:
                    registry.async_remove(registered_entity.entity_id)
                    registry.async_update_entity(entity.entity_id, new_unique_id=new_unique_id)
                    _LOGGER.info("Migrated customized legacy diagnostic entity key=%s", key)
            else:
                _LOGGER.warning("Could not migrate legacy diagnostic entity key=%s", key)
            continue
        if entity.entity_id == legacy_entity_id:
            if registry.async_get(new_entity_id) not in (None, entity):
                _LOGGER.warning("Could not migrate legacy diagnostic entity key=%s", key)
                continue
            registry.async_update_entity(
                entity.entity_id,
                new_entity_id=new_entity_id,
                new_unique_id=new_unique_id,
            )
        else:
            registry.async_update_entity(entity.entity_id, new_unique_id=new_unique_id)
        _LOGGER.info("Migrated legacy diagnostic entity key=%s to a privacy-safe ID", key)


async def async_setup_entry(hass: HomeAssistant, entry: ElectricIrelandConfigEntry) -> bool:
    _LOGGER.debug(
        "Setting up Electric Ireland entry, account=%s",
        _redact_id(entry.data["account_number"]),
    )
    _migrate_legacy_entity_ids(hass, entry)
    coordinator = ElectricIrelandCoordinator(hass, entry)

    entry.runtime_data = coordinator

    try:
        await coordinator.async_clear_discounted_statistics()
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await coordinator.async_close()
        raise

    entry.async_on_unload(coordinator.async_add_listener(lambda: None))

    import_full = entry.data.get("import_full_history", False)
    needs_backfill = import_full or not entry.data.get("tariff_stats_initialized")
    if needs_backfill:
        entry.async_create_background_task(
            hass,
            coordinator.async_tariff_backfill(full_history=import_full),
            "electric_ireland_backfill",
        )
        _LOGGER.debug(
            "Launching %s backfill background task, account=%s",
            "full history" if import_full else "initial 30-day",
            _redact_id(entry.data["account_number"]),
        )
    else:
        _LOGGER.debug("No backfill needed, tariff_stats already initialized")

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _LOGGER.debug(
        "Platforms forwarded for account=%s",
        _redact_id(entry.data["account_number"]),
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ElectricIrelandConfigEntry) -> bool:
    _LOGGER.debug(
        "Unloading Electric Ireland entry, account=%s",
        _redact_id(entry.data["account_number"]),
    )
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    await entry.runtime_data.async_close()
    return unload_ok
