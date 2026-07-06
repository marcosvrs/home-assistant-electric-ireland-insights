"""Constants for Electric Ireland Insights."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Final

DOMAIN: Final = "electric_ireland_insights"
NAME: Final = "Electric Ireland Insights (Unofficial)"
VERSION: Final = "1.0.0"

CONF_DISCOUNT_PERCENTAGE: Final = "discount_percentage"
DEFAULT_DISCOUNT_PERCENTAGE: Final = 0

LOOKUP_DAYS = 4
INITIAL_LOOKBACK_DAYS = 30
SCAN_INTERVAL = timedelta(hours=3)
DATA_GAP_THRESHOLD_DAYS = 5

# Maps API tariff bucket keys to stable snake_case identifiers used in
# statistic IDs (e.g. ``electric_ireland_insights:{acct}_consumption_off_peak``).
TARIFF_BUCKET_MAP: dict[str, str] = {
    "flatRate": "flat_rate",
    "offPeak": "off_peak",
    "midPeak": "mid_peak",
    "onPeak": "on_peak",
}


def _redact_id(value: str | None, visible: int = 4) -> str:
    """Return a redacted identifier for safe logging."""
    if not value:
        return "<empty>"
    if len(value) <= visible:
        return "*" * len(value)
    return f"{'*' * (len(value) - visible)}{value[-visible:]}"


def hash_account_id(account: str) -> str:
    """Return a stable, non-reversible hash for the account number.

    Used for HA-facing stable identifiers (statistic IDs, entity unique IDs,
    device identifiers, repair issue IDs, event payloads) so that the raw
    account number does not leak into long-term HA storage.
    """
    return hashlib.sha256(account.encode("utf-8")).hexdigest()[:16]
