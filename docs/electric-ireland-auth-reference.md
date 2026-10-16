# Electric Ireland Portal — Authentication & Scraping Reference

Reverse-engineered specification for the Electric Ireland online account portal
(`youraccountonline.electricireland.ie`). This document covers all non-API aspects:
authentication, session management, HTML scraping, navigation, and edge-case behaviour.

For JSON API endpoint specifications, see
[`electric-ireland-api.openapi.yaml`](electric-ireland-api.openapi.yaml).

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Authentication Flow](#2-authentication-flow)
3. [Session Management](#3-session-management)
4. [Navigation & Account Selection](#4-navigation--account-selection)
5. [Meter ID Extraction (HTML Scraping)](#5-meter-id-extraction-html-scraping)
6. [Bidgely SDK Architecture](#6-bidgely-sdk-architecture)
7. [Cookie Inventory](#7-cookie-inventory)
8. [Storage (localStorage / sessionStorage)](#8-storage-localstorage--sessionstorage)
9. [Error Detection & Edge Cases](#9-error-detection--edge-cases)
10. [Rate Limiting & Best Practices](#10-rate-limiting--best-practices)
11. [Alternative Data Sources](#11-alternative-data-sources)
12. [Account Flags & Capabilities](#12-account-flags--capabilities)

---

## 1. Architecture Overview

```
 Consumer (aiohttp)
       │
       │  Session cookies
       ▼
 ┌──────────────────────────────────────────────────┐
 │  youraccountonline.electricireland.ie             │
 │  ASP.NET Core MVC Application (Azure App Service) │
 │                                                    │
 │  ┌─────────────────────────────────────────────┐  │
 │  │  Login: POST /                               │  │
 │  │  Navigation: POST /Accounts/OnEvent          │  │
 │  │  Insights page: HTML with <div id=modelData> │  │
 │  └─────────────────────────────────────────────┘  │
 │                                                    │
 │  ┌─────────────────────────────────────────────┐  │
 │  │  /MeterInsight/* — Server-side proxy         │  │
 │  │  Proxies to Bidgely API, returns JSON        │  │
 │  └─────────────┬───────────────────────────────┘  │
 └────────────────┼──────────────────────────────────┘
                  │  Internal (opaque)
                  ▼
 ┌──────────────────────────────────────────────────┐
 │  Bidgely Energy Analytics Platform                │
 │  Production: api.eu.bidgely.com                   │
 │  EI UAT: eiuatapi.bidgely.com                     │
 │  SSO: ssoprod.bidgely.com                         │
 │  CDN: static.bidgely.com                          │
 │       d12of87xj1f8rc.cloudfront.net               │
 └──────────────────────────────────────────────────┘
```

**Technology stack**:

- **Server**: ASP.NET Core MVC on Azure App Service
- **Load balancer**: Azure ARR (Application Request Routing) with sticky sessions
- **Session**: ASP.NET Core distributed session (`DistributedSession`)
- **CSRF**: Cookie + hidden-input double-submit pattern (`rvt` token)
- **Anti-bot**: Honeypot fields (`PotText`, `__EiTokPotText`)
- **Frontend**: jQuery + Bootstrap + Moment.js (account pages), React Native Web + Bidgely SDK (Insights)
- **Analytics**: Google Analytics, Facebook Pixel, Twitter Pixel, TikTok Pixel, Snapchat Pixel, OneTrust (cookies), EdgeTier Arthur (chat)

---

## 2. Authentication Flow

### Step 1 — Fetch the Login Page

```
GET https://youraccountonline.electricireland.ie/
```

**Purpose**: Obtain the CSRF token and anti-bot tokens.

**Response** (HTML):

| Element | Location | Description |
|---------|----------|-------------|
| `rvt` cookie | `Set-Cookie` header | CSRF token (HttpOnly, Secure, SameSite=Strict) |
| `Source` | `<input type="hidden" name="Source">` | Session source token (e.g. `EF-*R47oc8RpYWigS71grleJFA`) |
| `rvt` (form) | `<input type="hidden" name="rvt">` | Duplicate of cookie value, submitted in form body |
| `PotText` | `<input name="PotText">` | Honeypot — must be submitted **empty** |
| `__EiTokPotText` | `<input name="__EiTokPotText">` | Honeypot — must be submitted **empty** |

**Extraction targets** (CSS selectors):

```
input[name="Source"]         → value attribute
input[name="rvt"]            → value attribute
input[name="PotText"]        → leave empty
input[name="__EiTokPotText"] → leave empty
```

### Step 2 — Submit Login Credentials

```
POST https://youraccountonline.electricireland.ie/
Content-Type: application/x-www-form-urlencoded
Cookie: rvt=<CSRF_TOKEN>; ...
```

**Form body** (all fields required):

| Field | Value | Notes |
|-------|-------|-------|
| `LoginFormData.UserName` | User's email address | |
| `LoginFormData.Password` | User's password | |
| `rvt` | CSRF token from Step 1 | Must match the `rvt` cookie |
| `Source` | Source token from Step 1 | |
| `PotText` | (empty string) | Honeypot — non-empty triggers bot detection |
| `__EiTokPotText` | (empty string) | Honeypot — non-empty triggers bot detection |
| `ReturnUrl` | (empty string) | |
| `AccountNumber` | (empty string) | Not used for login |

**Success response** (HTTP 302 or 200):

| Signal | How to detect |
|--------|--------------|
| Success | Response sets `EI.RP` and `.AspNetCore.Session` cookies; body contains account dashboard HTML with account numbers |
| Invalid credentials | HTTP 200; body contains `"Incorrect email address and/or password"` in HTML |
| Account locked | HTTP 200; body contains lockout message |

**Cookies set on successful login**:

| Cookie | Properties |
|--------|-----------|
| `EI.RP` | `max-age=1200`, `HttpOnly`, `Secure`, `SameSite=Lax` |
| `.AspNetCore.Session` | `HttpOnly`, `Secure`, `SameSite=Lax`, session-scoped |
| `ARRAffinity` | Azure load-balancer sticky session |
| `ARRAffinitySameSite` | SameSite variant of above |
| `rvt` | Rotated CSRF token for next request |

### Step 3 — Navigate to Insights (Account Selection)

After login, the user lands on the "My Accounts" page. To reach the Insights page (where
meter IDs and API endpoints are available), a POST to the event handler is required.

```
POST https://youraccountonline.electricireland.ie/Accounts/OnEvent
Content-Type: application/x-www-form-urlencoded
Cookie: <all session cookies>
```

**Form body**:

| Field | Value | Notes |
|-------|-------|-------|
| `SelectedAccount.AccountNumber` | Encrypted account reference | `EF-*...` value from the account dashboard HTML |
| `triggers_event` | `AccountSelection.ToInsights` | Routing event name |
| `rvt` | Current CSRF token | From the latest `rvt` cookie |
| `flow-form-id` | Flow form identifier | `EF-*...` from `<input name="flow-form-id">` |
| `FlowHandler` | Flow handler reference | `EF-*...` from `<input name="FlowHandler">` |
| `FlowScreenName` | Screen name reference | `EF-*...` from `<input name="FlowScreenName">` |
| `ScreenTitle` | (empty string) | |

**All `EF-*` prefixed values** are encrypted, session-specific tokens. They cannot be
predicted or reused across sessions.

**Cached login shortcut**: If meter IDs are already known (cached from a previous session),
the integration can send a minimal `OnEvent` POST with just `triggers_event` and
`SelectedAccount.AccountNumber` (the encrypted account reference from the current session's
dashboard page). The full `FlowHandler`/`FlowScreenName` fields are not strictly required
but should be included for robustness.

**Response**: HTML page containing the Insights view with the `<div id="modelData">`
element from which meter IDs are extracted.

---

## 3. Session Management

### Session Lifecycle

```
Login (POST /)
    │
    ├─ Cookies set: EI.RP, .AspNetCore.Session, rvt, ARRAffinity
    │
    ▼
Active Session (10–20 min idle timeout)
    │
    ├─ Each API call resets the idle timer (ResetSessionExpiry)
    ├─ The `rvt` cookie is rotated on every HTML page load
    ├─ API calls (JSON responses) do NOT rotate `rvt`
    │
    ▼
Session Expired
    │
    ├─ All API calls return HTTP 200 + text/html (login page)
    ├─ No 401/403 — detection by Content-Type only
    └─ Must re-authenticate from Step 1
```

### Timing

| Property | Value |
|----------|-------|
| `EI.RP` cookie max-age | 1200 seconds (20 minutes) |
| Observed idle timeout | ~10–15 minutes without API calls |
| Session extension | Each successful MeterInsight API call resets the timer |
| CSRF token lifetime | Rotated on every HTML response; stable across API (JSON) calls |

### Session Expiry Detection

**The server returns HTTP 200 for expired sessions.** There is no 401 or 403 status code.

| Indicator | Valid session | Expired session |
|-----------|--------------|-----------------|
| HTTP status | `200` | `200` |
| `Content-Type` | `application/json` or `application/json; charset=utf-8` | `text/html; charset=utf-8` |
| Response body | JSON object | Full HTML login page |

**Detection algorithm**:

```python
response = await session.get(url)
content_type = response.headers.get("Content-Type", "")
if "text/html" in content_type:
    # Session expired — re-authenticate
elif "application/json" in content_type:
    # Valid response — parse JSON
```

---

## 4. Navigation & Account Selection

The portal uses a **flow-based navigation system**. All navigation between sections is done
via POST requests to `/Accounts/OnEvent` with different `triggers_event` values.

### Known Navigation Events

| `triggers_event` value | Destination |
|------------------------|-------------|
| `AccountSelection.ToInsights` | Insights page (usage charts, Bidgely SDK) |
| `AccountSelection.ToBillingAndPayments` | Bills & Payments |
| `AccountSelection.ToPlanAndDirectDebit` | Plan & Direct Debit |
| `AccountSelection.ToDetails` | Account Details |
| `AccountSelection.ToMeterReading` | Meter Reading submission |
| `AccountSelection.ToMovingHouse` | Moving House |
| `AccountSelection.ToCompetition` | Competition entry |

### Other Known Routes

| URL | Method | Purpose |
|-----|--------|---------|
| `/Accounts/Init` | GET | My Accounts dashboard (redirects after login) |
| `/Login/ForgotPassword` | GET | Password reset |
| `/Login/SignUp` | GET | Registration |
| `/Files/GetInvoicePdf?i=EF-*...` | GET | Download bill PDF |
| `/marketing-consent/check` | GET | Marketing consent status (JSON) |
| `/DeferredComponentPartials/Resolve?i=EF-*...` | GET | Lazy-loaded page components |
| `/accountdashboard/Current?i=EF-*...` | GET | Account dashboard (after account selection) |
| `/accountdashboard/NewContainedView?i=EF-*...` | GET | Help, Contact Us, Terms pages |

---

## 5. Meter ID Extraction (HTML Scraping)

After navigating to the Insights page, the HTML contains a hidden `<div>` with all
meter identifiers needed for the MeterInsight API endpoints.

### Target Element

```html
<div id="modelData"
     data-partner="7006723196"
     data-contract="1502212609"
     data-premise="63353984"
     data-accountno="951785073"
     data-microgen="False"
     data-prepaycustomer="False"
     data-dualfuelaccount="False"
     data-showreviewtab="False"
     data-startwithreviewtab="False"
     hidden>
</div>
```

### Attribute Reference

| Attribute | Type | Description | API Usage |
|-----------|------|-------------|-----------|
| `data-partner` | Numeric string | Bidgely partner ID | Path parameter `{partnerId}` |
| `data-contract` | Numeric string | Bidgely contract ID | Path parameter `{contractId}` |
| `data-premise` | Numeric string | Bidgely premise ID | Path parameter `{premiseId}` |
| `data-accountno` | Numeric string | EI account number (visible to customer) | Display / unique ID |
| `data-microgen` | `"True"` / `"False"` | Microgeneration (solar) customer | Enables export/return stats |
| `data-prepaycustomer` | `"True"` / `"False"` | Pay-as-you-go meter | Hides projected bill tab |
| `data-dualfuelaccount` | `"True"` / `"False"` | Combined gas + electric account | May have gas data |
| `data-showreviewtab` | `"True"` / `"False"` | Show annual review tab | UI feature flag |
| `data-startwithreviewtab` | `"True"` / `"False"` | Default to review tab on load | UI feature flag |

### Extraction (BeautifulSoup)

```python
from bs4 import BeautifulSoup

soup = BeautifulSoup(html, "html.parser")
model = soup.find(id="modelData")
if model:
    partner_id  = model["data-partner"]
    contract_id = model["data-contract"]
    premise_id  = model["data-premise"]
    account_no  = model["data-accountno"]
    is_microgen = model["data-microgen"].lower() == "true"
    is_prepay   = model["data-prepaycustomer"].lower() == "true"
    is_dualfuel = model["data-dualfuelaccount"].lower() == "true"
```

### Caching

Meter IDs are stable across sessions for the same account. They can be safely cached in
the config entry and reused without re-scraping on every update cycle. Only re-scrape if
the cached login path fails (e.g., account changes, meter replacement).

---

## 6. Bidgely SDK Architecture

The Insights page loads the **Bidgely Web SDK**, a React Native Web application that
renders the usage charts, appliance breakdown, and other analytics widgets.

### SDK Loading

```
Scripts loaded (in order):
1. /vendor/bidgely/js/bundle.js           (1.1 MB — Bidgely SDK core)
2. /js/components/insights-react-component/index.mjs  (7.3 MB — React app)
3. /js/components/insights-react-component/insights-react-listeners.js
4. /js/pages/insights/insights.js          (widget initialization)
5. https://static.bidgely.com/scripts/xdomain.min.js  (cross-domain lib)
```

### SDK Initialization

```javascript
// From insights.js — how the SDK is initialized:
BidgelyWebSdk.initialize(window.RunMode, window.bidgelyWebSdkPayload, callback);
```

### Data Fetching Pattern

The React app does **not** call Bidgely directly. Instead, it receives a `fetchFunction`
that makes relative API calls through the EI server:

```javascript
// From insights-react-listeners.js:
const fetchData = async (endpoint, timeoutSeconds) => {
    const fullUrl = `/${endpoint}`;
    const result = await fetch(fullUrl, { timeout: 10 * 1000 });
    if (result.status === 204) throw new ResponseError('No content', 204);
    if (!result.ok) throw new Error(`Failed to fetch ${fullUrl}`);
    ResetSessionExpiry();  // Extends session on each call
    return result.json();
};

renderInsightsApp({
    fetchFunction: fetchData,
    accountDetails: {
        partnerId: modelData.partnerId,
        contractId: modelData.contractId,
        premiseId: modelData.premiseId,
        accountNumber: modelData.accountNumber
    },
    // ... configuration flags from modelData attributes
});
```

All API calls from the React app flow through the EI server as
`/MeterInsight/{partnerId}/{contractId}/{premiseId}/...` endpoints. The server proxies
these to Bidgely's backend and returns the JSON response to the browser.

### Known Bidgely Infrastructure

| URL | Environment | Purpose |
|-----|-------------|---------|
| `api.eu.bidgely.com` | EU Production | Production API |
| `eiuatapi.bidgely.com` | EI UAT | Electric Ireland testing |
| `naapi.bidgely.com` | NA Production | North America (not EI) |
| `ssoprod.bidgely.com` | Production | Bidgely SSO |
| `sso-nonprod.bidgely.com` | Non-production | Bidgely SSO testing |
| `btocdevapi.bidgely.com` | Development | B2C development API |
| `static.bidgely.com` | CDN | Fonts, images, scripts |
| `d12of87xj1f8rc.cloudfront.net` | CDN | Rate/tariff images |

---

## 7. Cookie Inventory

### HttpOnly Cookies (not accessible via JavaScript)

| Cookie | Domain | Purpose | Properties |
|--------|--------|---------|------------|
| `rvt` | `.electricireland.ie` | CSRF token | HttpOnly, Secure, SameSite=Strict; **rotated on every HTML response** |
| `EI.RP` | `.electricireland.ie` | Session marker | HttpOnly, Secure, SameSite=Lax; `max-age=1200` |
| `.AspNetCore.Session` | `.electricireland.ie` | ASP.NET Core session | HttpOnly, Secure, SameSite=Lax; session-scoped |
| `ARRAffinity` | `.electricireland.ie` | Azure LB affinity | HttpOnly, Secure |
| `ARRAffinitySameSite` | `.electricireland.ie` | Azure LB (SameSite) | HttpOnly, Secure, SameSite=None |

### JavaScript-Accessible Cookies

| Cookie | Purpose |
|--------|---------|
| `OptanonAlertBoxClosed` | OneTrust cookie consent dismissal timestamp |
| `OptanonConsent` | OneTrust consent categories |
| `_ga`, `_ga_*` | Google Analytics |
| `_gcl_au` | Google Ads conversion linker |
| `_twpid` | Twitter Pixel |
| `_fbp` | Facebook Pixel |
| `_ttp` | TikTok Pixel |
| `_scid` | Snapchat Pixel |
| `show_nav` | UI state (nav collapsed/expanded) |

---

## 8. Storage (localStorage / sessionStorage)

### localStorage

| Key | Purpose | Relevance |
|-----|---------|-----------|
| `arthur-chat-state` | EdgeTier chat widget state (minimized, URLs visited) | None |
| `arthur-loader-state` | Chat widget loader state | None |
| `lastExternalReferrer` | Referrer tracking | None |
| `lastExternalReferrerTime` | Referrer timestamp | None |
| `_gcl_ls` | Google conversion linker state | None |

### sessionStorage

Empty — no data stored in sessionStorage.

**Conclusion**: No authentication tokens, API keys, or session data are stored in
browser storage. All auth state is cookie-based.

---

## 9. Error Detection & Edge Cases

### Response Behaviour Matrix

| Scenario | `hourly-usage` | `usage-daily` | `bill-period` | `appliance-usage` |
|----------|---------------|---------------|---------------|-------------------|
| **Valid request** | `200` + JSON | `200` + JSON | `200` + JSON | `200` + JSON |
| **No data for date** | `204` No Content | Days have `null` tariffs | N/A | N/A |
| **Future date** | `204` No Content | Days have `null` tariffs | N/A | N/A |
| **Date before meter** | `204` No Content | N/A | N/A | N/A |
| **Invalid date format** | `200`, body `""` | `200`, body `""` | N/A | N/A |
| **Missing required params** | `200`, body `""` | `200`, body `""` | N/A (no params) | `200` + HTML redirect |
| **Swapped start/end** | N/A (single date) | `422` + JSON error | N/A | Not tested |
| **Invalid meter IDs** | Not tested | Not tested | `200` + HTML redirect | `200` + HTML redirect |
| **Unauthorized meter IDs** | Not tested | Not tested | `200` + HTML redirect | `200` + HTML redirect |
| **Session expired** | `200` + HTML | `200` + HTML | `200` + HTML | `200` + HTML |

### JSON Casing Inconsistency

Success and error responses use **different property casing**:

| Property | Success (camelCase) | Error (PascalCase) |
|----------|--------------------|--------------------|
| Status code | `subStatusCode` | `SubStatusCode` |
| Success flag | `isSuccess` | `IsSuccess` |
| Message | `message` | `Message` |

Consumers must handle both casings when parsing responses.

### Empty String Responses

Several endpoints return `200 OK` with body `""` (a JSON-encoded empty string) for
invalid or missing parameters. This is valid JSON but is **not** an object or array.
Parse defensively:

```python
text = await response.text()
if not text or text == '""':
    return None  # No data / invalid params
data = json.loads(text)
```

### `usage-daily` Range Limitation

When queried with a date range spanning multiple billing periods, `usage-daily` appears to
return data only for the **most recent billing period** within the range. To retrieve data
for older periods, query each billing period individually using exact dates from the
`/bill-period` endpoint.

### The 204 Response

The `hourly-usage` endpoint returns `204 No Content` with `Content-Type: application/json`
for dates with no available data. The response body is empty (zero bytes). This is
distinct from the empty-string `""` response returned for invalid parameters.

---

## 10. Rate Limiting & Best Practices

### Observed Behaviour

No explicit rate limiting headers (`X-RateLimit-*`, `Retry-After`) were observed. However:

- The EI server is a server-side proxy to Bidgely. Excessive requests may trigger
  Bidgely-side rate limiting (not directly observable).
- The current integration fetches hourly data **sequentially** (one day at a time) to
  avoid overwhelming the upstream service.
- The Insights React app makes at most 3–4 concurrent requests on page load.

### Recommended Request Pattern

```
1. POST /                           → Login
2. POST /Accounts/OnEvent           → Navigate to Insights
3. GET  /MeterInsight/.../bill-period    → Get available date ranges
4. GET  /MeterInsight/.../usage-daily?...  → Pre-flight: find days with data
5. For each day with data:
   GET  /MeterInsight/.../hourly-usage?date=YYYY-MM-DD  → Hourly detail
```

**Sequential, not parallel**: Issue API calls one at a time. The upstream Bidgely service
is sensitive to concurrent requests from the same session.

### Session Lifetime Optimisation

- The integration updates hourly (`SCAN_INTERVAL = 60 min`).
- The session expires in ~10–20 min idle.
- Therefore, a fresh login is required on every update cycle.
- Within a single update cycle, batch all API calls quickly (the session extends with each call).

---

## 11. Alternative Data Sources

| Source | Auth Method | Data Available | Limitations |
|--------|------------|----------------|-------------|
| **Electric Ireland** (this doc) | Form-based + cookies | Hourly kWh + cost with tariff breakdown | No public API; scraping required for auth |
| **ESB Networks** (`myaccount.esbnetworks.ie`) | Azure AD B2C (OAuth) | 30-min kWh (raw HDF data), CSV export | Max 2 logins/24hr; CAPTCHA since Nov 2024; no cost data |
| **Bidgely direct** (`api.eu.bidgely.com`) | Unknown (SHA-1 signature?) | Full analytics suite | Credentials embedded server-side; no public access |
| **SMDAC** (Smart Metering Data Access Code) | OAuth2 (planned) | Official smart meter data | Not yet operational for consumers in Ireland |
| **`bidgely` PyPI package** | API key from utility | Historical consumption via Bidgely API | Requires Bidgely API credentials (not publicly available for EI) |

---

## 12. Account Flags & Capabilities

The `modelData` element on the Insights page exposes several boolean flags that indicate
the account's capabilities and tariff configuration.

### Flag Descriptions

| Flag | When `True` | Integration Impact |
|------|------------|-------------------|
| `data-microgen` | Customer has microgeneration (solar PV) | May have grid-export/return data; consider creating return/compensation statistics |
| `data-prepaycustomer` | Pay-as-you-go (PAYG) meter | Projected bill tab hidden; data patterns may differ |
| `data-dualfuelaccount` | Combined gas + electric account | May have gas consumption data accessible via separate meter IDs |
| `data-showreviewtab` | Annual review feature enabled | Informational only |
| `data-startwithreviewtab` | Default to annual review on page load | Informational only |

### Tariff Bucket Mapping

The API returns usage data in tariff-bucketed fields. The active buckets depend on the
customer's tariff plan:

| Tariff Plan | Active Buckets | Inactive Buckets |
|-------------|---------------|------------------|
| Time-of-Use (standard) | `onPeak`, `midPeak`, `offPeak` | `flatRate` = `null` |
| Flat rate (no TOU) | `flatRate` | `onPeak`, `midPeak`, `offPeak` = `null` |
| Night-saver / other | Varies | Varies |

When processing datapoints, sum all non-null tariff buckets for the total consumption/cost.

```python
def total_consumption(datapoint: dict) -> float:
    total = 0.0
    for bucket in ("offPeak", "midPeak", "onPeak", "flatRate"):
        if datapoint.get(bucket) is not None:
            total += datapoint[bucket]["consumption"]
    return total
```
