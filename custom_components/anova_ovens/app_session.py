"""Optional Anova app login, for commands a Personal Access Token may not send.

The lamp (and the camera) are refused with "unauthorized" over a Personal
Access Token. The Anova app signs in with Firebase and connects to the same
WebSocket with an app handshake, which is what this module reproduces. It
is unofficial and may break if Anova changes its app.
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from typing import Any
import uuid
from urllib.parse import urlencode

import aiohttp
import websockets

from .const import ACCESSORIES_APO, WS_ENDPOINT

_LOGGER = logging.getLogger(__name__)

# Public web API key from the Anova app's Firebase config; the user's
# refresh token is the credential.
FIREBASE_API_KEY = "AIzaSyB0VNqmJVAeR1fn_NbqqhwSytyMOZ_JO9c"
FIREBASE_SIGN_IN_URL = (
    "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
)
FIREBASE_REFRESH_URL = "https://securetoken.googleapis.com/v1/token"
APP_SUBPROTOCOL = "ANOVA_V2"
APP_PLATFORM = "android"


class AppAuthError(Exception):
    """The Anova app login was rejected."""


class AppConnectionError(Exception):
    """The Anova app connection failed."""


async def async_sign_in(
    session: aiohttp.ClientSession, email: str, password: str
) -> str:
    """Sign in with an Anova email and password and return a refresh token."""
    try:
        async with session.post(
            FIREBASE_SIGN_IN_URL,
            params={"key": FIREBASE_API_KEY},
            json={"email": email, "password": password, "returnSecureToken": True},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
        raise AppConnectionError("Could not reach the Anova login service") from err

    if resp.status != 200 or not isinstance(data.get("refreshToken"), str):
        raise AppAuthError("Anova rejected the email or password")
    return data["refreshToken"]


class AnovaAppSession:
    """Send oven commands over an app-authenticated connection."""

    def __init__(
        self,
        http: aiohttp.ClientSession,
        ssl_context: ssl.SSLContext,
        refresh_token: str,
    ) -> None:
        self._http = http
        self._ssl_context = ssl_context
        self.refresh_token = refresh_token
        self._id_token: str | None = None
        self._id_token_expires = 0.0
        self._lock = asyncio.Lock()

    async def _async_id_token(self) -> str:
        """Return a valid Firebase ID token, refreshing it when near expiry."""
        if self._id_token and time.monotonic() < self._id_token_expires - 90:
            return self._id_token

        try:
            async with self._http.post(
                FIREBASE_REFRESH_URL,
                params={"key": FIREBASE_API_KEY},
                data={"grant_type": "refresh_token", "refresh_token": self.refresh_token},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            raise AppConnectionError("Could not reach the Anova login service") from err

        if resp.status != 200 or not isinstance(data.get("id_token"), str):
            raise AppAuthError("The Anova app login has expired; sign in again")

        # Firebase may rotate the refresh token; callers persist the new one
        self.refresh_token = data.get("refresh_token") or self.refresh_token
        self._id_token = data["id_token"]
        self._id_token_expires = time.monotonic() + int(data.get("expires_in", 3600))
        return self._id_token

    async def async_send_command(
        self,
        device_id: str,
        command: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 15,
    ) -> dict[str, Any]:
        """Send one command and return Anova's RESPONSE message."""
        async with self._lock:
            params = {
                "token": await self._async_id_token(),
                "supportedAccessories": ACCESSORIES_APO,
                "platform": APP_PLATFORM,
            }
            message: dict[str, Any] = {
                "command": command,
                "requestId": str(uuid.uuid4()),
                "payload": {"id": device_id, "type": command},
            }
            if payload is not None:
                message["payload"]["payload"] = payload

            try:
                async with asyncio.timeout(timeout):
                    async with websockets.connect(
                        f"{WS_ENDPOINT}?{urlencode(params)}",
                        ssl=self._ssl_context,
                        subprotocols=[APP_SUBPROTOCOL],
                        open_timeout=10,
                        close_timeout=5,
                    ) as websocket:
                        await websocket.send(json.dumps(message))
                        async for raw in websocket:
                            data = json.loads(raw)
                            if (
                                data.get("command") == "RESPONSE"
                                and data.get("requestId") == message["requestId"]
                            ):
                                return data
            except (TimeoutError, websockets.exceptions.WebSocketException, OSError) as err:
                raise AppConnectionError(
                    f"No reply from Anova to {command}: {type(err).__name__}"
                ) from err

        raise AppConnectionError(f"Connection closed before Anova replied to {command}")
