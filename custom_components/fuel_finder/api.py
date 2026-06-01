"""Async client for the UK Fuel Finder API with on-demand nearby search.

The official Fuel Finder scheme (https://www.fuel-finder.service.gov.uk) exposes
two authenticated, batch-paginated endpoints:

* ``/api/v1/pfs``             -- station ("petrol filling station") info records
* ``/api/v1/pfs/fuel-prices`` -- fuel price records

Both are protected by an OAuth2 *client-credentials* token obtained from
``/api/v1/oauth/generate_access_token``. We page through every batch of each
endpoint, correlate price records to station records by ``node_id``, then filter
and sort locally by distance from a point.

Prices come from the feed in **pence** (e.g. ``145.9``); :func:`coerce_price`
normalises them to pounds and we expose both pounds (``price``) and pence
(``price_pence``) to callers.

Field handling mirrors the reference implementation
(github.com/beecho01/Fuel-Prices-UK). If live data differs, the helpers most
likely to need adjusting are :func:`_extract_station_identifier` (record key),
:func:`_extract_fuel_entries_from_row` / :func:`_extract_source_fuel_type`
(price shape), and :func:`coerce_price` (pence-vs-pounds heuristic). Enable debug
logging to dump a sample merged record.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from math import asin, cos, radians, sin, sqrt
from typing import Any

try:  # aiohttp is always present inside Home Assistant; guarded so the pure
    # parsing/merge/search logic can be imported and unit-tested offline.
    import aiohttp

    _CLIENT_ERRORS: tuple[type[Exception], ...] = (
        aiohttp.ClientError,
        asyncio.TimeoutError,
    )
except ImportError:  # pragma: no cover - exercised only in minimal test envs
    aiohttp = None  # type: ignore[assignment]
    _CLIENT_ERRORS = (asyncio.TimeoutError,)

from .const import (
    API_BASE_URL,
    BACKOFF_429_SECONDS,
    BATCH_PARAM,
    CACHE_TTL_SECONDS,
    DATE_FORMATS,
    DEFAULT_TIMEOUT_SECONDS,
    EFFECTIVE_START_PARAM,
    FUEL_PRICES_PATH,
    FUEL_TYPE_MAP,
    MAX_429_RETRIES,
    MAX_BATCHES,
    MIN_REQUEST_INTERVAL,
    OAUTH_TOKEN_PATH,
    PFS_PATH,
    POSTCODES_IO_URL,
    RATE_LIMIT_BACKOFF_MULTIPLIER,
    RATE_LIMIT_MAX_BACKOFF_SECONDS,
    TOKEN_REFRESH_MARGIN,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

_DEFAULT_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}


class FuelFinderAuthError(Exception):
    """Raised when authentication fails (bad client_id/secret)."""


class FuelFinderApiError(Exception):
    """Raised when an API request fails."""


# --------------------------------------------------------------------------- #
# Pure helpers (no network) -- safe to import and unit-test without aiohttp.
# --------------------------------------------------------------------------- #
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in kilometres."""
    lat1, lon1, lat2, lon2 = map(radians, (lat1, lon1, lat2, lon2))
    d_lat = lat2 - lat1
    d_lon = lon2 - lon1
    a = sin(d_lat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(d_lon / 2) ** 2
    return 6371.0 * 2 * asin(sqrt(a))


def coerce_price(value: Any) -> float | None:
    """Normalise a feed price to GBP (pounds).

    The Fuel Finder feed quotes prices in pence (e.g. ``145.9``). Some feeds use
    tenths-of-a-penny integers (e.g. ``1459``). This mirrors the reference
    ``price_parser.coerce_price``: values >= 1000 are divided by 1000, values
    >= 50 by 100, otherwise assumed to already be pounds.
    """
    if isinstance(value, dict):
        for key in ("price", "value", "amount", "pence_per_litre", "ppl"):
            if key in value:
                coerced = coerce_price(value[key])
                if coerced is not None:
                    return coerced
        return None
    if value in (None, ""):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if price >= 50:
        price = price / (1000 if price >= 1000 else 100)
    return round(price, 3)


def _safe_float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _clean_text(value: Any) -> str | None:
    """Trim surrounding whitespace from a feed string; None if empty."""
    if not isinstance(value, str):
        return value if value is None else str(value).strip() or None
    return value.strip() or None


def _first_present(record: dict[str, Any], *keys: str) -> Any:
    """Return the first present, non-None value among the given keys."""
    for key in keys:
        value = record.get(key)
        if value is not None:
            return value
    return None


def _extract_station_identifier(row: dict[str, Any]) -> str:
    """Return the station's stable id. Reference uses ``node_id``."""
    for key in ("node_id", "site_id", "id", "station_id", "pfs_id"):
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def normalise_fuel_type(value: Any) -> str | None:
    """Map a feed fuel-grade code to a supported grade (E10/E5/B7/SDV).

    Handles the documented mappings (B7_STANDARD->B7, B7_PREMIUM->SDV,
    DIESEL->B7) plus a few common aliases. Returns ``None`` for grades we do
    not surface.
    """
    if value is None:
        return None
    key = str(value).strip().upper().replace("-", "_").replace(" ", "_")
    for suffix in ("_PRICE", "_PENCE", "_PPL"):
        if key.endswith(suffix):
            key = key[: -len(suffix)]
    aliases = {
        "DIESEL": "B7",
        "PREMIUM_DIESEL": "B7_PREMIUM",
        "UNLEADED": "E10",
        "PREMIUM_UNLEADED": "E5",
    }
    key = aliases.get(key, key)
    return FUEL_TYPE_MAP.get(key)


def _format_address(location: dict[str, Any]) -> str:
    parts = (
        location.get("address_line_1"),
        location.get("address_line_2"),
        location.get("city"),
        location.get("county"),
        location.get("postcode"),
    )
    return ", ".join(
        str(part).strip()
        for part in parts
        if isinstance(part, str) and part.strip()
    )


def _parse_datetime(value: Any) -> str | None:
    """Best-effort parse of a feed timestamp into an ISO 8601 string."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        except (OSError, ValueError):
            return None
    if isinstance(value, str):
        stripped = value.strip()
        for fmt in DATE_FORMATS:
            try:
                dt = datetime.strptime(stripped, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.isoformat()
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(stripped.replace("Z", "+00:00")).isoformat()
        except ValueError:
            return None
    return None


def _latest_iso(current: str | None, candidate: str | None) -> str | None:
    if not candidate:
        return current
    if not current:
        return candidate
    try:
        current_dt = datetime.fromisoformat(current.replace("Z", "+00:00"))
        candidate_dt = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return candidate
    return candidate if candidate_dt >= current_dt else current


def _extract_records_from_payload(payload: Any) -> list[dict[str, Any]] | None:
    """Find the record list inside a batch payload.

    The feed wraps records under a container key (``data``/``results``/...);
    we walk up to three levels deep and return the first list of dicts.
    """
    candidates: list[list[dict[str, Any]]] = []

    def _collect(value: Any, depth: int = 0) -> None:
        if depth > 3 or value is None:
            return
        if isinstance(value, list):
            rows = [item for item in value if isinstance(item, dict)]
            if rows:
                candidates.append(rows)
        elif isinstance(value, dict):
            for child in value.values():
                _collect(child, depth + 1)

    _collect(payload)
    return candidates[0] if candidates else None


def _extract_total_batches_hint(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    values: list[Any] = []
    keys = ("total_batches", "total_pages", "totalPages", "last_page")
    for key in keys:
        if key in payload:
            values.append(payload[key])
    for container_key in ("pagination", "meta", "metadata", "page"):
        container = payload.get(container_key)
        if isinstance(container, dict):
            for key in keys:
                if key in container:
                    values.append(container[key])
    for candidate in values:
        try:
            number = int(candidate)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return None


def _build_batch_signature(rows: list[dict[str, Any]]) -> tuple[int, str, str]:
    first = rows[0] if rows else {}
    last = rows[-1] if rows else {}
    first_id = _extract_station_identifier(first) or repr(sorted(first.keys()))
    last_id = _extract_station_identifier(last) or repr(sorted(last.keys()))
    return (len(rows), first_id, last_id)


def _extract_fuel_entries_from_row(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Return per-grade price entries from a price record across known shapes."""
    entries: list[dict[str, Any]] = []
    for key in ("fuel_prices", "fuelPrices", "prices", "fuel_price_data"):
        container = row.get(key)
        if isinstance(container, list):
            entries.extend(item for item in container if isinstance(item, dict))
        elif isinstance(container, dict):
            for fuel_key, value in container.items():
                if isinstance(value, dict):
                    entry = dict(value)
                    entry.setdefault("fuel_type", fuel_key)
                else:
                    entry = {"fuel_type": fuel_key, "price": value}
                entries.append(entry)
    # Shape: the row itself is a single price entry ({node_id, fuel_type, price})
    if _extract_source_fuel_type(row) and coerce_price(row) is not None:
        entries.append(dict(row))
    return entries


def _extract_source_fuel_type(entry: dict[str, Any]) -> str | None:
    for key in ("fuel_type", "fuelType", "type", "grade", "product", "fuel"):
        value = entry.get(key)
        if value is not None and normalise_fuel_type(value):
            return str(value)
    return None


def _extract_fuel_entry_price(entry: dict[str, Any]) -> float | None:
    for key in (
        "price",
        "value",
        "pump_price",
        "pumpPrice",
        "pence_per_litre",
        "pencePerLitre",
        "amount",
    ):
        if key in entry:
            parsed = coerce_price(entry[key])
            if parsed is not None:
                return parsed
    return None


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class FuelFinderClient:
    """Handles OAuth2 client-credentials auth, paged fetch, and local search."""

    def __init__(
        self,
        session: "aiohttp.ClientSession",
        client_id: str,
        client_secret: str,
    ) -> None:
        self._session = session
        self._client_id = (client_id or "").strip()
        self._client_secret = (client_secret or "").strip()

        self._access_token: str | None = None
        self._token_expiry: datetime | None = None
        self._auth_lock = asyncio.Lock()

        # Merged station_index keyed by node_id, cached for CACHE_TTL_SECONDS.
        self._station_index: dict[str, dict[str, Any]] = {}
        self._last_refresh: float | None = None
        self._cache_lock = asyncio.Lock()

        # Rate-limit state (min interval + 429 cooldown).
        self._request_lock = asyncio.Lock()
        self._last_request_at: float | None = None
        self._cooldown_until: float = 0.0
        self._min_interval = MIN_REQUEST_INTERVAL

    # -- Auth --------------------------------------------------------------- #
    async def async_validate(self) -> None:
        """Validate credentials by obtaining a token. Raises on failure."""
        await self._get_access_token(force_refresh=True)

    @property
    def _token_is_valid(self) -> bool:
        return bool(
            self._access_token
            and self._token_expiry
            and datetime.now(timezone.utc) < self._token_expiry
        )

    async def _get_access_token(self, *, force_refresh: bool = False) -> str:
        if not self._client_id or not self._client_secret:
            raise FuelFinderAuthError("Fuel Finder API credentials are missing")

        async with self._auth_lock:
            if not force_refresh and self._token_is_valid:
                return self._access_token or ""

            url = f"{API_BASE_URL}{OAUTH_TOKEN_PATH}"
            headers = {**_DEFAULT_HEADERS, "Content-Type": "application/json"}
            # NB: client-credentials here use a bare id/secret body -- there is
            # no OAuth ``grant_type`` field (per the reference implementation).
            payload = {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            }

            data = await self._request("POST", url, headers=headers, json=payload)

            # Token may be top-level or nested under "data".
            token_data = data.get("data") if isinstance(data, dict) else None
            if not isinstance(token_data, dict):
                token_data = data if isinstance(data, dict) else {}

            token = token_data.get("access_token")
            if not token or not isinstance(token, str):
                raise FuelFinderAuthError("No access_token in token response")

            try:
                expires_in = max(int(token_data.get("expires_in")), 120)
            except (TypeError, ValueError):
                expires_in = 3600
            self._access_token = token
            self._token_expiry = datetime.now(timezone.utc) + timedelta(
                seconds=expires_in - TOKEN_REFRESH_MARGIN
            )
            _LOGGER.debug(
                "Fuel Finder token obtained (expires %s)",
                self._token_expiry.isoformat(),
            )
            return token

    # -- HTTP --------------------------------------------------------------- #
    async def _respect_rate_limit(self) -> None:
        """Hold a minimum interval between requests; honour any 429 cooldown.

        Must be called while holding ``self._request_lock``.
        """
        loop = asyncio.get_running_loop()
        now = loop.time()
        remaining_cooldown = self._cooldown_until - now
        if remaining_cooldown > 0:
            _LOGGER.debug("Fuel Finder 429 cooldown: waiting %.2fs", remaining_cooldown)
            await asyncio.sleep(remaining_cooldown)
            now = loop.time()
        if self._last_request_at is not None:
            elapsed = now - self._last_request_at
            if elapsed < self._min_interval:
                await asyncio.sleep(self._min_interval - elapsed)
        self._last_request_at = loop.time()

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Perform one HTTP request with rate limiting and 429 retry/backoff."""
        timeout = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT_SECONDS) if aiohttp else None
        for retry in range(MAX_429_RETRIES + 1):
            try:
                async with self._request_lock:
                    await self._respect_rate_limit()
                    async with self._session.request(
                        method, url, headers=headers, params=params,
                        json=json, timeout=timeout,
                    ) as resp:
                        body = await _parse_json_response(resp)
                        status = resp.status

                        if status in (400, 401, 403) and url.endswith(OAUTH_TOKEN_PATH):
                            raise FuelFinderAuthError(
                                f"Authentication failed (HTTP {status})"
                            )
                        if status == 429:
                            backoff = min(
                                _retry_after_seconds(resp)
                                * (RATE_LIMIT_BACKOFF_MULTIPLIER ** retry),
                                RATE_LIMIT_MAX_BACKOFF_SECONDS,
                            )
                            self._cooldown_until = (
                                asyncio.get_running_loop().time() + backoff
                            )
                            if retry >= MAX_429_RETRIES:
                                raise FuelFinderApiError(
                                    f"{method} {url} failed (429): rate limited"
                                )
                            _LOGGER.warning(
                                "Fuel Finder 429; retrying in %.2fs (%s/%s)",
                                backoff, retry + 1, MAX_429_RETRIES,
                            )
                            await asyncio.sleep(backoff)
                            continue
                        if status >= 400:
                            message = _extract_api_error(body) or getattr(
                                resp, "reason", status
                            )
                            raise FuelFinderApiError(
                                f"{method} {url} failed ({status}): {message}"
                            )
                        return body
            except (FuelFinderAuthError, FuelFinderApiError):
                raise
            except _CLIENT_ERRORS as err:
                raise FuelFinderApiError(f"{method} {url} failed: {err}") from err
        raise FuelFinderApiError(f"{method} {url} failed after retries")

    async def _api_get(self, path: str, params: dict[str, Any]) -> Any:
        """Authenticated GET with a single token-refresh retry on 401/403."""
        url = f"{API_BASE_URL}{path}"
        for attempt in (1, 2):
            token = await self._get_access_token(force_refresh=(attempt == 2))
            headers = {**_DEFAULT_HEADERS, "Authorization": f"Bearer {token}"}
            try:
                return await self._request("GET", url, headers=headers, params=params)
            except FuelFinderApiError as err:
                # A stale token surfaces as 401/403; refresh once and retry.
                if attempt == 1 and (" (401)" in str(err) or " (403)" in str(err)):
                    self._access_token = None
                    self._token_expiry = None
                    continue
                raise
        raise FuelFinderApiError(f"GET {path} failed after token refresh")

    # -- Fetch / pagination ------------------------------------------------- #
    async def _fetch_batched(
        self, path: str, *, effective_start: str | None = None
    ) -> list[dict[str, Any]]:
        """Page through ``batch-number`` until the last batch is detected."""
        records: list[dict[str, Any]] = []
        previous_signature: tuple[int, str, str] | None = None
        total_hint: int | None = None
        batch = 1
        while batch <= MAX_BATCHES:
            params: dict[str, Any] = {BATCH_PARAM: batch}
            if effective_start:
                params[EFFECTIVE_START_PARAM] = effective_start
            try:
                payload = await self._api_get(path, params)
            except FuelFinderApiError as err:
                # A 404 past the first batch means we ran off the end of pages.
                if batch > 1 and " (404)" in str(err):
                    break
                raise

            if total_hint is None:
                total_hint = _extract_total_batches_hint(payload)

            rows = _extract_records_from_payload(payload)
            if rows is None:
                if batch == 1:
                    raise FuelFinderApiError(
                        f"Unexpected response shape from {path}"
                    )
                break
            if not rows:  # empty batch == end of data
                break

            signature = _build_batch_signature(rows)
            if previous_signature is not None and signature == previous_signature:
                _LOGGER.warning("Repeated batch signature on %s; stopping", path)
                break

            records.extend(rows)
            if total_hint is not None and batch >= total_hint:
                break
            previous_signature = signature
            batch += 1

        if batch > MAX_BATCHES:
            _LOGGER.warning("Hit MAX_BATCHES (%s) paging %s", MAX_BATCHES, path)
        return records

    async def _ensure_data(self) -> None:
        """Refresh and merge station+price data, cached for CACHE_TTL_SECONDS."""
        loop = asyncio.get_running_loop()
        async with self._cache_lock:
            if (
                self._station_index
                and self._last_refresh is not None
                and loop.time() - self._last_refresh < CACHE_TTL_SECONDS
            ):
                return

            stations = await self._fetch_batched(PFS_PATH)
            prices = await self._fetch_batched(FUEL_PRICES_PATH)

            index: dict[str, dict[str, Any]] = {}
            self._merge_station_info(index, stations)
            self._merge_station_prices(index, prices)

            if not index:
                raise FuelFinderApiError("Fuel Finder returned no stations")

            self._station_index = index
            self._last_refresh = loop.time()
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "Fuel Finder merged %s stations; sample: %s",
                    len(index),
                    next(iter(index.values()), None),
                )

    @staticmethod
    def _merge_station_info(
        index: dict[str, dict[str, Any]], rows: list[dict[str, Any]]
    ) -> None:
        for row in rows:
            node_id = _extract_station_identifier(row)
            if not node_id:
                continue
            location = row.get("location")
            location = location if isinstance(location, dict) else {}
            existing = index.setdefault(node_id, {"site_id": node_id, "prices": {}})
            existing["site_id"] = node_id
            existing["name"] = _clean_text(row.get("trading_name")) or existing.get(
                "name"
            )
            existing["brand"] = (
                _clean_text(row.get("brand_name"))
                or _clean_text(row.get("trading_name"))
                or existing.get("brand")
            )
            existing["address"] = _format_address(location) or existing.get("address")
            existing["postcode"] = location.get("postcode") or existing.get("postcode")
            existing["latitude"] = _safe_float(location.get("latitude"))
            existing["longitude"] = _safe_float(location.get("longitude"))
            existing.setdefault("prices", {})

    @staticmethod
    def _merge_station_prices(
        index: dict[str, dict[str, Any]], rows: list[dict[str, Any]]
    ) -> None:
        for row in rows:
            node_id = _extract_station_identifier(row)
            if not node_id:
                continue
            station = index.setdefault(node_id, {"site_id": node_id, "prices": {}})
            prices = station.setdefault("prices", {})
            last_updated = station.get("last_updated")

            for entry in _extract_fuel_entries_from_row(row):
                source = _extract_source_fuel_type(entry)
                grade = normalise_fuel_type(source) if source else None
                if not grade:
                    continue
                price = _extract_fuel_entry_price(entry)
                if price is None:
                    continue
                ts = _parse_datetime(
                    _first_present(
                        entry,
                        "price_last_updated",
                        "priceLastUpdated",
                        "last_updated",
                        "lastUpdated",
                        "updated_at",
                        "updatedAt",
                    )
                )
                prices[grade] = {
                    "price": price,
                    "price_pence": round(price * 100, 1),
                    "source_fuel_type": source,
                    "last_updated": ts,
                }
                last_updated = _latest_iso(last_updated, ts)
            station["prices"] = prices
            station["last_updated"] = last_updated

    # -- Geocoding ---------------------------------------------------------- #
    async def async_geocode_postcode(self, postcode: str) -> tuple[float, float]:
        """Resolve a UK postcode to (lat, lon) via the free postcodes.io API."""
        url = POSTCODES_IO_URL.format(postcode=postcode.strip().replace(" ", ""))
        try:
            async with self._session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except _CLIENT_ERRORS as err:
            raise FuelFinderApiError(f"Postcode lookup failed: {err}") from err
        result = (data or {}).get("result") or {}
        lat, lon = result.get("latitude"), result.get("longitude")
        if lat is None or lon is None:
            raise FuelFinderApiError(f"Could not geocode postcode '{postcode}'")
        return float(lat), float(lon)

    # -- Search ------------------------------------------------------------- #
    async def async_search(
        self,
        latitude: float,
        longitude: float,
        radius_km: float,
        fuel_type: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Return up to ``limit`` nearby stations with the cheapest fuel first."""
        await self._ensure_data()
        return self._search_index(latitude, longitude, radius_km, fuel_type, limit)

    def _search_index(
        self,
        latitude: float,
        longitude: float,
        radius_km: float,
        fuel_type: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Pure search over the merged station index (no network)."""
        grade = normalise_fuel_type(fuel_type) or fuel_type
        found: list[dict[str, Any]] = []
        for station in self._station_index.values():
            lat = _safe_float(station.get("latitude"))
            lon = _safe_float(station.get("longitude"))
            if lat is None or lon is None:
                continue
            distance = haversine_km(latitude, longitude, lat, lon)
            if distance > radius_km:
                continue
            entry = (station.get("prices") or {}).get(grade)
            if not isinstance(entry, dict) or entry.get("price") is None:
                continue
            found.append(
                {
                    "site_id": station.get("site_id"),
                    "brand": station.get("brand"),
                    "name": station.get("name"),
                    "address": station.get("address"),
                    "postcode": station.get("postcode"),
                    "latitude": lat,
                    "longitude": lon,
                    "distance_km": round(distance, 2),
                    "fuel_type": grade,
                    "price": entry["price"],
                    "price_pence": entry.get(
                        "price_pence", round(entry["price"] * 100, 1)
                    ),
                    "last_updated": entry.get("last_updated")
                    or station.get("last_updated"),
                }
            )
        found.sort(key=lambda s: (s["price"], s["distance_km"]))
        return found[:limit]


# --------------------------------------------------------------------------- #
# Response helpers
# --------------------------------------------------------------------------- #
async def _parse_json_response(response: Any) -> Any:
    try:
        return await response.json(content_type=None)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - tolerate non-JSON error bodies
        return {}


def _extract_api_error(payload: Any) -> str | None:
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    if not isinstance(payload, dict):
        return None
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    data = payload.get("data")
    if isinstance(data, dict):
        nested = data.get("message")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    error = payload.get("error")
    if isinstance(error, dict):
        details = error.get("details")
        if isinstance(details, str) and details.strip():
            return details.strip()
    return None


def _retry_after_seconds(response: Any) -> float:
    headers = getattr(response, "headers", None)
    retry_after = headers.get("Retry-After") if headers else None
    if not retry_after:
        return BACKOFF_429_SECONDS
    text = str(retry_after).strip()
    try:
        return max(float(text), 0.5)
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(text)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return max((target - datetime.now(timezone.utc)).total_seconds(), 0.5)
    except (TypeError, ValueError, OverflowError):
        return BACKOFF_429_SECONDS
