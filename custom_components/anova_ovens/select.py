"""Select platform for Anova Precision Oven: fan speed of the running cook."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CMD_APO_SET_FAN, FAN_SPEEDS
from .cook_control import AnovaCookControl, cook_controls_supported
from .coordinator import AnovaOvenConfigEntry, AnovaOvenCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AnovaOvenConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the fan speed select for v2 ovens with the app login."""
    coordinator = entry.runtime_data.coordinator
    known_devices: set[str] = set()

    def create_entities_for_device(device_id: str) -> None:
        if device_id in known_devices or not cook_controls_supported(
            coordinator, device_id
        ):
            return
        known_devices.add(device_id)
        async_add_entities([AnovaOvenFanSpeed(coordinator, device_id)])

    for device_id in coordinator.devices:
        create_entities_for_device(device_id)

    @callback
    def handle_coordinator_update() -> None:
        for device_id in coordinator.devices:
            create_entities_for_device(device_id)

    entry.async_on_unload(coordinator.async_add_listener(handle_coordinator_update))


class AnovaOvenFanSpeed(AnovaCookControl, SelectEntity):
    """Fan speed of the running cook (CMD_APO_SET_FAN).

    The app offers four speeds, sent as 0 / 33 / 67 / 100.
    """

    _attr_icon = "mdi:fan"
    _attr_options = list(FAN_SPEEDS)

    def __init__(self, coordinator: AnovaOvenCoordinator, device_id: str) -> None:
        """Initialize the fan speed select."""
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_fan_speed_control"
        self._attr_name = "Fan"

    @property
    def current_option(self) -> str | None:
        """The running stage's fan speed, as the nearest app option."""
        speed = self._stage_do.get("fan", {}).get("speed")
        if not isinstance(speed, (int, float)):
            return "off" if self._nodes.get("fan", {}).get("speed") == "off" else None
        return min(FAN_SPEEDS, key=lambda option: abs(FAN_SPEEDS[option] - speed))

    async def async_select_option(self, option: str) -> None:
        """Set the fan speed on the running cook."""
        speed = FAN_SPEEDS[option]
        await self._async_send(CMD_APO_SET_FAN, {"speed": speed}, {"fan": {"speed": speed}})
