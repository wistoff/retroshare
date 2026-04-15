# retroshare

Sync ROMs from multiple sources to an R36S handheld running ArkOS over Wi-Fi, and push save data back the other way. Includes a Docker app that merges ROM libraries from multiple Samba shares into a single endpoint.

## Overview

**Two components:**

1. **Docker app** (runs on Unraid) — merges ROM folders from multiple Samba shares into one unified share via a web UI, plus a writable share that receives save data from handhelds
2. **Sync scripts** (run on R36S) — pull ROMs from the server to the handheld, and push save files back to the server's writable share

**Bidirectional sync:** ROMs flow server → R36S, save data flows R36S → server. Before a save is uploaded, the matching ROM is **promoted** into your own share — so a save file never exists on the server without the ROM it belongs to, even if that ROM originally came from a friend's read-only source.

## Docker App

### Prerequisites

- Unraid server with Docker support
- Remote Samba shares already mounted on Unraid (e.g. under `/mnt/remotes/`)

### Setup

1. Clone this repo on your Unraid server
2. Edit `docker-compose.yml` to add your source volume mounts under `/sources/`:
   ```yaml
   volumes:
     - /mnt/user/data:/sources/local:rw
     - /mnt/remotes/friend1:/sources/friend1:ro
     - /mnt/remotes/friend2:/sources/friend2:ro
     - /mnt/user/appdata/retroshare:/config
   ```
   **Your own share must be mounted `:rw`** — that's where promoted ROMs and uploaded saves land. Friends' shares stay `:ro` and are never written to.
3. Start the container:
   ```bash
   docker compose up -d --build
   ```

### Usage

1. Open the web UI at `http://<unraid-ip>:7868`
2. Add sources — for each, browse to the folder that contains system folders (e.g. `snes/`, `gba/`)
3. The app builds a merged directory using symlinks and shares it via Samba

Two Samba shares are exposed on the same port:

- `smb://<unraid-ip>:7867/roms` — read-only merged ROM tree (served from `/merged/`)
- `smb://<unraid-ip>:7867/saves` — writable save share, backed by `/sources/local/.saves/`. R36S devices push save files here.

On collision between sources, the local source **always wins** — a local copy of a game takes precedence over any friend's copy of the same file. This is what makes ROM promotion work: copying a file into `/sources/local/` and triggering a rebuild automatically repoints the merged symlink at the local copy.

### Ports

| Port | Service |
|------|---------|
| 7867 | SMB (`roms` read-only, `saves` writable) |
| 7868 | Web UI + HTTP API (including `/api/promote`) |

## R36S Sync Scripts

### Prerequisites

