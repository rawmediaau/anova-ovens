"""The Anova Precision Oven integration."""
from __future__ import annotations

import re

import voluptuous as vol

from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .app_session import AnovaAppSession
from .const import CONF_APP_REFRESH_TOKEN, CONF_TOKEN, DOMAIN
from .coordinator import (
    AnovaOvenConfigEntry,
    AnovaOvenCoordinator,
    AnovaOvenRuntimeData,
)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.SWITCH,
    Platform.CAMERA,
    Platform.NUMBER,
]


SERVICE_SEND_COMMAND = "send_command"
ATTR_COMMAND = "command"
ATTR_PAYLOAD = "payload"
ATTR_COOKER_ID = "cooker_id"
ATTR_USE_APP_LOGIN = "use_app_login"

SEND_COMMAND_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_COMMAND): vol.All(
            cv.string, vol.Match(re.compile(r"^CMD_APO_[A-Z0-9_]+$"))
        ),
        vol.Optional(ATTR_PAYLOAD): dict,
        vol.Optional(ATTR_COOKER_ID): cv.string,
        vol.Optional(ATTR_USE_APP_LOGIN, default=False): cv.boolean,
    }
)


async def _async_send_command(call: ServiceCall) -> ServiceResponse:
    """Send a raw oven command and return Anova's reply (advanced/testing)."""
    hass = call.hass
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    coordinators = [entry.runtime_data.coordinator for entry in entries]
    cooker_id = call.data.get(ATTR_COOKER_ID)

    matches = [
        (coordinator, device_id)
        for coordinator in coordinators
        for device_id in coordinator.devices
        if cooker_id in (None, device_id)
    ]
    if len(matches) != 1:
        raise HomeAssistantError(
            "Set cooker_id to choose an oven" if matches else "No matching oven found"
        )

    coordinator, device_id = matches[0]
    command, payload = call.data[ATTR_COMMAND], call.data.get(ATTR_PAYLOAD)
    try:
        if call.data[ATTR_USE_APP_LOGIN]:
            response = await coordinator.send_app_command(device_id, command, payload)
        else:
            response = await coordinator.send_command_and_wait(
                device_id, command, payload
            )
    except TimeoutError as err:
        raise HomeAssistantError("Anova did not reply within 15 seconds") from err
    except Exception as err:
        raise HomeAssistantError(str(err)) from err
    return {"response": response}


async def async_setup_entry(hass: HomeAssistant, entry: AnovaOvenConfigEntry) -> bool:
    """Set up Anova Precision Oven from a config entry."""
    if not hass.services.has_service(DOMAIN, SERVICE_SEND_COMMAND):
        async_register_admin_service(
            hass,
            DOMAIN,
            SERVICE_SEND_COMMAND,
            _async_send_command,
            SEND_COMMAND_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )

    coordinator = AnovaOvenCoordinator(hass, entry.data[CONF_TOKEN])
    entry.runtime_data = AnovaOvenRuntimeData(
        coordinator=coordinator, options=dict(entry.options)
    )

    if refresh_token := entry.data.get(CONF_APP_REFRESH_TOKEN):
        ssl_context = await coordinator._create_ssl_context()
        coordinator.app_session = AnovaAppSession(
            async_get_clientsession(hass), ssl_context, refresh_token
        )

        def _save_refresh_token(token: str) -> None:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_APP_REFRESH_TOKEN: token}
            )

        coordinator.on_app_token_refreshed = _save_refresh_token

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if hass.is_running:
        hass.async_create_background_task(
            coordinator.start_websocket(),
            "anova_ovens_websocket",
        )
    else:
        started = False

        async def _start_websocket(_event):
            nonlocal started
            started = True
            hass.async_create_background_task(
                coordinator.start_websocket(),
                "anova_ovens_websocket",
            )

        remove_listener = hass.bus.async_listen_once(
            "homeassistant_started", _start_websocket
        )

        @callback
        def _remove_start_listener() -> None:
            # A one-time listener that already ran cannot be removed again
            if not started:
                remove_listener()

        entry.async_on_unload(_remove_start_listener)

    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: AnovaOvenConfigEntry
) -> None:
    """Reload when the app login or other options change."""
    app_session = entry.runtime_data.coordinator.app_session
    current = app_session.refresh_token if app_session else None
    # A token rotated by the session itself is already in use; no reload
    if (
        entry.data.get(CONF_APP_REFRESH_TOKEN) != current
        or dict(entry.options) != entry.runtime_data.options
    ):
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: AnovaOvenConfigEntry
) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.coordinator.stop_websocket()

    return unload_ok
