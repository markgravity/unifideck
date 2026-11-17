"""Display-session env detection for launching the real AnkerGames client.

The plugin runs headless under ``plugin_loader`` (Decky's own process,
started by systemd with no desktop session attached), so spawning any GUI
binary from here needs a borrowed ``WAYLAND_DISPLAY``/``DISPLAY`` and
``DBUS_SESSION_BUS_ADDRESS`` or it either fails outright or hangs waiting on
a compositor that was never connected. ``stores/ubisoft/binaries.py``
solves the identical problem for Wine/Proton processes by reading
``/proc/<pid>/environ`` off the live Steam or gamescope-session process;
this is that same technique, written locally rather than imported across
stores (``shared/store_base.py``'s self-containment note — this store has
no Wine prefix to share, so importing Ubisoft's resolver for four lines of
``/proc` reading would be a bigger dependency than the logic it replaces).
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[AnkerGames wrapper-env]"

#: What a GUI process on this session needs; checked against our own
#: environment first, then borrowed from a running desktop process.
_DISPLAY_ENV_VARS = (
    "XDG_RUNTIME_DIR",
    "WAYLAND_DISPLAY",
    "DISPLAY",
    "DBUS_SESSION_BUS_ADDRESS",
    "XAUTHORITY",
)

#: Processes worth borrowing a live desktop session from, in preference
#: order — ``plasmashell``/``kwin_wayland`` for a KDE desktop session (this
#: store's primary target: Bazzite's desktop mode), ``gamescope-session``
#: and ``steam`` as fallbacks for a SteamOS-style compositor session.
_SESSION_PROCESS_NAMES = ("plasmashell", "kwin_wayland", "gamescope-session", "steam")


def detect_display_env() -> dict[str, str]:
    """Env vars to merge into a GUI child's environment.

    Tries this process's own environment first (already correct when the
    plugin host itself has a desktop session — e.g. running under a user
    systemd unit with ``XDG_RUNTIME_DIR`` inherited); falls back to reading
    the display vars out of a running desktop-session process's own
    ``/proc/<pid>/environ``.
    """
    result = _collect_from_self()
    if result.get("WAYLAND_DISPLAY") or result.get("DISPLAY"):
        return result
    if _fill_from_session_process(result):
        return result
    logger.warning(
        "%s no WAYLAND_DISPLAY/DISPLAY found in this process or any of %s; "
        "the real launcher will likely fail to open a window",
        _LOG_PREFIX, _SESSION_PROCESS_NAMES,
    )
    return result


def _collect_from_self() -> dict[str, str]:
    return {v: os.environ[v] for v in _DISPLAY_ENV_VARS if os.environ.get(v)}


def _fill_from_session_process(result: dict[str, str]) -> bool:
    for proc_name in _SESSION_PROCESS_NAMES:
        for pid in _pgrep(proc_name):
            if _scan_pid_into(pid, result):
                logger.info(
                    "%s display env borrowed from PID %s (%s)",
                    _LOG_PREFIX, pid, proc_name,
                )
                return True
    return False


def _scan_pid_into(pid: str, result: dict[str, str]) -> bool:
    found = _read_proc_environ(pid, _DISPLAY_ENV_VARS)
    if not found:
        return False
    for k, v in found.items():
        result.setdefault(k, v)
    return bool(result.get("WAYLAND_DISPLAY") or result.get("DISPLAY"))


def _pgrep(process_name: str) -> list[str]:
    try:
        proc = subprocess.run(
            ["pgrep", "-u", str(os.getuid()), "-x", process_name],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    return [line.strip() for line in proc.stdout.strip().splitlines() if line.strip()]


def _read_proc_environ(pid: str, targets: tuple[str, ...]) -> dict[str, str]:
    try:
        env_bytes = Path(f"/proc/{pid}/environ").read_bytes()
    except (PermissionError, FileNotFoundError, OSError):
        return {}
    result: dict[str, str] = {}
    for entry in env_bytes.split(b"\x00"):
        decoded = entry.decode("utf-8", errors="replace")
        if "=" not in decoded:
            continue
        k, v = decoded.split("=", 1)
        if k in targets:
            result[k] = v
    return result
