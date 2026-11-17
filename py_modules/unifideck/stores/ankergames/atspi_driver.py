"""Drives the real AnkerGames launcher's UI via the Linux accessibility bus.

Why this exists: AnkerGames' CDN gates every download behind a per-request
`X-AG-Proof` header whose value this project could not reverse-engineer (see
``README.md`` — static Ghidra analysis, live gdb breakpoints, and byte-exact
crate fingerprinting all failed to recover the algorithm). The official
"Anker Launcher" client (a Tauri/WebKitGTK app) computes it correctly every
time, because it has the real code. Rather than guess the header, this
module drives *that real client's own UI* — through AT-SPI (the same
accessibility bus screen readers use), not synthetic mouse clicks — so the
client does the one step only it can do, and :mod:`install` adopts whatever
it produces on disk afterward.

AT-SPI over pixel-coordinate automation: GTK/WebKitGTK exposes every button,
entry and list item by its accessible *name* and *role*, stable across
window size, theme and layout changes. A query like "the button named
'Download, 328 MB · direct'" cannot land on the wrong widget the way a
screen coordinate can.

Linux-desktop-only. Requires:
  * ``gi`` (PyGObject) with the ``Atspi`` 2.0 typelib — present on any
    desktop that ships GNOME's accessibility stack (true for SteamOS/
    Bazzite's KDE session too, which bundles ``at-spi2-core``).
  * The target app to actually be exposing its accessibility tree, which on
    WebKitGTK is lazy — the first query after launch can come back shallow;
    :func:`get_app` and the finder helpers retry with a short backoff.
"""

from __future__ import annotations

import shutil
import subprocess

import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[AnkerGames AT-SPI]"
_APP_NAME = "anker-launcher"
_FIND_RETRY_DELAYS = (0.0, 0.5, 1.0, 2.0, 2.0)  # seconds; lazy-a11y backoff


class AtspiUnavailable(RuntimeError):
    """Raised when the Atspi GI binding or the accessibility bus is missing.

    Callers should treat this the same as "the real client cannot be driven
    on this device" — e.g. a headless server, or a desktop with the
    accessibility bus disabled — and fail the install with a clear reason
    rather than a cryptic import error.
    """


def _load_atspi() -> Any:
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except Exception as exc:  # ImportError, ValueError (missing typelib), …
        raise AtspiUnavailable(
            "python3-gi with the Atspi 2.0 typelib is required to drive the "
            "real AnkerGames launcher (package `python3-gobject` + "
            "`at-spi2-core`, or distro equivalent)",
        ) from exc
    Atspi.set_timeout(3000, 0)
    return Atspi


def get_app(atspi: Any) -> Any | None:
    """The AnkerGames launcher's accessible application node, or ``None``.

    Searches the AT-SPI desktop's direct children for the app by process
    name (``anker-launcher``, the Rust binary's own name — stable across
    launcher versions, unlike its window title, which has carried the
    product name, the current game, and nothing at all across observed
    runs).
    """
    desktop = atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        try:
            child = desktop.get_child_at_index(i)
        except Exception:
            continue
        if child is not None and child.get_name() == _APP_NAME:
            return child
    return None


def find_all(
    node: Any, pred: Callable[[Any], bool], *, max_depth: int = 25,
) -> list[Any]:
    """Every descendant of *node* (inclusive) matching *pred*, depth-first.

    ``max_depth`` guards against the rare cyclic/self-referential AT-SPI
    node observed on some WebKitGTK versions; 25 comfortably exceeds this
    app's deepest observed real nesting (~14, through the webview's own
    frame → scroll pane → document → article → list → list item chain).
    """
    out: list[Any] = []
    _find_all_inner(node, pred, out, 0, max_depth)
    return out


def _find_all_inner(
    node: Any, pred: Callable[[Any], bool], out: list[Any], depth: int, max_depth: int,
) -> None:
    try:
        if pred(node):
            out.append(node)
    except Exception:
        pass
    if depth >= max_depth:
        return
    try:
        count = node.get_child_count()
    except Exception:
        return
    for i in range(count):
        try:
            child = node.get_child_at_index(i)
        except Exception:
            continue
        if child is not None:
            _find_all_inner(child, pred, out, depth + 1, max_depth)


def find_one(node: Any, pred: Callable[[Any], bool]) -> Any | None:
    """The first descendant matching *pred* in document order, or ``None``."""
    matches = find_all(node, pred, max_depth=25)
    return matches[0] if matches else None


def is_button_named(name: str) -> Callable[[Any], bool]:
    """Predicate: an accessible with role ``button`` and this exact name."""

    def _pred(node: Any) -> bool:
        return node.get_role_name() == "button" and (node.get_name() or "") == name

    return _pred


def is_button_containing(substring: str) -> Callable[[Any], bool]:
    """Predicate: a ``button`` whose name contains *substring* (case-sensitive).

    The launcher's button names carry live state (a download's current
    size/progress, a game's size, "direct" vs "torrent") that this project
    does not control — matching a stable *substring* like ``"Download, "``
    rather than the full name is what keeps a caller from breaking when the
    size or suffix changes between runs.
    """

    def _pred(node: Any) -> bool:
        return node.get_role_name() == "button" and substring in (node.get_name() or "")

    return _pred


