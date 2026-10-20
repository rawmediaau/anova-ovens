"""Diagnostics support for the Anova Precision Oven integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_APP_EMAIL, CONF_APP_REFRESH_TOKEN, CONF_TOKEN
from .coordinator import AnovaOvenConfigEntry

TO_REDACT = {
    CONF_TOKEN,
    CONF_APP_REFRESH_TOKEN,
    CONF_APP_EMAIL,
    # Live-stream playback credentials returned by CMD_APO_START_LIVE_STREAM
    "srtPlayback",
    "webRTCPlayback",
    "cookerId",
    "userId",
    "deviceId",
    "serialNumber",
    "macAddress",
    "ipAddress",
    "ssid",
    "bssid",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AnovaOvenConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry with sensitive fields redacted."""
    coordinator = entry.runtime_data.coordinator

    return {
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "domain": entry.domain,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
            "unique_id": entry.unique_id,
        },
        "coordinator": {
            "running": coordinator._running,
            "device_count": len(coordinator.devices),
            "devices": async_redact_data(coordinator.devices, TO_REDACT),
            "recent_responses": async_redact_data(
                list(coordinator.recent_responses), TO_REDACT
            ),
        },
    }
