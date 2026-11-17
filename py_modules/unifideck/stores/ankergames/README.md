# AnkerGames connector — personal-fork notes

This store was added in a personal fork (`markgravity/unifideck`) and is
**not intended to be proposed upstream** to `mubaraknumann/unifideck`.
AnkerGames is a third-party "repack" distributor, not a game store: observed
downloads bundle Steam DRM-crack tooling (`ColdClientLoader.ini`,
`SmartSteamEmu.ini`, `CPY.ini`, `steamclient_loader_x64.exe`) alongside the
game files. That is a different legal and project-fit category from the
stores Unifideck otherwise integrates (Epic, GOG, Amazon, Ubisoft,
Battle.net, Microsoft, itch.io, GameVault, each a legitimate storefront or a
user's own server), and `CONTRIBUTING.md` requires discussing new
integrations with the maintainer before opening a PR — this one would not
clear that discussion.

## How the API was learned

Reverse-engineered from the official "Anker Launcher" client (Tauri/Rust,
v0.1.30, Linux x86_64 AppImage), 2026-11-16:

1. Extracted the AppImage's embedded squashfs and grepped the Rust binary's
   string table for route fragments, header names and the auth field names
   (`device_code`, `user_code`, `verification_uri_complete`, `hardware_hash`).
2. The binary honours an `ANKER_API_BASE` environment variable. Pointing it
   at `mitmdump --mode reverse:https://ankergames.net --listen-port 9000`
   made the client talk to the local port in plaintext, which sidesteps the
   client's embedded/rustls certificate trust store entirely — no CA
   installation, no TLS interception needed.
3. Logged in once for real (device-code pairing, approved in a browser
   already signed in to the account used for this research) and captured
   one real install (a 154 MB free game, "Slotbound") end to end.

No part of the client's Rust source was read (it is closed-source and
stripped) — every field name and status code below was observed on the
wire.

## Endpoints (base `https://ankergames.net/api/v1`)

| Method | Path | Notes |
|---|---|---|
| POST | `/device/code` | `{name, platform, app_version, hardware_hash}` → `{device_code, user_code, verification_uri, verification_uri_complete, expires_in, interval}` |
| POST | `/device/poll` | `{device_code}` → `202 {"status":"pending"}` or `200 {"status":"approved","token":"<id>\|<secret>","expires_at","device":{...}}` |
| POST | `/device/key` | `Authorization: Bearer <token>`, `{"public_key": "<base64url ed25519>"}` → `204`. Registers this device's signing key (seen embedded as `dk` in later signed download URLs). |
| GET | `/me` | profile, subscription, device/download quota, `session.refresh_url` |
| GET | `/auth/refresh` | refresh URL from `/me`, not independently confirmed |
| GET | `/client/update?version=&platform=&arch=&channel=` | update check, minisign-signed |
| POST | `/hardware/resolve` | `{gpu, cpu, vram_mb, ram_mb}` → benchmark score (cosmetic, not required for install) |
| GET | `/home` | homepage sections |
| GET | `/games?per_page=200&updated_since=` or `&sync_until=&cursor=` | catalog sync, two pagination styles observed |
| GET | `/games/{slug}` | full detail incl. `version.file_id`, `version.build_id`, `size_gb` |
| GET | `/games/{slug}/achievements` | |
| GET | `/games/{slug}/reviews` | |
| GET | `/library` | owned/library entries for the account |
| GET | `/saves`, `/saves/{id}/history` | cloud-save metadata (account had none to inspect) |
| GET | `/notifications?per_page=` | |
| **POST** | **`/downloads/{file_id}/authorize`** | `Authorization: Bearer`, header `X-AG-Capabilities: device-proof`. `425 {"error","reason":"wait_required","retry_after","wait_seconds"}` until the server-side throttle clears (observed 5s), then `200` with the real payload below. |

`downloads/{file_id}/authorize` success body:

```json
{
  "url": "https://tunnel5.dlproxy.uk/download/ag1.<base64url-json>.<sig>",
  "file": {"id": 7681, "label": "Direct", "artifact_id": "...", "build_id": "...", "artifact_type": "full"},
  "game": {"id": 4094, "slug": "slotbound", "title": "Slotbound", "size_gb": 0.15, "build_id": "..."},
  "transfer": {"connections": 4, "speed_limit_mbps": 0, "concurrent_downloads": 1, "proof": "device"}
}
```

