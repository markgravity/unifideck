"""AnkerGames library reading.

AnkerGames has no purchase/ownership model: ``GET /games`` is the whole
*catalog* (every title the site hosts, 2,213 at capture time), not what one
account owns. The closest thing to a per-account library is ``GET
/library`` — the games the user has explicitly added/adopted on the
website or in the launcher — which read back empty for the account used to
reverse-engineer this (see ``README.md``). This reader therefore follows
``/library``, the way every other store here returns *owned* games, not the
whole storefront; a user who wants to browse the full catalog does that on
ankergames.net itself.

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


class AnkerGamesFetchError(RuntimeError):
    """The library could not be read — unknown, not "owns nothing"."""


class AnkerGamesCatalog:
    """Reads ``/library`` and maps it to :class:`Game` records."""

    def __init__(self, auth: AnkerGamesAuth) -> None:
        self._auth = auth

    async def fetch(self) -> list[Game]:
        if not self._auth.is_authenticated():
            raise AnkerGamesFetchError("not authenticated")
        try:
            status, body = await self._auth.client.get("/library")
        except Exception as exc:
            raise AnkerGamesFetchError(str(exc)) from exc
        if status != 200 or not isinstance(body, dict):
            raise AnkerGamesFetchError(f"GET /library returned {status}: {body!r}")
        entries = body.get("data")
        if not isinstance(entries, list):
            raise AnkerGamesFetchError("GET /library: unexpected body shape")
        games: list[Game] = []
        for entry in entries:
            game = _map_entry(entry)
            if game is not None:
                games.append(game)
        logger.info("[AnkerGamesCatalog] %d game(s) in library", len(games))
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
