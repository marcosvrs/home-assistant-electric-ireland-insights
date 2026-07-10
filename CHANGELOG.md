# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-07-09

### Added

- Initial stable release of the Electric Ireland Insights integration.
- Config flow with credential validation, account selection, re-authentication, and reconfiguration.
- Imports hourly electricity consumption and cost as external statistics into the Home Assistant recorder.
- Optional full-history backfill (typically 6–13 months) as a background task.
- Per-tariff statistics for time-of-use tariffs (off-peak, mid-peak, on-peak, flat-rate).
- Optional discount percentage that creates a separate `_cost_discounted` statistic.
- Diagnostic sensors for last import time and data freshness.
- Repair issues for stale data and backfill failures.
- Diagnostics redaction for credentials, account number, and meter identifiers.

### Changed

- Home Assistant-facing identifiers (statistic IDs, entity unique IDs, device identifiers, repair issue placeholders) use a SHA-256 hash of the account number instead of the raw account number.

### Fixed

- Backfill failures now surface a repair issue instead of failing silently.
- Empty backfill no longer marks historical import as complete, allowing retry on the next load.
- Coordinator session is closed when setup fails before unload.
- Bill-period cache is invalidated when meter identifiers are rediscovered.

### Migration note

This release changes Home Assistant-facing identifiers from raw account numbers to SHA-256 hashes and adds cached meter identifiers to the config entry. No automatic migration is provided because the pre-1.0 tags had no confirmed public installations. Users coming from a pre-1.0 version should remove and re-add the integration.

### Known limitations

- Data is published by ESB with a 1–3 day delay.
- The integration scrapes the Electric Ireland web portal; portal changes can break login.
- DST transition behavior has not yet been verified with real portal fixtures.
- Standing charges and levies are not included in cost statistics.
