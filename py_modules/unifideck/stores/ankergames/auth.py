"""AnkerGames sign-in — the device-code flow, measured on the real client.

Mirrors the shape of ``stores/microsoft/microsoft_device_auth.py`` (the
other device-code store here) but needs no embedded browser: the
``verification_uri_complete`` AnkerGames hands back already carries the
pairing code, so ``start_auth`` just returns that URL and the frontend (or
the user, by hand) opens it in whatever browser is already signed in to
their AnkerGames account. See ``README.md`` for the endpoints.

Session persistence is one small JSON file (token + device id), like every
other HTTP-only store here (``gamevault/auth.py`` is the closest analog).
The device's own ed25519 keypair is a second, separate file: the public key
is registered once via ``/device/key`` and is what later shows up as ``dk``
inside every signed download URL. The private key is generated, registered,
and then kept only because losing it would silently desync "what public key
did we register" from "what we think we registered" — nothing in this
connector re-signs anything with it; AnkerGames' own server is what issues
the already-signed download URL.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from unifideck.core.types import AuthResult, Events, Result

from .ankergames_config import AnkerGamesConfig
from .http import AnkerGamesAuthError, AnkerGamesClient

if TYPE_CHECKING:
    from unifideck.event_bus.event_bus import EventBus

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[AnkerGamesAuth]"
_DEFAULT_INTERVAL_S = 5
_SLOW_DOWN_STEP_S = 5
_MAX_INTERVAL_S = 30


@dataclass(frozen=True)
class DeviceCodeGrant:
    """What ``POST /device/code`` handed back."""

    device_code: str
    user_code: str
    verification_uri_complete: str
    expires_in: int
    interval: int


class AnkerGamesAuth:
    """Owns the bearer token, the device keypair and the pairing poll."""

    def __init__(self, bus: EventBus, config: AnkerGamesConfig) -> None:
        self._bus = bus
        self._config = config
        self.client = AnkerGamesClient(config)
        self._task: asyncio.Task[None] | None = None
        self._load_session()

    # ── public state ─────────────────────────────────────────────────

    def is_authenticated(self) -> bool:
        """Whether a bearer token is loaded (not re-validated against the server)."""
        return bool(self.client.token)

    @property
    def pairing_in_progress(self) -> bool:
        """Whether a ``start_auth`` poll is still waiting on the user."""
        return self._task is not None and not self._task.done()

    # ── StoreBase-facing flow ───────────────────────────────────────

    async def start_auth(self, **_: Any) -> AuthResult:
        """Request a pairing code and start polling for its approval."""
        await self.cancel_pending()
        try:
            status, body = await self.client.post(
                "/device/code",
                json={
                    "name": self._config.device_name,
                    "platform": "linux",
                    "app_version": self._config.launcher_version,
                    "hardware_hash": _hardware_hash(),
                },
            )
        except Exception as exc:
            logger.warning("%s /device/code failed: %s", _LOG_PREFIX, exc)
            return AuthResult(success=False, error="network_unreachable", store="ankergames")
        grant = _parse_grant(status, body)
        if grant is None:
            logger.warning("%s /device/code returned %s: %r", _LOG_PREFIX, status, body)
            return AuthResult(success=False, error="device_code_request_failed", store="ankergames")

        await self._emit(Events.STORE_AUTH_STARTED)
        self._task = asyncio.create_task(
            self._poll(grant), name="ankergames-device-code",
        )
        return AuthResult(
            success=True,
            store="ankergames",
            url=grant.verification_uri_complete,
            metadata={
                "pending": True,
                "flow": "device_code",
                "user_code": grant.user_code,
                "verification_uri_complete": grant.verification_uri_complete,
                "expires_in": grant.expires_in,
            },
        )

    async def complete_auth(self, **_: Any) -> AuthResult:
        """AnkerGames has no separate "complete" step — report the poll's state."""
        if self.is_authenticated():
            return AuthResult(success=True, action="authenticated", tokens_cached=True, store="ankergames")
        if self.pairing_in_progress:
            return AuthResult(success=True, action="pending", store="ankergames", metadata={"pending": True})
        return AuthResult(success=False, error="not_authenticated", store="ankergames")

    async def logout(self) -> Result:
        """Drop the local session. Does not revoke the device server-side.

        AnkerGames exposes device management at ``/devices`` for the user to
        remove a device themselves (seen in the web UI, not reverse an API
        endpoint for it) — this connector only forgets its own copy of the
        token.
        """
        await self.cancel_pending()
        self.client.token = None
        _session_path(self._config).unlink(missing_ok=True)
        await self._emit(Events.STORE_LOGOUT)
        return Result(success=True, store="ankergames")

    async def cancel_pending(self) -> None:
        """Stop a running pairing poll, if any."""
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # ── the poll ─────────────────────────────────────────────────────

    async def _poll(self, grant: DeviceCodeGrant) -> None:
        try:
            await self._wait_for_approval(grant)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s pairing poll crashed", _LOG_PREFIX)
            await self._emit(Events.STORE_AUTH_FAILED, error="device_code_failed")

    async def _wait_for_approval(self, grant: DeviceCodeGrant) -> None:
        deadline = time.monotonic() + grant.expires_in
        interval = grant.interval or _DEFAULT_INTERVAL_S
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            try:
                status, body = await self.client.post(
                    "/device/poll", json={"device_code": grant.device_code},
                )
            except Exception as exc:
                logger.debug("%s poll request error: %s", _LOG_PREFIX, exc)
                continue
            if status == 202:
                continue  # {"status": "pending"}
            if status == 200 and isinstance(body, dict) and body.get("status") == "approved":
                await self._accept(body)
                return
            if status == 429:
                interval = min(interval + _SLOW_DOWN_STEP_S, _MAX_INTERVAL_S)
                continue
            logger.warning("%s poll returned %s: %r", _LOG_PREFIX, status, body)
            await self._emit(Events.STORE_AUTH_FAILED, error="device_code_declined")
            return
        await self._emit(Events.STORE_AUTH_FAILED, error="device_code_expired")

    async def _accept(self, body: dict[str, Any]) -> None:
        token = body.get("token")
        if not isinstance(token, str) or not token:
            await self._emit(Events.STORE_AUTH_FAILED, error="device_code_failed")
            return
        self.client.token = token
        _save_session(self._config, token=token, device=body.get("device") or {})
        logger.info("%s paired as device %r", _LOG_PREFIX, (body.get("device") or {}).get("id"))
        await self._register_device_key()
        await self._emit(Events.STORE_AUTH_COMPLETE)

    async def _register_device_key(self) -> None:
        """Register this device's ed25519 public key, generating one if needed.

        Best-effort: a failure here is logged and otherwise ignored rather
        than failing the whole sign-in — downloads were observed to work
        immediately after pairing in manual testing, before this call had
        even run, so it is not known to be load-bearing (see ``README.md``).
        """
        try:
            public_key_b64 = _load_or_create_device_key(self._config)
            status, body = await self.client.post(
                "/device/key", json={"public_key": public_key_b64},
            )
            if status not in (200, 204):
                logger.warning("%s /device/key returned %s: %r", _LOG_PREFIX, status, body)
        except Exception:
            logger.exception("%s device key registration failed", _LOG_PREFIX)

    # ── session persistence ─────────────────────────────────────────

    def _load_session(self) -> None:
        path = _session_path(self._config)
        try:
            data = json.loads(path.read_text())
            token = data.get("token")
            if isinstance(token, str) and token:
                self.client.token = token
        except (OSError, ValueError):
            pass

    async def ensure_fresh(self) -> bool:
        """Probe ``/me``; drop the token on 401 so callers re-request auth.

        Call before a sync or an install when staleness would otherwise
        surface as a confusing generic HTTP error deep in the catalog or
        download path.
        """
        if not self.is_authenticated():
            return False
        try:
            await self.client.get("/me")
        except AnkerGamesAuthError:
            logger.info("%s token rejected by /me; clearing it", _LOG_PREFIX)
            self.client.token = None
            _session_path(self._config).unlink(missing_ok=True)
            return False
        except Exception as exc:
            # Network/5xx: unknown, not necessarily invalid. Leave the
            # token in place — a sync that fails outright will surface its
            # own error rather than this call forcing a spurious re-login.
            logger.debug("%s /me probe inconclusive: %s", _LOG_PREFIX, exc)
        return self.is_authenticated()

    async def _emit(self, event: Events, **payload: Any) -> None:
        with contextlib.suppress(Exception):
            await self._bus.emit(event, store="ankergames", **payload)


