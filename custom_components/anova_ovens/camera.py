"""Camera platform for Anova Precision Oven (v2, via the app login).

The oven only streams while cooking. Starting its live stream over the app
login returns a Cloudflare WebRTC (WHEP) playback URL. The stream is VP8,
which Home Assistant's go2rtc cannot receive over WebRTC, so the camera
needs the MediaMTX relay (the Anova Camera Relay add-on, set in the
integration's options): the relay pulls the WHEP stream and re-serves it
as RTSP.

Snapshots: ffmpeg reads one frame from the relay. HA allows a snapshot 10
seconds and connecting can take longer, so frames are grabbed in the
background and a request returns the latest frame.

Live view (optional, off by default): go2rtc plays the relay through
ffmpeg, transcoding VP8 to H264, which costs CPU while someone watches.

The oven's stream is kept alive while requested and stopped when it is not.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import time
from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import CONF_CAMERA_LIVE_VIEW, CONF_CAMERA_RELAY, OVEN_TYPE_V2
from .coordinator import AnovaOvenConfigEntry, AnovaOvenCoordinator
from .entity import AnovaOvenEntity
from .relay import CameraRelay, RelayError

_LOGGER = logging.getLogger(__name__)

CMD_APO_START_LIVE_STREAM = "CMD_APO_START_LIVE_STREAM"
CMD_APO_STOP_LIVE_STREAM = "CMD_APO_STOP_LIVE_STREAM"

# The app re-sends the start command about once a minute to keep streaming
STREAM_KEEPALIVE = timedelta(seconds=50)
# Stop the oven's stream when nothing has asked for it this long
STREAM_IDLE_STOP_SECONDS = 600
# How long a background ffmpeg grab may take (relay connect + key frame)
FRAME_GRAB_TIMEOUT_SECONDS = 30
# How long a snapshot request waits for a grab before returning the last frame
SNAPSHOT_WAIT_SECONDS = 8
# Hard limit so a stuck grab cannot block all later snapshots
GRAB_TOTAL_TIMEOUT_SECONDS = 60


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AnovaOvenConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the oven camera for v2 ovens with the app login and a relay."""
    coordinator = entry.runtime_data.coordinator
    relay_host = entry.options.get(CONF_CAMERA_RELAY, "").strip()
    if coordinator.app_session is None or not relay_host:
        return

    relay = CameraRelay(async_get_clientsession(hass), relay_host)
    live_view = entry.options.get(CONF_CAMERA_LIVE_VIEW, False)
    known_devices: set[str] = set()

    def create_entities_for_device(device_id: str) -> None:
        info = coordinator.get_device_info(device_id) or {}
        if device_id in known_devices or info.get("type") != OVEN_TYPE_V2:
            return

        known_devices.add(device_id)
        async_add_entities([AnovaOvenCamera(coordinator, device_id, relay, live_view)])
        _LOGGER.debug("Created camera entity for device %s", device_id)

    for device_id in coordinator.devices:
        create_entities_for_device(device_id)

    @callback
    def handle_coordinator_update() -> None:
        for device_id in coordinator.devices:
            create_entities_for_device(device_id)

    entry.async_on_unload(coordinator.async_add_listener(handle_coordinator_update))


