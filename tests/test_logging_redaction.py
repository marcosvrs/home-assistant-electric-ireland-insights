"""Regression tests ensuring sensitive identifiers never leak into integration logs."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

from custom_components.electric_ireland_insights.const import _redact_id

TEST_ACCOUNT = "100000001"
TEST_PARTNER = "P123456789"
TEST_CONTRACT = "C123456789"
TEST_PREMISE = "PR123456789"


async def test_redact_id_helper() -> None:
    assert _redact_id("1234567890") == "******7890"
    assert _redact_id("abcd") == "****"
    assert _redact_id("abc") == "***"
    assert _redact_id("") == "<empty>"
    assert _redact_id(None) == "<empty>"


def _integration_messages(caplog) -> list[str]:
    return [
        record.message
        for record in caplog.records
        if record.name.startswith("custom_components.electric_ireland_insights")
    ]


def _make_mock_api_instance() -> AsyncMock:
    instance = AsyncMock()
    instance.authenticate = AsyncMock(
        return_value=(
            {"partner": TEST_PARTNER, "contract": TEST_CONTRACT, "premise": TEST_PREMISE},
            {"partner": TEST_PARTNER, "contract": TEST_CONTRACT, "premise": TEST_PREMISE},
        )
    )
    instance.get_bill_periods = AsyncMock(return_value=[])
    instance.get_hourly_usage = AsyncMock(return_value=[])
    return instance


async def test_init_logs_redact_account_number(
    recorder_mock,
    hass,
    enable_custom_integrations,
    mock_config_entry,
    caplog,
):
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
        mock_api_class.return_value = _make_mock_api_instance()
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    messages = _integration_messages(caplog)
    assert any(_redact_id(TEST_ACCOUNT) in msg for msg in messages)
    assert not any(TEST_ACCOUNT in msg for msg in messages)


async def test_unload_logs_redact_account_number(
    recorder_mock,
    hass,
    enable_custom_integrations,
    mock_config_entry,
    caplog,
):
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
        mock_api_class.return_value = _make_mock_api_instance()
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    caplog.clear()
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    messages = _integration_messages(caplog)
    assert any(_redact_id(TEST_ACCOUNT) in msg for msg in messages)
    assert not any(TEST_ACCOUNT in msg for msg in messages)


async def test_sensor_setup_logs_redact_account_number(
    recorder_mock,
    hass,
    enable_custom_integrations,
    mock_config_entry,
    caplog,
):
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
        mock_api_class.return_value = _make_mock_api_instance()
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    messages = _integration_messages(caplog)
    assert any(_redact_id(TEST_ACCOUNT) in msg for msg in messages)
    assert not any(TEST_ACCOUNT in msg for msg in messages)


async def test_config_flow_logs_redact_account_number(
    recorder_mock,
    hass,
    enable_custom_integrations,
    mock_setup_entry,
    caplog,
):
    caplog.set_level(logging.DEBUG, logger="custom_components.electric_ireland_insights.config_flow")

    with (
        patch("custom_components.electric_ireland_insights.config_flow.async_create_clientsession") as mock_session,
        patch("custom_components.electric_ireland_insights.config_flow.ElectricIrelandAPI") as mock_api_class,
    ):
        api_instance = AsyncMock()
        api_instance.discover_accounts = AsyncMock(
            return_value=[{"account_number": TEST_ACCOUNT, "display_name": "Home"}]
        )
        api_instance.validate_credentials = AsyncMock(
            return_value={
                "partner": TEST_PARTNER,
                "contract": TEST_CONTRACT,
                "premise": TEST_PREMISE,
            }
        )
        mock_api_class.return_value = api_instance
        mock_session.return_value = AsyncMock()

        result = await hass.config_entries.flow.async_init(
            "electric_ireland_insights",
            context={"source": "user"},
            data={"username": "test@example.com", "password": "secret"},
        )
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"import_full_history": False, "discount_percentage": 0},
        )
        assert result2["type"] == "create_entry"

    messages = _integration_messages(caplog)
    assert any(_redact_id(TEST_ACCOUNT) in msg for msg in messages)
    assert not any(TEST_ACCOUNT in msg for msg in messages)


async def test_coordinator_logs_redact_account_and_partner(
    recorder_mock,
    hass,
    enable_custom_integrations,
    mock_config_entry,
    caplog,
):
    caplog.set_level(logging.DEBUG, logger="custom_components.electric_ireland_insights")
    mock_config_entry.add_to_hass(hass)

    with (
        patch("custom_components.electric_ireland_insights.coordinator.ElectricIrelandAPI") as mock_api_class,
        patch("custom_components.electric_ireland_insights.coordinator.async_create_clientsession"),
    ):
        mock_api_class.return_value = _make_mock_api_instance()
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    messages = _integration_messages(caplog)
    assert any(_redact_id(TEST_ACCOUNT) in msg for msg in messages)
    assert any(_redact_id(TEST_PARTNER) in msg for msg in messages)
    assert not any(TEST_ACCOUNT in msg for msg in messages)
    assert not any(TEST_PARTNER in msg for msg in messages)
    assert not any(TEST_CONTRACT in msg for msg in messages)
    assert not any(TEST_PREMISE in msg for msg in messages)


async def test_api_logs_redact_meter_ids(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.electric_ireland_insights.api")

    import aiohttp
    from aioresponses import aioresponses as aioresponses_mock

    from custom_components.electric_ireland_insights.api import ElectricIrelandAPI

    login_html = '<html><body><input name="Source" value="src"/><input name="rvt" value="rvt"/></body></html>'
    dashboard_html = (
        "<html><body>"
        '<div class="my-accounts__item">'
        f'<p class="account-number">{TEST_ACCOUNT}</p>'
        '<h2 class="account-electricity-icon"></h2>'
        '<form action="/Accounts/OnEvent"></form>'
        "</div>"
        "</body></html>"
    )
    insights_html = (
        "<html><body>"
        f'<div id="modelData" data-partner="{TEST_PARTNER}" '
        f'data-contract="{TEST_CONTRACT}" data-premise="{TEST_PREMISE}">'
        "</div></body></html>"
    )

    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
        with aioresponses_mock() as m:
            m.get("https://youraccountonline.electricireland.ie/", status=200, body=login_html)
            m.post("https://youraccountonline.electricireland.ie/", status=200, body=dashboard_html)
            m.post(
                "https://youraccountonline.electricireland.ie/Accounts/OnEvent",
                status=200,
                body=insights_html,
            )

            api = ElectricIrelandAPI("user@example.com", "secret", TEST_ACCOUNT)
            await api.validate_credentials(session)

    messages = _integration_messages(caplog)
    assert "Performing Login..." in messages
    assert "Navigating to Insights page..." in messages
    assert (
        "Discovered meter IDs: "
        f"partner={_redact_id(TEST_PARTNER)}, "
        f"contract={_redact_id(TEST_CONTRACT)}, "
        f"premise={_redact_id(TEST_PREMISE)}"
    ) in messages
    assert "Login successful (meter IDs discovered)" in messages
    assert any(_redact_id(TEST_PARTNER) in msg for msg in messages)
    assert not any(TEST_ACCOUNT in msg for msg in messages)
    assert not any(TEST_PARTNER in msg for msg in messages)
    assert not any(TEST_CONTRACT in msg for msg in messages)
    assert not any(TEST_PREMISE in msg for msg in messages)
