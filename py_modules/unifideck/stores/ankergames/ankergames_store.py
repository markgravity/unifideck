"""AnkerGames store — Layer-4 ``StoreBase`` implementation.

See ``README.md`` in this package for what AnkerGames is, why it is a
personal-fork-only connector (not proposed upstream), and exactly how every
endpoint below was reverse-engineered. This module must contain exactly one
``StoreBase`` subclass — ``StoreRegistry._load_store_class`` picks the first
one it finds.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from unifideck.core.types import AuthResult, Game, InstallResult, Result, StoreInfo
from unifideck.stores.shared.store_base import StoreBase

from .ankergames_config import load_config
from .auth import AnkerGamesAuth
from .catalog import AnkerGamesCatalog, AnkerGamesFetchError
from .download import AnkerGamesArchiveSource, ProgressCallback
from .install import AnkerGamesInstaller

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
    from unifideck.core.cache_manager import CacheManager
    from unifideck.event_bus.event_bus import EventBus

logger = logging.getLogger(__name__)

STORE_NAME = "ankergames"


class AnkerGamesStore(StoreBase):
    """AnkerGames connector — device-code sign-in, HTTP catalog, zip installs."""

    store_info = StoreInfo(
        name="ankergames",
        display_name="AnkerGames",
        auth_method="device_code",
        icon_asset="ankergames.png",
        supports_install=True,
    )

    def __init__(
        self,
        bus: "EventBus",
        cache: "CacheManager",
        plugin_dir: str | None = None,
        config: "ConfigManager" | None = None,
    ) -> None:
        super().__init__(bus, cache, plugin_dir, config)
        self._config = load_config(config)
        self._auth = AnkerGamesAuth(bus, self._config)
        self._catalog = AnkerGamesCatalog(self._auth)
        self._source = AnkerGamesArchiveSource(self._auth, self._catalog, self._config)
        self._installer = AnkerGamesInstaller(
            source=self._source, default_install_root=self._config.default_install_root,
        )

    # ── StoreBase abstract methods ──────────────────────────────────

    async def is_available(self) -> bool:
        """A token is loaded and still accepted by the server."""
        return await self._auth.ensure_fresh()

    async def start_auth(self, **kwargs: Any) -> AuthResult:
        return await self._auth.start_auth(**kwargs)

    async def complete_auth(self, **kwargs: Any) -> AuthResult:
        return await self._auth.complete_auth(**kwargs)

    async def logout(self) -> Result:
        return await self._auth.logout()

    async def get_library(self, *, force: bool = False) -> list[Game] | None:
        del force  # the catalog does not cache; nothing to bypass
        try:
            games = await self._catalog.fetch()
        except AnkerGamesFetchError:
            logger.exception("[AnkerGamesStore] get_library failed")
            return None
        installed = self._installer.get_installed()
        for game in games:
            marker = installed.get(game.store_game_id)
            if marker:
                game.installed = True
                game.install_path = marker.get("install_path")
                game.exe_path = marker.get("exe_path") or None
        return games

    async def install_game(
        self, game_id: str, base_path: str | None = None,
        progress_cb: ProgressCallback | None = None, **kwargs: Any,
    ) -> InstallResult:
        return await self._installer.install_game(
            game_id,
            install_path=base_path or kwargs.get("install_path"),
            progress_callback=progress_cb or kwargs.get("progress_callback"),
        )

    async def uninstall_game(self, game_id: str, **kwargs: Any) -> Result:
        return await self._installer.uninstall_game(game_id)

    async def update_game(self, game_id: str, **kwargs: Any) -> InstallResult:
        """AnkerGames has no delta-patch API observed; re-install in place."""
        return await self.install_game(game_id, **kwargs)

    async def check_for_updates(self) -> list[str]:
        """Not implemented — would need a per-game ``build_id`` comparison.

        See the "What this connector does NOT cover" section of
        ``README.md``.
        """
        return []

    async def get_installed_path(self, game_id: str) -> str | None:
        info = self._installer.get_install_info(game_id)
        path = (info or {}).get("install_path")
        return path if isinstance(path, str) and path else None

    async def get_game_size(self, game_id: str) -> int | None:
        return await self._installer.get_game_size(game_id)
