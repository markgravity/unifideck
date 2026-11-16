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
