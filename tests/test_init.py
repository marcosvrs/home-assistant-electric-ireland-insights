"""Tests for the Electric Ireland Insights __init__ setup."""

import logging
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.device_registry import DeviceEntryDisabler
from homeassistant.helpers.device_registry import async_get as async_get_device_registry
from homeassistant.helpers.entity_registry import (
    RegistryEntryDisabler,
    RegistryEntryHider,
)
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from homeassistant.helpers.issue_registry import IssueSeverity, async_create_issue
from homeassistant.helpers.issue_registry import async_get as async_get_issue_registry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.electric_ireland_insights import (
    _migrate_legacy_config_entry_identity,
    _migrate_legacy_device,
    _migrate_legacy_discount_to_options,
    _migrate_legacy_entity_ids,
    _migrate_legacy_repair_issues,
    async_migrate_entry,
    async_setup_entry,
)
from custom_components.electric_ireland_insights.const import CONF_DISCOUNT_PERCENTAGE, DOMAIN, NAME, hash_account_id

TEST_METER_IDS = {"partner": "P1", "contract": "C1", "premise": "PR1"}
ACCOUNT_HASH = hash_account_id("100000001")


async def test_setup_entry_success(recorder_mock, hass, enable_custom_integrations, mock_config_entry, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.electric_ireland_insights")
    mock_config_entry.add_to_hass(hass)
    async_create_issue(
        hass,
        DOMAIN,
        f"data_gap_{mock_config_entry.data['account_number']}",
        is_fixable=False,
        severity=IssueSeverity.WARNING,
        translation_key="data_gap",
        translation_placeholders={"account": mock_config_entry.data["account_number"], "days": "7.0"},
    )
    with (
        patch("custom_components.electric_ireland_insights.coordinator.ElectricIrelandAPI") as mock_api_class,
        patch("custom_components.electric_ireland_insights.coordinator.async_create_clientsession"),
        patch(
            "custom_components.electric_ireland_insights.coordinator.get_last_statistics",
            return_value={},
        ),
    ):
        mock_api_instance = AsyncMock()
        mock_api_instance.authenticate = AsyncMock(return_value=(TEST_METER_IDS, TEST_METER_IDS))
        mock_api_instance.get_bill_periods = AsyncMock(return_value=[])
        mock_api_instance.get_hourly_usage = AsyncMock(return_value=[])
        mock_api_class.return_value = mock_api_instance

        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        issue_registry = async_get_issue_registry(hass)
        assert (
            issue_registry.async_get_issue(
                DOMAIN,
                f"data_gap_{mock_config_entry.data['account_number']}",
            )
            is None
        )
        assert "Setting up Electric Ireland entry" in caplog.text
        assert "Platforms forwarded" in caplog.text
        assert mock_config_entry.state == ConfigEntryState.LOADED


async def test_legacy_repair_issue_is_removed(hass, mock_config_entry):
    """Legacy raw-account repair issues are removed before refresh."""
    mock_config_entry.add_to_hass(hass)
    legacy_issue_id = f"data_gap_{mock_config_entry.data['account_number']}"
    async_create_issue(
        hass,
        DOMAIN,
        legacy_issue_id,
        is_fixable=False,
        severity=IssueSeverity.WARNING,
        translation_key="data_gap",
        translation_placeholders={"account": mock_config_entry.data["account_number"], "days": "7.0"},
    )

    _migrate_legacy_repair_issues(hass, mock_config_entry)

    issue_registry = async_get_issue_registry(hass)
    assert issue_registry.async_get_issue(DOMAIN, legacy_issue_id) is None


async def test_setup_entry_migrates_legacy_discount_to_options(
    recorder_mock, hass, enable_custom_integrations, mock_config_entry
):
    """Setup migrates a data-only discount before coordinator cleanup."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_DISCOUNT_PERCENTAGE: 20},
        options={},
    )
    with (
        patch("custom_components.electric_ireland_insights.coordinator.ElectricIrelandAPI") as mock_api_class,
        patch("custom_components.electric_ireland_insights.coordinator.async_create_clientsession"),
        patch(
            "custom_components.electric_ireland_insights.coordinator.get_last_statistics",
            return_value={},
        ),
    ):
        mock_api_instance = AsyncMock()
        mock_api_instance.authenticate = AsyncMock(return_value=(TEST_METER_IDS, TEST_METER_IDS))
        mock_api_instance.get_bill_periods = AsyncMock(return_value=[])
        mock_api_instance.get_hourly_usage = AsyncMock(return_value=[])
        mock_api_class.return_value = mock_api_instance

        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        await async_wait_recording_done(hass)

    assert mock_config_entry.options == {CONF_DISCOUNT_PERCENTAGE: 20}


async def test_legacy_discount_migration_does_not_override_options(hass, mock_config_entry):
    """An explicit options discount remains authoritative over legacy data."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_DISCOUNT_PERCENTAGE: 20},
        options={CONF_DISCOUNT_PERCENTAGE: 0},
    )

    _migrate_legacy_discount_to_options(hass, mock_config_entry)

    assert mock_config_entry.options == {CONF_DISCOUNT_PERCENTAGE: 0}


async def test_legacy_config_entry_identity_is_migrated(hass, mock_config_entry):
    """A raw config-entry identity becomes privacy-safe."""
    mock_config_entry.add_to_hass(hass)
    account = mock_config_entry.data["account_number"]
    hass.config_entries.async_update_entry(
        mock_config_entry,
        title=f"{NAME} ({account}) - Main meter",
        unique_id=account,
    )

    await _migrate_legacy_config_entry_identity(hass, mock_config_entry)

    assert mock_config_entry.unique_id == ACCOUNT_HASH
    assert mock_config_entry.title == f"{NAME} ({ACCOUNT_HASH}) - Main meter"


