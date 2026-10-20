"""Sensor platform for Anova Precision Oven."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import AnovaOvenConfigEntry, AnovaOvenCoordinator
from .entity import AnovaOvenEntity

_LOGGER = logging.getLogger(__name__)


def _get_nested(state: dict, *keys: str, default: Any = None) -> Any:
    """Safely traverse nested dicts."""
    current = state
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


@dataclass(frozen=True, kw_only=True)
class AnovaOvenSensorDescription(SensorEntityDescription):
    """Describes an Anova Oven sensor."""

    value_fn: Callable[[dict[str, Any]], Any]
    extra_attrs_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    available_fn: Callable[[dict[str, Any]], bool] | None = None


SENSOR_DESCRIPTIONS: tuple[AnovaOvenSensorDescription, ...] = (
    AnovaOvenSensorDescription(
        key="temp_dry",
        name="Dry temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=lambda s: _get_nested(
            s, "nodes", "temperatureBulbs", "dry", "current", "celsius"
        ),
        extra_attrs_fn=lambda s: {
            "setpoint_celsius": _get_nested(
                s, "nodes", "temperatureBulbs", "dry", "setpoint", "celsius"
            ),
            "setpoint_fahrenheit": _get_nested(
                s, "nodes", "temperatureBulbs", "dry", "setpoint", "fahrenheit"
            ),
            "fahrenheit": _get_nested(
                s, "nodes", "temperatureBulbs", "dry", "current", "fahrenheit"
            ),
        },
    ),
    AnovaOvenSensorDescription(
        key="temp_wet",
        name="Wet temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=lambda s: _get_nested(
            s, "nodes", "temperatureBulbs", "wet", "current", "celsius"
        ),
        extra_attrs_fn=lambda s: {
            "setpoint_celsius": _get_nested(
                s, "nodes", "temperatureBulbs", "wet", "setpoint", "celsius"
            ),
            "setpoint_fahrenheit": _get_nested(
                s, "nodes", "temperatureBulbs", "wet", "setpoint", "fahrenheit"
            ),
            "fahrenheit": _get_nested(
                s, "nodes", "temperatureBulbs", "wet", "current", "fahrenheit"
            ),
        },
    ),
    AnovaOvenSensorDescription(
        key="probe_temp",
        name="Probe temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        available_fn=lambda s: bool(
            _get_nested(s, "nodes", "temperatureProbe", "connected")
        ),
        value_fn=lambda s: _get_nested(
            s, "nodes", "temperatureProbe", "current", "celsius"
        )
        if _get_nested(s, "nodes", "temperatureProbe", "connected")
        else None,
        extra_attrs_fn=lambda s: {
            "connected": _get_nested(
                s, "nodes", "temperatureProbe", "connected", default=False
            ),
            "setpoint_celsius": _get_nested(
                s, "nodes", "temperatureProbe", "setpoint", "celsius"
            ),
            "setpoint_fahrenheit": _get_nested(
                s, "nodes", "temperatureProbe", "setpoint", "fahrenheit"
            ),
            "fahrenheit": _get_nested(
                s, "nodes", "temperatureProbe", "current", "fahrenheit"
            ),
        },
    ),
    AnovaOvenSensorDescription(
        key="humidity",
        name="Humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda s: _get_nested(
            s, "nodes", "steamGenerators", "relativeHumidity", "current"
        ),
    ),
    AnovaOvenSensorDescription(
        key="timer",
        name="Timer",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        value_fn=lambda s: _get_nested(s, "nodes", "timer", "current"),
        extra_attrs_fn=lambda s: {
            "mode": _get_nested(s, "nodes", "timer", "mode"),
            "initial": _get_nested(s, "nodes", "timer", "initial"),
        },
    ),
    AnovaOvenSensorDescription(
        key="mode",
        name="Mode",
        icon="mdi:chef-hat",
        value_fn=lambda s: _get_nested(s, "state", "mode"),
        extra_attrs_fn=lambda s: {
            "temperature_mode": _get_nested(s, "nodes", "temperatureBulbs", "mode"),
            "temperature_unit": _get_nested(s, "state", "temperatureUnit"),
            "firmware_version": _get_nested(s, "systemInfo", "firmwareVersion"),
            "hardware_version": _get_nested(s, "systemInfo", "hardwareVersion"),
        },
    ),
    AnovaOvenSensorDescription(
        key="dry_setpoint",
        name="Dry temperature setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        available_fn=lambda s: not (
            s.get("version") == 2
            and _get_nested(s, "nodes", "temperatureBulbs", "mode") == "wet"
        ),
        value_fn=lambda s: _get_nested(
            s, "nodes", "temperatureBulbs", "dry", "setpoint", "celsius"
        ),
    ),
    AnovaOvenSensorDescription(
        key="wet_setpoint",
        name="Wet temperature setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        available_fn=lambda s: _get_nested(
            s, "nodes", "temperatureBulbs", "mode"
        )
        == "wet",
        value_fn=lambda s: _get_nested(
            s, "nodes", "temperatureBulbs", "wet", "setpoint", "celsius"
        ),
    ),
    AnovaOvenSensorDescription(
        key="probe_setpoint",
        name="Probe temperature setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        available_fn=lambda s: bool(
            _get_nested(s, "nodes", "temperatureProbe", "connected")
        ),
        value_fn=lambda s: _get_nested(
            s, "nodes", "temperatureProbe", "setpoint", "celsius"
        ),
    ),
    AnovaOvenSensorDescription(
        key="humidity_setpoint",
        name="Humidity setpoint",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda s: _get_nested(
            s, "nodes", "steamGenerators", "relativeHumidity", "setpoint"
        ),
    ),
    AnovaOvenSensorDescription(
        key="fan_speed",
        name="Fan speed",
        icon="mdi:fan",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: _fan_speed_value(s),
        extra_attrs_fn=lambda s: (
            {"level": _get_nested(s, "nodes", "fan", "speed")}
            if s.get("version") == 2
            else {}
        ),
    ),
    AnovaOvenSensorDescription(
        key="rack_position",
        name="Rack position",
        icon="mdi:grid",
        value_fn=lambda s: (
            s.get("rackPosition")
            if s.get("rackPosition") is not None
            else _get_nested(s, "stages", default=[{}])[0].get("rackPosition")
            if isinstance(s.get("stages"), list) and s.get("stages")
            else None
        ),
    ),
    AnovaOvenSensorDescription(
        key="timer_start_type",
        name="Timer start type",
        icon="mdi:timer-cog",
        value_fn=lambda s: {
            "when-preheated": "When preheated",
            "immediately": "Immediately",
            "manual": "Manual",
            "on-detection": "On detection",
        }.get(
            _get_nested(s, "nodes", "timer", "startType") or "",
            _get_nested(s, "nodes", "timer", "startType"),
        ),
    ),
    AnovaOvenSensorDescription(
        key="heating_elements",
        name="Heating elements",
        icon="mdi:heating-coil",
        value_fn=lambda s: _heating_elements_value(s),
        extra_attrs_fn=lambda s: {
            "top": _get_nested(
                s, "nodes", "heatingElements", "top", "on", default=False
            ),
            "bottom": _get_nested(
                s, "nodes", "heatingElements", "bottom", "on", default=False
            ),
            "rear": _get_nested(
                s, "nodes", "heatingElements", "rear", "on", default=False
            ),
        },
    ),
    AnovaOvenSensorDescription(
        key="firmware",
        name="Firmware version",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: _get_nested(s, "systemInfo", "firmwareVersion"),
    ),
    AnovaOvenSensorDescription(
        key="hardware",
        name="Hardware version",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: _get_nested(s, "systemInfo", "hardwareVersion"),
    ),
    AnovaOvenSensorDescription(
        key="cook_session",
        name="Cook session",
        icon="mdi:chef-hat",
        value_fn=lambda s: s.get("cookId") or "idle",
        extra_attrs_fn=lambda s: {
            k: v
            for k, v in {
                "origin_source": s.get("originSource"),
                "cook_type": s.get("type"),
                "stage_count": len(s.get("stages", []))
                if s.get("stages")
                else None,
            }.items()
            if v is not None
        },
    ),
)


def _fan_speed_value(state: dict[str, Any]) -> int | None:
    """Return fan speed as a percentage.

    v1 reports a number; v2 reports a level ("off", "max", ...), so use the
    active stage's requested speed when the oven is cooking.
    """
    speed = _get_nested(state, "nodes", "fan", "speed")
    if isinstance(speed, (int, float)):
        return speed
    if speed == "off":
        return 0

    stage_id = _get_nested(state, "cook", "activeStageId")
    for stage in _get_nested(state, "cook", "stages", default=[]):
        if isinstance(stage, dict) and stage.get("id") == stage_id:
            stage_speed = _get_nested(stage, "do", "fan", "speed")
            if isinstance(stage_speed, (int, float)):
                return stage_speed
    return None


def _heating_elements_value(state: dict[str, Any]) -> str | None:
    """Compute combined heating elements display value."""
    elements = _get_nested(state, "nodes", "heatingElements")
    if not isinstance(elements, dict):
        return None

    active = []
    if _get_nested(elements, "top", "on", default=False):
        active.append("Top")
    if _get_nested(elements, "bottom", "on", default=False):
        active.append("Bottom")
    if _get_nested(elements, "rear", "on", default=False):
        active.append("Rear")

    if not active:
        return "Off"
    if len(active) == 3:
        return "All"
    return " + ".join(active)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AnovaOvenConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Anova Oven sensors based on a config entry."""
    coordinator = entry.runtime_data.coordinator

    known_devices: set[str] = set()

    def create_entities_for_device(device_id: str) -> None:
        if device_id in known_devices:
            return

        known_devices.add(device_id)

        entities: list[AnovaOvenSensor] = []
        for description in SENSOR_DESCRIPTIONS:
            try:
                entities.append(
                    AnovaOvenSensor(coordinator, device_id, description)
                )
            except Exception:
                _LOGGER.error(
                    "Failed to create %s sensor", description.key, exc_info=True
                )

        async_add_entities(entities)
        _LOGGER.debug(
            "Created %d sensor entities for device %s", len(entities), device_id
        )

    for device_id in coordinator.devices:
        create_entities_for_device(device_id)

    @callback
    def handle_coordinator_update() -> None:
        for device_id in coordinator.devices:
            create_entities_for_device(device_id)

    entry.async_on_unload(coordinator.async_add_listener(handle_coordinator_update))


class AnovaOvenSensor(AnovaOvenEntity, SensorEntity):
    """Sensor entity for Anova Precision Oven."""

    entity_description: AnovaOvenSensorDescription

    def __init__(
        self,
        coordinator: AnovaOvenCoordinator,
        device_id: str,
        description: AnovaOvenSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device_id)
        self.entity_description = description
        self._attr_unique_id = f"{device_id}_{description.key}"

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        if not super().available:
            return False
        if self.entity_description.available_fn is not None:
            state = self.coordinator.get_device_state(self._device_id)
            if not state:
                return False
            return self.entity_description.available_fn(state)
        return True

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        state = self.coordinator.get_device_state(self._device_id)
        if not state:
            return None
        return self.entity_description.value_fn(state)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        if self.entity_description.extra_attrs_fn is None:
            return {}
        state = self.coordinator.get_device_state(self._device_id)
        if not state:
            return {}
        return self.entity_description.extra_attrs_fn(state)
