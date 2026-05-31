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
- Choose `unit`: `km` (default) or `miles` — affects both the `radius` input
  and the distance output
- Filter by `fuel_type` (`E5`, `E10`, `B7`, `SDV`), `radius` and `limit`
- Results include brand, address, postcode, `distance_km`, `distance_miles`,
  price (£) and price_pence
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

### By latitude/longitude (km)

```yaml
action: fuel_finder.search_price
data:
  latitude: 53.2769
  longitude: -0.6608
  radius: 8
  unit: km
  fuel_type: E5
  limit: 5
response_variable: fuel
```

### By postcode (miles)

```yaml
action: fuel_finder.search_price
data:
  postcode: "LN1 1XX"
  radius: 5
  unit: miles
  fuel_type: E5
response_variable: fuel
```

### Response shape

```yaml
search:
  latitude: 53.2769
  longitude: -0.6608
  radius: 8          # in the requested unit
  radius_km: 8.0     # always in km (internal)
  unit: km
  fuel_type: E5
  count: 5
cheapest:
  brand: Tesco
  name: Tesco Superstore
  address: Linwood Rd, Market Rasen, LN8 3AW
  postcode: LN8 3AW
  latitude: 53.38367
  longitude: -0.33618
  distance_km: 24.61
  distance_miles: 15.29
  fuel_type: E5
  price: 1.599        # pounds
  price_pence: 159.9
  last_updated: "2026-05-18T14:19:15+00:00"
stations:
  - ...               # same shape, all stations within radius, cheapest first
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
      {{ state_attr('device_tracker.cal_s_cupra_position', 'latitude') is not none }}
actions:
  - action: fuel_finder.search_price
    data:
      latitude: "{{ state_attr('device_tracker.cal_s_cupra_position', 'latitude') }}"
      longitude: "{{ state_attr('device_tracker.cal_s_cupra_position', 'longitude') }}"
      fuel_type: E5
      radius: 10
      unit: miles
      limit: 3
    response_variable: fuel
  - choose:
      - conditions:
          - condition: template
            value_template: "{{ fuel.cheapest is not none }}"
        sequence:
          - action: notify.mobile_app_cal_s_oneplus
            data:
              title: "⛽ Low fuel ({{ states('sensor.cal_s_cupra_fuel_level') }}%)"
              message: >-
                Cheapest E5: {{ fuel.cheapest.brand }} —
                {{ fuel.cheapest.price_pence }}p/L,
                {{ fuel.cheapest.distance_miles }} miles away
                ({{ fuel.cheapest.postcode }}).
              data:
                actions:
                  - action: URI
                    title: Navigate
                    uri: >-
                      google.navigation:q={{ fuel.cheapest.latitude }},{{ fuel.cheapest.longitude }}
    default:
      - action: notify.mobile_app_cal_s_oneplus
        data:
          title: "⛽ Low fuel ({{ states('sensor.cal_s_cupra_fuel_level') }}%)"
          message: >-
            No E5 found within 10 miles. Range left:
            {{ states('sensor.cal_s_cupra_combustion_range') }} km.
```

## Troubleshooting

If results come back empty, enable debug logging to inspect the raw API records:

```yaml
logger:
  logs:
    custom_components.fuel_finder: debug
```

The field helpers most likely to need adjusting if live data differs:
`_extract_station_identifier`, `_extract_fuel_entries_from_row` /
`_extract_source_fuel_type`, and `coerce_price` in `api.py`.

Data © Crown copyright, provided under the Open Government Licence v3.0.

## Disclaimer

Not affiliated with the CMA, DESNZ, or the Fuel Finder service. Provided as-is.
