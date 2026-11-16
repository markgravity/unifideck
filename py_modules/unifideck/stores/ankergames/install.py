"""AnkerGames install pipeline — authorize/download → extract → register.

Same shape as ``stores/gamevault/install.py``'s acquire → extract →
find-executable → marker → release pipeline, with
:class:`~.download.AnkerGamesArchiveSource` as the one acquire step
(GameVault's analog is its ``ArchiveSource``). Every download observed here
is a flat zip (see ``README.md``): there is no separate "run the installer"
phase the way a Windows-installer-shaped archive would need — the archive's
own top-level folder already *is* the game.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from unifideck.core.safe_delete import foreign_installs_under, safe_rmtree
from unifideck.core.types import InstallResult, Result
from unifideck.stores.shared.launch_target import find_executable

from .archive import extract_archive
from .download import AcquiredArchive, AnkerGamesArchiveSource, ProgressCallback
from .markers import (
    load_all_install_info,
    load_install_info,
    remove_install_info,
    save_install_info,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

STORE_NAME = "ankergames"
_MAX_DIR_ATTEMPTS = 20


class AnkerGamesInstaller:
    """Acquire → extract → register, for one game at a time."""

    def __init__(self, *, source: AnkerGamesArchiveSource, default_install_root: str) -> None:
        self._source = source
        self._default_install_root = Path(default_install_root).expanduser()

    async def install_game(
        self,
        slug: str,
        *,
        install_path: str | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> InstallResult:
        target_dir = Path(install_path).expanduser() if install_path else self._default_install_root
        await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)

        acquired: AcquiredArchive | None = None
        created_dir: Path | None = None
        try:
            acquired = await self._source.acquire(slug, progress_callback=progress_callback)
            if progress_callback:
                await progress_callback({"phase": "extracting"})

            game_dir = await asyncio.to_thread(_free_game_dir, target_dir, acquired.dir_name, slug)
            if not await asyncio.to_thread(game_dir.exists):
                created_dir = game_dir
            await extract_archive(acquired.path, game_dir)

            exe_path = await asyncio.to_thread(
                find_executable, str(game_dir), prefer_native=False, title=acquired.title,
            )
            save_install_info(
                slug,
                title=acquired.title,
                install_path=str(game_dir),
                exe_path=exe_path or "",
                archive_path=str(acquired.path),
            )
            return InstallResult(
                success=True, store=STORE_NAME, game_id=slug, install_path=str(game_dir),
                metadata={"exe_path": exe_path or "", "title": acquired.title},
            )
        except asyncio.CancelledError:
            await self._discard_partial_extract(created_dir, slug)
            raise
        except Exception as exc:
            logger.exception("[AnkerGamesInstaller] install_game(%s) failed", slug)
            return InstallResult(success=False, error=str(exc), store=STORE_NAME, game_id=slug)
        finally:
            self._source.release(acquired)

    async def uninstall_game(self, slug: str) -> Result:
        info = load_install_info(slug)
        if info is None:
            return Result(success=False, error="not_installed", store=STORE_NAME)
        install_path = info.get("install_path")
        if isinstance(install_path, str) and install_path:
            try:
                ok = await asyncio.to_thread(safe_rmtree, install_path)
            except Exception as exc:
                logger.exception("[AnkerGamesInstaller] uninstall_game(%s) failed", slug)
                return Result(success=False, error=str(exc), store=STORE_NAME)
            if not ok:
                return Result(success=False, error="unsafe_path", store=STORE_NAME)
        remove_install_info(slug)
        return Result(success=True, store=STORE_NAME)

    def get_install_info(self, slug: str) -> dict | None:
        return load_install_info(slug)

    def get_installed(self) -> dict[str, dict]:
        return load_all_install_info()

    async def get_game_size(self, slug: str) -> int | None:
        info = load_install_info(slug)
        if info and isinstance(info.get("install_path"), str):
            path = Path(info["install_path"])
            if await asyncio.to_thread(path.is_dir):
                return await asyncio.to_thread(_dir_size, path)
        return await self._source.size(slug)

    @staticmethod
    async def _discard_partial_extract(game_dir: Path | None, slug: str) -> None:
        if game_dir is None:
            return
        foreign = await asyncio.to_thread(
            foreign_installs_under, game_dir, owner_key=f"{STORE_NAME}:{slug}",
        )
        if foreign:
            return
        try:
            await asyncio.to_thread(safe_rmtree, game_dir)
        except Exception:
            logger.exception("[AnkerGamesInstaller] could not discard partial extract %s", game_dir)


def _free_game_dir(target_dir: Path, dir_name: str, slug: str) -> Path:
    first = target_dir / dir_name
    for attempt in range(_MAX_DIR_ATTEMPTS):
        candidate = first if attempt == 0 else target_dir / (
            f"{dir_name} (AnkerGames)" if attempt == 1 else f"{dir_name} (AnkerGames {attempt})"
        )
        if not foreign_installs_under(candidate, owner_key=f"{STORE_NAME}:{slug}"):
            return candidate
    return first


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
