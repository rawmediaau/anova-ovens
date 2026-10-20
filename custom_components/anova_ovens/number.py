"""Number platform for Anova Precision Oven: cook timer (v2, app login)."""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CMD_APO_SET_TIMER, OVEN_TYPE_V2
from .coordinator import AnovaOvenConfigEntry, AnovaOvenCoordinator
from .entity import AnovaOvenEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AnovaOvenConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the cook timer for v2 ovens when the app login is configured."""
    coordinator = entry.runtime_data.coordinator
    if coordinator.app_session is None:
        return

    known_devices: set[str] = set()

    def create_entities_for_device(device_id: str) -> None:
        info = coordinator.get_device_info(device_id) or {}
        if device_id in known_devices or info.get("type") != OVEN_TYPE_V2:
            return

        known_devices.add(device_id)
        async_add_entities([AnovaOvenCookTimer(coordinator, device_id)])

    for device_id in coordinator.devices:
        create_entities_for_device(device_id)

    @callback
    def handle_coordinator_update() -> None:
        for device_id in coordinator.devices:
            create_entities_for_device(device_id)

    entry.async_on_unload(coordinator.async_add_listener(handle_coordinator_update))


class AnovaOvenCookTimer(AnovaOvenEntity, NumberEntity):
    """Set the running cook's timer without restarting the cook.

    Sends CMD_APO_SET_TIMER over the app login (verified on an oven_v2:
    the running cook kept going with the new timer). Only available while
    cooking.
    """

    _attr_icon = "mdi:timer-cog-outline"
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 1
    _attr_native_max_value = 24 * 60
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(self, coordinator: AnovaOvenCoordinator, device_id: str) -> None:
        """Initialize the cook timer."""
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_cook_timer"
        self._attr_name = "Cook timer"

    @property
    def _state(self) -> dict:
        return self.coordinator.get_device_state(self._device_id) or {}

    @property
    def available(self) -> bool:
        """Only while a cook is running."""
        return super().available and self._state.get("state", {}).get("mode") == "cook"

    @property
    def native_value(self) -> float | None:
        """The running cook's timer length, in minutes."""
        initial = self._state.get("nodes", {}).get("timer", {}).get("initial")
        if not isinstance(initial, (int, float)):
            return None
        return round(initial / 60, 1)

    async def async_set_native_value(self, value: float) -> None:
        """Set the timer on the running cook."""
        try:
            response = await self.coordinator.send_app_command(
                self._device_id, CMD_APO_SET_TIMER, {"initial": int(value * 60)}
            )
        except Exception as err:
            raise HomeAssistantError(f"Could not set the cook timer: {err}") from err
        result = response.get("payload") or {}
        if result.get("status") != "ok":
            raise HomeAssistantError(
                f"Anova refused the cook timer: {result.get('error', 'unknown error')}"
            )
        await self.coordinator.async_request_refresh()