async def test_hashed_config_entry_title_is_migrated(hass, mock_config_entry):
    """A raw account title is migrated when the ID is already hashed."""
    mock_config_entry.add_to_hass(hass)
    account = mock_config_entry.data["account_number"]
    hass.config_entries.async_update_entry(
        mock_config_entry,
        title=f"{NAME} ({account}) - Main meter",
        unique_id=ACCOUNT_HASH,
    )

    await _migrate_legacy_config_entry_identity(hass, mock_config_entry)

    assert mock_config_entry.unique_id == ACCOUNT_HASH
    assert mock_config_entry.title == f"{NAME} ({ACCOUNT_HASH}) - Main meter"


async def test_legacy_config_entry_identity_duplicate_is_removed(hass, mock_config_entry):
    """A same-account hashed duplicate is removed before migration."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    mock_config_entry.add_to_hass(hass)
    account = mock_config_entry.data["account_number"]
    hass.config_entries.async_update_entry(
        mock_config_entry,
        title=f"{NAME} ({account}) - Main meter",
        unique_id=account,
    )
    hashed_entry = MockConfigEntry(
        domain=DOMAIN,
        data=mock_config_entry.data,
        unique_id=ACCOUNT_HASH,
    )
    hashed_entry.add_to_hass(hass)

    await _migrate_legacy_config_entry_identity(hass, mock_config_entry)

    assert mock_config_entry.unique_id == ACCOUNT_HASH
    assert mock_config_entry.title == f"{NAME} ({ACCOUNT_HASH}) - Main meter"
    assert hass.config_entries.async_get_entry(hashed_entry.entry_id) is None


async def test_legacy_config_entry_identity_preserves_duplicate_state(hass, mock_config_entry):
    """Duplicate data, options, and registry records survive identity migration."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    mock_config_entry.add_to_hass(hass)
    account = mock_config_entry.data["account_number"]
    hass.config_entries.async_update_entry(
        mock_config_entry,
        title=f"{NAME} ({account}) - Main meter",
        unique_id=account,
        options={CONF_DISCOUNT_PERCENTAGE: 20},
    )
    hashed_entry = MockConfigEntry(
        domain=DOMAIN,
        data={**mock_config_entry.data, "password": "new-password", "partner_id": "new-partner"},
        options={CONF_DISCOUNT_PERCENTAGE: 0},
        unique_id=ACCOUNT_HASH,
    )
    hashed_entry.add_to_hass(hass)

    device_registry = async_get_device_registry(hass)
    duplicate_device = device_registry.async_get_or_create(
        config_entry_id=hashed_entry.entry_id,
        identifiers={(DOMAIN, ACCOUNT_HASH)},
        name="Electric Ireland Insights",
    )
    device_registry.async_update_device(
        duplicate_device.id,
        area_id="hashed-area",
        name_by_user="Main meter",
    )
    entity_registry = async_get_entity_registry(hass)
    duplicate_entity = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "hashed-last-import",
        config_entry=hashed_entry,
        suggested_object_id="hashed_last_import",
        translation_key="last_import_time",
    )
    entity_registry.async_update_entity(
        duplicate_entity.entity_id,
        icon="mdi:flash",
        name="Custom import time",
    )

    await _migrate_legacy_config_entry_identity(hass, mock_config_entry)
    assert mock_config_entry.options == {CONF_DISCOUNT_PERCENTAGE: 0}
    assert mock_config_entry.data["password"] == "new-password"
    assert mock_config_entry.data["partner_id"] == "new-partner"
    assert hass.config_entries.async_get_entry(hashed_entry.entry_id) is None
    retained_device = device_registry.async_get_device(identifiers={(DOMAIN, ACCOUNT_HASH)})
    assert retained_device is not None
    assert retained_device.config_entries == {mock_config_entry.entry_id}
    assert retained_device.area_id == "hashed-area"
    assert retained_device.name_by_user == "Main meter"
    retained_entity = entity_registry.async_get(duplicate_entity.entity_id)
    assert retained_entity is not None
    assert retained_entity.config_entry_id == mock_config_entry.entry_id
    assert retained_entity.icon == "mdi:flash"
    assert retained_entity.name == "Custom import time"


