"""Switch platform for Anova Precision Oven."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CMD_APO_SET_HEATING_ELEMENTS, CMD_APO_SET_LAMP, CMD_APO_SET_VENT
from .cook_control import AnovaCookControl, cook_controls_supported
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
        entities: list[SwitchEntity] = [AnovaOvenLampSwitch(coordinator, device_id)]
        if cook_controls_supported(coordinator, device_id):
            entities.append(AnovaOvenVentSwitch(coordinator, device_id))
            entities.extend(
                AnovaOvenElementSwitch(coordinator, device_id, element)
                for element in HEATING_ELEMENTS
            )
        async_add_entities(entities)
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


HEATING_ELEMENTS = ("top", "bottom", "rear")


class AnovaOvenVentSwitch(AnovaCookControl, SwitchEntity):
    """Exhaust vent of the running cook, on = open (CMD_APO_SET_VENT)."""

    _attr_icon = "mdi:hvac"

    def __init__(self, coordinator: AnovaOvenCoordinator, device_id: str) -> None:
        """Initialize the vent switch."""
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_vent_control"
        self._attr_name = "Vent open"

    @property
    def is_on(self) -> bool | None:
        """Return true if the running stage has the vent open."""
        for source in (self._stage_do, self._nodes):
            state = source.get("exhaustVent", {}).get("state")
            if state in ("open", "closed"):
                return state == "open"
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Open the vent."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Close the vent."""
        await self._async_set(False)

    async def _async_set(self, open_: bool) -> None:
        await self._async_send(
            CMD_APO_SET_VENT,
            {"open": open_},
            {"exhaustVent": {"state": "open" if open_ else "closed"}},
        )


class AnovaOvenElementSwitch(AnovaCookControl, SwitchEntity):
    """One heating element of the running cook (CMD_APO_SET_HEATING_ELEMENTS).

    The command sets all three elements, so the others are sent unchanged.
    The last element that is on cannot be turned off.
    """

    _attr_icon = "mdi:heating-coil"

    def __init__(
        self, coordinator: AnovaOvenCoordinator, device_id: str, element: str
    ) -> None:
        """Initialize the element switch."""
        super().__init__(coordinator, device_id)
        self._element = element
        self._attr_unique_id = f"{device_id}_{element}_element"
        self._attr_name = f"{element.capitalize()} element"

    def _elements(self) -> dict[str, bool] | None:
        """On/off of each element in the running stage (or the live node)."""
        for source in (self._stage_do, self._nodes):
            elements = source.get("heatingElements")
            if isinstance(elements, dict) and all(
                isinstance(elements.get(name), dict) for name in HEATING_ELEMENTS
            ):
                return {
                    name: bool(elements[name].get("on")) for name in HEATING_ELEMENTS
                }
        return None

    @property
    def is_on(self) -> bool | None:
        """Return true if this element is on in the running stage."""
        elements = self._elements()
        return None if elements is None else elements[self._element]

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the element on."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the element off."""
        await self._async_set(False)

    async def _async_set(self, on: bool) -> None:
        elements = self._elements()
        if elements is None:
            raise HomeAssistantError("The running cook's heating elements are unknown")
        elements[self._element] = on
        if not any(elements.values()):
            raise HomeAssistantError("At least one heating element must stay on")
        settings = {name: {"on": value} for name, value in elements.items()}
        await self._async_send(
            CMD_APO_SET_HEATING_ELEMENTS, settings, {"heatingElements": settings}
        )