class AnkerGamesAtspiDriver:
    """One drive-the-launcher session: connect, find, click, type, poll."""

    def __init__(self) -> None:
        self._atspi = _load_atspi()

    def get_app(self, *, retries: int = len(_FIND_RETRY_DELAYS)) -> Any:
        """The launcher's accessible app node; retries for WebKitGTK's lazy a11y init.

        Raises :class:`AtspiUnavailable` if the app never appears — either it
        is not running, or the accessibility bus cannot see it (e.g. running
        under a different user/session than this process).
        """
        last_delay = 0.0
        for i, delay in enumerate(_FIND_RETRY_DELAYS[:retries] or (0.0,)):
            if delay:
                time.sleep(delay)
            app = get_app(self._atspi)
            if app is not None:
                return app
            last_delay = delay
        raise AtspiUnavailable(
            f"{_LOG_PREFIX} AnkerGames launcher not visible on the accessibility "
            f"bus after {retries} attempts (last wait {last_delay}s) — is it running?",
        )

    def click_button_named(self, name: str) -> bool:
        """Find a button by exact name anywhere in the app and invoke it.

        Returns whether the click was *issued*, not whether the launcher
        acted on it the way the caller expected — callers confirm the real
        outcome by polling the tree or the filesystem afterward, the same
        way a human would look at the screen after clicking.
        """
        app = self.get_app()
        button = find_one(app, is_button_named(name))
        if button is None:
            logger.warning("%s button %r not found", _LOG_PREFIX, name)
            return False
        return bool(self._atspi.Action.do_action(button, 0))

    def click_button_containing(self, substring: str) -> str | None:
        """Click the first button whose name contains *substring*.

        Returns the button's full (matched) name on success, so the caller
        can log exactly what it clicked — useful since these names carry
        live state (see :func:`is_button_containing`).
        """
        app = self.get_app()
        button = find_one(app, is_button_containing(substring))
        if button is None:
            logger.warning("%s no button containing %r found", _LOG_PREFIX, substring)
            return None
        name = button.get_name()
        if not self._atspi.Action.do_action(button, 0):
            return None
        return name

    def set_search_text(self, text: str) -> bool:
        """Focus the game-search box via AT-SPI, then type *text* into it.

        Prefers AT-SPI's ``EditableText`` interface (writes straight into
        the widget's model — faster, and immune to focus races). The
        search box does not support it here: WebKitGTK exposes this
        particular ``<input>`` as role ``embedded`` wrapping a ``page``
        (observed name ``"Search games"``) with no ``EditableText``
        interface, one level of web-content embedding AT-SPI does not
        bridge the way it does for plain buttons/labels elsewhere in this
        same app. Falls back to AT-SPI's own ``Component.grab_focus`` (not
        a screen click — this asks the accessibility bus to focus the
        widget by object reference) followed by a keystroke simulation
        (``ydotool``) — the one place this driver synthesizes input rather
        than acting through the accessibility tree directly, and still
        entirely request-driven, not a pixel-coordinate click.
        """
        app = self.get_app()
        box = find_one(app, lambda n: n.get_role_name() == "entry")
        if box is not None:
            try:
                self._atspi.EditableText.set_text_contents(box, text)
                return True
            except Exception:
                logger.debug("%s EditableText failed on an 'entry' node; falling back", _LOG_PREFIX)

        box = find_one(app, lambda n: (n.get_name() or "") == "Search games")
        if box is None:
            logger.warning("%s no search box found (tried role=entry and name='Search games')", _LOG_PREFIX)
            return False
        try:
            self._atspi.Component.grab_focus(box)
        except Exception:
            logger.exception("%s grab_focus on the search box failed", _LOG_PREFIX)
            return False
        return _type_via_ydotool(text)

    def dump_tree(self, *, max_depth: int = 20) -> str:
        """The full accessible tree as indented text, for debugging/polling.

        Not meant for parsing by callers beyond simple substring checks
        (e.g. ``"Installed N"`` counters, a known error phrase) — the tree
        shape is whatever WebKitGTK's accessibility layer currently
        produces for this page, which shifts with the web app's own markup.
        """
        app = self.get_app()
        lines: list[str] = []
        self._dump_inner(app, lines, 0, max_depth)
        return "\n".join(lines)

    def _dump_inner(self, node: Any, lines: list[str], depth: int, max_depth: int) -> None:
        try:
            name = node.get_name()
            role = node.get_role_name()
        except Exception:
            return
        if name:
            lines.append("  " * depth + f"[{role}] {name!r}")
        if depth >= max_depth:
            return
        try:
            count = node.get_child_count()
        except Exception:
            return
        for i in range(count):
            try:
                child = node.get_child_at_index(i)
            except Exception:
                continue
            if child is not None:
                self._dump_inner(child, lines, depth + 1, max_depth)


def _type_via_ydotool(text: str) -> bool:
    """Type *text* into whatever currently has keyboard focus, via ydotool.

    Only used by :meth:`AnkerGamesAtspiDriver.set_search_text` for the one
    widget AT-SPI cannot write into directly (see its docstring) — this
    assumes :func:`AnkerGamesAtspiDriver.set_search_text`'s own
    ``grab_focus`` call already landed keyboard focus on the right field,
    so it clears any stale content (select-all, delete) before typing
    rather than appending to it.
    """
    if shutil.which("ydotool") is None:
        logger.warning("%s ydotool not found; cannot type into the search box", _LOG_PREFIX)
        return False
    try:
        subprocess.run(["ydotool", "key", "ctrl+a"], check=True, timeout=5)  # noqa: S603, S607
        subprocess.run(["ydotool", "key", "Delete"], check=True, timeout=5)  # noqa: S603, S607
        subprocess.run(  # noqa: S603, S607
            ["ydotool", "type", "--key-delay", "12", "--", text],
            check=True, timeout=15,
        )
        return True
    except (subprocess.SubprocessError, OSError):
        logger.exception("%s ydotool typing failed", _LOG_PREFIX)
        return False