async def test_legacy_config_entry_identity_removal_failure_keeps_raw_id(
    hass,
    mock_config_entry,
    caplog,
):
    """A failed duplicate removal does not move state to the raw entry."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    mock_config_entry.add_to_hass(hass)
    account = mock_config_entry.data["account_number"]
    hass.config_entries.async_update_entry(
        mock_config_entry,
        title=f"{NAME} ({account}) - Main meter",
        unique_id=account,
        options={CONF_DISCOUNT_PERCENTAGE: 20},
    )
    hashed_entry = MockConfigEntry(
        domain=DOMAIN,
        data={**mock_config_entry.data, "password": "hashed-password"},
        options={CONF_DISCOUNT_PERCENTAGE: 0},
        unique_id=ACCOUNT_HASH,
    )
    hashed_entry.add_to_hass(hass)

    device_registry = async_get_device_registry(hass)
    duplicate_device = device_registry.async_get_or_create(
        config_entry_id=hashed_entry.entry_id,
        identifiers={(DOMAIN, ACCOUNT_HASH)},
        name="Electric Ireland Insights",
    )
    device_registry.async_update_device(
        duplicate_device.id,
        area_id="hashed-area",
        name_by_user="Main meter",
    )
    entity_registry = async_get_entity_registry(hass)
    duplicate_entity = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "hashed-last-import",
        config_entry=hashed_entry,
        suggested_object_id="hashed_last_import",
        translation_key="last_import_time",
    )
    entity_registry.async_update_entity(
        duplicate_entity.entity_id,
        icon="mdi:flash",
        name="Custom import time",
    )
    caplog.set_level(logging.WARNING, logger="custom_components.electric_ireland_insights")

    with patch.object(
        hass.config_entries,
        "async_remove",
        new_callable=AsyncMock,
        return_value={"require_restart": True},
    ):
        await _migrate_legacy_config_entry_identity(hass, mock_config_entry)

    assert mock_config_entry.unique_id == account
    assert mock_config_entry.title == f"{NAME} ({ACCOUNT_HASH}) - Main meter"
    assert mock_config_entry.options == {CONF_DISCOUNT_PERCENTAGE: 20}
    assert mock_config_entry.data["password"] == "testpass"
    assert hass.config_entries.async_get_entry(hashed_entry.entry_id) is hashed_entry
    assert hashed_entry.data["password"] == "hashed-password"
    retained_device = device_registry.async_get_device(identifiers={(DOMAIN, ACCOUNT_HASH)})
    assert retained_device is not None
    assert retained_device.config_entries == {hashed_entry.entry_id}
    assert retained_device.area_id == "hashed-area"
    assert retained_device.name_by_user == "Main meter"
    retained_entity = entity_registry.async_get(duplicate_entity.entity_id)
    assert retained_entity is not None
    assert retained_entity.config_entry_id == hashed_entry.entry_id
    assert retained_entity.icon == "mdi:flash"
    assert retained_entity.name == "Custom import time"
    assert "Could not remove duplicate Electric Ireland config entry" in caplog.text


async def test_legacy_config_entry_identity_removal_exception_restores_registry_state(
    hass,
    mock_config_entry,
):
    """An exception during duplicate removal restores registry ownership."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    mock_config_entry.add_to_hass(hass)
    account = mock_config_entry.data["account_number"]
    hass.config_entries.async_update_entry(
        mock_config_entry,
        title=f"{NAME} ({account}) - Main meter",
        unique_id=account,
        options={CONF_DISCOUNT_PERCENTAGE: 20},
    )
    hashed_entry = MockConfigEntry(
        domain=DOMAIN,
        data=mock_config_entry.data,
        options={CONF_DISCOUNT_PERCENTAGE: 0},
        unique_id=ACCOUNT_HASH,
    )
    hashed_entry.add_to_hass(hass)

    device_registry = async_get_device_registry(hass)
    duplicate_device = device_registry.async_get_or_create(
        config_entry_id=hashed_entry.entry_id,
        identifiers={(DOMAIN, ACCOUNT_HASH)},
        name="Electric Ireland Insights",
    )
    device_registry.async_update_device(
        duplicate_device.id,
        area_id="hashed-area",
        name_by_user="Main meter",
    )
    entity_registry = async_get_entity_registry(hass)
    duplicate_entity = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "hashed-last-import",
        config_entry=hashed_entry,
        suggested_object_id="hashed_last_import",
        translation_key="last_import_time",
    )
    entity_registry.async_update_entity(
        duplicate_entity.entity_id,
        icon="mdi:flash",
        name="Custom import time",
    )

    with (
        patch.object(
            hass.config_entries,
            "async_remove",
            new_callable=AsyncMock,
            side_effect=RuntimeError("remove failed"),
        ),
        pytest.raises(RuntimeError, match="remove failed"),
    ):
        await _migrate_legacy_config_entry_identity(hass, mock_config_entry)

    assert mock_config_entry.unique_id == account
    assert mock_config_entry.options == {CONF_DISCOUNT_PERCENTAGE: 20}
    assert hass.config_entries.async_get_entry(hashed_entry.entry_id) is hashed_entry
    assert hashed_entry.options == {CONF_DISCOUNT_PERCENTAGE: 0}
    retained_device = device_registry.async_get_device(identifiers={(DOMAIN, ACCOUNT_HASH)})
    assert retained_device is not None
    assert retained_device.config_entries == {hashed_entry.entry_id}
    assert retained_device.area_id == "hashed-area"
    assert retained_device.name_by_user == "Main meter"
    retained_entity = entity_registry.async_get(duplicate_entity.entity_id)
    assert retained_entity is not None
    assert retained_entity.config_entry_id == hashed_entry.entry_id
    assert retained_entity.icon == "mdi:flash"
    assert retained_entity.name == "Custom import time"


