---
title: Electric Ireland Insights (Unofficial)
description: Instructions on how to integrate Electric Ireland energy data into Home Assistant.
ha_category:
  - Energy
ha_release: "2026.3"
ha_iot_class: Cloud Polling
ha_config_flow: true
ha_codeowners:
  - "@marcosvrs"
ha_domain: electric_ireland_insights
ha_platforms:
  - diagnostics
  - sensor
ha_integration_type: service
---

# Electric Ireland Insights (Unofficial)

[Electric Ireland](https://www.electricireland.ie/) is an Irish electricity and gas supplier. The Electric Ireland Insights integration imports hourly energy consumption and cost data from the Electric Ireland Insights portal directly into the Home Assistant Energy Dashboard as external statistics.

> **Note**: This is an independent, community-built integration. It is not affiliated with, authorized by, or endorsed by Electric Ireland, ESB Group, or any of their subsidiaries. This integration works by scraping the Electric Ireland web portal — there is no official API. Changes to the website may break this integration at any time without notice. Users are solely responsible for ensuring their use complies with Electric Ireland's terms of service and applicable laws. The authors accept no responsibility for any consequences arising from its use, including account restrictions or legal action. All data remains on the user's local Home Assistant instance. See [LEGAL.md](https://github.com/marcosvrs/home-assistant-electric-ireland-insights/blob/master/LEGAL.md) for full details.

## Prerequisites

- **Home Assistant 2026.3.0** or newer
- An active Electric Ireland account with **Insights access** enabled
- An **electricity** account (gas-only accounts are not supported)
- A **smart meter** installed at your premises (required for hourly data)

## Installation

1. Install via [HACS](https://hacs.xyz/) (search for "Electric Ireland Insights") or manually copy the `electric_ireland_insights` folder into `<config>/custom_components/` (the final path should be `<config>/custom_components/electric_ireland_insights/`).
2. Restart Home Assistant.
3. Go to **Settings** → **Devices & services**.
4. Click **+ Add integration**.
5. Search for and select **Electric Ireland Insights**.
6. Follow the on-screen instructions to complete the setup.

During setup you will be asked for:

| Parameter | Description |
|-----------|-------------|
| **Username** | Your Electric Ireland portal email address |
| **Password** | Your Electric Ireland portal password |

After authenticating, the integration discovers all electricity accounts linked to your login. If only one account is found, it is selected automatically. If multiple accounts are found, a dropdown is shown for you to select which account to configure. Each config entry supports one account — add the integration again for additional accounts.

A final **Setup Options** step is shown before setup completes:

| Option | Default | Description |
|--------|---------|-------------|
| **Import full history** | **Checked (on)** | Fetch all available historical data from your bill periods (typically 6–13 months) as a background task. Because Electric Ireland does not publish an official API, this is done via many sequential web-portal requests and may take **10–30 minutes** for the initial import. There is a small rate-limit risk during this one-time background work. Uncheck this option if you only want the last 30 days imported now; you can still trigger a full import later via **Reconfigure**. |
| **Discount percentage** | `0` | Your electricity plan discount (for example, `20` for a 20% Saver discount). This is saved as an integration option and can be changed later from **Options** without re-entering credentials. |

> **Why is full history on by default?** The Energy Dashboard is most useful with complete historical context. The import runs in the background and does not block Home Assistant. If you are concerned about rate limiting or want a faster first setup, uncheck this option.

### Adding to the Energy Dashboard

After setup, add the imported statistics to the Energy Dashboard:

1. Go to **Settings → Dashboards → Energy**.
2. Under **Grid consumption**, click **Add consumption**.
3. Search for and select `Electric Ireland Consumption ({account_hash})`.
4. For **Use an entity tracking the total costs**, select:
   - `Electric Ireland Cost ({account_hash})` for the gross cost as reported by Electric Ireland, or
   - `Electric Ireland Cost Discounted ({account_hash})` if you configured a discount percentage and want the cost to reflect your billed amount more closely.

For per-tariff breakdown (stacked colored bars by time-of-use), see the [per-tariff setup](#setting-up-the-energy-dashboard-with-per-tariff-breakdown) section below.

## Removal

1. Go to **Settings** → **Devices & services**.
2. Select the **Electric Ireland Insights** integration card.
3. Click the three-dot menu (**⋮**) and select **Delete**.

Imported statistics remain in the Home Assistant recorder after removal. To also delete historical energy data, use **Developer tools → Statistics** and search for `electric_ireland_insights` to remove individual statistics.

## Data updates

The integration fetches data from the Electric Ireland portal **every 3 hours**.

- **First install**: the initial setup fetches up to **30 days** of data (this may take a few minutes as each day requires a separate request to the Electric Ireland portal).
- **Full history**: full history is enabled by default during setup. To import all available historical data again later (typically 6–13 months), go to **Reconfigure** and check **Import full history**. This runs as a background task without blocking Home Assistant, typically taking 10–30 minutes.
- **Subsequent runs**: fetches up to the last **4 days**, limited to dates within known billing periods, to pick up newly published readings. Dates outside any billing period are skipped.
- **Pre-flight optimization**: the integration periodically queries billing period boundaries (cached for 24 hours) to identify which dates contain meter data, reducing unnecessary API calls. If the query fails entirely (no cached data available), the integration falls back to the full lookback window.
- **Provider delay**: Electric Ireland publishes meter data with a **1–3 day delay** (data comes from ESB). The Data Freshness diagnostic sensor shows how old the latest available reading is (see [Diagnostic entities](#diagnostic-entities)).
- **Data gap detection**: if no new data arrives for more than **5 days**, the integration creates a **repair issue** in Home Assistant (visible in **Settings → Repairs**). The issue is automatically removed when data resumes.

## Statistics

The integration imports data as **external statistics** directly into the HA recorder — no sensor entities are needed for the Energy Dashboard.

### Grid consumption and cost (hourly resolution)

| Statistic ID | Description | Unit |
|---|---|---|
| `electric_ireland_insights:{account_hash}_consumption` | Hourly electricity consumption (total) | kWh |
| `electric_ireland_insights:{account_hash}_cost` | Hourly electricity cost (gross with VAT, no standing charge) | EUR |
| `electric_ireland_insights:{account_hash}_cost_discounted` | Hourly electricity cost with your configured discount applied (only created when discount > 0) | EUR |

`{account_hash}` is the first 16 characters of the SHA-256 hash of your account number. The raw account number is not used in HA-facing identifiers.

### Per-tariff breakdown

When the current data window contains time-of-use tariff buckets, the integration automatically detects which buckets are active and imports separate statistics for each:

| Statistic ID | Description | Unit |
|---|---|---|
| `electric_ireland_insights:{account_hash}_consumption_off_peak` | Off-peak consumption | kWh |
| `electric_ireland_insights:{account_hash}_consumption_mid_peak` | Mid-peak consumption | kWh |
| `electric_ireland_insights:{account_hash}_consumption_on_peak` | On-peak consumption | kWh |
| `electric_ireland_insights:{account_hash}_consumption_flat_rate` | Flat-rate consumption from hours tagged as flat rate within a smart tariff plan | kWh |
| `electric_ireland_insights:{account_hash}_cost_off_peak` | Off-peak cost (gross) | EUR |
| `electric_ireland_insights:{account_hash}_cost_mid_peak` | Mid-peak cost (gross) | EUR |
| `electric_ireland_insights:{account_hash}_cost_on_peak` | On-peak cost (gross) | EUR |
| `electric_ireland_insights:{account_hash}_cost_flat_rate` | Flat-rate cost from hours tagged as flat rate within a smart tariff plan (gross) | EUR |
| `electric_ireland_insights:{account_hash}_cost_off_peak_discounted` | Off-peak cost with discount applied (only created when discount > 0) | EUR |
| `electric_ireland_insights:{account_hash}_cost_mid_peak_discounted` | Mid-peak cost with discount applied (only created when discount > 0) | EUR |
| `electric_ireland_insights:{account_hash}_cost_on_peak_discounted` | On-peak cost with discount applied (only created when discount > 0) | EUR |
| `electric_ireland_insights:{account_hash}_cost_flat_rate_discounted` | Flat-rate cost with discount applied (only created when discount > 0) | EUR |

- Per-tariff statistics are created **only when the current data window contains at least one non-flat bucket** (off-peak, mid-peak, or on-peak). The integration evaluates this on every poll — it does not permanently label an account as "smart" or "flat rate".
- If you're on a **flat-rate** tariff (only `flat_rate` buckets appear in the current window), per-tariff statistics are not created (they would be identical to the totals).
- If only one non-flat bucket appears in the current window (e.g., off-peak only), per-tariff statistics are still created.
- The `_flat_rate` bucket appears only when a smart-tariff account has hours tagged as flat rate in the same window as non-flat buckets. If an account temporarily switches entirely to flat rate and no non-flat buckets are present in the current window, only the aggregate statistics are created.
- Per-tariff statistics may not appear immediately after setup — they are populated during the background backfill and on subsequent poll updates.

### Setting up the Energy Dashboard with per-tariff breakdown

To see stacked colored bars showing consumption broken down by tariff:

1. Go to **Settings → Dashboards → Energy**.
2. Under **Grid consumption**, click **Add consumption**.
3. Search for and add each per-tariff consumption statistic that the integration created for your account. The available tariff buckets depend on your plan and may include off-peak, mid-peak, on-peak, or flat-rate.
4. For each consumption statistic, select the matching cost statistic (e.g., `Electric Ireland Cost Off-Peak ({account_hash})` for the off-peak consumption entry). If you configured a discount, use the `_discounted` cost statistic (e.g., `Electric Ireland Cost Off-Peak Discounted ({account_hash})`) to match your billed amount more closely.
5. **Remove** the original aggregate `Electric Ireland Consumption ({account_hash})` entry to avoid double-counting.

The Energy Dashboard will now display separate colored bars per hour/day for each tariff bucket.

> **Note**: Do not add both aggregate and per-tariff statistics to the Energy Dashboard grid consumption at the same time — this will result in double-counted consumption. The aggregate statistics remain available in the recorder for historical queries but are not usable as entity states in automations.

## Diagnostic entities

Two diagnostic sensor entities are created under the integration's device. Entity IDs include the account hash (e.g., `sensor.electric_ireland_insights_<account_hash>_last_import_time`). In multi-account setups, each account has its own set of diagnostic sensors.

| Entity | Description |
|--------|-------------|
| **Last Import Time** | Timestamp of the last successful data import |
| **Data Freshness** | How many days since the latest available reading (as a decimal, e.g., 1.3 days) |

These entities are **disabled by default**. Enable them in **Settings → Devices & services → Electric Ireland Insights → device → entities**.

## Reconfiguration

To update your password, trigger a full-history import, or troubleshoot data import issues, use **Settings → Devices & services → Electric Ireland Insights → ⋮ → Reconfigure**.

| Option | Description |
|--------|-------------|
| **Password** | Re-enter your current password (required to re-authenticate the session, even if unchanged) |
| **Re-discover meter IDs** | Clears the cached meter identifiers (partner, contract, premise) and forces the integration to re-discover them from the Electric Ireland portal on the next refresh. Use this when data imports have stopped but your credentials are still valid — typically caused by Electric Ireland changing internal account identifiers after a meter swap or account migration. |
| **Import full history** | Fetches all available historical data from your bill periods (typically 6–13 months). Runs as a background task without blocking Home Assistant. Only needed once — subsequent polls keep data current automatically. |

If the password has changed, cached meter IDs are cleared automatically — you don't need to check the re-discovery option.

## Options

To change your discount percentage after setup, use **Settings → Devices & services → Electric Ireland Insights → Configure** (or **Options**, depending on your Home Assistant version).

| Option | Description |
|--------|-------------|
| **Discount percentage** | Your electricity plan discount (e.g., 20 for 20% Saver). Creates a separate `_cost_discounted` statistic with the discount applied. The integration reloads after this option changes. To also recalculate existing `_cost_discounted` data with the new discount, update the option and then use **Reconfigure → Import full history**. The `_cost` statistic always remains gross. |

## Events

The integration fires an event after each successful data import:

| Event | Description |
|-------|-------------|
| `electric_ireland_insights_data_imported` | Fired when new data is imported into the recorder |

**Payload fields:**

| Field | Type | Description |
|-------|------|-------------|
| `account` | string | Account hash (first 16 characters of the SHA-256 hash of the account number) |
| `datapoint_count` | integer | Number of hourly datapoints fetched in this update (includes re-fetched overlap days, not just new data) |
| `latest_data_timestamp` | string or null | ISO 8601 timestamp of the newest datapoint, or null if no data was available |
| `tariff_buckets` | list of strings | Sorted list of tariff bucket names seen (e.g., `["mid_peak", "off_peak", "on_peak"]`) |

### Automation: notify when new data arrives

```yaml
automation:
  - alias: "Electric Ireland: new data imported"
    triggers:
      - trigger: event
        event_type: electric_ireland_insights_data_imported
    actions:
      - action: notify.mobile_app
        data:
          message: >
            Electric Ireland imported {{ trigger.event.data.datapoint_count }} datapoints.
            Tariffs: {{ trigger.event.data.tariff_buckets | join(', ') }}
```

### Automation: alert when data is stale

> **Note**: The Data Freshness sensor is disabled by default. Enable it first in **Settings → Devices & services → Electric Ireland Insights → device → entities**.

Replace `<account_hash>` below with your account hash (the first 16 characters of the SHA-256 hash of your account number):

```yaml
automation:
  - alias: "Alert: Electric Ireland data stale"
    triggers:
      - trigger: numeric_state
        entity_id: sensor.electric_ireland_insights_<account_hash>_data_freshness
        above: 5
    actions:
      - action: notify.mobile_app
        data:
          message: "Electric Ireland data is {{ states('sensor.electric_ireland_insights_<account_hash>_data_freshness') }} days old."
```

## Known limitations

- **1–3 day data delay**: Hourly readings are published by ESB with a delay; the integration cannot fetch data faster than ESB publishes it.
- **DST transition gap**: Spring-forward and fall-back day behavior has not yet been verified with real Electric Ireland capture fixtures. The current DST regression tests are skipped until those captures are available.
- **Discount applies to future data only by default**: Changing the discount percentage in **Options** affects only newly fetched or re-fetched `_cost_discounted` data (the last 4 days on each poll). To recalculate all historical `_cost_discounted` data with a new discount, update the option and then use **Reconfigure → Import full history**. The `_cost` statistic always remains gross. Standing charges and levies are never included.
- **Scraping dependency**: The integration authenticates via the Electric Ireland web portal. Changes to the portal's HTML structure may break the login flow until the integration is updated.
- **Single account per entry**: Each config entry supports one electricity account. To monitor multiple accounts, add the integration once per account.

## Troubleshooting

### Login failure / Invalid credentials

Verify your username (email address) and password by logging in at [youraccountonline.electricireland.ie](https://youraccountonline.electricireland.ie). If your password has changed, use **Reconfigure** to update it.

### Account not found

The integration automatically discovers electricity accounts linked to your login. If no accounts are found, ensure your login has an **electricity** account with Insights access enabled (gas-only accounts are not supported). You can verify by logging in at [youraccountonline.electricireland.ie](https://youraccountonline.electricireland.ie) and checking the Insights section.

### No data / Data freshness increasing

Electric Ireland publishes data with a 1–3 day delay. If no new data arrives for more than 5 days, the integration automatically creates a **repair issue** visible in **Settings → Repairs**.

To investigate further:

1. Check that your smart meter is functioning correctly.
2. Verify the Electric Ireland Insights portal shows data at [youraccountonline.electricireland.ie](https://youraccountonline.electricireland.ie).
3. Check the integration logs for errors (**Settings → System → Logs**, filter by `electric_ireland_insights`).

The Data Freshness diagnostic sensor can also help monitor this, but it must be **enabled** first (it is disabled by default).

### Re-authentication required

If the integration enters a re-authentication state, go to **Settings → Devices & services → Electric Ireland Insights** and follow the re-authentication flow to update your credentials.

### Debug logging

To help diagnose issues, enable debug logging for the integration:

1. Go to **Settings → Devices & services → Electric Ireland Insights → ⋮ → Enable debug logging**.
2. Reproduce the issue.
3. Go back and select **Disable debug logging** to download the log file.

Alternatively, add the following to your `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.electric_ireland_insights: debug
```

### What to look for in debug logs

When debug logging is enabled, the integration logs detailed information at each stage. Here's what key messages mean:

| Log message | What it means |
|------------|---------------|
| `Setting up Electric Ireland entry, account=...` | Integration is initialising for this account |
| `Platforms forwarded for account=...` | Sensor entities have been registered |
| `Launching full history import background task` | A full historical data import is running in the background |
| `Unloading Electric Ireland entry, account=...` | Integration is being removed or reloaded |
| `Performing Login...` | Login flow is starting against the Electric Ireland website |
| `Discovered N account(s)` | Account discovery completed after login |
| `Connection lost — data import paused` | ⚠️ The integration cannot reach Electric Ireland (will retry automatically) |
| `Connection restored — data import resumed` | ✅ Connectivity recovered |
| `Unexpected exception` | ❌ An unhandled error — include this in your bug report |