`file_id` is `GET /games/{slug}` → `data.version.file_id`. The `url` is a
short-lived, IP- and device-key-bound signed link to a CDN edge
(`*.dlproxy.uk`); its payload (base64url between the two dots) decodes to a
JSON object with `dk` (this device's registered public key), `i`/`x`
(issued/expires, Unix seconds), `ip` (the requesting IPv6 /64) and other
opaque fields — never generate or edit this token, just fetch and use it
as-is, promptly (observed expiry ~6 hours, not load-bearing — treat it as
single-use and re-authorize per download attempt).

The downloaded file is a plain `.zip` (observed: `Slotbound-AnkerGames.zip`,
`Content-Disposition` filename), extracted into
`<title>/` with a cracked Steam-emu runtime already inside — there is no
separate "install" step on AnkerGames' side, unlike a real storefront's
installer.

## CONFIRMED GAP (2026-11-16): the download GET needs a device-proof signature

`install_game` currently fails at the final fetch with a real, loud error —
it does not silently produce a broken download. Live-tested twice (from the
Mac and from the paired device itself, Bazzite, same IPv6 /64 as the
token's `ip` field, so IP binding is not the cause):

```
GET <the authorized url>  → 403
"This download link was made for another device. Go back to the game
page and start the download again."
```

The signed-URL payload (base64url between the two dots, see below) carries
`dk` (this device's already-registered public key) but the CDN still
rejects a plain `GET` with no further proof. The binary's string table has
an `X-AG-Proof` header name (seen in the extracted strings, never captured
on the wire because the real app's download goes through its own bundled
aria2c, which this reverse-engineering pass did not instrument at the
HTTP-header level) — the missing piece is almost certainly a per-request
signature, made with the ed25519 **private** key this connector already
generates and registers (`auth._load_or_create_device_key`), that the real
client attaches to the download request and this connector does not yet
compute. What exactly gets signed (the `j` nonce? the `c` blob? the full
URL? a timestamp?) was not determined — the only way to find out from here
is disassembling the real binary around its download/aria2c-handoff code
(Ghidra/Hopper on `usr/bin/anker-launcher` inside the AppImage's squashfs),
which is out of scope for this reverse-engineering pass.

Everything before this point is confirmed working end-to-end against the
real server: pairing, `/me`, catalog, `/games/{slug}`, and
`/downloads/{file_id}/authorize` (including its `425` throttle) all return
exactly what `README.md` documents. `install_game` surfaces this specific
403 and its body verbatim in `InstallResult.error`, so a future fix is a
one-function change in `download.py`'s `_authorize`/`acquire` — add
whatever header the real signature turns out to be — not a rewrite.

## RESOLVED (2026-11-17): `install_game` works — via a wrapper install, not the header

The `X-AG-Proof` algorithm itself was never recovered. Three more
independent passes after the gap above — live `gdb` breakpoints on the
running client (confirmed the value is *not* the authorize URL, *not* an
Ed25519 signature of it, and *not* an IP-exactness check; all three tested
against the real server and all three 403), byte-exact fingerprinting of a
locally-rebuilt `ring 0.17.14` against the stripped target binary
(confirmed real Ed25519/X25519/SHA-512 code exists and roughly where, but
the two call sites reachable from the fingerprinted primitives turned out
to be TLS's own X25519 key exchange and a PKCS8-key-load consistency
check, not a per-request signer), and SHA-512-constant-table call-graph
tracing — all failed to pin down the exact bytes. See the project's session
history for the full trace; it is not reproduced here because none of it
leads anywhere actionable.

`install_game` therefore does not use the direct-API path
(`_install_game_direct`, kept as a truthfully-failing reference) by
default. It instead drives the **real** Anker Launcher client — which
computes the header correctly, because it has the real source — through
its own UI, the same shape as Unifideck's other wrapper stores
(Battle.net, Ubisoft): the vendor's own client does the one step only it
can do, and this connector adopts whatever lands on disk afterward.

Implementation (`atspi_driver.py` + `wrapper_install.py` +
`wrapper_env.py`):

1. Launch the real client (`stores.ankergames.launcher_appimage_path`) if
   it is not already running, borrowing `WAYLAND_DISPLAY`/`DISPLAY`/
   `DBUS_SESSION_BUS_ADDRESS` from a live desktop-session process
   (`plasmashell`, `kwin_wayland`, `gamescope-session`, or `steam`) the
   same way `stores/ubisoft/binaries.py` borrows a display for Wine.
2. Drive it through **AT-SPI** (`org.a11y.Atspi`, the same accessibility
   bus screen readers use) — find the "Browse" button, the search box,
   the matching result, the "Download"/"Install" button, by their
   accessible *name*, not a screen coordinate. This is what makes it
   reliable across window size/theme/layout: a query like "the button
   named `'Download, 328 MB · direct'`" cannot land on the wrong widget
   the way `x,y` can.
3. One widget resists AT-SPI's `EditableText` interface — the search box
   is exposed as role `embedded` wrapping an empty `page` (a web-content
   embedding boundary AT-SPI does not bridge the way it does plain
   buttons elsewhere in the same app). For that one step only, the driver
   focuses the widget via AT-SPI (`Component.grab_focus` — still by
   object reference, not a click) and then types through `ydotool`,
   rather than falling back to coordinate-based automation.
4. Poll the real client's own games directory
   (`stores.ankergames.launcher_games_root`) for a new subdirectory
   carrying its `.anker-installation.json` completion marker, then
   resolve an executable and write this connector's *own* marker
   (`markers.py`) — so `get_installed_path`/`get_game_size`/uninstall
   never need to know an install happened this way rather than through
   `download.py`.

Verified live end-to-end on 2026-11-17 (Bazzite, KDE desktop session):
`AnkerGamesStore.install_game("dungeonbound")` → real "Dungeonbound"
files landing on disk, a correct `exe_path`
(`.../Dungeonbound/dungeonbound.exe`), and `get_library()` reporting it
installed afterward — through this connector's own code, not a manual
click in the official client.

Needs a Linux desktop session this process can reach (AT-SPI + a
compositor) — this path does not work headless, same constraint every
other wrapper store in this project already has for its vendor client.

## What this connector does NOT cover

- Library/ownership semantics: AnkerGames has no purchase/ownership model —
  every catalogued game is downloadable by any signed-in account, so
  `get_library` returns the account's **adopted/added** list from `/library`
  (which read empty `{"data":[]}` for the throwaway test account), not "every
  game that exists" (`/games` is the catalog, not the library).
- Achievements/reviews/saves RPCs — endpoints exist and are noted above, not
  wired into `StoreBase`.
- `check_for_updates` — returns `[]` (not implemented): would need comparing
  each installed marker's `build_id` against `GET /games/{slug}` per game.
