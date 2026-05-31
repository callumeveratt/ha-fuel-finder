"""The Fuel Finder Search integration.

Provides an on-demand service, ``fuel_finder.search_price``, that returns the
cheapest nearby fuel stations for a given location (lat/long or postcode) and
fuel grade. Designed to be called from automations with a vehicle's live GPS.
"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import FuelFinderApiError, FuelFinderClient
from .const import (
    ATTR_FUEL_TYPE,
    ATTR_LATITUDE,
    ATTR_LIMIT,
    ATTR_LONGITUDE,
    ATTR_POSTCODE,
    ATTR_RADIUS,
    ATTR_RADIUS_ALIAS,
    ATTR_UNIT,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    DEFAULT_FUEL_TYPE,
    DEFAULT_LIMIT,
    DEFAULT_RADIUS_KM,
    DEFAULT_UNIT,
    DOMAIN,
    FUEL_TYPES,
    KM_TO_MILES,
    MILES_TO_KM,
    SERVICE_SEARCH_PRICE,
    UNIT_KM,
    UNIT_MILES,
)

_LOGGER = logging.getLogger(__name__)

SEARCH_PRICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_LATITUDE): vol.Coerce(float),
        vol.Optional(ATTR_LONGITUDE): vol.Coerce(float),
        vol.Optional(ATTR_POSTCODE): cv.string,
        vol.Optional(ATTR_RADIUS): vol.All(
            vol.Coerce(float), vol.Range(min=0.1, max=100)
        ),
        vol.Optional(ATTR_RADIUS_ALIAS): vol.All(
            vol.Coerce(float), vol.Range(min=0.1, max=100)
        ),
        vol.Optional(ATTR_FUEL_TYPE, default=DEFAULT_FUEL_TYPE): vol.In(FUEL_TYPES),
        vol.Optional(ATTR_LIMIT, default=DEFAULT_LIMIT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=25)
        ),
        vol.Optional(ATTR_UNIT, default=DEFAULT_UNIT): vol.In([UNIT_KM, UNIT_MILES]),
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Fuel Finder from a config entry."""
    session = async_get_clientsession(hass)
    client = FuelFinderClient(
        session, entry.data[CONF_CLIENT_ID], entry.data[CONF_CLIENT_SECRET]
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = client

    if not hass.services.has_service(DOMAIN, SERVICE_SEARCH_PRICE):
        _register_search_service(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if not hass.data.get(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_SEARCH_PRICE)
    return True


def _register_search_service(hass: HomeAssistant) -> None:
    """Register the global search_price service (returns response data)."""

    async def _handle_search(call: ServiceCall) -> ServiceResponse:
        clients: dict[str, FuelFinderClient] = hass.data.get(DOMAIN, {})
        if not clients:
            raise HomeAssistantError("Fuel Finder is not configured")
        client = next(iter(clients.values()))

        lat = call.data.get(ATTR_LATITUDE)
        lon = call.data.get(ATTR_LONGITUDE)
        postcode = call.data.get(ATTR_POSTCODE)

        try:
            if lat is None or lon is None:
                if not postcode:
                    raise HomeAssistantError(
                        "Provide either latitude+longitude or a postcode"
                    )
                lat, lon = await client.async_geocode_postcode(postcode)

            unit = call.data[ATTR_UNIT]
            radius_input = (
                call.data.get(ATTR_RADIUS)
                or call.data.get(ATTR_RADIUS_ALIAS)
                or (DEFAULT_RADIUS_KM if unit == UNIT_KM else round(DEFAULT_RADIUS_KM * KM_TO_MILES, 1))
            )
            radius_km = radius_input * MILES_TO_KM if unit == UNIT_MILES else radius_input

            stations = await client.async_search(
                latitude=lat,
                longitude=lon,
                radius_km=radius_km,
                fuel_type=call.data[ATTR_FUEL_TYPE],
                limit=call.data[ATTR_LIMIT],
            )
        except FuelFinderApiError as err:
            raise HomeAssistantError(str(err)) from err

        # Always expose both distance_km and distance_miles for convenience.
        for station in stations:
            station["distance_miles"] = round(station["distance_km"] * KM_TO_MILES, 2)

        return {
            "search": {
                "latitude": lat,
                "longitude": lon,
                "radius_km": radius_km,
                "radius": radius_input,
                "unit": unit,
                "fuel_type": call.data[ATTR_FUEL_TYPE],
                "count": len(stations),
            },
            "stations": stations,
            "cheapest": stations[0] if stations else None,
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEARCH_PRICE,
        _handle_search,
        schema=SEARCH_PRICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
