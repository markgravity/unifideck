"""AnkerGamesBrowseRPCMixin — curated-category browse data for the QAM tab.

AnkerGames is the one store here with no real ownership model (see
``stores/ankergames/catalog.py``), so its Library tab shows the full
catalog flat. This mixin is the one RPC route that instead exposes the
site's own curated homepage sections (Spotlight, Trending, Most
Downloaded, ...) for a dedicated browse tab in the Quick Access panel —
a Steam-Store-like experience the Library tab's filter-only architecture
cannot render (see ``src/views/QuickAccessPanel.tsx``'s docstring on why
this lives in the QAM and not the Library).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class AnkerGamesBrowseRPCMixin:
    """One route: the AnkerGames store's curated home-page sections."""

    registry: Any

    async def get_ankergames_browse_sections(self) -> dict[str, list[dict[str, Any]]]:
        """Curated categories (Trending, Most Downloaded, ...) for the browse tab.

        ``{}`` when the store isn't registered or its own fetch failed — the
        store method already logs the real error; this route just guarantees
        a shape the frontend can always render without a null check.
        """
        store = self.registry.get_store("ankergames")
        if store is None:
            return {}
        return await store.get_browse_sections()