async def test_setup_entry_stops_on_legacy_config_identity_collision(
    hass,
    mock_config_entry,
    caplog,
):
    """A privacy-safe ID owned by another account blocks setup."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    mock_config_entry.add_to_hass(hass)
    account = mock_config_entry.data["account_number"]
    hass.config_entries.async_update_entry(
        mock_config_entry,
        title=f"{NAME} ({account}) - Main meter",
        unique_id=account,
    )
    other_entry = MockConfigEntry(
        domain=DOMAIN,
        data={**mock_config_entry.data, "account_number": "100000002"},
        unique_id=ACCOUNT_HASH,
    )
    other_entry.add_to_hass(hass)
    caplog.set_level(logging.WARNING, logger="custom_components.electric_ireland_insights")

    with pytest.raises(ConfigEntryError) as error:
        await async_setup_entry(hass, mock_config_entry)
    assert error.value.translation_domain == DOMAIN
    assert error.value.translation_key == "identity_collision"

    assert mock_config_entry.unique_id == account
    assert mock_config_entry.title == f"{NAME} ({ACCOUNT_HASH}) - Main meter"
    assert hass.config_entries.async_get_entry(other_entry.entry_id) is other_entry
    assert "privacy-safe ID is already in use" in caplog.text


async def test_setup_entry_with_full_history_import(recorder_mock, hass, enable_custom_integrations, caplog):
    """Test setup entry triggers background task when import_full_history is True."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "username": "test@test.com",
            "password": "testpass",
            "account_number": "100000001",
            "partner_id": None,
            "contract_id": None,
            "premise_id": None,
            "import_full_history": True,
        },
        version=1,
        unique_id=ACCOUNT_HASH,
    )
    caplog.set_level(logging.DEBUG, logger="custom_components.electric_ireland_insights")
    entry.add_to_hass(hass)
    with (
        patch("custom_components.electric_ireland_insights.coordinator.ElectricIrelandAPI") as mock_api_class,
        patch("custom_components.electric_ireland_insights.coordinator.async_create_clientsession"),
        patch(
            "custom_components.electric_ireland_insights.coordinator.get_last_statistics",
            return_value={},
        ),
        patch(
            "custom_components.electric_ireland_insights.coordinator.ElectricIrelandCoordinator.async_tariff_backfill",
            new_callable=AsyncMock,
        ),
    ):
        mock_api_instance = AsyncMock()
        mock_api_instance.authenticate = AsyncMock(return_value=(TEST_METER_IDS, TEST_METER_IDS))
        mock_api_instance.get_bill_periods = AsyncMock(return_value=[])
        mock_api_instance.get_hourly_usage = AsyncMock(return_value=[])
        mock_api_class.return_value = mock_api_instance

        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state == ConfigEntryState.LOADED
        assert "Launching full history backfill background task" in caplog.text
        updated_entry = hass.config_entries.async_get_entry(entry.entry_id)
        assert updated_entry.data.get("import_full_history") is True


async def test_setup_entry_config_entry_not_ready(recorder_mock, hass, enable_custom_integrations, mock_config_entry):
    mock_config_entry.add_to_hass(hass)
    with (
        patch("custom_components.electric_ireland_insights.coordinator.ElectricIrelandAPI") as mock_api_class,
        patch("custom_components.electric_ireland_insights.coordinator.async_create_clientsession"),
        patch(
            "custom_components.electric_ireland_insights.coordinator.get_last_statistics",
            return_value={},
        ),
    ):
        from custom_components.electric_ireland_insights.exceptions import CannotConnect

        mock_api_instance = AsyncMock()
        mock_api_instance.authenticate = AsyncMock(side_effect=CannotConnect("timeout"))
        mock_api_class.return_value = mock_api_instance

        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        assert mock_config_entry.state == ConfigEntryState.SETUP_RETRY


async def test_unload_entry(recorder_mock, hass, enable_custom_integrations, mock_config_entry, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.electric_ireland_insights")
    mock_config_entry.add_to_hass(hass)
    with (
        patch("custom_components.electric_ireland_insights.coordinator.ElectricIrelandAPI") as mock_api_class,
        patch("custom_components.electric_ireland_insights.coordinator.async_create_clientsession"),
        patch(
            "custom_components.electric_ireland_insights.coordinator.get_last_statistics",
            return_value={},
        ),
    ):
        mock_api_instance = AsyncMock()
        mock_api_instance.authenticate = AsyncMock(return_value=(TEST_METER_IDS, TEST_METER_IDS))
        mock_api_instance.get_bill_periods = AsyncMock(return_value=[])
        mock_api_instance.get_hourly_usage = AsyncMock(return_value=[])
        mock_api_class.return_value = mock_api_instance

        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        assert mock_config_entry.state == ConfigEntryState.LOADED

        coordinator = mock_config_entry.runtime_data
        await hass.config_entries.async_unload(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        assert "Unloading Electric Ireland entry" in caplog.text
        assert coordinator._session.close.called
        assert mock_config_entry.state == ConfigEntryState.NOT_LOADED


async def test_unload_entry_keeps_session_open_when_platform_unload_fails(
    recorder_mock, hass, enable_custom_integrations, mock_config_entry
):
    """A failed platform unload leaves the coordinator available for retry."""
    mock_config_entry.add_to_hass(hass)
    with (
        patch("custom_components.electric_ireland_insights.coordinator.ElectricIrelandAPI") as mock_api_class,
        patch("custom_components.electric_ireland_insights.coordinator.async_create_clientsession"),
        patch(
            "custom_components.electric_ireland_insights.coordinator.get_last_statistics",
            return_value={},
        ),
    ):
        mock_api_instance = AsyncMock()
        mock_api_instance.authenticate = AsyncMock(return_value=(TEST_METER_IDS, TEST_METER_IDS))
        mock_api_instance.get_bill_periods = AsyncMock(return_value=[])
        mock_api_instance.get_hourly_usage = AsyncMock(return_value=[])
        mock_api_class.return_value = mock_api_instance

        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        coordinator = mock_config_entry.runtime_data
        with patch.object(
            hass.config_entries,
            "async_unload_platforms",
            new_callable=AsyncMock,
            return_value=False,
        ):
            result = await hass.config_entries.async_unload(mock_config_entry.entry_id)
            await hass.async_block_till_done()

        assert result is False
        assert mock_config_entry.state == ConfigEntryState.FAILED_UNLOAD
        assert coordinator._closed is False
        assert coordinator._session.close.called is False
        await coordinator.async_close()


async def test_async_migrate_entry_promotes_legacy_version(hass):
    """Legacy version 1 entries are promoted before setup."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"account_number": "100000001"},
        version=1,
        unique_id=ACCOUNT_HASH,
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True
    assert entry.version == 2


async def test_async_migrate_entry_keeps_current_version(hass):
    """Current config-entry versions do not need migration."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"account_number": "100000001"},
        version=2,
        unique_id=ACCOUNT_HASH,
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True
    assert entry.version == 2


async def test_setup_entry_version_two_reaches_setup(recorder_mock, hass, enable_custom_integrations, caplog):
    """Version 2 entries reach setup and load successfully."""
    caplog.set_level(logging.DEBUG, logger="custom_components.electric_ireland_insights")
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "username": "test@test.com",
            "password": "testpass",
            "account_number": "100000001",
            "partner_id": None,
            "contract_id": None,
            "premise_id": None,
        },
        version=2,
        unique_id=ACCOUNT_HASH,
    )
    entry.add_to_hass(hass)

    with (
        patch("custom_components.electric_ireland_insights.coordinator.ElectricIrelandAPI") as mock_api_class,
        patch("custom_components.electric_ireland_insights.coordinator.async_create_clientsession"),
        patch(
            "custom_components.electric_ireland_insights.coordinator.get_last_statistics",
            return_value={},
        ),
    ):
        mock_api_instance = AsyncMock()
        mock_api_instance.authenticate = AsyncMock(return_value=(TEST_METER_IDS, TEST_METER_IDS))
        mock_api_instance.get_bill_periods = AsyncMock(return_value=[])
        mock_api_instance.get_hourly_usage = AsyncMock(return_value=[])
        mock_api_class.return_value = mock_api_instance

        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.version == 2
    assert "Migrating Electric Ireland entry" not in caplog.text


