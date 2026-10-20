"""Climate platform for Anova Precision Oven."""
from __future__ import annotations

import copy
import logging
from typing import Any
import uuid

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    CMD_APO_START,
    CMD_APO_STOP,
    DEFAULT_FAN_SPEED,
    DEFAULT_HUMIDITY,
    DEFAULT_TEMPERATURE_CELSIUS,
    OVEN_TYPE_V1,
    OVEN_TYPE_V2,
    TEMP_MAX_CELSIUS,
    TEMP_MAX_WET_CELSIUS,
    TEMP_MIN_CELSIUS,
)
from .coordinator import AnovaOvenConfigEntry, AnovaOvenCoordinator
from .entity import AnovaOvenEntity

_LOGGER = logging.getLogger(__name__)

PRESET_DRY = "dry"
PRESET_WET = "wet"

HVAC_MODE_MAPPING = {
    "idle": HVACMode.OFF,
    "cook": HVACMode.HEAT,
    "preheat": HVACMode.HEAT,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AnovaOvenConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Anova Oven climate based on a config entry."""
    coordinator = entry.runtime_data.coordinator

    known_devices: set[str] = set()

    def create_entities_for_device(device_id: str) -> None:
        if device_id in known_devices:
            return

        known_devices.add(device_id)
        async_add_entities([AnovaOvenClimate(coordinator, device_id)])
        _LOGGER.debug("Created climate entity for device %s", device_id)

    for device_id in coordinator.devices:
        create_entities_for_device(device_id)

    @callback
    def handle_coordinator_update() -> None:
        for device_id in coordinator.devices:
            create_entities_for_device(device_id)

    entry.async_on_unload(coordinator.async_add_listener(handle_coordinator_update))


class AnovaOvenClimate(AnovaOvenEntity, ClimateEntity):
    """Anova Oven climate control."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_min_temp = TEMP_MIN_CELSIUS
    _attr_target_temperature_step = 1

    def __init__(
        self,
        coordinator: AnovaOvenCoordinator,
        device_id: str,
    ) -> None:
        """Initialize the climate entity."""
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_climate"
        self._attr_name = None
        # Bulb mode for the next cook started from HA while the oven is idle
        self._idle_preset = PRESET_DRY

    @property
    def hvac_mode(self) -> HVACMode:
        """Return current HVAC mode."""
        state = self.coordinator.get_device_state(self._device_id)
        if not state:
            return HVACMode.OFF

        mode = state.get("state", {}).get("mode", "idle")
        return HVAC_MODE_MAPPING.get(mode, HVACMode.OFF)

    @property
    def hvac_action(self) -> HVACAction:
        """Return current HVAC action."""
        state = self.coordinator.get_device_state(self._device_id)
        if not state:
            return HVACAction.OFF

        mode = state.get("state", {}).get("mode", "idle")
        if mode == "preheat":
            return HVACAction.PREHEATING
        if mode == "cook":
            return HVACAction.HEATING
        return HVACAction.OFF

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Return supported features; dry/wet presets are v2 only."""
        features = self._attr_supported_features
        if self._is_v2:
            features |= ClimateEntityFeature.PRESET_MODE
        return features

    @property
    def preset_modes(self) -> list[str] | None:
        """Return available presets (v2 only)."""
        return [PRESET_DRY, PRESET_WET] if self._is_v2 else None

    @property
    def preset_mode(self) -> str | None:
        """Return dry (oven) or wet (sous vide) bulb mode."""
        if not self._is_v2:
            return None
        if self.hvac_mode == HVACMode.OFF:
            return self._idle_preset
        state = self.coordinator.get_device_state(self._device_id) or {}
        mode = state.get("nodes", {}).get("temperatureBulbs", {}).get("mode")
        return PRESET_WET if mode == PRESET_WET else PRESET_DRY

    @property
    def max_temp(self) -> float:
        """Return the maximum target for the current bulb mode."""
        if self.preset_mode == PRESET_WET:
            return TEMP_MAX_WET_CELSIUS
        return TEMP_MAX_CELSIUS

    def _active_bulb(self) -> dict[str, Any]:
        """Return the temperature bulb (dry or wet) the oven is controlling."""
        state = self.coordinator.get_device_state(self._device_id)
        if not state:
            return {}

        bulbs = state.get("nodes", {}).get("temperatureBulbs", {})
        # v1 behaviour is unchanged: always report the dry bulb
        mode = "wet" if self._is_v2 and bulbs.get("mode") == "wet" else "dry"
        return bulbs.get(mode, {})

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        return self._active_bulb().get("current", {}).get("celsius")

    @property
    def target_temperature(self) -> float | None:
        """Return the target temperature."""
        return self._active_bulb().get("setpoint", {}).get("celsius")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        state = self.coordinator.get_device_state(self._device_id)
        if not state:
            return {}

        nodes = state.get("nodes", {})
        temp_bulbs = nodes.get("temperatureBulbs", {})
        timer = nodes.get("timer", {})
        steam = nodes.get("steamGenerators", {})

        attrs = {
            "temperature_mode": temp_bulbs.get("mode"),
            "timer_mode": timer.get("mode"),
            "timer_current": timer.get("current"),
            "timer_initial": timer.get("initial"),
            "humidity": steam.get("relativeHumidity", {}).get("current"),
        }

        if temp_bulbs.get("mode") == "wet":
            wet = temp_bulbs.get("wet", {})
            attrs["wet_temperature_celsius"] = wet.get("current", {}).get("celsius")

        return attrs

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target HVAC mode."""
        if hvac_mode == HVACMode.OFF:
            await self._async_turn_off()
        elif hvac_mode == HVACMode.HEAT:
            await self._async_turn_on()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return

        if not isinstance(temperature, (int, float)):
            _LOGGER.error("Invalid temperature type: %s", type(temperature).__name__)
            return

        if temperature < TEMP_MIN_CELSIUS or temperature > self.max_temp:
            _LOGGER.error(
                "Temperature %s°C out of range (%s-%s°C)",
                temperature,
                TEMP_MIN_CELSIUS,
                self.max_temp,
            )
            return

        if self.hvac_mode == HVACMode.HEAT:
            await self._async_start_cook(temperature, self.preset_mode or PRESET_DRY)
        else:
            _LOGGER.warning("Cannot set temperature when oven is off")

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Switch between dry (oven) and wet (sous vide) bulb mode."""
        if preset_mode not in (self.preset_modes or []):
            _LOGGER.error("Unknown preset mode: %s", preset_mode)
            return

        if self.hvac_mode == HVACMode.OFF:
            self._idle_preset = preset_mode
            self.async_write_ha_state()
            return

        temperature = self.target_temperature or DEFAULT_TEMPERATURE_CELSIUS
        if preset_mode == PRESET_WET:
            temperature = min(temperature, TEMP_MAX_WET_CELSIUS)
        await self._async_start_cook(temperature, preset_mode)

    async def async_turn_on(self) -> None:
        """Turn on the oven."""
        await self._async_turn_on()

    async def async_turn_off(self) -> None:
        """Turn off the oven."""
        await self._async_turn_off()

    async def _async_turn_on(self) -> None:
        """Start cooking with current or default temperature."""
        target_temp = self.target_temperature
        if target_temp is None:
            target_temp = DEFAULT_TEMPERATURE_CELSIUS
        if self._idle_preset == PRESET_WET:
            target_temp = min(target_temp, TEMP_MAX_WET_CELSIUS)

        await self._async_start_cook(target_temp, self._idle_preset)

    async def _async_turn_off(self) -> None:
        """Stop cooking."""
        try:
            await self.coordinator.send_command(self._device_id, CMD_APO_STOP)
            await self.coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Failed to stop oven: %s", err)

    async def _async_start_cook(self, temperature: float, mode: str) -> None:
        """Start cooking at the given temperature and bulb mode.

        On a v2 oven that is already cooking, the running cook is resent
        from its active stage with only the setpoint and bulb mode changed,
        so steam, fan, elements and later stages are kept. Otherwise a
        fresh single-stage cook is started.
        """
        device_info = self.coordinator.get_device_info(self._device_id)
        device_type = (
            device_info.get("type", OVEN_TYPE_V1) if device_info else OVEN_TYPE_V1
        )

        payload = None
        if device_type == OVEN_TYPE_V2 and self.hvac_mode == HVACMode.HEAT:
            payload = self._modify_running_cook_payload(temperature, mode)
        if payload is None:
            payload = self._create_cook_payload(temperature, device_type, mode)

        try:
            await self.coordinator.send_command(
                self._device_id, CMD_APO_START, payload
            )
            await self.coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Failed to start oven: %s", err)

    def _modify_running_cook_payload(
        self, temperature: float, mode: str
    ) -> dict[str, Any] | None:
        """Rebuild the running v2 cook with a new setpoint, or None if none."""
        state = self.coordinator.get_device_state(self._device_id) or {}
        cook = state.get("cook")
        if not isinstance(cook, dict) or not isinstance(cook.get("stages"), list):
            return None

        index = cook.get("activeStageIndex") or 0
        stages = copy.deepcopy(cook["stages"][index:])
        if not stages or not isinstance(stages[0].get("do"), dict):
            return None

        for stage in stages:
            stage["id"] = str(uuid.uuid4())

        action = stages[0]["do"]
        action["temperatureBulbs"] = {
            "mode": mode,
            mode: {"setpoint": {"celsius": temperature}},
        }
        if mode == PRESET_WET and "steamGenerators" not in action:
            action["steamGenerators"] = {
                "mode": "relative-humidity",
                "relativeHumidity": {"setpoint": 100},
            }

        # Resending restarts the stage timer, so carry over only what is left
        timer = state.get("nodes", {}).get("timer", {})
        started = dt_util.parse_datetime(timer.get("startedAtTimestamp") or "")
        initial = action.get("timer", {}).get("initial")
        if timer.get("mode") == "running" and started and isinstance(initial, int):
            elapsed = int((dt_util.utcnow() - started).total_seconds())
            action["timer"]["initial"] = max(1, initial - elapsed)

        return {
            "stages": stages,
            "cookId": str(uuid.uuid4()),
            "cookerId": self._device_id,
            "cookableId": cook.get("cookableId", ""),
            "title": cook.get("cookTitle", ""),
            "type": OVEN_TYPE_V2,
            "originSource": "api",
            "cookableType": cook.get("cookableType", "manual"),
        }

    def _create_cook_payload(
        self, temperature: float, device_type: str, mode: str = PRESET_DRY
    ) -> dict[str, Any]:
        """Create a cook command payload."""
        cook_id = str(uuid.uuid4())
        stage_id = str(uuid.uuid4())

        max_temp = TEMP_MAX_WET_CELSIUS if mode == PRESET_WET else TEMP_MAX_CELSIUS
        temperature = max(TEMP_MIN_CELSIUS, min(max_temp, temperature))
        fahrenheit = round(temperature * 9 / 5 + 32)
        # Wet-bulb (sous vide) cooking needs full steam
        humidity = 100 if mode == PRESET_WET else DEFAULT_HUMIDITY

        if device_type == OVEN_TYPE_V2:
            # v2 stages wrap the node settings in "do"; no "exit" means the
            # stage runs until stopped.
            return {
                "stages": [
                    {
                        "id": stage_id,
                        "do": {
                            "type": "cook",
                            "fan": {"speed": DEFAULT_FAN_SPEED},
                            "heatingElements": {
                                "top": {"on": True},
                                "bottom": {"on": False},
                                "rear": {"on": True},
                            },
                            "exhaustVent": {"state": "closed"},
                            "temperatureBulbs": {
                                "mode": mode,
                                mode: {"setpoint": {"celsius": temperature}},
                            },
                            "steamGenerators": {
                                "mode": "relative-humidity",
                                "relativeHumidity": {"setpoint": humidity},
                            },
                        },
                        "title": "Cook",
                        "description": "Home Assistant Cook",
                        "rackPosition": 3,
                    }
                ],
                "cookId": cook_id,
                "cookerId": self._device_id,
                "cookableId": "",
                "title": "",
                "type": device_type,
                "originSource": "api",
                "cookableType": "manual",
            }

        payload: dict[str, Any] = {
            "stages": [
                {
                    "id": stage_id,
                    "type": "cook",
                    "title": "Cook",
                    "description": "Home Assistant Cook",
                    "userActionRequired": False,
                    "temperatureBulbs": {
                        "mode": mode,
                        mode: {
                            "setpoint": {
                                "celsius": temperature,
                                "fahrenheit": fahrenheit,
                            }
                        },
                    },
                    "heatingElements": {
                        "top": {"on": True},
                        "bottom": {"on": False},
                        "rear": {"on": True},
                    },
                    "fan": {"speed": DEFAULT_FAN_SPEED},
                    "vent": {"open": False},
                    "steamGenerators": {
                        "mode": "relative-humidity",
                        "relativeHumidity": {"setpoint": humidity},
                    },
                }
            ],
            "cookId": cook_id,
            "originSource": "api",
        }

        return payload