class AnovaOvenCamera(AnovaOvenEntity, Camera):
    """Snapshots (and optionally live view) of the oven cavity via the relay."""

    def __init__(
        self,
        coordinator: AnovaOvenCoordinator,
        device_id: str,
        relay: CameraRelay,
        live_view: bool,
    ) -> None:
        """Initialize the camera."""
        AnovaOvenEntity.__init__(self, coordinator, device_id)
        Camera.__init__(self)
        self._attr_unique_id = f"{device_id}_camera"
        self._attr_name = "Camera"
        self._attr_supported_features = (
            CameraEntityFeature.STREAM if live_view else CameraEntityFeature(0)
        )

        self._relay = relay
        self._relay_path = f"anova_oven_{device_id}"
        self._lock = asyncio.Lock()
        self._whep_url: str | None = None
        self._last_requested = 0.0
        self._cancel_keepalive: Any = None
        self._probing_providers = False
        self._grab_task: asyncio.Task | None = None
        self._last_image: bytes | None = None

    @property
    def _is_cooking(self) -> bool:
        state = self.coordinator.get_device_state(self._device_id) or {}
        return state.get("state", {}).get("mode") == "cook"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Explain why there is no picture when idle."""
        return {"streaming": self._whep_url is not None, "cooking": self._is_cooking}

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a fresh frame if one arrives in time, else the last frame."""
        if not self._is_cooking:
            return self._last_image

        if self._grab_task is None or self._grab_task.done():
            self._grab_task = self.hass.async_create_background_task(
                self._async_grab_frame_limited(), f"{self.entity_id} frame grab"
            )
        try:
            await asyncio.wait_for(asyncio.shield(self._grab_task), SNAPSHOT_WAIT_SECONDS)
        except TimeoutError:
            pass
        return self._last_image

    async def _async_grab_frame_limited(self) -> None:
        """Run one grab with an overall limit."""
        try:
            async with asyncio.timeout(GRAB_TOTAL_TIMEOUT_SECONDS):
                await self._async_grab_frame()
        except TimeoutError:
            _LOGGER.warning(
                "Oven camera frame grab stopped after %ss", GRAB_TOTAL_TIMEOUT_SECONDS
            )
        except Exception:
            _LOGGER.exception("Oven camera frame grab failed")

    async def _async_grab_frame(self) -> None:
        """Read one frame from the relay with ffmpeg."""
        if not await self._async_ensure_stream():
            return

        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            get_ffmpeg_manager(self.hass).binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-rtsp_transport",
            "tcp",
            "-i",
            self._relay.rtsp_url(self._relay_path),
            "-frames:v",
            "1",
            "-c:v",
            "mjpeg",
            "-f",
            "image2pipe",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(FRAME_GRAB_TIMEOUT_SECONDS):
                stdout, stderr = await proc.communicate()
        except TimeoutError:
            stdout, stderr = b"", b"(timed out waiting for a frame)"
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

        if stdout:
            self._last_image = stdout
            _LOGGER.debug("Oven camera frame took %.1fs", time.monotonic() - started)
            return
        _LOGGER.warning(
            "No frame from the oven camera relay after %.0fs (ffmpeg exit %s): %s",
            time.monotonic() - started,
            proc.returncode,
            stderr.decode(errors="replace").strip()[-500:] or "no output",
        )

    async def async_refresh_providers(self, *, write_state: bool = True) -> None:
        """Let HA link this camera to go2rtc without starting the oven stream.

        HA picks the stream provider once, when the entity is added, by
        calling stream_source(). At that point the oven may not be cooking,
        which would leave the camera without go2rtc for good.
        """
        self._probing_providers = True
        try:
            await super().async_refresh_providers(write_state=write_state)
        finally:
            self._probing_providers = False

    async def stream_source(self) -> str | None:
        """Live view: go2rtc reads the relay through ffmpeg as H264."""
        if not self.supported_features & CameraEntityFeature.STREAM:
            return None
        source = f"ffmpeg:{self._relay.rtsp_url(self._relay_path)}#video=h264"
        if self._probing_providers:
            return source
        return source if await self._async_ensure_stream() else None

    async def _async_ensure_stream(self) -> bool:
        """Start (or keep) the oven's stream and point the relay at it."""
        if not self._is_cooking:
            await self._async_stop_stream()
            return False

        async with self._lock:
            self._last_requested = time.monotonic()
            if self._whep_url is None:
                try:
                    self._whep_url = await self._async_start_stream()
                except Exception as err:
                    _LOGGER.warning("Could not start the oven camera stream: %s", err)
                    return False
                self._start_keepalive()
            try:
                await self._relay.async_set_source(self._relay_path, self._whep_url)
            except RelayError as err:
                _LOGGER.warning("Oven camera: %s", err)
                return False
            return True

    async def _async_start_stream(self) -> str:
        """Send the live-stream start command and return the WHEP URL."""
        response = await self.coordinator.send_app_command(
            self._device_id, CMD_APO_START_LIVE_STREAM
        )
        result = response.get("payload", {})
        url = result.get("data", {}).get("webRTCPlayback", {}).get("url")
        if result.get("status") != "ok" or not url:
            raise RuntimeError(f"Anova refused live video: {result.get('error')}")
        return url

    @callback
    def _start_keepalive(self) -> None:
        if self._cancel_keepalive is None:
            self._cancel_keepalive = async_track_time_interval(
                self.hass, self._async_keepalive, STREAM_KEEPALIVE
            )

    async def _async_keepalive(self, _now: Any = None) -> None:
        """Keep streaming while cooking and watched or recently asked for."""
        if await self._relay.async_reader_count(self._relay_path):
            # Someone is watching the live view
            self._last_requested = time.monotonic()
        idle = time.monotonic() - self._last_requested > STREAM_IDLE_STOP_SECONDS
        if idle or not self._is_cooking:
            await self._async_stop_stream()
            return
        try:
            url = await self._async_start_stream()
        except Exception as err:
            _LOGGER.debug("Oven camera keepalive failed: %s", err)
            return
        self._whep_url = url
        try:
            await self._relay.async_set_source(self._relay_path, url)
        except RelayError as err:
            _LOGGER.debug("Oven camera keepalive: %s", err)

    async def _async_stop_stream(self) -> None:
        """Stop the oven's live stream if this camera started it."""
        if self._cancel_keepalive:
            self._cancel_keepalive()
            self._cancel_keepalive = None
        if self._whep_url is None:
            return
        self._whep_url = None
        try:
            await self.coordinator.send_app_command(
                self._device_id, CMD_APO_STOP_LIVE_STREAM
            )
        except Exception as err:
            _LOGGER.debug("Stopping the oven camera stream failed: %s", err)

    async def async_will_remove_from_hass(self) -> None:
        """Stop streaming when the entity is removed."""
        if self._grab_task and not self._grab_task.done():
            self._grab_task.cancel()
        await self._async_stop_stream()
        await super().async_will_remove_from_hass()
