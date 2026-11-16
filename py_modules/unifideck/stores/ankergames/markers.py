"""AnkerGames install markers — adapted from ``stores/gamevault/markers.py``.

One JSON file per game (keyed by slug) under
``~/.local/share/unifideck/ankergames_installed/``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[AnkerGames markers]"


def _marker_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "unifideck" / "ankergames_installed"


def _marker_path(slug: str) -> Path:
    return _marker_dir() / f"{slug}.json"


def save_install_info(
    slug: str, *, title: str, install_path: str, exe_path: str, archive_path: str = "",
) -> None:
    try:
        _marker_dir().mkdir(parents=True, exist_ok=True)
        _marker_path(slug).write_text(json.dumps({
            "game_id": slug,
            "title": title,
            "install_path": install_path,
            "exe_path": exe_path,
            "archive_path": archive_path,
        }, indent=2))
    except OSError:
        logger.exception("%s could not save marker for %s", _LOG_PREFIX, slug)


def load_install_info(slug: str) -> dict[str, Any] | None:
    p = _marker_path(slug)
    try:
        if p.exists():
            loaded = json.loads(p.read_text())
            if isinstance(loaded, dict):
                return loaded
    except (OSError, ValueError) as exc:
        logger.debug("%s could not read marker for %s: %s", _LOG_PREFIX, slug, exc)
    return None


def remove_install_info(slug: str) -> None:
    p = _marker_path(slug)
    try:
        if p.exists():
            p.unlink()
    except OSError as exc:
        logger.warning("%s could not remove marker for %s: %s", _LOG_PREFIX, slug, exc)


def load_all_install_info() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    marker_dir = _marker_dir()
    if not marker_dir.exists():
        return result
    for f in marker_dir.glob("*.json"):
        try:
            loaded = json.loads(f.read_text())
        except (OSError, ValueError) as exc:
            logger.warning("%s skipping unreadable marker %s: %s", _LOG_PREFIX, f.name, exc)
            continue
        if isinstance(loaded, dict):
            result[f.stem] = loaded
    return result
