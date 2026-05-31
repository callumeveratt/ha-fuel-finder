"""Standalone tests for the Fuel Finder API client parse/merge/search logic.

Runs WITHOUT Home Assistant or a network. A tiny fake aiohttp-style session
feeds representative station + price JSON (shaped like the real API per the
beecho01/Fuel-Prices-UK reference) through the full pipeline: OAuth token,
batch pagination, station/price merge, and the local radius/sort search.

Run:  python tests/test_api_parsing.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

# Load the api module (and its const dependency) under a synthetic package so
# relative imports resolve WITHOUT executing the integration's __init__.py,
# which depends on Home Assistant / voluptuous not installed in this env.
PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "fuel_finder"
_PKG = "fuel_finder_under_test"


def _load(module: str):
    spec = importlib.util.spec_from_file_location(
        f"{_PKG}.{module}", PKG_DIR / f"{module}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_pkg = types.ModuleType(_PKG)
_pkg.__path__ = [str(PKG_DIR)]
sys.modules[_PKG] = _pkg
_load("const")
_api = _load("api")

FuelFinderClient = _api.FuelFinderClient
coerce_price = _api.coerce_price
haversine_km = _api.haversine_km
normalise_fuel_type = _api.normalise_fuel_type

# --------------------------------------------------------------------------- #
# Representative payloads (shaped like the real Fuel Finder API).
# Central London query point used by the search tests.
# --------------------------------------------------------------------------- #
QUERY_LAT, QUERY_LON = 51.5074, -0.1278  # Charing Cross

# /api/v1/pfs  -- station info records (batch 1; batch 2 is empty == end).
STATION_BATCH_1 = {
    "data": [
        {
            "node_id": "ST001",
            "trading_name": "Charing Cross Service Station",
            "brand_name": "Shell",
            "location": {
                "address_line_1": "1 Strand",
                "address_line_2": "",
                "city": "London",
                "county": "Greater London",
                "country": "England",
                "postcode": "WC2N 5HX",
                "latitude": 51.5080,
                "longitude": -0.1281,
            },
        },
        {
            "node_id": "ST002",
            "trading_name": "Westminster Filling Station",
            "brand_name": "BP",
            "location": {
                "address_line_1": "10 Victoria St",
                "city": "London",
                "county": "Greater London",
                "postcode": "SW1H 0NB",
                "latitude": 51.4990,
                "longitude": -0.1340,
            },
        },
        {
            # Far away (Manchester) -- must be filtered out by the radius.
            "node_id": "ST003",
            "trading_name": "Manchester Central",
            "brand_name": "Esso",
            "location": {
                "address_line_1": "1 Deansgate",
                "city": "Manchester",
                "county": "Greater Manchester",
                "postcode": "M3 1AA",
                "latitude": 53.4808,
                "longitude": -2.2426,
            },
        },
    ]
}

# /api/v1/pfs/fuel-prices -- prices in PENCE, mixed source grade codes.
PRICE_BATCH_1 = {
    "data": [
        {
            "node_id": "ST001",
            "fuel_prices": [
                {"fuel_type": "E10", "price": 145.9,
                 "price_last_updated": "2026-05-30T08:00:00Z"},
                {"fuel_type": "E5", "price": 152.9},
                {"fuel_type": "B7_STANDARD", "price": 149.9},   # -> B7
                {"fuel_type": "B7_PREMIUM", "price": 162.9},    # -> SDV
            ],
        },
        {
            "node_id": "ST002",
            "fuel_prices": [
                {"fuel_type": "E10", "price": 142.7},           # cheapest E10
                {"fuel_type": "DIESEL", "price": 147.5},        # -> B7
            ],
        },
        {
            "node_id": "ST003",  # Manchester -- present but out of radius
            "fuel_prices": [{"fuel_type": "E10", "price": 139.9}],
        },
    ]
}


class _FakeResponse:
    def __init__(self, status: int, payload, headers=None):
        self.status = status
        self._payload = payload
        self.headers = headers or {}
        self.reason = "OK" if status < 400 else "ERR"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        return self._payload

    def raise_for_status(self):
        if self.status >= 400:
            raise AssertionError(f"HTTP {self.status}")


class FakeSession:
    """Minimal aiohttp.ClientSession stand-in driving canned responses."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, *, headers=None, params=None, json=None, timeout=None):
        self.calls.append((method, url, params or {}))
        # OAuth token endpoint.
        if url.endswith("/api/v1/oauth/generate_access_token"):
            return _FakeResponse(
                200, {"data": {"access_token": "fake-token", "expires_in": 3600}}
            )
        batch = (params or {}).get("batch-number", 1)
        if url.endswith("/api/v1/pfs/fuel-prices"):
            return _FakeResponse(200, PRICE_BATCH_1 if batch == 1 else {"data": []})
        if url.endswith("/api/v1/pfs"):
            return _FakeResponse(200, STATION_BATCH_1 if batch == 1 else {"data": []})
        raise AssertionError(f"Unexpected URL: {url}")

    def get(self, url, **kwargs):  # used only by geocoder; unused here
        raise AssertionError("network get not expected in tests")


