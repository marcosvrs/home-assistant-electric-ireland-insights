"""Tests for the Electric Ireland Insights __init__ setup."""

import logging
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.electric_ireland_insights import (
    _migrate_legacy_discount_to_options,
    _migrate_legacy_entity_ids,
)
from custom_components.electric_ireland_insights.const import CONF_DISCOUNT_PERCENTAGE, DOMAIN, hash_account_id

TEST_METER_IDS = {"partner": "P1", "contract": "C1", "premise": "PR1"}
ACCOUNT_HASH = hash_account_id("100000001")


async def test_setup_entry_success(recorder_mock, hass, enable_custom_integrations, mock_config_entry, caplog):
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

        assert "Setting up Electric Ireland entry" in caplog.text
        assert "Platforms forwarded" in caplog.text
        assert mock_config_entry.state == ConfigEntryState.LOADED


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


async def test_setup_entry_version_one_without_migration(recorder_mock, hass, enable_custom_integrations, caplog):
    """Test version 1 entries load directly without migration."""
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
        version=1,
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

    assert entry.version == 1
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
    """A duplicate raw-account entity is removed when the hashed entity exists."""
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

    _migrate_legacy_entity_ids(hass, mock_config_entry)

    assert registry.async_get(legacy.entity_id) is None
    retained = registry.async_get(hashed.entity_id)
    assert retained is not None
    assert retained.unique_id == f"{DOMAIN}_{account_hash}_{key}"


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

    _migrate_legacy_entity_ids(hass, mock_config_entry)

    migrated = registry.async_get(custom_entity_id)
    assert migrated is not None
    assert migrated.unique_id == f"{DOMAIN}_{account_hash}_{key}"
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