async def test_setup_entry_closes_session_on_first_refresh_failure(
    recorder_mock, hass, enable_custom_integrations, mock_config_entry
):
    """If first refresh fails, the coordinator session must be closed before the exception propagates."""
    from custom_components.electric_ireland_insights.exceptions import CannotConnect

    mock_config_entry.add_to_hass(hass)
    mock_session = AsyncMock()
    with (
        patch("custom_components.electric_ireland_insights.coordinator.ElectricIrelandAPI") as mock_api_class,
        patch(
            "custom_components.electric_ireland_insights.coordinator.async_create_clientsession",
            return_value=mock_session,
        ),
        patch(
            "custom_components.electric_ireland_insights.coordinator.get_last_statistics",
            return_value={},
        ),
    ):
        mock_api_instance = AsyncMock()
        mock_api_instance.authenticate = AsyncMock(side_effect=CannotConnect("timeout"))
        mock_api_class.return_value = mock_api_instance

        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        assert mock_config_entry.state == ConfigEntryState.SETUP_RETRY
        assert mock_api_instance.get_hourly_usage.call_count == 0
        assert mock_session.close.called


async def test_unload_entry_closes_session_after_platforms(
    recorder_mock, hass, enable_custom_integrations, mock_config_entry
):
    """async_unload_entry must unload platforms before closing the coordinator session."""
    mock_config_entry.add_to_hass(hass)
    with (
        patch("custom_components.electric_ireland_insights.coordinator.ElectricIrelandAPI") as mock_api_class,
        patch("custom_components.electric_ireland_insights.coordinator.async_create_clientsession"),
        patch(
            "custom_components.electric_ireland_insights.coordinator.get_last_statistics",
            return_value={},
        ),
    ):
        mock_api_instance = AsyncMock()
        mock_api_instance.authenticate = AsyncMock(return_value=(TEST_METER_IDS, TEST_METER_IDS))
        mock_api_instance.get_bill_periods = AsyncMock(return_value=[])
        mock_api_instance.get_hourly_usage = AsyncMock(return_value=[])
        mock_api_class.return_value = mock_api_instance

        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        assert mock_config_entry.state == ConfigEntryState.LOADED

        coordinator = mock_config_entry.runtime_data
        original_close = coordinator.async_close
        order: list[str] = []

        async def tracking_close() -> None:
            order.append("close")
            await original_close()

        coordinator.async_close = tracking_close

        with patch.object(
            hass.config_entries,
            "async_unload_platforms",
            new_callable=AsyncMock,
        ) as mock_unload:

            async def tracking_unload(*args, **kwargs):
                order.append("unload")
                return True

            mock_unload.side_effect = tracking_unload

            result = await hass.config_entries.async_unload(mock_config_entry.entry_id)
            await hass.async_block_till_done()

        assert result is True
        assert order == ["unload", "close"]


