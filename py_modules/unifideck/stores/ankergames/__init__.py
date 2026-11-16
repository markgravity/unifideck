"""AnkerGames store connector.

PERSONAL-FORK NOTICE: ankergames.net is a third-party "repack" distributor —
observed downloads ship Steam DRM-crack tooling (ColdClientLoader,
SmartSteamEmu, CPY.ini) alongside the game files. This connector exists so
one account's own launcher traffic can be driven from Unifideck; it is not
intended to be proposed upstream (see stores/ankergames/README.md), and
whoever enables it is responsible for their own jurisdiction's law and for
AnkerGames' own terms of service.

Every endpoint this package calls was learned by reverse-engineering the
official "Anker Launcher" Tauri/Rust client (v0.1.30) with a local
reverse-mode mitmproxy, not from any published API document. AnkerGames can
change or break this at any time without notice.
"""

from .ankergames_store import AnkerGamesStore

__all__ = ["AnkerGamesStore"]
