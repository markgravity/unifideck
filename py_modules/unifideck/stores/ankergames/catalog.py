"""AnkerGames library reading.

AnkerGames has no purchase/ownership model: ``GET /games`` is the whole
*catalog* (every title the site hosts, 2,213 at capture time), not what one
account owns. ``GET /library`` — the games a user explicitly added/adopted
on the website or in the launcher — read back empty for the account used to
reverse-engineer this (see ``README.md``), and is this store's actual
"owned" list, but an empty list there means Unifideck's library view has
nothing to show an "Install" button on, same problem GameVault's self-hosted
catalog would have if it only showed owned games. GameVault's own
``local_catalog.py`` resolves this by treating "every game in the vault" as
the library; this store mirrors that choice and reads the full ``/games``
catalog instead of ``/library`` — anyone signed in can download anything,
so there is no "ownership" to gate a browsable list behind.

Mirrors ``stores/gamevault/library.py``'s split: a thin ``fetch`` that
raises rather than truncates on failure, kept separate from the
install-status overlay so the raise-never-truncate rule lives in exactly
one place for this store too.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from unifideck.core.types import Game

if TYPE_CHECKING:
    from .auth import AnkerGamesAuth

logger = logging.getLogger(__name__)

STORE_NAME = "ankergames"

#: Page size for ``GET /games`` — the pagination style observed uses
#: ``meta.next_cursor``, not an offset, so this just bounds how many round
#: trips a full-catalog fetch costs (2,213 games / 200 per page ≈ 12 pages).
_PAGE_SIZE = 200

#: Hard stop on pagination round trips, so a server bug that never clears
#: ``next_cursor`` can't loop forever (12 pages observed at capture time；
#: 50 leaves generous headroom for catalog growth).
_MAX_PAGES = 50


class AnkerGamesFetchError(RuntimeError):
    """The catalog could not be read — unknown, not "catalog is empty"."""


class AnkerGamesCatalog:
    """Reads the full ``/games`` catalog and maps it to :class:`Game` records."""

    def __init__(self, auth: AnkerGamesAuth) -> None:
        self._auth = auth

    async def fetch(self) -> list[Game]:
        if not self._auth.is_authenticated():
            raise AnkerGamesFetchError("not authenticated")
        games: list[Game] = []
        cursor: str | None = None
        for _ in range(_MAX_PAGES):
            params: dict[str, Any] = {"per_page": _PAGE_SIZE}
            if cursor:
                params["cursor"] = cursor
            try:
                status, body = await self._auth.client.get("/games", params=params)
            except Exception as exc:
                raise AnkerGamesFetchError(str(exc)) from exc
            if status != 200 or not isinstance(body, dict):
                raise AnkerGamesFetchError(f"GET /games returned {status}: {body!r}")
            entries = body.get("data")
            if not isinstance(entries, list):
                raise AnkerGamesFetchError("GET /games: unexpected body shape")
            for entry in entries:
                game = _map_entry(entry)
                if game is not None:
                    games.append(game)
            meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
            cursor = meta.get("next_cursor")
            if not cursor or not entries:
                break
        logger.info("[AnkerGamesCatalog] %d game(s) in catalog", len(games))
        return games

    async def fetch_detail(self, slug: str) -> dict[str, Any] | None:
        """``GET /games/{slug}`` — carries ``version.file_id`` for downloads."""
        status, body = await self._auth.client.get(f"/games/{slug}")
        if status != 200 or not isinstance(body, dict):
            logger.warning("[AnkerGamesCatalog] GET /games/%s returned %s", slug, status)
            return None
        data = body.get("data")
        return data if isinstance(data, dict) else None


def _map_entry(entry: Any) -> Game | None:
    """One ``/library`` row → a :class:`Game`.

    The exact row shape for a populated library was not observed (the test
    account's was empty) — this reads the same field names ``/games/{slug}``
    uses, since a library row is documented nowhere else; adjust here if a
    populated account shows a different shape.
    """
    if not isinstance(entry, dict):
        return None
    game = entry.get("game") if isinstance(entry.get("game"), dict) else entry
    slug = game.get("slug")
    title = game.get("title")
    if not isinstance(slug, str) or not isinstance(title, str):
        return None
    size_gb = game.get("size_gb")
    version = game.get("version") if isinstance(game.get("version"), dict) else {}
    return Game(
        app_id=0,  # filled in later by SyncService, per core.types.Game
        store=STORE_NAME,
        store_game_id=slug,
        title=title,
        icon_url=game.get("poster"),
        hero_url=game.get("cover"),
        tags=[t for t in (game.get("genres") or []) if isinstance(t, str)],
        metadata={
            "size_gb": size_gb if isinstance(size_gb, int | float) else None,
            "file_id": version.get("file_id"),
            "build_id": version.get("build_id"),
            "version_label": version.get("label"),
        },
    )