async def test_legacy_device_registry_record_is_migrated(hass, mock_config_entry):
    """A raw-account device becomes the privacy-safe device in place."""
    mock_config_entry.add_to_hass(hass)
    registry = async_get_device_registry(hass)
    account = mock_config_entry.data["account_number"]
    legacy = registry.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, account)},
        name=f"Electric Ireland Insights ({account})",
    )
    registry.async_update_device(legacy.id, name_by_user="Main meter")

    _migrate_legacy_device(hass, mock_config_entry)

    migrated = registry.async_get_device(identifiers={(DOMAIN, ACCOUNT_HASH)})
    assert migrated is not None
    assert migrated.id == legacy.id
    assert migrated.identifiers == {(DOMAIN, ACCOUNT_HASH)}
    assert migrated.name == f"Electric Ireland Insights ({ACCOUNT_HASH})"
    assert migrated.name_by_user == "Main meter"
    assert registry.async_get_device(identifiers={(DOMAIN, account)}) is None


async def test_duplicate_legacy_device_is_merged(hass, mock_config_entry):
    """A same-entry duplicate device is merged without losing its customizations."""
    mock_config_entry.add_to_hass(hass)
    device_registry = async_get_device_registry(hass)
    entity_registry = async_get_entity_registry(hass)
    account = mock_config_entry.data["account_number"]
    legacy = device_registry.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, account)},
        name=f"Electric Ireland Insights ({account})",
    )
    device_registry.async_update_device(
        legacy.id,
        area_id="legacy-area",
        disabled_by=DeviceEntryDisabler.USER,
        labels={"legacy"},
        name_by_user="Main meter",
        serial_number=account,
    )
    hashed = device_registry.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, ACCOUNT_HASH)},
        name=f"Electric Ireland Insights ({ACCOUNT_HASH})",
    )
    device_registry.async_update_device(
        hashed.id,
        area_id="hashed-area",
        labels={"hashed"},
        name_by_user="Hashed meter",
    )
    legacy_entity = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "legacy-device-entity",
        config_entry=mock_config_entry,
        suggested_object_id="legacy_device_entity",
    )
    hashed_entity = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "hashed-device-entity",
        config_entry=mock_config_entry,
        suggested_object_id="hashed_device_entity",
    )
    entity_registry.async_update_entity(legacy_entity.entity_id, device_id=legacy.id)
    entity_registry.async_update_entity(hashed_entity.entity_id, device_id=hashed.id)

    _migrate_legacy_device(hass, mock_config_entry)

    migrated = device_registry.async_get_device(identifiers={(DOMAIN, ACCOUNT_HASH)})
    assert migrated is not None
    assert migrated.id == legacy.id
    assert migrated.identifiers == {(DOMAIN, ACCOUNT_HASH)}
    assert migrated.area_id == "legacy-area"
    assert migrated.disabled_by is DeviceEntryDisabler.USER
    assert migrated.labels == {"hashed", "legacy"}
    assert migrated.name_by_user == "Main meter"
    assert migrated.name == f"Electric Ireland Insights ({ACCOUNT_HASH})"
    assert migrated.serial_number == ACCOUNT_HASH
    assert entity_registry.async_get(legacy_entity.entity_id).device_id == legacy.id
    assert entity_registry.async_get(hashed_entity.entity_id).device_id == legacy.id
    assert device_registry.async_get_device(identifiers={(DOMAIN, account)}) is None
    assert device_registry.deleted_devices.get(hashed.id) is None
    assert device_registry.deleted_devices.get(legacy.id) is None


async def test_legacy_device_migration_leaves_cross_entry_collision(hass, mock_config_entry):
    """A device identifier owned by another entry is not merged destructively."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    mock_config_entry.add_to_hass(hass)
    other_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "username": "other@test.com",
            "password": "testpass",
            "account_number": "100000002",
        },
        unique_id="other-account",
    )
    other_entry.add_to_hass(hass)

    registry = async_get_device_registry(hass)
    account = mock_config_entry.data["account_number"]
    legacy = registry.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, account)},
        name=f"Electric Ireland Insights ({account})",
    )
    hashed = registry.async_get_or_create(
        config_entry_id=other_entry.entry_id,
        identifiers={(DOMAIN, ACCOUNT_HASH)},
        name=f"Electric Ireland Insights ({ACCOUNT_HASH})",
    )

    _migrate_legacy_device(hass, mock_config_entry)

    assert registry.async_get_device(identifiers={(DOMAIN, account)}) is legacy
    assert registry.async_get_device(identifiers={(DOMAIN, ACCOUNT_HASH)}) is hashed


async def test_legacy_device_migration_skips_device_owned_by_another_entry(hass, mock_config_entry):
    """A raw device owned by another entry is not rewritten."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    mock_config_entry.add_to_hass(hass)
    other_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "username": "other@test.com",
            "password": "testpass",
            "account_number": "100000002",
        },
        unique_id="other-account",
    )
    other_entry.add_to_hass(hass)

    registry = async_get_device_registry(hass)
    account = mock_config_entry.data["account_number"]
    legacy_name = f"Electric Ireland Insights ({account})"
    legacy = registry.async_get_or_create(
        config_entry_id=other_entry.entry_id,
        identifiers={(DOMAIN, account)},
        name=legacy_name,
    )

    _migrate_legacy_device(hass, mock_config_entry)

    assert registry.async_get_device(identifiers={(DOMAIN, account)}) is legacy
    assert legacy.identifiers == {(DOMAIN, account)}
    assert legacy.name == legacy_name
    assert registry.async_get_device(identifiers={(DOMAIN, ACCOUNT_HASH)}) is None


