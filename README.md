# Fuel Finder Search (UK) — Home Assistant integration

An on-demand fuel-price lookup for Home Assistant built on the UK Government
**Fuel Finder** open-data scheme (the statutory scheme that replaced the
interim CMA road-fuel feeds on 1 May 2026).

Unlike fixed-location fuel integrations, this one exposes a **service that
returns data** — `fuel_finder.search_price` — which you call with *any*
location (latitude/longitude **or** a postcode). That makes it ideal for
automations that pass a vehicle's live GPS to find the cheapest fuel *wherever
the car currently is*.

## Features

- `fuel_finder.search_price` service with response data (cheapest first)
- Search by `latitude`+`longitude` **or** by UK `postcode` (geocoded via the
  free [postcodes.io](https://postcodes.io) API — no key)
- Filter by `fuel_type` (`E5`, `E10`, `B7`, `SDV`), `radius_km` and `limit`
- Results include brand, address, postcode, distance, price and coordinates
- Dataset cached in memory (1 hour) to respect the API rate limit

## Prerequisites — API credentials

The Fuel Finder API uses OAuth2 client credentials. You need to register, for
free, as an **Information Recipient** application:

1. Go to <https://www.developer.fuel-finder.service.gov.uk/> and sign in with
   **GOV.UK One Login** (create an account if needed).
2. Register an **Information Recipient** application.
3. Create API application credentials and note the **client ID** and
   **client secret**.

## Installation (HACS)

1. HACS → ⋮ → **Custom repositories** → add
   `https://github.com/callumeveratt/ha-fuel-finder` as category **Integration**.
2. Download **Fuel Finder Search (UK)**, then **restart** Home Assistant.
3. **Settings → Devices & Services → Add Integration → Fuel Finder Search**,
   and enter your client ID and secret.

## Using the service

```yaml
action: fuel_finder.search_price
data:
  latitude: 53.2769
  longitude: -0.6608
  radius_km: 8
  fuel_type: E5
  limit: 5
response_variable: fuel
```

Or by postcode:

```yaml
action: fuel_finder.search_price
data:
  postcode: "LN1 1XX"
  fuel_type: E5
response_variable: fuel
```

The response looks like:

```yaml
search:
  latitude: 53.2769
  longitude: -0.6608
  radius_km: 8
  fuel_type: E5
  count: 12
cheapest:
  brand: Tesco
  address: ...
  postcode: LN6 ...
  distance_km: 1.8
  price: 145.9
  price_pence: 145.9
  latitude: 53.2...
  longitude: -0.6...
stations: [ ... ]
```

## Example: low-fuel alert for a car

```yaml
alias: Cupra low fuel - nearby E5
triggers:
  - trigger: numeric_state
    entity_id: sensor.cal_s_cupra_fuel_level
    below: 15
conditions:
  - condition: template
    value_template: >-
      {{ states('sensor.cal_s_cupra_fuel_level') not in ['unknown','unavailable'] }}
actions:
  - action: fuel_finder.search_price
    data:
      latitude: "{{ state_attr('device_tracker.cal_s_cupra_position','latitude') }}"
      longitude: "{{ state_attr('device_tracker.cal_s_cupra_position','longitude') }}"
      fuel_type: E5
      radius_km: 10
      limit: 3
    response_variable: fuel
  - action: notify.mobile_app_cal_s_oneplus
    data:
      title: "⛽ Low fuel ({{ states('sensor.cal_s_cupra_fuel_level') }}%)"
      message: >-
        Cheapest E5 nearby: {{ fuel.cheapest.brand }} —
        {{ fuel.cheapest.price_pence }}p ({{ fuel.cheapest.distance_km }} km).
      data:
        actions:
          - action: URI
            title: Navigate
            uri: >-
              google.navigation:q={{ fuel.cheapest.latitude }},{{ fuel.cheapest.longitude }}
```

## Notes / status

- The public schema for the Fuel Finder API response is not fully documented.
  The client reads station/price fields defensively across common shapes; if
  results come back empty, enable debug logging to inspect the raw records:

  ```yaml
  logger:
    logs:
      custom_components.fuel_finder: debug
  ```

  and adjust the field helpers in `api.py` (`_extract_station_identifier`,
  `_extract_fuel_entries_from_row` / `_extract_source_fuel_type`, and
  `coerce_price`) to match the live response.
- Data © Crown copyright, provided under the Open Government Licence v3.0.

## Disclaimer

Not affiliated with the CMA, DESNZ, or the Fuel Finder service. Provided as-is.
