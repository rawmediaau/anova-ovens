"""Config flow for Anova Precision Oven integration."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import ssl
from typing import Any
from urllib.parse import urlencode

import voluptuous as vol
import websockets

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from .app_session import AppAuthError, AppConnectionError, async_sign_in
from .const import (
    ACCESSORIES_APO,
    CONF_APP_EMAIL,
    CONF_APP_PASSWORD,
    CONF_APP_REFRESH_TOKEN,
    CONF_CAMERA_LIVE_VIEW,
    CONF_CAMERA_RELAY,
    CONF_TOKEN,
    DOMAIN,
    WS_ENDPOINT,
)
from .relay import CameraRelay, RelayError, relay_host

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TOKEN): str,
        # Optional Anova app login (lamp and camera); also settable later
        vol.Optional(CONF_APP_EMAIL, default=""): str,
        vol.Optional(CONF_APP_PASSWORD, default=""): str,
    }
)


async def validate_token(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect."""
    token = data[CONF_TOKEN].strip() if isinstance(data[CONF_TOKEN], str) else ""

    if not token:
        raise InvalidToken("Token cannot be empty")

    if not token.startswith("anova-"):
        raise InvalidToken("Token must start with 'anova-'")

    if len(token) < 20:
        raise InvalidToken("Token appears to be invalid or too short")

    params = {
        "token": token,
        "supportedAccessories": ACCESSORIES_APO,
    }
    url = f"{WS_ENDPOINT}?{urlencode(params)}"

    def _create_ssl_context():
        return ssl.create_default_context()

    ssl_context = await hass.async_add_executor_job(_create_ssl_context)

    try:
        websocket = await asyncio.wait_for(
            websockets.connect(url, ssl=ssl_context),
            timeout=10,
        )
        async with websocket:
            await asyncio.wait_for(websocket.recv(), timeout=5)
    except asyncio.TimeoutError as err:
        _LOGGER.error("Connection timeout during validation")
        raise CannotConnect("Connection timeout") from err
    except websockets.exceptions.InvalidStatusCode as err:
        _LOGGER.error(
            "Validation failed with status %s", err.status_code
        )
        if err.status_code == 401:
            raise InvalidToken("Invalid or expired token") from err
        raise CannotConnect(
            f"Connection failed with status {err.status_code}"
        ) from err
    except websockets.exceptions.WebSocketException as err:
        _LOGGER.error("WebSocket error during validation: %s", type(err).__name__)
        raise CannotConnect(f"WebSocket error: {type(err).__name__}") from err
    except Exception as err:
        _LOGGER.error("Unexpected error during validation: %s", type(err).__name__)
        raise CannotConnect(f"Unexpected error: {type(err).__name__}") from err

    return {"title": "Anova Ovens"}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Anova Precision Oven."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow."""
        return OptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input.get(CONF_APP_EMAIL, "").strip()
            password = user_input.get(CONF_APP_PASSWORD, "")
            refresh_token: str | None = None
            try:
                info = await validate_token(self.hass, user_input)
                if email:
                    if not password:
                        raise AppPasswordRequired
                    refresh_token = await async_sign_in(
                        async_get_clientsession(self.hass), email, password
                    )
            except InvalidToken:
                errors["base"] = "invalid_token"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except AppPasswordRequired:
                errors["base"] = "app_password_required"
            except AppAuthError:
                errors["base"] = "app_invalid_auth"
            except AppConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                token = user_input[CONF_TOKEN].strip()
                token_hash = hashlib.sha256(token.encode()).hexdigest()[:16]
                await self.async_set_unique_id(token_hash)
                self._abort_if_unique_id_configured()

                # The password is never stored, only the sign-in's refresh token
                data: dict[str, Any] = {CONF_TOKEN: token}
                options: dict[str, Any] = {}
                if refresh_token:
                    data[CONF_APP_REFRESH_TOKEN] = refresh_token
                    options[CONF_APP_EMAIL] = email
                return self.async_create_entry(
                    title=info["title"], data=data, options=options
                )

        # Keep what was typed (except the password) when showing errors
        schema = self.add_suggested_values_to_schema(
            STEP_USER_DATA_SCHEMA,
            {k: v for k, v in (user_input or {}).items() if k != CONF_APP_PASSWORD},
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Optional settings: the Anova app login and the oven camera relay."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Choose what to change."""
        return self.async_show_menu(step_id="init", menu_options=["app_login", "camera"])

    async def async_step_app_login(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the app login, needed for the lamp and camera on v2 ovens.

        Leave the email empty to remove the login. Only the resulting refresh
        token is stored, never the password.
        """
        errors: dict[str, str] = {}
        entry = self.config_entry

        if user_input is not None:
            email = user_input.get(CONF_APP_EMAIL, "").strip()
            password = user_input.get(CONF_APP_PASSWORD, "")
            data = {**entry.data}
            options = {**entry.options}

            if not email:
                data.pop(CONF_APP_REFRESH_TOKEN, None)
                options.pop(CONF_APP_EMAIL, None)
                self.hass.config_entries.async_update_entry(entry, data=data)
                return self.async_create_entry(data=options)

            if not password:
                errors["base"] = "app_password_required"
            else:
                try:
                    refresh_token = await async_sign_in(
                        async_get_clientsession(self.hass), email, password
                    )
                except AppAuthError:
                    errors["base"] = "app_invalid_auth"
                except AppConnectionError:
                    errors["base"] = "cannot_connect"
                else:
                    data[CONF_APP_REFRESH_TOKEN] = refresh_token
                    self.hass.config_entries.async_update_entry(entry, data=data)
                    return self.async_create_entry(
                        data={**options, CONF_APP_EMAIL: email}
                    )

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_APP_EMAIL, default=entry.options.get(CONF_APP_EMAIL, "")
                ): str,
                vol.Optional(CONF_APP_PASSWORD, default=""): str,
            }
        )
        return self.async_show_form(
            step_id="app_login", data_schema=schema, errors=errors
        )

    async def async_step_camera(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Set the camera relay (MediaMTX) and whether live view is offered.

        Leave the relay empty to turn the camera off.
        """
        errors: dict[str, str] = {}
        entry = self.config_entry

        if user_input is not None:
            host = relay_host(user_input.get(CONF_CAMERA_RELAY, ""))
            live_view = bool(user_input.get(CONF_CAMERA_LIVE_VIEW, False))
            if host:
                try:
                    await CameraRelay(
                        async_get_clientsession(self.hass), host
                    ).async_check()
                except RelayError as err:
                    _LOGGER.warning("Oven camera relay check failed: %s", err)
                    errors["base"] = "relay_cannot_connect"
            if not errors:
                return self.async_create_entry(
                    data={
                        **entry.options,
                        CONF_CAMERA_RELAY: host,
                        CONF_CAMERA_LIVE_VIEW: live_view and bool(host),
                    }
                )

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_CAMERA_RELAY,
                    default=entry.options.get(CONF_CAMERA_RELAY, ""),
                ): str,
                vol.Optional(
                    CONF_CAMERA_LIVE_VIEW,
                    default=entry.options.get(CONF_CAMERA_LIVE_VIEW, False),
                ): bool,
            }
        )
        return self.async_show_form(step_id="camera", data_schema=schema, errors=errors)


class AppPasswordRequired(HomeAssistantError):
    """Error to indicate an app email was given without a password."""


class InvalidToken(HomeAssistantError):
    """Error to indicate the token format is invalid."""


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""