async def test_legacy_device_migration_skips_shared_device(hass, mock_config_entry):
    """A device shared with another entry is not rewritten."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    mock_config_entry.add_to_hass(hass)
    other_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "username": "other@test.com",
            "password": "testpass",
            "account_number": "100000002",
        },
        unique_id="other-account",
    )
    other_entry.add_to_hass(hass)

    registry = async_get_device_registry(hass)
    account = mock_config_entry.data["account_number"]
    legacy_name = f"Electric Ireland Insights ({account})"
    legacy = registry.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, account)},
        name=legacy_name,
    )
    registry.async_update_device(legacy.id, add_config_entry_id=other_entry.entry_id)

    _migrate_legacy_device(hass, mock_config_entry)

    shared = registry.async_get_device(identifiers={(DOMAIN, account)})
    assert shared is not None
    assert shared.id == legacy.id
    assert shared.config_entries == {mock_config_entry.entry_id, other_entry.entry_id}
    assert shared.identifiers == {(DOMAIN, account)}
    assert shared.name == legacy_name
    assert registry.async_get_device(identifiers={(DOMAIN, ACCOUNT_HASH)}) is None


async def test_legacy_diagnostic_entity_ids_are_migrated(hass, mock_config_entry):
    """Legacy diagnostic IDs are renamed without retaining raw account IDs."""
    mock_config_entry.add_to_hass(hass)
    registry = async_get_entity_registry(hass)
    account = mock_config_entry.data["account_number"]

    for key in ("last_import_time", "data_freshness_days"):
        registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{DOMAIN}_{account}_{key}",
            config_entry=mock_config_entry,
            suggested_object_id=f"{DOMAIN}_{account}_{key}",
            translation_key=key,
        )

    _migrate_legacy_entity_ids(hass, mock_config_entry)

    account_hash = hash_account_id(account)
    for key in ("last_import_time", "data_freshness_days"):
        migrated = registry.async_get(f"sensor.{DOMAIN}_{account_hash}_{key}")
        assert migrated is not None
        assert migrated.unique_id == f"{DOMAIN}_{account_hash}_{key}"
        assert registry.async_get(f"sensor.{DOMAIN}_{account}_{key}") is None


async def test_duplicate_legacy_diagnostic_entity_is_removed(hass, mock_config_entry):
    """A duplicate raw-account entity is removed after customizations transfer."""
    mock_config_entry.add_to_hass(hass)
    registry = async_get_entity_registry(hass)
    account = mock_config_entry.data["account_number"]
    account_hash = hash_account_id(account)
    key = "last_import_time"

    hashed = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_{account_hash}_{key}",
        config_entry=mock_config_entry,
        suggested_object_id=f"{DOMAIN}_{account_hash}_{key}",
        translation_key=key,
    )
    legacy = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_{account}_{key}",
        config_entry=mock_config_entry,
        suggested_object_id=f"{DOMAIN}_{account}_{key}",
        translation_key=key,
    )
    registry.async_update_entity(
        legacy.entity_id,
        aliases={"sensor.legacy_import_time"},
        disabled_by=RegistryEntryDisabler.USER,
        hidden_by=RegistryEntryHider.USER,
        icon="mdi:flash",
        labels={"important"},
        name="Custom import time",
    )

    _migrate_legacy_entity_ids(hass, mock_config_entry)

    assert registry.async_get(legacy.entity_id) is None
    retained = registry.async_get(hashed.entity_id)
    assert retained is not None
    assert retained.unique_id == f"{DOMAIN}_{account_hash}_{key}"
    assert retained.aliases == {"sensor.legacy_import_time"}
    assert retained.disabled_by is RegistryEntryDisabler.USER
    assert retained.hidden_by is RegistryEntryHider.USER
    assert retained.icon == "mdi:flash"
    assert retained.labels == {"important"}
    assert retained.name == "Custom import time"


async def test_enabled_legacy_entity_wins_over_disabled_hashed_entity(hass, mock_config_entry):
    """An enabled legacy diagnostic entity stays enabled after merging."""
    mock_config_entry.add_to_hass(hass)
    registry = async_get_entity_registry(hass)
    account = mock_config_entry.data["account_number"]
    account_hash = hash_account_id(account)
    key = "last_import_time"

    hashed = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_{account_hash}_{key}",
        config_entry=mock_config_entry,
        suggested_object_id=f"{DOMAIN}_{account_hash}_{key}",
        translation_key=key,
    )
    legacy = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_{account}_{key}",
        config_entry=mock_config_entry,
        suggested_object_id=f"{DOMAIN}_{account}_{key}",
        translation_key=key,
    )
    registry.async_update_entity(
        hashed.entity_id,
        disabled_by=RegistryEntryDisabler.INTEGRATION,
    )

    _migrate_legacy_entity_ids(hass, mock_config_entry)

    assert registry.async_get(legacy.entity_id) is None
    retained = registry.async_get(hashed.entity_id)
    assert retained is not None
    assert retained.disabled_by is None


async def test_disabled_legacy_entity_preserves_disabled_state_when_merged(hass, mock_config_entry):
    """Merging two integration-disabled entities retains the disabled state."""
    mock_config_entry.add_to_hass(hass)
    registry = async_get_entity_registry(hass)
    account = mock_config_entry.data["account_number"]
    account_hash = hash_account_id(account)
    key = "last_import_time"

    hashed = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_{account_hash}_{key}",
        config_entry=mock_config_entry,
        suggested_object_id=f"{DOMAIN}_{account_hash}_{key}",
        translation_key=key,
    )
    legacy = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_{account}_{key}",
        config_entry=mock_config_entry,
        suggested_object_id=f"{DOMAIN}_{account}_{key}",
        translation_key=key,
    )
    registry.async_update_entity(
        hashed.entity_id,
        disabled_by=RegistryEntryDisabler.INTEGRATION,
    )
    registry.async_update_entity(
        legacy.entity_id,
        disabled_by=RegistryEntryDisabler.INTEGRATION,
    )

    _migrate_legacy_entity_ids(hass, mock_config_entry)

    assert registry.async_get(legacy.entity_id) is None
    retained = registry.async_get(hashed.entity_id)
    assert retained is not None
    assert retained.disabled_by is RegistryEntryDisabler.INTEGRATION


async def test_custom_legacy_diagnostic_entity_id_is_preserved(hass, mock_config_entry):
    """A customized legacy entity ID is preserved while its unique ID is migrated."""
    mock_config_entry.add_to_hass(hass)
    registry = async_get_entity_registry(hass)
    account = mock_config_entry.data["account_number"]
    account_hash = hash_account_id(account)
    key = "last_import_time"

    legacy = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_{account}_{key}",
        config_entry=mock_config_entry,
        suggested_object_id=f"{DOMAIN}_{account}_{key}",
        translation_key=key,
    )
    custom_entity_id = "sensor.my_custom_last_import"
    registry.async_update_entity(legacy.entity_id, new_entity_id=custom_entity_id)

    _migrate_legacy_entity_ids(hass, mock_config_entry)

    migrated = registry.async_get(custom_entity_id)
    assert migrated is not None
    assert migrated.unique_id == f"{DOMAIN}_{account_hash}_{key}"
    assert registry.async_get(f"sensor.{DOMAIN}_{account_hash}_{key}") is None


async def test_custom_legacy_entity_wins_over_duplicate_hashed_entity(hass, mock_config_entry):
    """A customized legacy entity keeps its ID when a hashed duplicate exists."""
    mock_config_entry.add_to_hass(hass)
    registry = async_get_entity_registry(hass)
    account = mock_config_entry.data["account_number"]
    account_hash = hash_account_id(account)
    key = "last_import_time"

    hashed = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_{account_hash}_{key}",
        config_entry=mock_config_entry,
        suggested_object_id=f"{DOMAIN}_{account_hash}_{key}",
        translation_key=key,
    )
    legacy = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_{account}_{key}",
        config_entry=mock_config_entry,
        suggested_object_id=f"{DOMAIN}_{account}_{key}",
        translation_key=key,
    )
    custom_entity_id = "sensor.my_custom_last_import"
    registry.async_update_entity(legacy.entity_id, new_entity_id=custom_entity_id)

    registry.async_update_entity(
        hashed.entity_id,
        aliases={"sensor.hashed_import_time"},
        area_id="hashed-area",
        categories={"custom": "hashed"},
        disabled_by=RegistryEntryDisabler.USER,
        hidden_by=RegistryEntryHider.USER,
        icon="mdi:flash",
        labels={"hashed"},
        name="Hashed import time",
    )

    _migrate_legacy_entity_ids(hass, mock_config_entry)

    migrated = registry.async_get(custom_entity_id)
    assert migrated is not None
    assert migrated.unique_id == f"{DOMAIN}_{account_hash}_{key}"
    assert migrated.aliases == {"sensor.hashed_import_time"}
    assert migrated.area_id == "hashed-area"
    assert migrated.categories == {"custom": "hashed"}
    assert migrated.disabled_by is RegistryEntryDisabler.USER
    assert migrated.hidden_by is RegistryEntryHider.USER
    assert migrated.icon == "mdi:flash"
    assert migrated.labels == {"hashed"}
    assert migrated.name == "Hashed import time"
    assert registry.async_get(hashed.entity_id) is None


async def test_legacy_entity_migration_leaves_cross_entry_collisions(hass, mock_config_entry):
    """Legacy entities remain unchanged when hashed IDs are already occupied."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    mock_config_entry.add_to_hass(hass)
    other_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "username": "other@test.com",
            "password": "testpass",
            "account_number": "100000002",
        },
        unique_id="other-account",
    )
    other_entry.add_to_hass(hass)

    registry = async_get_entity_registry(hass)
    account = mock_config_entry.data["account_number"]
    account_hash = hash_account_id(account)

    cross_entry_key = "last_import_time"
    hashed = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_{account_hash}_{cross_entry_key}",
        config_entry=other_entry,
        suggested_object_id=f"{DOMAIN}_{account_hash}_{cross_entry_key}",
        translation_key=cross_entry_key,
    )
    legacy = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_{account}_{cross_entry_key}",
        config_entry=mock_config_entry,
        suggested_object_id=f"{DOMAIN}_{account}_{cross_entry_key}",
        translation_key=cross_entry_key,
    )

    entity_id_key = "data_freshness_days"
    occupied = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_unrelated_{entity_id_key}",
        config_entry=mock_config_entry,
        suggested_object_id=f"{DOMAIN}_{account_hash}_{entity_id_key}",
        translation_key=entity_id_key,
    )
    legacy_with_entity_collision = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DOMAIN}_{account}_{entity_id_key}",
        config_entry=mock_config_entry,
        suggested_object_id=f"{DOMAIN}_{account}_{entity_id_key}",
        translation_key=entity_id_key,
    )

    _migrate_legacy_entity_ids(hass, mock_config_entry)

    assert registry.async_get(legacy.entity_id) is legacy
    assert registry.async_get(hashed.entity_id) is hashed
    assert registry.async_get(legacy_with_entity_collision.entity_id) is legacy_with_entity_collision
    assert registry.async_get(occupied.entity_id) is occupied
