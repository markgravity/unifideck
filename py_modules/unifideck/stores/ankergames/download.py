"""AnkerGames archive acquisition — authorize, wait out the throttle, stream.

Implements the same ``ArchiveSource``-shaped contract
``stores/gamevault/sources.py`` defines (``acquire`` / ``release`` /
``size``), reusing its streaming-download plumbing pattern rather than
importing across stores (a store package here is meant to be
self-contained; see ``shared/store_base.py``'s docstring). The one
AnkerGames-specific step is the authorize round trip: the server answers
``425`` with a ``retry_after`` while its own throttle counts down (observed
5s on a free download), and the real file URL only exists after that
clears.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from .ankergames_config import AnkerGamesConfig
from .auth import AnkerGamesAuth
from .catalog import AnkerGamesCatalog
from .http import AnkerGamesThrottleError

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[AnkerGames download]"
ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]
_CHUNK_BYTES = 1024 * 1024
_REPORT_INTERVAL_S = 1.0


@dataclass(frozen=True)
class AcquiredArchive:
    """One archive on local disk, ready for ``install.extract_archive``."""

    path: Path
    title: str
    dir_name: str


class AnkerGamesArchiveSource:
    """Authorizes and streams one game's archive, then deletes it."""

    def __init__(
        self,
        auth: AnkerGamesAuth,
        catalog: AnkerGamesCatalog,
        config: AnkerGamesConfig,
    ) -> None:
        self._auth = auth
        self._catalog = catalog
        self._config = config

    async def acquire(
        self, slug: str, *, progress_callback: ProgressCallback | None,
    ) -> AcquiredArchive:
        detail = await self._catalog.fetch_detail(slug)
        if detail is None:
            raise RuntimeError(f"AnkerGames: could not read catalog detail for {slug!r}")
        version = detail.get("version") if isinstance(detail.get("version"), dict) else {}
        file_id = version.get("file_id")
        title = detail.get("title") if isinstance(detail.get("title"), str) else slug
        if not isinstance(file_id, int):
            raise RuntimeError(f"AnkerGames: {slug!r} has no downloadable version.file_id")

        authorized = await self._authorize(file_id)
        staging = Path(self._config.download_dir).expanduser()
        await asyncio.to_thread(staging.mkdir, parents=True, exist_ok=True)
        return await _download_archive(
            url=authorized["url"],
            staging=staging,
            fallback_name=f"{title}-AnkerGames.zip",
            title=title,
            progress_callback=progress_callback,
        )

    async def _authorize(self, file_id: int) -> dict[str, Any]:
        """``POST /downloads/{file_id}/authorize``, retrying on the server's throttle."""
        deadline = time.monotonic() + self._config.authorize_max_wait_s
        while True:
            try:
                status, body = await self._auth.client.post(
                    f"/downloads/{file_id}/authorize",
                    extra_headers={"X-AG-Capabilities": "device-proof"},
                )
            except AnkerGamesThrottleError as exc:
                if time.monotonic() + exc.retry_after > deadline:
                    raise RuntimeError(
                        "AnkerGames: download stayed throttled past "
                        f"{self._config.authorize_max_wait_s}s",
                    ) from exc
                logger.info(
                    "%s throttled, retrying in %.0fs", _LOG_PREFIX, exc.retry_after,
                )
                await asyncio.sleep(exc.retry_after)
                continue
            if status != 200 or not isinstance(body, dict) or "url" not in body:
                raise RuntimeError(f"AnkerGames: authorize returned {status}: {body!r}")
            return body

    def release(self, acquired: AcquiredArchive | None) -> None:
        """Delete the staged download; best-effort, never raises."""
        if acquired is None or not acquired.path.exists():
            return
        try:
            acquired.path.unlink()
        except OSError as exc:
            logger.warning("%s could not delete %s: %s", _LOG_PREFIX, acquired.path, exc)

    async def size(self, slug: str) -> int | None:
        """Bytes, from the catalog's ``size_gb`` — no network round trip."""
        detail = await self._catalog.fetch_detail(slug)
        size_gb = (detail or {}).get("size_gb")
        if isinstance(size_gb, int | float):
            return int(size_gb * 1_000_000_000)
        return None


# ── streaming download (pattern shared with stores/gamevault/sources.py) ───

async def _download_archive(
    *,
    url: str,
    staging: Path,
    fallback_name: str,
    title: str,
    progress_callback: ProgressCallback | None,
) -> AcquiredArchive:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=0), allow_redirects=True,
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"AnkerGames: download returned HTTP {resp.status}")
            archive_path = staging / _safe_name(
                _filename_from_cd(resp.headers.get("Content-Disposition", "")) or fallback_name,
            )
            total = int(resp.headers.get("Content-Length") or resp.content_length or 0)
            downloaded = await _stream_to_file(
                resp.content, archive_path, total, progress_callback,
            )
    logger.info("%s downloaded %s (%d bytes)", _LOG_PREFIX, archive_path.name, downloaded)
    return AcquiredArchive(path=archive_path, title=title, dir_name=_safe_dir_name(title))


async def _stream_to_file(
    content: Any, archive_path: Path, total: int, progress_callback: ProgressCallback | None,
) -> int:
    downloaded = 0
    last_report = 0.0
    start = time.monotonic()
    try:
        with archive_path.open("wb") as fh:
            async for chunk in content.iter_chunked(_CHUNK_BYTES):
                fh.write(chunk)
                downloaded += len(chunk)
                now = time.monotonic()
                if progress_callback and now - last_report >= _REPORT_INTERVAL_S:
                    elapsed = now - start
                    speed = downloaded / elapsed if elapsed > 0 else 0.0
                    remaining = total - downloaded
                    await progress_callback({
                        "phase": "downloading",
                        "percentage": round(downloaded / total * 100, 1) if total else 0,
                        "downloaded_bytes": downloaded,
                        "total_bytes": total,
                        "speed_bps": speed,
                        "eta_seconds": int(remaining / speed) if speed > 0 and total else 0,
                    })
                    last_report = now
    except BaseException:
        with contextlib.suppress(OSError):
            archive_path.unlink(missing_ok=True)
        raise
    return downloaded


def _filename_from_cd(content_disposition: str) -> str | None:
    m = re.search(r'filename\*?=["\']?([^"\';\r\n]+)["\']?', content_disposition)
    if not m:
        return None
    name = m.group(1).strip()
    if "''" in name:
        name = name.split("''", 1)[1]
    return name.strip('"\'')


def _safe_name(candidate: str) -> str:
    name = Path(candidate).name.strip()
    return name if name and set(name) != {"."} else "ankergames_download.zip"


def _safe_dir_name(title: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title).strip(" .")
    return cleaned or "game"
