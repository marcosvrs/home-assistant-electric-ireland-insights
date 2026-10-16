"""Electric Ireland Insights integration."""

from __future__ import annotations

import logging

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.typing import ConfigType

from .const import _IDENTITY_MIGRATION_RESTART_REQUIRED, DOMAIN, _redact_id
from .coordinator import ElectricIrelandCoordinator
from .identity_migration import async_migrate_identity

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type ElectricIrelandConfigEntry = ConfigEntry[ElectricIrelandCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Clear restart-required migration markers from the previous process."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if _IDENTITY_MIGRATION_RESTART_REQUIRED not in entry.data:
            continue
        hass.config_entries.async_update_entry(
            entry,
            data={key: value for key, value in entry.data.items() if key != _IDENTITY_MIGRATION_RESTART_REQUIRED},
        )
    return True


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate legacy config-entry versions before setup."""
    if config_entry.version < 2:
        _LOGGER.debug(
            "Migrating Electric Ireland entry from version %s to 2",
            config_entry.version,
        )
        hass.config_entries.async_update_entry(config_entry, version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ElectricIrelandConfigEntry) -> bool:
    _LOGGER.debug(
        "Setting up Electric Ireland entry, account=%s",
        _redact_id(entry.data["account_number"]),
    )
    if not await async_migrate_identity(hass, entry):
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="identity_collision",
        )
    coordinator = ElectricIrelandCoordinator(hass, entry)

    entry.runtime_data = coordinator

    try:
        await coordinator.async_migrate_legacy_statistics()
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
    if unload_ok:
        await entry.runtime_data.async_close()
    return unload_ok
