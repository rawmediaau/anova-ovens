"""Number platform for Anova Precision Oven: running-cook controls (v2, app login)."""
from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CMD_APO_SET_PROBE,
    CMD_APO_SET_STEAM_GENERATORS,
    CMD_APO_SET_TIMER,
    PROBE_MAX_CELSIUS,
    PROBE_MIN_CELSIUS,
    TIMER_MAX_SECONDS,
)
from .cook_control import AnovaCookControl, cook_controls_supported
from .coordinator import AnovaOvenConfigEntry, AnovaOvenCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AnovaOvenConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up running-cook numbers for v2 ovens with the app login."""
    coordinator = entry.runtime_data.coordinator
    known_devices: set[str] = set()

    def create_entities_for_device(device_id: str) -> None:
        if device_id in known_devices or not cook_controls_supported(
            coordinator, device_id
        ):
            return
        known_devices.add(device_id)
        async_add_entities(
            [
                AnovaOvenCookTimer(coordinator, device_id),
                AnovaOvenSteam(coordinator, device_id),
                AnovaOvenProbeTarget(coordinator, device_id),
            ]
        )

    for device_id in coordinator.devices:
        create_entities_for_device(device_id)

    @callback
    def handle_coordinator_update() -> None:
        for device_id in coordinator.devices:
            create_entities_for_device(device_id)

    entry.async_on_unload(coordinator.async_add_listener(handle_coordinator_update))


class AnovaOvenCookNumber(AnovaCookControl, NumberEntity):
    """A number that changes the running cook."""

    _attr_mode = NumberMode.BOX

    def __init__(
        self, coordinator: AnovaOvenCoordinator, device_id: str, key: str, name: str
    ) -> None:
        """Initialize the control."""
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_{key}"
        self._attr_name = name


class AnovaOvenCookTimer(AnovaOvenCookNumber):
    """Cook timer, in minutes (CMD_APO_SET_TIMER; verified on an oven_v2)."""

    _attr_icon = "mdi:timer-cog-outline"
    _attr_native_min_value = 1
    _attr_native_max_value = TIMER_MAX_SECONDS // 60
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(self, coordinator: AnovaOvenCoordinator, device_id: str) -> None:
        """Initialize the cook timer."""
        super().__init__(coordinator, device_id, "cook_timer", "Cook timer")

    @property
    def native_value(self) -> float | None:
        """The running cook's timer length, in minutes."""
        initial = self._stage_do.get("timer", {}).get("initial")
        if not isinstance(initial, (int, float)):
            initial = self._nodes.get("timer", {}).get("initial")
        if not isinstance(initial, (int, float)):
            return None
        return round(initial / 60, 1)

    async def async_set_native_value(self, value: float) -> None:
        """Set the timer on the running cook."""
        seconds = int(value * 60)
        await self._async_send(
            CMD_APO_SET_TIMER, {"initial": seconds}, {"timer": {"initial": seconds}}
        )


class AnovaOvenSteam(AnovaOvenCookNumber):
    """Steam as relative humidity (CMD_APO_SET_STEAM_GENERATORS)."""

    _attr_icon = "mdi:water-percent"
    _attr_device_class = NumberDeviceClass.HUMIDITY
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator: AnovaOvenCoordinator, device_id: str) -> None:
        """Initialize the steam control."""
        super().__init__(coordinator, device_id, "steam", "Steam")

    @property
    def native_value(self) -> float | None:
        """The running stage's relative humidity target."""
        for source in (self._stage_do, self._nodes):
            value = (
                source.get("steamGenerators", {})
                .get("relativeHumidity", {})
                .get("setpoint")
            )
            if isinstance(value, (int, float)):
                return value
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Set the relative humidity target on the running cook."""
        settings = {
            "mode": "relative-humidity",
            "relativeHumidity": {"setpoint": int(value)},
        }
        await self._async_send(
            CMD_APO_SET_STEAM_GENERATORS, settings, {"steamGenerators": settings}
        )


class AnovaOvenProbeTarget(AnovaOvenCookNumber):
    """Food probe target temperature (CMD_APO_SET_PROBE)."""

    _attr_icon = "mdi:thermometer-probe"
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_min_value = PROBE_MIN_CELSIUS
    _attr_native_max_value = PROBE_MAX_CELSIUS
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: AnovaOvenCoordinator, device_id: str) -> None:
        """Initialize the probe target."""
        super().__init__(coordinator, device_id, "probe_target", "Probe target")

    @property
    def native_value(self) -> float | None:
        """The running stage's probe target."""
        for source in (self._stage_do, self._nodes):
            value = (
                source.get("temperatureProbe", {}).get("setpoint", {}).get("celsius")
            )
            if isinstance(value, (int, float)) and value > 0:
                return value
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Set the probe target on the running cook."""
        setpoint = {"setpoint": {"celsius": value}}
        await self._async_send(
            CMD_APO_SET_PROBE, setpoint, {"temperatureProbe": setpoint}
        )
