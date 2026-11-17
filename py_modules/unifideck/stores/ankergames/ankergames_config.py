"""Config section reader for ``stores.ankergames`` in ``defaults/config.json``.

Kept as its own tiny module, the way ``microsoft_config.py`` does it, so the
store/auth/catalog/install modules all read one already-defaulted object
instead of five copies of ``config.get("stores.ankergames", {})``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from unifideck.config import ConfigManager

_DEFAULT_BASE_URL = "https://ankergames.net/api/v1"
_DEFAULT_LAUNCHER_VERSION = "0.1.30"
_DEFAULT_SESSION_FILE = "~/.config/unifideck/ankergames_session.json"
_DEFAULT_DEVICE_KEY_FILE = "~/.config/unifideck/ankergames_device_key.json"
_DEFAULT_DOWNLOAD_DIR = "~/.local/share/unifideck/ankergames_downloads"
_DEFAULT_INSTALL_ROOT = "~/Games/AnkerGames"
_DEFAULT_DEVICE_NAME = "unifideck"
_DEFAULT_POLL_TIMEOUT_S = 600
_DEFAULT_AUTHORIZE_MAX_WAIT_S = 30
_DEFAULT_LAUNCHER_APPIMAGE_PATH = "~/Applications/AnkerLauncher-0.1.30-x86_64.AppImage"
_DEFAULT_LAUNCHER_GAMES_ROOT = "~/Downloads/AnkerGames/Games"
_DEFAULT_WRAPPER_INSTALL_TIMEOUT_S = 1800


@dataclass(frozen=True)
class AnkerGamesConfig:
    """Resolved ``stores.ankergames`` section, every field defaulted."""

    base_url: str = _DEFAULT_BASE_URL
    launcher_version: str = _DEFAULT_LAUNCHER_VERSION
    session_file: str = _DEFAULT_SESSION_FILE
    device_key_file: str = _DEFAULT_DEVICE_KEY_FILE
    download_dir: str = _DEFAULT_DOWNLOAD_DIR
    default_install_root: str = _DEFAULT_INSTALL_ROOT
    device_name: str = _DEFAULT_DEVICE_NAME
    poll_timeout_s: int = _DEFAULT_POLL_TIMEOUT_S
    authorize_max_wait_s: int = _DEFAULT_AUTHORIZE_MAX_WAIT_S
    # ── wrapper install (drives the real client; see wrapper_install.py) ──
    launcher_appimage_path: str = _DEFAULT_LAUNCHER_APPIMAGE_PATH
    launcher_games_root: str = _DEFAULT_LAUNCHER_GAMES_ROOT
    wrapper_install_timeout_s: int = _DEFAULT_WRAPPER_INSTALL_TIMEOUT_S

    @property
    def user_agent(self) -> str:
        """The exact UA string the real client sends.

        AnkerGames does not appear to gate on it (the ``/downloads/.../
        authorize`` throttle and the Bearer check are what actually guard
        the endpoints), but matching it is one fewer variable if that ever
        changes. See ``README.md`` for the full reverse-engineering notes.
        """
        return f"AnkerLauncher/{self.launcher_version} (+https://ankergames.net)"


def load_config(config: ConfigManager | None) -> AnkerGamesConfig:
    """Build an :class:`AnkerGamesConfig` from ``config.get("stores.ankergames")``."""
    raw: dict[str, Any] = (config.get("stores.ankergames") if config else None) or {}
    defaults = AnkerGamesConfig()
    return AnkerGamesConfig(
        base_url=raw.get("base_url", defaults.base_url),
        launcher_version=raw.get("launcher_version", defaults.launcher_version),
        session_file=raw.get("session_file", defaults.session_file),
        device_key_file=raw.get("device_key_file", defaults.device_key_file),
        download_dir=raw.get("download_dir", defaults.download_dir),
        default_install_root=raw.get(
            "default_install_root", defaults.default_install_root,
        ),
        device_name=raw.get("device_name", defaults.device_name),
        poll_timeout_s=int(raw.get("poll_timeout_s", defaults.poll_timeout_s)),
        authorize_max_wait_s=int(
            raw.get("authorize_max_wait_s", defaults.authorize_max_wait_s),
        ),
        launcher_appimage_path=raw.get(
            "launcher_appimage_path", defaults.launcher_appimage_path,
        ),
        launcher_games_root=raw.get(
            "launcher_games_root", defaults.launcher_games_root,
        ),
        wrapper_install_timeout_s=int(
            raw.get("wrapper_install_timeout_s", defaults.wrapper_install_timeout_s),
        ),
    )
