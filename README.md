# Home Assistant Electric Ireland Integration

[![Open Integration](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=marcosvrs&repository=home-assistant-electric-ireland-insights&category=integration)

> **Origin & Attribution**: This integration is a continuation of the foundational work by [**@barreeeiroo**](https://github.com/barreeeiroo) and his original [Home-Assistant-Electric-Ireland](https://github.com/barreeeiroo/Home-Assistant-Electric-Ireland) project. The v0.4.0 rewrite builds on that codebase with a Platinum-tier architecture, external statistics, and per-tariff Energy Dashboard support. We gratefully acknowledge his pioneering effort that made this integration possible.
>
> **Disclaimer**: This is an independent, community-built integration. It is **not affiliated with, authorized by, or endorsed by** Electric Ireland, ESB Group, or any of their subsidiaries. "Electric Ireland" is a registered trademark of Electric Ireland Ltd.
>
> This integration works by scraping the Electric Ireland web portal — **there is no official API**. Changes to the website may break this integration at any time without notice. Users are solely responsible for ensuring their use complies with Electric Ireland's terms of service and applicable laws.
>
> The authors accept **no responsibility or liability** for any consequences arising from use of this integration, including account suspension, service restrictions, or legal action by Electric Ireland or their partners. This software is provided **"as is"** under the [GNU General Public License v3.0](LICENSE).
>
> Under [GDPR Article 20](https://gdpr-info.eu/art-20-gdpr/), users have the right to receive their personal energy data in a portable format. This integration helps users import their own data into Home Assistant — all data remains on the user's local instance.
>
> See [LEGAL.md](LEGAL.md) for full legal notice, privacy information, and trademark details.

Home Assistant integration with **Electric Ireland insights**.

It is capable of:

* Reporting **consumed energy** in kWh (hourly resolution).
* Reporting **usage cost** in EUR (hourly resolution; see the FAQ below for more details on this).

It will also aggregate the report data into statistical buckets, so they can be fed into the Energy Dashboard. Data
is imported as external statistics directly into the recorder — no sensor entities are needed for energy or cost data.

![](https://i.imgur.com/6ew3JIf.png)

## FAQs

### How does it work?

It scrapes the Insights page that Electric Ireland provides. It will first mimic a user login interaction,
navigate to the Insights page for the configured account, and then call the MeterInsight API to fetch hourly usage data.

As this data is also fed from ESB ([Electrical Supply Board](https://esb.ie)), it is not in real time. They publish
data with 1-3 days delay; the integration polls **every 3 hours** and ingests any newly available data. On first
install, it fetches up to 30 days of history; subsequent polls look back 4 days to pick up newly published readings.
Full historical data (typically 6–13 months) can be imported on demand via the **Import full history** option during setup or reconfiguration.

### How does the discount percentage work?

The Electric Ireland API reports **gross** cost as per tariff price (with VAT). If your plan includes a discount (for example, the 20% Saver discount or 30% Off Direct Debit), you can enter your discount percentage during setup or reconfiguration.

The integration always keeps the `_cost` statistic as the **gross** amount imported from the portal. When a discount percentage is configured, it also creates a separate `_cost_discounted` statistic with the discount applied. Use `_cost_discounted` in the Energy Dashboard if you want cost tracking that matches your billed amount more closely.

Standing charges and levies are not included in either cost statistic because Electric Ireland does not expose them in the Insights portal.

## Technical Details

### Statistics

This integration imports external statistics directly into the HA recorder — no sensor entities are needed for the Energy Dashboard.

#### Grid consumption and cost (hourly resolution)

| Statistic ID | Description | Unit |
|---|---|---|
| `electric_ireland_insights:{account}_consumption` | Hourly electricity consumption (total) | kWh |
| `electric_ireland_insights:{account}_cost` | Hourly electricity cost (gross, with VAT, no standing charge) | EUR |
| `electric_ireland_insights:{account}_cost_discounted` | Hourly electricity cost with your configured discount applied (only created when discount > 0) | EUR |

Add the consumption and whichever cost statistic you prefer under **Settings → Energy → Grid consumption**. Use `_cost_discounted` if you want the Energy Dashboard to reflect your plan discount.

#### Per-tariff breakdown (smart meter accounts)

For accounts on a time-of-use tariff, the integration also imports separate statistics per tariff bucket:

| Statistic ID | Description | Unit |
|---|---|---|
| `electric_ireland_insights:{account}_consumption_off_peak` | Off-peak consumption | kWh |
| `electric_ireland_insights:{account}_consumption_mid_peak` | Mid-peak consumption | kWh |
| `electric_ireland_insights:{account}_consumption_on_peak` | On-peak consumption | kWh |
| `electric_ireland_insights:{account}_consumption_flat_rate` | Flat-rate consumption during tariff transition periods | kWh |
| `electric_ireland_insights:{account}_cost_off_peak` | Off-peak cost (gross) | EUR |
| `electric_ireland_insights:{account}_cost_mid_peak` | Mid-peak cost (gross) | EUR |
| `electric_ireland_insights:{account}_cost_on_peak` | On-peak cost (gross) | EUR |
| `electric_ireland_insights:{account}_cost_flat_rate` | Flat-rate cost during tariff transition periods (gross) | EUR |
| `electric_ireland_insights:{account}_cost_off_peak_discounted` | Off-peak cost with discount applied (only created when discount > 0) | EUR |
| `electric_ireland_insights:{account}_cost_mid_peak_discounted` | Mid-peak cost with discount applied (only created when discount > 0) | EUR |
| `electric_ireland_insights:{account}_cost_on_peak_discounted` | On-peak cost with discount applied (only created when discount > 0) | EUR |
| `electric_ireland_insights:{account}_cost_flat_rate_discounted` | Flat-rate cost with discount applied (only created when discount > 0) | EUR |

Pure flat-rate accounts only have the aggregate statistics above. Accounts with smart tariff history that temporarily switch to flat rate during a contract change also get `_flat_rate` per-tariff statistics for that transition period. See [docs/index.md](docs/index.md) for detailed setup instructions.

### Smarter Data Fetching

Before fetching hourly data, the coordinator calls the `/bill-period` endpoint to determine which date ranges actually contain meter data. Hourly requests are then limited to dates within known billing periods (cached for 24 hours) rather than blindly fetching the entire lookback window. If the pre-flight call fails, the integration falls back to the full lookback window (30 days on first install, 4 days on subsequent runs).

### Diagnostic Entities

Two diagnostic sensor entities are created for monitoring the integration's health:

* **Last Import Time**: Timestamp of the last successful data import
* **Data Freshness**: How many days old the latest available data is (typically 1-3 days due to ESB reporting delay)

These appear in **Settings → Devices & services** under the integration's device.

### Data Retrieval Flow

1. Open an `aiohttp` session against the Electric Ireland website, and:
    1. Create a GET request to retrieve the cookies and the login state token.
    2. Do a POST request to login into Electric Ireland.
    3. Scrape the dashboard to find the `div` with the target Account Number.
    4. Navigate to the Insights page for that Account Number to obtain the meter IDs (partner, contract, premise).
2. **Pre-flight**: call `/MeterInsight/{partner}/{contract}/{premise}/bill-period` to discover billing period boundaries. Hourly requests are then bounded to dates within known periods. Falls back to the full lookback window if this call fails.
3. Using the same session, call the MeterInsight API sequentially:
    1. For each day in the bounded date set, request `/MeterInsight/{partner}/{contract}/{premise}/hourly-usage`.
    2. Each response contains 24 hourly datapoints with consumption (kWh) and cost (EUR) per tariff bucket.
    3. The active tariff bucket (flatRate, offPeak, midPeak, or onPeak) is extracted for each hour.
4. Import the collected datapoints as external statistics via `async_add_external_statistics`, maintaining cumulative sum continuity with any existing recorded data.

### Schedule

Every 3 hours:

* Performs the login flow mentioned above to establish a session.
* On **first install**: fetches up to 30 days of historical data.
* On **subsequent runs**: fetches the last 4 days to pick up any newly published meter readings.
* **Full history (opt-in)**: during setup or via **Reconfigure → Import full history**, the user can trigger a background task that fetches all available bill period data (typically 6–13 months). This runs without blocking Home Assistant and typically takes 10–30 minutes.
* Requests are made **sequentially** (one day at a time) to avoid rate limiting.
* Both consumption and cost are returned in the same response, with 24 hourly datapoints per day.
* Data is timestamped at the end of each hourly interval (e.g., `00:59:59` for the midnight hour) and normalized to the hour start for statistics alignment.

## Breaking Changes in v0.4.0

This is a **major architectural change**. If you are upgrading from v0.2.x:

1. **New statistic IDs**: Statistics are now imported as external statistics with IDs like `electric_ireland_insights:{account_number}_consumption`. The old entity-based statistics (`sensor.electric_ireland_consumption_*`) will no longer be updated.

2. **Energy Dashboard reconfiguration required**: You must re-configure your Energy Dashboard to use the new statistic IDs. Go to **Settings → Energy → Grid consumption** and select the new `electric_ireland_insights` statistics.

3. **Old statistics not migrated**: Historical data from v0.2.x will remain in your database but will not be carried over to the new statistic IDs. The integration will import up to 30 days of history on first startup. Use **Reconfigure → Import full history** to fetch all available data.

4. **`homeassistant-historical-sensor` dependency removed**: The alpha library dependency has been removed. No action required — HA will uninstall it automatically.

## Known Limitations

* **1-3 day data delay**: Hourly meter readings are published by ESB with a 1-3 day delay. This integration cannot fetch data faster than ESB publishes it.
* **Discount applies to future `_cost_discounted` data only by default**: Changing the discount percentage affects only newly fetched or re-fetched `_cost_discounted` data (the last 4 days on each poll). The `_cost` statistic always remains gross. To recalculate all historical `_cost_discounted` data with a new discount, use **Reconfigure → Import full history** after changing the discount. Standing charges and levies are never included.
* **Scraping dependency**: The integration authenticates via the Electric Ireland web portal. Changes to the portal's HTML structure may break the login flow until the integration is updated.

## Acknowledgements

* [**@barreeeiroo**](https://github.com/barreeeiroo) — Original author and creator of the Electric Ireland Home Assistant integration. This project began as a fork of [barreeeiroo/Home-Assistant-Electric-Ireland](https://github.com/barreeeiroo/Home-Assistant-Electric-Ireland) and his original vision, design, and effort made every subsequent improvement possible. We honor and thank him for building the foundation that the community now continues to evolve.
* [Opower integration](https://github.com/home-assistant/core/tree/dev/homeassistant/components/opower): served as the architectural reference for the external statistics and coordinator pattern used in v0.4.0.