# ── module-level helpers (no instance state needed) ────────────────────────

def _parse_grant(status: int, body: Any) -> DeviceCodeGrant | None:
    if status != 201 or not isinstance(body, dict):
        return None
    device_code, user_code = body.get("device_code"), body.get("user_code")
    uri = body.get("verification_uri_complete")
    if not (isinstance(device_code, str) and isinstance(user_code, str) and isinstance(uri, str)):
        return None
    return DeviceCodeGrant(
        device_code=device_code,
        user_code=user_code,
        verification_uri_complete=uri,
        expires_in=body.get("expires_in", 600) if isinstance(body.get("expires_in"), int) else 600,
        interval=body.get("interval", _DEFAULT_INTERVAL_S) if isinstance(body.get("interval"), int) else _DEFAULT_INTERVAL_S,
    )


def _hardware_hash() -> str:
    """A stable-enough per-machine identifier.

    The real client sends a 64-hex-char hash (presumably of real hardware
    IDs); the server was not observed rejecting a different-shaped value,
    so this hashes the hostname instead of pulling in a hardware-fingerprint
    dependency for one optional field. Not used for anything security
    sensitive on AnkerGames' side as far as this reverse-engineering went.
    """
    import hashlib
    return hashlib.sha256(socket.gethostname().encode()).hexdigest()


def _session_path(config: AnkerGamesConfig) -> Path:
    return Path(config.session_file).expanduser()


def _save_session(config: AnkerGamesConfig, *, token: str, device: dict[str, Any]) -> None:
    path = _session_path(config)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"token": token, "device": device}, indent=2))
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    except OSError:
        logger.exception("%s could not persist session", _LOG_PREFIX)


def _load_or_create_device_key(config: AnkerGamesConfig) -> str:
    """Base64url (no padding) ed25519 public key, generating+persisting one if needed."""
    path = Path(config.device_key_file).expanduser()
    try:
        data = json.loads(path.read_text())
        seed_b64 = data.get("seed")
        if isinstance(seed_b64, str) and seed_b64:
            seed = base64.urlsafe_b64decode(seed_b64 + "==")
            private_key = Ed25519PrivateKey.from_private_bytes(seed)
            return _public_key_b64(private_key)
    except (OSError, ValueError):
        pass

    private_key = Ed25519PrivateKey.generate()
    seed = private_key.private_bytes_raw()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"seed": _b64url(seed)}, indent=2))
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    except OSError:
        logger.exception("%s could not persist device key", _LOG_PREFIX)
    return _public_key_b64(private_key)


def _public_key_b64(private_key: Ed25519PrivateKey) -> str:
    public_bytes = private_key.public_key().public_bytes_raw()
    return _b64url(public_bytes)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")
