"""Shared base for controls that change the running cook (v2, app login).

The Anova app changes the active stage of a running cook with one command
per control (CMD_APO_SET_TIMER, CMD_APO_SET_FAN, ...), sent over its own
login; the Personal Access Token cannot send them. Payloads and limits
follow the app (see PROTOCOL.md section 8 of the decompiled app notes).

A control reads its value from the running stage's settings, falling back
to the oven's live node. After Anova accepts a change, the stage held in
the coordinator is updated straight away, because the oven may not report
its state while cooking.
"""
from __future__ import annotations

from typing import Any

from homeassistant.exceptions import HomeAssistantError

from .const import OVEN_TYPE_V2
from .coordinator import AnovaOvenCoordinator
from .entity import AnovaOvenEntity


def cook_controls_supported(coordinator: AnovaOvenCoordinator, device_id: str) -> bool:
    """Cook controls need a v2 oven and the app login."""
    info = coordinator.get_device_info(device_id) or {}
    return coordinator.app_session is not None and info.get("type") == OVEN_TYPE_V2


class AnovaCookControl(AnovaOvenEntity):
    """Base for an entity that changes the running cook in place."""

    @property
    def _state(self) -> dict[str, Any]:
        return self.coordinator.get_device_state(self._device_id) or {}

    @property
    def _nodes(self) -> dict[str, Any]:
        nodes = self._state.get("nodes")
        return nodes if isinstance(nodes, dict) else {}

    @property
    def _stage_do(self) -> dict[str, Any]:
        """Settings of the running stage ({} if unknown)."""
        cook = self._state.get("cook")
        if not isinstance(cook, dict):
            return {}
        stages = cook.get("stages")
        if not isinstance(stages, list):
            return {}
        stage_id = cook.get("activeStageId")
        for stage in stages:
            if isinstance(stage, dict) and stage.get("id") == stage_id:
                do = stage.get("do")
                return do if isinstance(do, dict) else {}
        index = cook.get("activeStageIndex")
        if isinstance(index, int) and 0 <= index < len(stages):
            do = stages[index].get("do") if isinstance(stages[index], dict) else None
            return do if isinstance(do, dict) else {}
        return {}

    @property
    def available(self) -> bool:
        """Only while a cook is running."""
        return super().available and self._state.get("state", {}).get("mode") == "cook"

    async def _async_send(
        self, command: str, payload: dict[str, Any], stage_settings: dict[str, Any]
    ) -> None:
        """Send a change; on success record it in the running stage."""
        try:
            response = await self.coordinator.send_app_command(
                self._device_id, command, payload
            )
        except Exception as err:
            raise HomeAssistantError(f"Could not change the cook: {err}") from err
        result = response.get("payload") or {}
        if result.get("status") != "ok":
            raise HomeAssistantError(
                f"Anova refused the change: {result.get('error', 'unknown error')}"
            )
        if do := self._stage_do:
            do.update(stage_settings)
        self.coordinator.async_update_listeners()
