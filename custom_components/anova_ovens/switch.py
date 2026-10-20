"""Switch platform for Anova Precision Oven."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CMD_APO_SET_LAMP
from .coordinator import AnovaOvenConfigEntry, AnovaOvenCoordinator
from .entity import AnovaOvenEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AnovaOvenConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Anova Oven switches based on a config entry."""
    coordinator = entry.runtime_data.coordinator

    known_devices: set[str] = set()

    def create_entities_for_device(device_id: str) -> None:
        if device_id in known_devices:
            return

        known_devices.add(device_id)
        async_add_entities([AnovaOvenLampSwitch(coordinator, device_id)])
        _LOGGER.debug("Created switch entities for device %s", device_id)

    for device_id in coordinator.devices:
        create_entities_for_device(device_id)

    @callback
    def handle_coordinator_update() -> None:
        for device_id in coordinator.devices:
            create_entities_for_device(device_id)

    entry.async_on_unload(coordinator.async_add_listener(handle_coordinator_update))


class AnovaOvenLampSwitch(AnovaOvenEntity, SwitchEntity):
    """Oven lamp switch."""

    def __init__(
        self,
        coordinator: AnovaOvenCoordinator,
        device_id: str,
    ) -> None:
        """Initialize the lamp switch."""
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_lamp"
        self._attr_name = "Lamp"
        self._attr_icon = "mdi:lightbulb"

    @property
    def is_on(self) -> bool | None:
        """Return true if the lamp is on."""
        state = self.coordinator.get_device_state(self._device_id)
        if not state:
            return None

        return state.get("nodes", {}).get("lamp", {}).get("on", False)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the lamp."""
        await self._async_set_lamp(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the lamp."""
        await self._async_set_lamp(False)

    async def _async_set_lamp(self, on: bool) -> None:
        """Send the lamp command, over the app login when configured.

        A Personal Access Token is refused ("unauthorized") for this
        command, so the app login is needed for the lamp to respond.
        """
        if self.coordinator.app_session is not None:
            try:
                response = await self.coordinator.send_app_command(
                    self._device_id, CMD_APO_SET_LAMP, {"on": on}
                )
            except Exception as err:
                raise HomeAssistantError(f"Failed to set lamp: {err}") from err
            result = response.get("payload", {})
            if result.get("status") != "ok":
                raise HomeAssistantError(
                    f"Anova refused the lamp command: {result.get('error')}"
                )
            return

        try:
            await self.coordinator.send_command(
                self._device_id, CMD_APO_SET_LAMP, {"on": on}
            )
            await self.coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.error(
                "Failed to turn %s lamp: %s", "on" if on else "off", err
            )
