"""Electric Ireland Insights integration."""

from __future__ import annotations

import logging

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import (
    DeviceEntry,
    DeviceRegistry,
)
from homeassistant.helpers.device_registry import async_get as async_get_device_registry
from homeassistant.helpers.entity_registry import (
    EntityRegistry,
    RegistryEntry,
)
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry

from .const import CONF_DISCOUNT_PERCENTAGE, DOMAIN, NAME, _redact_id, hash_account_id
from .coordinator import ElectricIrelandCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type ElectricIrelandConfigEntry = ConfigEntry[ElectricIrelandCoordinator]

_LEGACY_DIAGNOSTIC_ENTITY_KEYS = frozenset({"last_import_time", "data_freshness_days"})


def _migrate_legacy_discount_to_options(hass: HomeAssistant, entry: ElectricIrelandConfigEntry) -> None:
    """Move a legacy data discount into config entry options."""
    if entry.options.get(CONF_DISCOUNT_PERCENTAGE) is not None:
        return

    legacy_discount = entry.data.get(CONF_DISCOUNT_PERCENTAGE)
    if legacy_discount is None:
        return

    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_DISCOUNT_PERCENTAGE: int(legacy_discount),
        },
    )
    _LOGGER.info("Migrated legacy discount percentage into config entry options")


def _migrate_legacy_config_entry_identity(hass: HomeAssistant, entry: ElectricIrelandConfigEntry) -> None:
    """Migrate raw config-entry identity to privacy-safe values."""
    account = entry.data["account_number"]
    account_hash = hash_account_id(account)
    legacy_title = f"{NAME} ({account})"
    if entry.unique_id != account and entry.title != legacy_title:
        return

    hass.config_entries.async_update_entry(
        entry,
        title=f"{NAME} ({account_hash})" if entry.title == legacy_title else entry.title,
        unique_id=account_hash if entry.unique_id == account else entry.unique_id,
    )
    _LOGGER.info("Migrated legacy config entry identity")


def _merge_device_registry_customizations(
    registry: DeviceRegistry,
    target: DeviceEntry,
    source: DeviceEntry,
) -> None:
    """Preserve user device customizations while removing a duplicate."""
    registry.async_update_device(
        target.id,
        area_id=target.area_id if target.area_id is not None else source.area_id,
        disabled_by=target.disabled_by if target.disabled_by is not None else source.disabled_by,
        labels=target.labels | source.labels,
        name_by_user=target.name_by_user if target.name_by_user is not None else source.name_by_user,
    )


def _migrate_legacy_device(hass: HomeAssistant, entry: ElectricIrelandConfigEntry) -> None:
    """Migrate a raw-account device to privacy-safe identifiers."""
    device_registry = async_get_device_registry(hass)
    account = entry.data["account_number"]
    account_identifier = (DOMAIN, account)
    hashed_identifier = (DOMAIN, hash_account_id(account))
    legacy_device = device_registry.async_get_device(identifiers={account_identifier})
    if legacy_device is None:
        return
    if entry.entry_id not in legacy_device.config_entries:
        _LOGGER.warning("Could not migrate legacy device: device belongs to another entry")
        return

    hashed_device = device_registry.async_get_device(identifiers={hashed_identifier})
    if hashed_device is not None and hashed_device.id != legacy_device.id:
        if legacy_device.config_entries == {entry.entry_id} and hashed_device.config_entries == {entry.entry_id}:
            entity_registry = async_get_entity_registry(hass)
            for entity in tuple(entity_registry.entities.values()):
                if entity.device_id == hashed_device.id:
                    entity_registry.async_update_entity(entity.entity_id, device_id=legacy_device.id)
            _merge_device_registry_customizations(device_registry, legacy_device, hashed_device)
            device_registry.async_remove_device(hashed_device.id)
            merged_identifiers = (legacy_device.identifiers | hashed_device.identifiers) - {account_identifier}
            merged_identifiers.add(hashed_identifier)
            merged_connections = legacy_device.connections | hashed_device.connections
            merged_name = legacy_device.name or hashed_device.name
            merged_serial_number = legacy_device.serial_number or hashed_device.serial_number
            device_registry.async_update_device(
                legacy_device.id,
                new_identifiers=merged_identifiers,
                new_connections=merged_connections,
                name=merged_name.replace(account, hashed_identifier[1]) if merged_name is not None else None,
                serial_number=merged_serial_number.replace(account, hashed_identifier[1])
                if merged_serial_number is not None
                else None,
            )
            _LOGGER.info("Merged duplicate legacy device into privacy-safe device")
        else:
            _LOGGER.warning("Could not migrate legacy device: privacy-safe identifier is already in use")
        return

    if legacy_device.config_entries != {entry.entry_id}:
        _LOGGER.warning("Could not migrate legacy device: device is shared with another entry")
        return

    new_name = legacy_device.name.replace(account, hashed_identifier[1]) if legacy_device.name else None
    new_serial_number = (
        legacy_device.serial_number.replace(account, hashed_identifier[1])
        if legacy_device.serial_number is not None
        else None
    )
    device_registry.async_update_device(
        legacy_device.id,
        new_identifiers=(legacy_device.identifiers - {account_identifier}) | {hashed_identifier},
        name=new_name,
        serial_number=new_serial_number,
    )
    _LOGGER.info("Migrated legacy device to privacy-safe identifier")


def _merge_entity_customizations(
    registry: EntityRegistry,
    source: RegistryEntry,
    target: RegistryEntry,
) -> None:
    """Preserve user registry customizations while removing a duplicate."""
    registry.async_update_entity(
        target.entity_id,
        aliases=source.aliases | target.aliases,
        area_id=source.area_id if source.area_id is not None else target.area_id,
        categories={**target.categories, **source.categories},
        disabled_by=source.disabled_by if source.disabled_by is not None else target.disabled_by,
        hidden_by=source.hidden_by if source.hidden_by is not None else target.hidden_by,
        icon=source.icon if source.icon is not None else target.icon,
        labels=source.labels | target.labels,
        name=source.name if source.name is not None else target.name,
    )


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
                    _merge_entity_customizations(registry, entity, registered_entity)
                    registry.async_remove(entity.entity_id)
                    _LOGGER.info("Removed duplicate legacy diagnostic entity key=%s", key)
                else:
                    _merge_entity_customizations(registry, registered_entity, entity)
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
    _migrate_legacy_config_entry_identity(hass, entry)
    _migrate_legacy_discount_to_options(hass, entry)
    _migrate_legacy_device(hass, entry)
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
    if unload_ok:
        await entry.runtime_data.async_close()
    return unload_ok
