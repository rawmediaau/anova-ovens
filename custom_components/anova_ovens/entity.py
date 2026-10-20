"""Base entity for the Anova Precision Oven integration."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, OVEN_TYPE_V2
from .coordinator import AnovaOvenCoordinator


class AnovaOvenEntity(CoordinatorEntity[AnovaOvenCoordinator]):
    """Base class for all Anova Oven entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AnovaOvenCoordinator,
        device_id: str,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._device_id = device_id

        device_info = coordinator.get_device_info(device_id)
        device_name = "Anova Oven"
        device_model = "Precision Oven"

        if device_info:
            device_name = device_info.get("name", device_name)
            device_model = device_info.get("type", device_model)

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=device_name,
            manufacturer="Anova",
            model=device_model,
        )

    @property
    def _is_v2(self) -> bool:
        """Return True for an oven_v2 device."""
        info = self.coordinator.get_device_info(self._device_id) or {}
        return info.get("type") == OVEN_TYPE_V2

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        if not self.coordinator.last_update_success:
            return False
        if self._device_id not in self.coordinator.devices:
            return False
        device = self.coordinator.devices.get(self._device_id, {})
        return bool(device.get("state", {}))