def _check(name: str, cond: bool) -> None:
    if not cond:
        raise AssertionError(f"FAILED: {name}")
    print(f"  ok: {name}")


def test_unit_helpers() -> None:
    print("unit helpers")
    # coerce_price: pence -> pounds, tenths-of-pence -> pounds, passthrough.
    _check("coerce 145.9p -> 1.459", coerce_price(145.9) == 1.459)
    _check("coerce 1459 (tenths) -> 1.459", coerce_price(1459) == 1.459)
    _check("coerce '152.9' string", coerce_price("152.9") == 1.529)
    _check("coerce already-pounds 1.45", coerce_price(1.45) == 1.45)
    _check("coerce None -> None", coerce_price(None) is None)
    _check("coerce nested dict", coerce_price({"price": 142.7}) == 1.427)

    # grade normalisation.
    _check("E10 -> E10", normalise_fuel_type("E10") == "E10")
    _check("B7_STANDARD -> B7", normalise_fuel_type("B7_STANDARD") == "B7")
    _check("B7_PREMIUM -> SDV", normalise_fuel_type("B7_PREMIUM") == "SDV")
    _check("DIESEL -> B7", normalise_fuel_type("DIESEL") == "B7")
    _check("e5 lowercase -> E5", normalise_fuel_type("e5") == "E5")
    _check("unknown -> None", normalise_fuel_type("LPG") is None)

    # haversine sanity (~0 for same point, >250km London->Manchester).
    _check("haversine same point ~0", haversine_km(51.5, -0.1, 51.5, -0.1) < 0.001)
    _check(
        "haversine London->Manchester ~262km",
        250 < haversine_km(51.5074, -0.1278, 53.4808, -2.2426) < 280,
    )


def test_end_to_end() -> None:
    print("end-to-end search (token -> paginate -> merge -> search)")
    session = FakeSession()
    client = FuelFinderClient(session, "client-id", "client-secret")
    client._min_interval = 0  # don't sleep between requests in tests

    results = asyncio.run(
        client.async_search(
            latitude=QUERY_LAT,
            longitude=QUERY_LON,
            radius_km=8.0,
            fuel_type="E10",
            limit=5,
        )
    )

    # Radius filter: Manchester (ST003) excluded; two London sites remain.
    _check("radius filter -> 2 results", len(results) == 2)
    ids = {r["site_id"] for r in results}
    _check("ST003 (Manchester) filtered out", "ST003" not in ids)

    # Cheapest-first ordering: ST002 (142.7p) before ST001 (145.9p).
    _check("cheapest first is ST002", results[0]["site_id"] == "ST002")
    _check("prices ascending", results[0]["price"] <= results[1]["price"])

    # Pence -> pound conversion + both fields exposed.
    cheapest = results[0]
    _check("price in pounds (1.427)", cheapest["price"] == 1.427)
    _check("price_pence (142.7)", cheapest["price_pence"] == 142.7)

    # Required output keys present and populated from nested station shape.
    for key in (
        "site_id", "brand", "name", "address", "postcode", "latitude",
        "longitude", "distance_km", "fuel_type", "price", "price_pence",
        "last_updated",
    ):
        _check(f"key present: {key}", key in cheapest)
    st001 = next(r for r in results if r["site_id"] == "ST001")
    _check("brand mapped from brand_name", st001["brand"] == "Shell")
    _check("name mapped from trading_name",
           st001["name"] == "Charing Cross Service Station")
    _check("address built from location", "Strand" in st001["address"])
    _check("postcode from nested location", st001["postcode"] == "WC2N 5HX")
    _check("fuel_type normalised", cheapest["fuel_type"] == "E10")
    _check("last_updated parsed to ISO",
           st001["last_updated"].startswith("2026-05-30T08:00:00"))

    # Grade mapping reached the merged index: B7 from B7_STANDARD, SDV from B7_PREMIUM.
    b7 = asyncio.run(
        client.async_search(QUERY_LAT, QUERY_LON, 8.0, "B7", 5)
    )
    _check("B7 search finds B7_STANDARD price (1.499)",
           any(r["site_id"] == "ST001" and r["price"] == 1.499 for r in b7))
    sdv = asyncio.run(
        client.async_search(QUERY_LAT, QUERY_LON, 8.0, "SDV", 5)
    )
    _check("SDV search finds B7_PREMIUM price (1.629)",
           any(r["site_id"] == "ST001" and r["price"] == 1.629 for r in sdv))

    # Pagination: each endpoint fetched batch 1 (data) + batch 2 (empty stop).
    pfs_batches = [c[2].get("batch-number") for c in session.calls
                   if c[1].endswith("/api/v1/pfs")]
    _check("paged station endpoint to empty batch 2", pfs_batches == [1, 2])

    # Token requested once and reused (single token POST).
    token_calls = [c for c in session.calls if c[1].endswith("generate_access_token")]
    _check("token requested exactly once", len(token_calls) == 1)


def main() -> int:
    test_unit_helpers()
    test_end_to_end()
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