- **R36S** running [ArkOS](https://github.com/christianhaitian/arkos/wiki) (Ubuntu 19.10 arm64)
- **USB Wi-Fi dongle** — the R36S has no built-in Wi-Fi. Common compatible chipsets include RTL8188, RTL8192, and MT7601U.

### Setup

1. Copy the scripts to the `tools/` folder on the GAMES SD card:
   - `scripts/Setup ROM Sync.sh`
   - `scripts/Sync ROMs.sh`
   - `scripts/Sync Saves.sh`
2. Copy `scripts/romsync.cfg.example` to `tools/romsync.cfg` and edit it:
   ```
   SERVER_IP=192.168.178.101
   SHARE_NAME=roms
   ROM_PATH=
   SMB_PORT=7867
   ```
3. Insert the SD card into the R36S and boot into EmulationStation
4. Go to **Tools** and run **Setup ROM Sync** (one time — installs `smbclient`)
5. **Verify your RetroArch save location.** `Sync Saves.sh` expects save files next to the ROM, e.g. `/roms2/snes/<game>.srm`. Check `~/.config/retroarch/retroarch.cfg` on the R36S:
   ```
   savefiles_in_content_dir = "true"
   savestates_in_content_dir = "true"
   ```
   If these are `"false"`, your saves live in `~/.config/retroarch/saves/` and the push script will find nothing.

### Usage

From the EmulationStation **Tools** menu, run **Sync ROMs** to pull new ROMs from the server. Run **Sync Saves** to push save data back.

**Sync ROMs** connects to the server, discovers matching system folders, and syncs ROMs according to the configured mode. **ADD mode** (default) downloads only new files. **REPLACE mode** also deletes local ROM files not present on the server. EmulationStation restarts automatically when files are added or removed.

**Sync Saves** walks every system folder under `/roms2/` for `.srm`, `.sav`, and `.state*` files. For each save, it asks the server to promote the matching ROM into your own share (no-op if the ROM is already local), then uploads the save atomically to `//<server>/saves/<system>/<file>`. A local `savesync.state` file tracks mtimes so unchanged saves are skipped between runs. Local and remote saves are never deleted.

### Configuration

| Key | Description |
|-----|-------------|
| `SERVER_IP` | IP address of the server |
| `SHARE_NAME` | Samba share name for ROMs (default `roms`) |
| `ROM_PATH` | Subfolder within the share where system folders live (leave empty if at root) |
| `SMB_PORT` | SMB port (default 445, use 7867 for the Docker app) |
| `SYNC_MODE` | `ADD` (default) — only download new files; `REPLACE` — also delete local ROMs not on server |
| `SAVES_SHARE` | Writable Samba share name for save uploads (default `saves`) — used by `Sync Saves.sh` |
| `HTTP_PORT` | HTTP API port used for ROM promotion (default `7868`) — used by `Sync Saves.sh` |

## How it works

### Docker app

**Merger** (`merger.py`):
1. Sources are bind-mounted into the container at `/sources/` — the local source writable, friends' shares read-only
2. Scans each source for system folders (top-level subdirectories)
3. For each ROM file, queries [OpenVGDB](https://github.com/OpenVGDB/OpenVGDB) to get the canonical No-Intro name
4. Cleans the canonical name: strips verbose metadata (`(En,Fr,De,Es,It)`, publisher/date tags), keeps standard region codes (`(USA)`, `(Europe)`, etc.), capitalizes "The"
5. Extension handling: if the canonical name has an extension, use it; otherwise preserve the source file's extension (important for `.zip` containers)
6. Local source is moved to the front of the list — local-wins on collision
7. Creates a symlink tree at `/merged/system/filename`
8. Writes `/config/ownership.json` mapping each merged entry to the absolute source file that backs it — consumed by `/api/promote`
9. Samba shares `/merged/` as a guest-accessible read-only share

**Promotion** (`/api/promote`):
1. R36S calls `POST /api/promote` with `{"system", "rom"}` before uploading a save
2. Server reads `ownership.json` to find the source file backing that entry
3. If the source is already `/sources/local/`, no-op
4. Otherwise, copies the file to `/sources/local/<system>/<rom>` using `<dest>.tmp` → `fsync` → `rename` → `fsync` of the dir (atomic-on-rename, durable before the save is uploaded)
5. Triggers a rebuild — local-wins ordering makes the merged symlink automatically repoint at the new local copy

**Scraper** (`scraper.py`):
1. Walks the merged symlink tree
2. For each ROM, reads the symlink target to recover the original filename (with full metadata)
3. Attempts thumbnail download from [Libretro's CDN](https://thumbnails.libretro.com/):
   - Tries the clean canonical name first
   - Falls back to the original filename with its metadata
   - Strips all metadata, then tries base title + common Libretro region combos (`(Europe)`, `(Europe) (En,Fr,De,Es,It)`, `(USA)`, etc.)
   - Also tries `&` ↔ `And` variations
4. On a match, saves the PNG to `/config/thumbnails/` and records it in `/config/gamecache.json`
5. Rate-limited to 1 request/second to avoid hammering the CDN

**API** (`server.py`):
- Serves the merged ROM list and cached thumbnail metadata as JSON at `http://<ip>:7868/`

### Sync ROMs (R36S)
1. **Connect** — connects to the Samba share using `smbclient` with guest access
2. **Discover** — lists system folders on the server and matches them with folders on the console
3. **Sync** — downloads only files not already present on the console
4. **Restart** — restarts EmulationStation if new files were added

### Sync Saves (R36S)
1. **Scan** — walks `/roms2/<system>/` for `.srm`, `.sav`, and `.state*` files, skipping anything whose mtime matches the recorded value in `savesync.state`
2. **Promote** — for each changed save, finds every ROM in the same system folder whose stem matches the save, and calls `POST http://<server>:7868/api/promote` to copy each candidate into the server's local share
3. **Upload** — `smbclient put` the save to `//<server>/saves/<system>/<name>.tmp`, then `rename` to the final name (atomic on rename)
4. **Record** — on success, writes the new mtime to `savesync.state` so the next run skips it

Local and remote save files are **never deleted**. A promote failure aborts the upload for that save (it will retry next run). If no matching ROM is found for a save (e.g. the ROM was removed from every share), the save is still uploaded — losing it is worse than orphaning it.

## Good to know

- **Saves are never lost.** A save is only considered synced after the server has promoted its ROM into your local share *and* the atomic rename on the save upload completes. On any failure the local save stays untouched and is retried next run.
- **Friends never see your saves.** The `[saves]` share is backed by a directory inside your own local source. It has no path into friends' read-only mounts.
- **Friends never see promoted ROMs either.** Promotion copies a file into your local source — friends' shares are never written to.
- **ADD mode (default) never deletes files.** REPLACE mode only deletes ROM files (matched by extension) — saves, gamelists, and media are preserved.
- **Only matching systems are synced.** If a system folder exists on the server but not on the console (or vice versa), it is skipped.
- **Safe to run repeatedly.** Re-running either sync script is a no-op if nothing changed.
- **Dual SD card support.** The sync scripts auto-detect whether ROMs live at `/roms/` or `/roms2/`.
- **Config file is auto-detected:** `/roms2/tools/romsync.cfg` on dual-SD setups, `/roms/tools/romsync.cfg` on single-SD. Shared by both `Sync ROMs.sh` and `Sync Saves.sh`.
- **No kernel CIFS dependency.** Uses `smbclient` (userspace) instead of `mount -t cifs`.
