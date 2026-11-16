"""Minimal AnkerGames HTTP client — one aiohttp session, JSON in and out.

Every call goes through :meth:`AnkerGamesClient.request`, so the bearer
token, the launcher's user-agent and the ``application/json`` Accept header
are attached exactly once rather than at every call site.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from .ankergames_config import AnkerGamesConfig

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[AnkerGames http]"


class AnkerGamesApiError(RuntimeError):
    """A non-2xx reply that is not one of the specific cases below."""

    def __init__(self, status: int, body: Any) -> None:
        """Keep both the status and whatever the server sent as the body."""
        super().__init__(f"HTTP {status}: {body!r}")
        self.status = status
        self.body = body


class AnkerGamesAuthError(AnkerGamesApiError):
    """401 — the bearer token is missing, expired or revoked."""


class AnkerGamesThrottleError(AnkerGamesApiError):
    """425 — ``/downloads/{id}/authorize`` is not ready yet.

    Carries ``retry_after`` (seconds) straight from the response body so the
    caller can back off exactly as long as the server asked, rather than
    guessing a fixed sleep.
    """

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        """Pull ``retry_after``/``wait_seconds`` out of *body* for the caller."""
        super().__init__(status, body)
        retry = body.get("retry_after", body.get("wait_seconds", 5))
        self.retry_after: float = float(retry) if isinstance(retry, int | float) else 5.0


class AnkerGamesClient:
    """One client per store instance; holds the bearer token in memory."""

    def __init__(self, config: AnkerGamesConfig) -> None:
        self._config = config
        self.token: str | None = None

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": self._config.user_agent,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if extra:
            headers.update(extra)
        return headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout_s: float = 30.0,
    ) -> tuple[int, Any]:
        """One request to ``{base_url}{path}``; returns ``(status, parsed_body)``.

        Raises :class:`AnkerGamesAuthError` on 401 and
        :class:`AnkerGamesThrottleError` on 425 so the two cases every
        caller actually branches on are typed, not re-parsed from the
        status code at each call site. Every other non-2xx is reported
        through the return value — most of this API's error payloads
        (``{"message": "..."}`` or ``{"error": "..."}``) are meant to be
        shown to the user, not treated as transport failures.
        """
        url = f"{self._config.base_url}{path}"
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method,
                url,
                json=json,
                params=params,
                headers=self._headers(extra_headers),
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as resp:
                body = await _read_body(resp)
                if resp.status == 401:
                    raise AnkerGamesAuthError(resp.status, body)
                if resp.status == 425 and isinstance(body, dict):
                    raise AnkerGamesThrottleError(resp.status, body)
                return resp.status, body

    async def get(self, path: str, **kwargs: Any) -> tuple[int, Any]:
        """``GET`` shorthand for :meth:`request`."""
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> tuple[int, Any]:
        """``POST`` shorthand for :meth:`request`."""
        return await self.request("POST", path, **kwargs)


async def _read_body(resp: aiohttp.ClientResponse) -> Any:
    """JSON if the server said so, else the raw text (never raises)."""
    ctype = resp.headers.get("Content-Type", "")
    if "application/json" in ctype:
        try:
            return await resp.json()
        except Exception as exc:  # malformed JSON despite the header
            logger.debug("%s could not parse JSON body: %s", _LOG_PREFIX, exc)
    try:
        return await resp.text()
    except Exception:
        return None
