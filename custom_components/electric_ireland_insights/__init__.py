"""Electric Ireland Insights integration."""

from __future__ import annotations

import logging

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN, _redact_id
from .coordinator import ElectricIrelandCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type ElectricIrelandConfigEntry = ConfigEntry[ElectricIrelandCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: ElectricIrelandConfigEntry) -> bool:
    _LOGGER.debug(
        "Setting up Electric Ireland entry, account=%s",
        _redact_id(entry.data["account_number"]),
    )
    coordinator = ElectricIrelandCoordinator(hass, entry)

    entry.runtime_data = coordinator

    try:
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
