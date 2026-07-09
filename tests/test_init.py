"""Tests for the Electric Ireland Insights __init__ setup."""

import logging
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState

from custom_components.electric_ireland_insights.const import DOMAIN, hash_account_id

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


async def test_setup_entry_closes_session_on_first_refresh_failure(recorder_mock, hass, enable_custom_integrations, mock_config_entry):
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
