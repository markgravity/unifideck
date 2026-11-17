"""Install a game by driving the real AnkerGames launcher's own UI.

The direct-API path (``download.py``) cannot complete an install: every
download GET the CDN serves is gated behind an ``X-AG-Proof`` header this
project could not reverse-engineer end to end (see ``README.md``). This
module is the fallback that actually works — it drives the official
"Anker Launcher" client (which computes the header correctly, since it has
the real source) through its own UI via :mod:`atspi_driver`, then adopts
whatever that client puts on disk. Same shape as Unifideck's other
*wrapper stores* (Battle.net, Ubisoft): the vendor's own client does the
one step only it can do; this code starts it, drives it, and folds the
result into our own install-marker format so the rest of the store
(``get_installed_path``, ``get_game_size``, uninstall) never needs to know
how the install actually happened.

Requires:
  * the real AnkerLauncher AppImage, at a path from config
    (``stores.ankergames.launcher_appimage_path``) — same expectation as
    Battle.net/Ubisoft needing their own vendor client installed;
  * a live desktop session this process can borrow display env from (see
    :mod:`wrapper_env`) — Linux-desktop-only, will not work headless.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from unifideck.core.types import InstallResult
from unifideck.stores.shared.launch_target import find_executable

from .atspi_driver import AnkerGamesAtspiDriver, AtspiUnavailable
from .markers import save_install_info
from .wrapper_env import detect_display_env

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[AnkerGames wrapper-install]"

#: How long to wait for the launcher's own accessibility tree to appear
#: after we start it cold. WebKitGTK's a11y bridge activates lazily and the
#: window itself takes a beat to paint; 25s covers a cold AppImage FUSE
#: mount (the slowest observed step) with room to spare.
_LAUNCH_WAIT_S = 25.0
_LAUNCH_POLL_INTERVAL_S = 1.0

#: How long to wait for a download+install to finish before giving up.
#: Driven by the UI, not a known byte count, so this is a wall-clock cap
#: rather than a per-file-size estimate; large games need a bigger value
#: than the default (see ``install_timeout_s`` in config).
_DEFAULT_INSTALL_TIMEOUT_S = 1800  # 30 min
_POLL_INTERVAL_S = 2.0

#: Marker files the real launcher writes into a finished game's own
#: directory — the most reliable "is this actually done" signal, since the
#: UI can report "Installed" a beat before the directory is fully in place.
_INSTALLATION_MARKER = ".anker-installation.json"


class AnkerGamesWrapperInstallError(RuntimeError):
    """The real launcher could not be driven to a finished install."""


class AnkerGamesWrapperInstaller:
    """Launches, drives and polls the real AnkerGames client for one install."""

    def __init__(
        self,
        *,
        launcher_appimage_path: str,
        games_root: str,
        install_timeout_s: int = _DEFAULT_INSTALL_TIMEOUT_S,
    ) -> None:
        self._appimage = Path(launcher_appimage_path).expanduser()
        # The real client's own library root — where IT puts installed
        # games, independent of this store's configured install root (the
        # two are reconciled in install_game(): we report whatever
        # directory the client actually used, not our own default).
        self._games_root = Path(games_root).expanduser()
        self._install_timeout_s = install_timeout_s
        self._driver: AnkerGamesAtspiDriver | None = None

    # ── public API ──────────────────────────────────────────────────

    async def install_game(self, slug: str, title: str) -> InstallResult:
        """Drive the real client to install *slug*, then adopt the result.

        ``title`` is the display name shown in the client's own UI — it is
        what we search for, since the client's accessible tree exposes
        titles, not our internal slugs (see ``README.md`` on why the two
        can differ in punctuation/case).
        """
        try:
            import asyncio

            return await asyncio.to_thread(self._install_game_sync, slug, title)
        except AtspiUnavailable as exc:
            return InstallResult(success=False, error=str(exc), store="ankergames", game_id=slug)

    # ── the synchronous drive, run off the event loop ────────────────

    def _install_game_sync(self, slug: str, title: str) -> InstallResult:
        before = self._snapshot_installed_dirs()
        try:
            self._ensure_launcher_running()
            driver = self._get_driver()
            self._navigate_to_game(driver, title)
            clicked = driver.click_button_containing("Download, ")
            if clicked is None:
                # Already downloaded, just needs extracting — the
                # Downloads list shows a plain "Install" button instead.
                clicked = driver.click_button_named("Install")
            if clicked is None:
                raise AnkerGamesWrapperInstallError(
                    f"found {title!r} but no Download/Install button was clickable",
                )
            logger.info("%s clicked %r for %r", _LOG_PREFIX, clicked, title)
        except AtspiUnavailable:
            raise
        except Exception as exc:
            logger.exception("%s drive failed for %r", _LOG_PREFIX, title)
            return InstallResult(success=False, error=str(exc), store="ankergames", game_id=slug)

        new_dir = self._wait_for_new_install(before, title)
        if new_dir is None:
            return InstallResult(
                success=False,
                error=f"timed out waiting for {title!r} to finish installing "
                      f"(waited {self._install_timeout_s}s)",
                store="ankergames", game_id=slug,
            )

        exe_path = find_executable(str(new_dir), title=title) or ""
        save_install_info(
            slug, title=title, install_path=str(new_dir), exe_path=exe_path,
            archive_path="",  # the real client manages its own archive lifecycle
        )
        return InstallResult(
            success=True, store="ankergames", game_id=slug, install_path=str(new_dir),
            metadata={"exe_path": exe_path, "title": title, "via": "wrapper_atspi"},
        )

    # ── launcher lifecycle ───────────────────────────────────────────

    def _get_driver(self) -> AnkerGamesAtspiDriver:
        if self._driver is None:
            self._driver = AnkerGamesAtspiDriver()
        return self._driver

    def _is_launcher_running(self) -> bool:
        try:
            proc = subprocess.run(
                ["pgrep", "-u", str(os.getuid()), "-x", "anker-launcher"],
                capture_output=True, timeout=5, check=False,
            )
        except (subprocess.SubprocessError, OSError):
            return False
        return proc.returncode == 0

    def _ensure_launcher_running(self) -> None:
        if self._is_launcher_running():
            return
        if not self._appimage.is_file():
            raise AnkerGamesWrapperInstallError(
                f"real AnkerGames launcher not found at {self._appimage} — "
                "set stores.ankergames.launcher_appimage_path",
            )
        env = {**os.environ, **detect_display_env(), "APPIMAGE_EXTRACT_AND_RUN": "1"}
        subprocess.Popen(  # noqa: S603 — fixed local AppImage, not user input
            [str(self._appimage)],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + _LAUNCH_WAIT_S
        while time.monotonic() < deadline:
            if self._is_launcher_running():
                # Give WebKitGTK's accessibility bridge a moment past
                # process-start before the first AT-SPI query — see
                # AnkerGamesAtspiDriver.get_app's own retry/backoff for the
                # rest of this story; this is just "has it even forked yet".
                time.sleep(_LAUNCH_POLL_INTERVAL_S)
                return
            time.sleep(_LAUNCH_POLL_INTERVAL_S)
        raise AnkerGamesWrapperInstallError(
            f"real launcher did not start within {_LAUNCH_WAIT_S}s",
        )

    # ── UI navigation ────────────────────────────────────────────────

    def _navigate_to_game(self, driver: AnkerGamesAtspiDriver, title: str) -> None:
        """Browse → search *title* → open the matching result.

        Deep-linking via the ``ankergames://game/<slug>`` scheme the
        client's own strings advertise was tried and does not reach a
        fresh/second invocation the way a registered OS URL handler would
        (no ``x-scheme-handler`` MimeType is declared in the shipped
        ``.desktop` file) — search-and-click is the reliable path, not a
        shortcut skipped out of laziness.
        """
        driver.click_button_named("Browse")
        time.sleep(1.0)  # page transition before the search entry exists
        if not driver.set_search_text(title):
            # One retry: WebKitGTK's a11y tree can still be mid-rebuild
            # right after a route change, the same lazy-activation story
            # as AnkerGamesAtspiDriver.get_app's own retry/backoff.
            time.sleep(1.5)
            if not driver.set_search_text(title):
                raise AnkerGamesWrapperInstallError("could not find the search box")
        time.sleep(1.5)  # debounced search; the result list redraws async
        clicked = driver.click_button_containing(title)
        if clicked is None:
            raise AnkerGamesWrapperInstallError(f"no search result button matched {title!r}")
        time.sleep(1.0)  # page transition

    # ── completion detection ─────────────────────────────────────────

    def _snapshot_installed_dirs(self) -> set[str]:
        if not self._games_root.is_dir():
            return set()
        return {p.name for p in self._games_root.iterdir() if p.is_dir()}

    def _wait_for_new_install(self, before: set[str], title: str) -> Path | None:
        """Poll the real client's games root for a new, fully-written directory.

        A directory appearing is not enough — the client extracts into it
        over time, and only writes :data:`_INSTALLATION_MARKER` once it
        considers the install complete (the same file ``markers.py``'s
        sibling ``get_installed`` logic in other stores treats as the
        source of truth, here read from the vendor's own copy of it rather
        than ours).
        """
        deadline = time.monotonic() + self._install_timeout_s
        while time.monotonic() < deadline:
            if self._games_root.is_dir():
                now = {p.name for p in self._games_root.iterdir() if p.is_dir()}
                for name in now - before:
                    candidate = self._games_root / name
                    if (candidate / _INSTALLATION_MARKER).is_file():
                        return candidate
                # Also match by title prefix for the case where the
                # directory existed before (a previous partial attempt)
                # and this run only completes it.
                for name in now:
                    if name.startswith(title) and (self._games_root / name / _INSTALLATION_MARKER).is_file():
                        return self._games_root / name
            time.sleep(_POLL_INTERVAL_S)
        return None
