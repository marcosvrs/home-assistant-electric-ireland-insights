# INTEGRATION MODULE: electric_ireland_insights

## OVERVIEW

Home Assistant custom integration that scrapes Electric Ireland for hourly energy data and imports it as external statistics. No sensor entities for core data — only diagnostic sensors for health monitoring.

## STRUCTURE

```
electric_ireland_insights/
├── __init__.py          # Entry point: setup, unload
├── api.py               # Scraping client: login, account discovery, MeterInsight API
├── refresh.py           # Current polling-window auth, billing-period selection, sequential fetches
├── coordinator.py       # DataUpdateCoordinator: scheduling, statistics import, backfill orchestration
├── statistics.py        # External statistics import, continuity, recorder migration
├── identity_migration.py # Privacy-safe account identity migration across HA registries
├── config_flow.py       # Config flow with shared account credential validation
├── sensor.py            # Diagnostic sensors only (last_import_time, data_freshness)
├── types.py             # TypedDicts: ElectricIrelandDatapoint, CoordinatorData, MeterIds
├── exceptions.py        # InvalidAuth, CannotConnect, AccountNotFound, CachedIdsInvalid
├── diagnostics.py       # async_get_config_entry_diagnostics with redaction
├── const.py             # DOMAIN, NAME, SCAN_INTERVAL, LOOKBACK constants
├── manifest.json       # Integration metadata, deps: [recorder], req: [beautifulsoup4]
├── strings.json        # Translations source: config flow steps/errors, entity names
├── translations/
│   └── en.json         # Runtime translations (must mirror strings.json for HACS)
├── icons.json          # Entity icons: mdi:clock-check-outline, mdi:calendar-clock
└── quality_scale.yaml  # IQS self-assessment: 51 of 52 rules done/exempt; brands rule is pending upstream submission
```

## FILE-BY-FILE GUIDE

| File | Key Symbols | Responsibility |
|------|-------------|---------------|
| `__init__.py` | `async_setup_entry`, `async_unload_entry`, `ElectricIrelandConfigEntry` | Calls `async_migrate_identity`, creates coordinator, assigns `entry.runtime_data`, forwards sensor platform. |
| `identity_migration.py` | `async_migrate_identity` | Owns account identity migration across config entries, device/entity registries, repair issues, and legacy options; setup handles the collision result. |
| `api.py` | `ElectricIrelandAPI`, `MeterInsightClient` | Session-based scraping: GET login page → extract CSRF → POST credentials → scrape account div → navigate Insights → call MeterInsight/hourly-usage. Sequential day-by-day. |
| `coordinator.py` | `ElectricIrelandCoordinator`, `_async_update_data` | HA polling schedule, state transitions, statistics import, events, and tariff backfill. Delegates the current-window data fetch to `RefreshWorkflow`. |
| `refresh.py` | `RefreshWorkflow`, `RefreshResult` | Owns current-window authentication fallback, billing-period cache and date selection, sequential requests, and partial/all-failure rules. |
| `statistics.py` | `StatisticsMixin`, `async_import_datapoints` | One statistics import operation for aggregate and applicable per-tariff data, cumulative continuity, recorder-thread writes, legacy statistic identity migration, and Energy Dashboard preference migration. |
| `config_flow.py` | `ElectricIrelandInsightsConfigFlow`, `_async_validate_account_credentials` | Config steps retain flow-specific outcomes; one shared check owns validation session, provider lock, and error normalization for account completion, reauth, and reconfigure. |
| `sensor.py` | `ElectricIrelandDiagnosticSensor`, `DIAGNOSTIC_SENSORS`, `PARALLEL_UPDATES = 0` | One `CoordinatorEntity` subclass instantiated per diagnostic description: last import timestamp + data freshness days. Disabled by default. EntityCategory.DIAGNOSTIC. |
| `types.py` | `ElectricIrelandDatapoint`, `CoordinatorData`, `MeterIds` | TypedDicts for type safety. Omitted from coverage (TypedDict-only). |
| `exceptions.py` | `InvalidAuth`, `CannotConnect`, `AccountNotFound`, `CachedIdsInvalid` | Auth errors → reauth flow. Connect errors → retry. CachedIdsInvalid → full login fallback (not auth failure). |
| `diagnostics.py` | `async_get_config_entry_diagnostics` | Redacts: username, password, account_number, partner_id, contract_id, premise_id. Exposes coordinator data for debugging. |

## DATA FLOW

```
Electric Ireland Website
    ↓ (aiohttp session with cookie jar)
ElectricIrelandCoordinator._async_update_data()
    ↓
RefreshWorkflow.async_fetch()
    ↓ (sequential API calls; list[ElectricIrelandDatapoint])
ElectricIrelandAPI.get_hourly_usage()
    ↓
coordinator statistics import → HA Recorder → Energy Dashboard
```

## CONVENTIONS (THIS MODULE)

- **Config entry version**: 2; `async_migrate_entry` upgrades legacy version 1 entries before setup.
- **Typed config entry**: `type ElectricIrelandConfigEntry = ConfigEntry[ElectricIrelandCoordinator]` used throughout.
- **Unique ID**: `hash_account_id(account_number)` (set in config flow); legacy raw IDs are migrated during setup with same-domain collision guards.
- **Statistic IDs**: `electric_ireland_insights:{account_hash}_consumption`, `electric_ireland_insights:{account_hash}_cost` (aggregate); plus per-tariff variants like `_consumption_off_peak`, `_cost_mid_peak` etc. Legacy raw-account IDs and account-bearing metadata names are migrated during setup.
- **Tariff bucket selection**: Active bucket (flatRate, offPeak, midPeak, onPeak) extracted per hour — only one is active at a time.
- **State logging**: Coordinator logs state transitions (unavailable ↔ available) once per transition, not every poll.

## ANTI-PATTERNS (THIS MODULE)

- **NEVER** create sensor entities for consumption/cost — external statistics only.
- **NEVER** fire concurrent API requests — Electric Ireland rate-limits aggressively.
- **NEVER** store runtime data in `hass.data[DOMAIN]` — use `entry.runtime_data`.
- **NEVER** use `requests` or any sync HTTP library — async only via `async_create_clientsession`.
- **NEVER** hardcode meter IDs — discover from website, cache in config entry.
