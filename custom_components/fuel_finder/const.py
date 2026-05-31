"""Constants for the Fuel Finder Search integration."""

from __future__ import annotations

DOMAIN = "fuel_finder"

# --- UK Fuel Finder API (official gov scheme) -------------------------------
# Reference: https://www.developer.fuel-finder.service.gov.uk/
API_BASE_URL = "https://www.fuel-finder.service.gov.uk"
OAUTH_TOKEN_PATH = "/api/v1/oauth/generate_access_token"
PFS_PATH = "/api/v1/pfs"  # station info, paged via ?batch-number=N
FUEL_PRICES_PATH = "/api/v1/pfs/fuel-prices"  # prices, paged via ?batch-number=N
BATCH_PARAM = "batch-number"  # 1-based batch (page) index
EFFECTIVE_START_PARAM = "effective-start-timestamp"  # optional incremental cutoff
TOKEN_REFRESH_MARGIN = 60  # seconds before expiry to refresh
MAX_BATCHES = 80  # safety cap when paging batches
DEFAULT_TIMEOUT_SECONDS = 20  # per-request HTTP timeout

# A descriptive User-Agent is requested by the scheme operators.
USER_AGENT = "ha-fuel-finder/1.0 (+https://github.com/home-assistant)"

# Politeness / rate limiting (API limit is ~100 requests/minute).
# 0.75s between requests keeps us comfortably under 100/min even with the
# two endpoints fetched back to back.
MIN_REQUEST_INTERVAL = 0.75  # seconds between requests
MAX_429_RETRIES = 3
BACKOFF_429_SECONDS = 5.0  # base wait when no Retry-After header is supplied
RATE_LIMIT_BACKOFF_MULTIPLIER = 2.0  # exponential factor per 429 retry
RATE_LIMIT_MAX_BACKOFF_SECONDS = 60.0  # cap on any single backoff wait

# Timestamp formats seen across the price feed (parsed best-effort).
DATE_FORMATS = (
    "%d/%m/%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%fZ",
)

# Free, key-less postcode -> lat/long lookup
POSTCODES_IO_URL = "https://api.postcodes.io/postcodes/{postcode}"

# Fuel grade keys, and normalisation of the API's source grade codes
FUEL_TYPES = ["E5", "E10", "B7", "SDV"]
DEFAULT_FUEL_TYPE = "E5"
FUEL_TYPE_MAP = {
    "E10": "E10",
    "E5": "E5",
    "B7": "B7",
    "B7_STANDARD": "B7",
    "DIESEL": "B7",
    "SDV": "SDV",
    "B7_PREMIUM": "SDV",
}

# Defaults
DEFAULT_RADIUS_KM = 8.0
DEFAULT_LIMIT = 5
CACHE_TTL_SECONDS = 3600  # cache merged dataset for 1 hour
REFRESH_INTERVAL_SECONDS = 3600  # background pre-warm interval

# Config keys
CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"

# Service
SERVICE_SEARCH_PRICE = "search_price"

# Service field names
ATTR_LATITUDE = "latitude"
ATTR_LONGITUDE = "longitude"
ATTR_POSTCODE = "postcode"
ATTR_RADIUS = "radius"
ATTR_FUEL_TYPE = "fuel_type"
ATTR_LIMIT = "limit"
