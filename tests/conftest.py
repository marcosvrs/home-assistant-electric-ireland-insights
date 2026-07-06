# pyright: reportMissingImports=false

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

# Disable pycares' global _run_safe_shutdown_loop daemon thread.
# pycares 5.x spawns a permanent daemon thread when any Channel is destroyed.
# pytest-homeassistant-custom-component >=0.13.316 whitelists this thread in
# verify_cleanup, but 0.13.205 (used on Python 3.12 CI) does not.
# Tests never resolve real DNS, so the shutdown manager is unnecessary.
try:
    from pycares import _ChannelShutdownManager  # type: ignore[attr-defined]

    _ChannelShutdownManager.start = lambda self: None  # type: ignore[method-assign]
except (ImportError, AttributeError):
    pass


SAMPLE_DATAPOINTS = [
    {"consumption": 0.222, "cost": 0.04, "start": 1774224000, "tariff_bucket": "off_peak"},
    {"consumption": 0.198, "cost": 0.04, "start": 1774227600, "tariff_bucket": "off_peak"},
    {"consumption": 0.173, "cost": 0.03, "start": 1774231200, "tariff_bucket": "off_peak"},
    {"consumption": 0.165, "cost": 0.03, "start": 1774234800, "tariff_bucket": "off_peak"},
    {"consumption": 0.149, "cost": 0.03, "start": 1774238400, "tariff_bucket": "off_peak"},
    {"consumption": 0.138, "cost": 0.03, "start": 1774242000, "tariff_bucket": "off_peak"},
    {"consumption": 0.155, "cost": 0.04, "start": 1774245600, "tariff_bucket": "off_peak"},
    {"consumption": 0.212, "cost": 0.05, "start": 1774249200, "tariff_bucket": "off_peak"},
    {"consumption": 0.305, "cost": 0.08, "start": 1774252800, "tariff_bucket": "on_peak"},
    {"consumption": 0.492, "cost": 0.14, "start": 1774256400, "tariff_bucket": "on_peak"},
    {"consumption": 0.684, "cost": 0.2, "start": 1774260000, "tariff_bucket": "on_peak"},
    {"consumption": 0.918, "cost": 0.28, "start": 1774263600, "tariff_bucket": "on_peak"},
    {"consumption": 1.102, "cost": 0.34, "start": 1774267200, "tariff_bucket": "on_peak"},
    {"consumption": 1.238, "cost": 0.39, "start": 1774270800, "tariff_bucket": "on_peak"},
    {"consumption": 1.356, "cost": 0.43, "start": 1774274400, "tariff_bucket": "on_peak"},
    {"consumption": 1.478, "cost": 0.48, "start": 1774278000, "tariff_bucket": "on_peak"},
    {"consumption": 1.592, "cost": 0.52, "start": 1774281600, "tariff_bucket": "on_peak"},
    {"consumption": 1.704, "cost": 0.57, "start": 1774285200, "tariff_bucket": "mid_peak"},
    {"consumption": 1.845, "cost": 0.63, "start": 1774288800, "tariff_bucket": "mid_peak"},
    {"consumption": 2.012, "cost": 0.7, "start": 1774292400, "tariff_bucket": "mid_peak"},
    {"consumption": 2.256, "cost": 0.78, "start": 1774296000, "tariff_bucket": "mid_peak"},
    {"consumption": 2.588, "cost": 0.88, "start": 1774299600, "tariff_bucket": "mid_peak"},
    {"consumption": 2.942, "cost": 0.99, "start": 1774303200, "tariff_bucket": "off_peak"},
    {"consumption": 3.417, "cost": 1.1, "start": 1774306800, "tariff_bucket": "off_peak"},
]


@pytest.fixture
def mock_config_entry():
    return MockConfigEntry(
        domain="electric_ireland_insights",
        data={
            "username": "test@test.com",
            "password": "testpass",
            "account_number": "100000001",
            "tariff_stats_initialized": True,
        },
        unique_id="e0d3b72f72183185",
    )


@pytest.fixture
def mock_api():
    api_mock = AsyncMock()
    api_instance = AsyncMock()
    api_instance.authenticate = AsyncMock(return_value=({"partner": "p1", "contract": "c1", "premise": "pr1"}, None))
    api_instance.get_bill_periods = AsyncMock(return_value=[])
    api_instance.get_hourly_usage = AsyncMock(return_value=[])
    api_instance.validate_credentials = AsyncMock(return_value={"partner": "p1", "contract": "c1", "premise": "pr1"})
    api_mock.return_value = api_instance

    with patch(
        "custom_components.electric_ireland_insights.api.ElectricIrelandAPI",
        new=api_mock,
        create=True,
    ):
        yield api_mock


@pytest.fixture
def mock_setup_entry():
    with patch(
        "custom_components.electric_ireland_insights.async_setup_entry",
        new=AsyncMock(return_value=True),
    ) as setup_mock:
        yield setup_mock
