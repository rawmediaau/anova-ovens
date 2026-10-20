"""Data update coordinator for Anova Precision Oven."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import json
import logging
import ssl
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import websockets
from websockets.exceptions import WebSocketException

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .app_session import AnovaAppSession
from .const import (
    ACCESSORIES_APO,
    DOMAIN,
    EVENT_APO_STATE,
    EVENT_APO_WIFI_LIST,
    OVEN_TYPE_V2,
    WS_ENDPOINT,
    WS_MAX_MESSAGE_SIZE,
    WS_RECONNECT_DELAY,
)

_LOGGER = logging.getLogger(__name__)

_REDACTED = "[REDACTED]"

_INTEGRATION_LOGGER_NAMES = (
    "custom_components.anova_ovens",
    "custom_components.anova_ovens.coordinator",
    "custom_components.anova_ovens.config_flow",
    "custom_components.anova_ovens.sensor",
    "custom_components.anova_ovens.binary_sensor",
    "custom_components.anova_ovens.climate",
    "custom_components.anova_ovens.switch",
    "custom_components.anova_ovens.camera",
    "custom_components.anova_ovens.entity",
    "custom_components.anova_ovens.diagnostics",
)


class _TokenRedactFilter(logging.Filter):
    """Redact the Anova token from log records.

    Defense-in-depth against transport-layer exceptions whose repr may
    include the token-bearing WebSocket URL. Installed on each
    integration logger because logging filters do not propagate to
    child loggers.
    """

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        token = self._token
        if not token:
            return True
        try:
            if isinstance(record.msg, str) and token in record.msg:
                record.msg = record.msg.replace(token, _REDACTED)
            if record.args:
                record.args = tuple(
                    arg.replace(token, _REDACTED) if isinstance(arg, str) else arg
                    for arg in record.args
                )
        except Exception:
            pass
        return True


def _add_fahrenheit(node: Any) -> None:
    """Fill in fahrenheit next to every celsius value (v2 reports celsius only)."""
    if isinstance(node, dict):
        celsius = node.get("celsius")
        if isinstance(celsius, (int, float)) and "fahrenheit" not in node:
            node["fahrenheit"] = round(celsius * 9 / 5 + 32, 1)
        for value in node.values():
            _add_fahrenheit(value)
    elif isinstance(node, list):
        for value in node:
            _add_fahrenheit(value)


def _normalize_v2_state(state: dict[str, Any]) -> None:
    """Map an oven_v2 state onto the v1 field layout the entities read."""
    nodes = state.get("nodes")
    if isinstance(nodes, dict):
        _add_fahrenheit(nodes.get("temperatureBulbs"))
        _add_fahrenheit(nodes.get("temperatureProbe"))

        # v2 reports the lamp that CMD_APO_SET_LAMP and the app control as
        # doorLamp (verified on an oven_v2); cavityLamp does not change
        if "lamp" not in nodes and isinstance(nodes.get("doorLamp"), dict):
            nodes["lamp"] = nodes["doorLamp"]

        # v2 timer reports a start time instead of a running count
        timer = nodes.get("timer")
        if isinstance(timer, dict) and "current" not in timer:
            initial = timer.get("initial")
            started = dt_util.parse_datetime(timer.get("startedAtTimestamp") or "")
            if timer.get("mode") == "running" and isinstance(initial, int) and started:
                elapsed = (dt_util.utcnow() - started).total_seconds()
                timer["current"] = max(0, int(elapsed))
            elif timer.get("mode") == "completed" and isinstance(initial, int):
                timer["current"] = initial
            elif timer.get("mode") == "idle":
                timer["current"] = 0

    # v2 nests the active cook under "cook"
    cook = state.get("cook")
    if isinstance(cook, dict):
        _add_fahrenheit(cook.get("stages"))
        for key in ("cookId", "originSource", "rackPosition", "stages"):
            if key in cook and key not in state:
                state[key] = cook[key]


class AnovaOvenCoordinator(DataUpdateCoordinator):
    """Coordinator to manage Anova Oven WebSocket connection and data updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        token: str,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
        )
        self.token = token
        self.websocket = None
        self.devices: dict[str, dict[str, Any]] = {}
        self._running = False
        self._listen_task = None
        self._ssl_context: ssl.SSLContext | None = None
        # Server RESPONSE messages, keyed by the requestId we sent
        self._pending: dict[str, asyncio.Future] = {}
        self._sent_commands: dict[str, str] = {}
        self.recent_responses: deque[dict[str, Any]] = deque(maxlen=20)
        # Optional Anova app login (see app_session.py), set up in __init__.py
        self.app_session: AnovaAppSession | None = None
        self.on_app_token_refreshed: Callable[[str], None] | None = None

        self._log_filter = _TokenRedactFilter(token)
        for name in _INTEGRATION_LOGGER_NAMES:
            logging.getLogger(name).addFilter(self._log_filter)

    def _build_connection_url(self) -> str:
        """Build the WebSocket connection URL."""
        params = {
            "token": self.token,
            "supportedAccessories": ACCESSORIES_APO,
        }
        return f"{WS_ENDPOINT}?{urlencode(params)}"

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from WebSocket connection."""
        if not self._running:
            await self.start_websocket()
        return self.devices

    async def _create_ssl_context(self) -> ssl.SSLContext:
        """Create SSL context in executor to avoid blocking."""
        def _create_context():
            return ssl.create_default_context()

        return await self.hass.async_add_executor_job(_create_context)

    async def start_websocket(self) -> None:
        """Start the WebSocket connection and listener."""
        if self._running:
            return

        try:
            if self._ssl_context is None:
                self._ssl_context = await self._create_ssl_context()

            self._running = True
            self._listen_task = self.hass.async_create_task(self._listen_websocket())
        except Exception:
            _LOGGER.error("Failed to start WebSocket", exc_info=True)

    async def stop_websocket(self) -> None:
        """Stop the WebSocket connection."""
        self._running = False
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        if self.websocket:
            await self.websocket.close()
            self.websocket = None

        for name in _INTEGRATION_LOGGER_NAMES:
            logging.getLogger(name).removeFilter(self._log_filter)

    async def _listen_websocket(self) -> None:
        """Listen to WebSocket messages with automatic reconnection."""
        while self._running:
            try:
                url = self._build_connection_url()
                _LOGGER.debug("Connecting to Anova WebSocket API")

                try:
                    websocket = await asyncio.wait_for(
                        websockets.connect(
                            url,
                            ssl=self._ssl_context,
                            open_timeout=10,
                            close_timeout=5,
                            max_size=WS_MAX_MESSAGE_SIZE,
                        ),
                        timeout=15,
                    )
                except asyncio.TimeoutError:
                    _LOGGER.error("WebSocket connection timed out after 15 seconds")
                    await asyncio.sleep(WS_RECONNECT_DELAY)
                    continue
                except websockets.exceptions.InvalidStatusCode as err:
                    _LOGGER.error(
                        "WebSocket connection rejected with status %s",
                        err.status_code,
                    )
                    if err.status_code in (401, 403):
                        _LOGGER.error(
                            "Authentication failed (%s) -- check your token",
                            err.status_code,
                        )
                    await asyncio.sleep(WS_RECONNECT_DELAY)
                    continue
                except Exception as err:
                    _LOGGER.error(
                        "Failed to establish WebSocket connection: %s",
                        type(err).__name__,
                    )
                    await asyncio.sleep(WS_RECONNECT_DELAY)
                    continue

                async with websocket:
                    self.websocket = websocket
                    _LOGGER.debug("Connected to Anova WebSocket API")

                    async for message in websocket:
                        if not self._running:
                            break
                        await self._handle_message(message)

            except WebSocketException as err:
                _LOGGER.error(
                    "WebSocket connection error: %s", type(err).__name__
                )
                if self._running:
                    await asyncio.sleep(WS_RECONNECT_DELAY)
            except asyncio.CancelledError:
                break
            except Exception as err:
                _LOGGER.error(
                    "Unexpected error in WebSocket listener: %s", type(err).__name__
                )
                if self._running:
                    await asyncio.sleep(WS_RECONNECT_DELAY)

    async def _handle_message(self, message: str) -> None:
        """Handle incoming WebSocket messages."""
        try:
            if len(message) > WS_MAX_MESSAGE_SIZE:
                _LOGGER.warning(
                    "Received oversized message (%d bytes), ignoring", len(message)
                )
                return

            data = json.loads(message)

            if not isinstance(data, dict):
                _LOGGER.warning("Received invalid message format (not a dict)")
                return

            command = data.get("command")
            if not command:
                _LOGGER.debug("Received message without command field")
                return

            if command == EVENT_APO_WIFI_LIST:
                devices = data.get("payload", [])
                if not isinstance(devices, list):
                    _LOGGER.warning("Invalid payload format for WIFI_LIST event")
                    return

                for device in devices:
                    if not isinstance(device, dict):
                        continue

                    device_id = device.get("cookerId")
                    if not device_id:
                        continue

                    is_new = device_id not in self.devices
                    if is_new:
                        self.devices[device_id] = {"state": {}}
                    self.devices[device_id]["info"] = device

                    if is_new:
                        _LOGGER.info(
                            "Discovered device: %s (%s)",
                            device.get("name", "Unknown"),
                            device_id,
                        )

                if devices:
                    self.async_set_updated_data(self.devices)

            elif command == "RESPONSE":
                request_id = data.get("requestId")
                sent = self._sent_commands.pop(request_id, None)
                self.recent_responses.append(
                    {"command": sent, "requestId": request_id, "response": data}
                )
                if sent is not None:
                    _LOGGER.debug("Response to %s: %s", sent, data.get("payload"))
                future = self._pending.pop(request_id, None)
                if future is not None and not future.done():
                    future.set_result(data)

            elif command == EVENT_APO_STATE:
                payload = data.get("payload", {})
                if not isinstance(payload, dict):
                    _LOGGER.warning("Invalid payload format for STATE event")
                    return

                device_id = payload.get("cookerId")

                if not device_id:
                    _LOGGER.warning(
                        "Received state update without cookerId, cannot identify device"
                    )
                    return

                if device_id not in self.devices:
                    _LOGGER.debug(
                        "Received state for unknown device %s, initializing", device_id
                    )
                    self.devices[device_id] = {}

                actual_state = payload.get("state", {})
                if not isinstance(actual_state, dict):
                    _LOGGER.warning("Invalid state format for device %s", device_id)
                    return

                if payload.get("type") == OVEN_TYPE_V2 or actual_state.get("version") == 2:
                    _normalize_v2_state(actual_state)

                self.devices[device_id]["state"] = actual_state
                self.async_set_updated_data(self.devices)

        except json.JSONDecodeError as err:
            _LOGGER.warning("Failed to decode WebSocket message: %s", err)
        except Exception as err:
            _LOGGER.error(
                "Error handling WebSocket message: %s", type(err).__name__
            )
            _LOGGER.debug("Message handling error details", exc_info=True)

    async def send_command(
        self,
        device_id: str,
        command: str,
        payload: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> str:
        """Send a command to a device."""
        if not self.websocket:
            raise UpdateFailed("WebSocket not connected")

        if not device_id or not isinstance(device_id, str):
            raise UpdateFailed("Invalid device_id")

        if not command or not isinstance(command, str):
            raise UpdateFailed("Invalid command")

        if payload is not None and not isinstance(payload, dict):
            raise UpdateFailed("Invalid payload format")

        message = {
            "command": command,
            "requestId": request_id or str(uuid.uuid4()),
            "payload": {
                "id": device_id,
                "type": command,
            },
        }

        if payload:
            message["payload"]["payload"] = payload

        try:
            self._sent_commands[message["requestId"]] = command
            if len(self._sent_commands) > 50:
                self._sent_commands.pop(next(iter(self._sent_commands)))
            await self.websocket.send(json.dumps(message))
            _LOGGER.debug("Sent command %s to device %s", command, device_id)
            return message["requestId"]
        except WebSocketException as err:
            _LOGGER.error(
                "Failed to send command %s: %s", command, type(err).__name__
            )
            raise UpdateFailed(
                f"Failed to send command: {type(err).__name__}"
            ) from err
        except Exception as err:
            _LOGGER.error(
                "Unexpected error sending command %s: %s",
                command,
                type(err).__name__,
            )
            raise UpdateFailed(
                f"Unexpected error: {type(err).__name__}"
            ) from err

    async def send_command_and_wait(
        self,
        device_id: str,
        command: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 15,
    ) -> dict[str, Any]:
        """Send a command and return the server's RESPONSE message."""
        request_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self.send_command(device_id, command, payload, request_id=request_id)
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)

    async def send_app_command(
        self, device_id: str, command: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send a command over the Anova app login and return the reply."""
        if self.app_session is None:
            raise UpdateFailed("Anova app login is not configured")

        old_token = self.app_session.refresh_token
        response = await self.app_session.async_send_command(device_id, command, payload)
        if self.app_session.refresh_token != old_token and self.on_app_token_refreshed:
            self.on_app_token_refreshed(self.app_session.refresh_token)

        self.recent_responses.append(
            {"command": command, "via": "app", "response": response}
        )
        # Only the status: live-stream replies carry playback credentials
        _LOGGER.debug(
            "App response to %s: %s",
            command,
            {k: v for k, v in (response.get("payload") or {}).items() if k != "data"},
        )
        return response

    def get_device_state(self, device_id: str) -> dict[str, Any] | None:
        """Get device state by ID."""
        device = self.devices.get(device_id)
        if device:
            return device.get("state")
        return None

    def get_device_info(self, device_id: str) -> dict[str, Any] | None:
        """Get device info by ID."""
        device = self.devices.get(device_id)
        if device:
            return device.get("info")
        return None


@dataclass
class AnovaOvenRuntimeData:
    """Runtime data stored on the config entry."""

    coordinator: AnovaOvenCoordinator
    # Options the entry was set up with; a change to them needs a reload
    options: dict[str, Any] = field(default_factory=dict)


AnovaOvenConfigEntry = ConfigEntry[AnovaOvenRuntimeData]
