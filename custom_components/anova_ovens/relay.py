"""Client for the MediaMTX camera relay (the Anova Camera Relay add-on).

The oven streams VP8 over WebRTC (WHEP) from Cloudflare. Home Assistant's
go2rtc only offers H264/H265 to WHEP servers, so Cloudflare refuses it.
MediaMTX can receive VP8 and re-serves the stream as RTSP, which ffmpeg
(snapshots) and go2rtc (live view, transcoded to H264) can read.

The relay pulls from Cloudflare only while something reads the path.
"""
from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

import aiohttp

RELAY_API_PORT = 9997
RELAY_RTSP_PORT = 8554
RELAY_TIMEOUT_SECONDS = 10


class RelayError(Exception):
    """The relay could not be reached or refused a request."""


def relay_host(value: str) -> str:
    """Normalise what the user typed (host, host:port or a URL) to a host."""
    value = value.strip()
    if "://" in value:
        value = urlsplit(value).hostname or ""
    return value.split(":")[0].strip("/")


class CameraRelay:
    """MediaMTX API and RTSP addresses for one relay host."""

    def __init__(self, http: aiohttp.ClientSession, host: str) -> None:
        self._http = http
        self.host = relay_host(host)
        self._api = f"http://{self.host}:{RELAY_API_PORT}/v3"

    def rtsp_url(self, path: str) -> str:
        """RTSP address of a relayed stream."""
        return f"rtsp://{self.host}:{RELAY_RTSP_PORT}/{path}"

    async def _request(
        self, method: str, endpoint: str, json: dict[str, Any] | None = None
    ) -> Any:
        try:
            async with asyncio.timeout(RELAY_TIMEOUT_SECONDS):
                async with self._http.request(
                    method, f"{self._api}/{endpoint}", json=json
                ) as resp:
                    body = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise RelayError(f"camera relay at {self.host} not reachable ({err!r})") from err
        if resp.status >= 400:
            raise RelayError(
                f"camera relay refused {endpoint}: HTTP {resp.status} "
                f"{(body or {}).get('error', '')}"
            )
        return body

    async def async_check(self) -> None:
        """Raise RelayError unless the relay's API answers."""
        await self._request("GET", "paths/list")

    async def async_set_source(self, path: str, whep_url: str) -> None:
        """Make a path pull the given WHEP URL on demand.

        Paths added through the API live only in the relay's memory, so a
        restarted relay has none: check every time, but only rewrite the
        path (which reloads the relay's config) when it is missing or stale.
        """
        source = "wheps://" + whep_url.split("://", 1)[-1]
        try:
            current = await self._request("GET", f"config/paths/get/{path}")
        except RelayError:
            current = None
        if (current or {}).get("source") != source:
            await self._request(
                "POST", f"config/paths/replace/{path}", {"source": source}
            )

    async def async_reader_count(self, path: str) -> int:
        """Number of clients currently reading a path (0 if it is unknown)."""
        try:
            info = await self._request("GET", f"paths/get/{path}")
        except RelayError:
            return 0
        return len(info.get("readers") or [])
