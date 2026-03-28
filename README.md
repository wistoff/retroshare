# retroshare

Sync ROMs from multiple sources to an R36S handheld running ArkOS over Wi-Fi. Includes a Docker app that merges ROM libraries from multiple Samba shares into a single endpoint.

## Overview

**Two components:**

1. **Docker app** (runs on Unraid) — merges ROM folders from multiple Samba shares into one unified share via a web UI
2. **Sync scripts** (run on R36S) — pull ROMs from the server to the handheld console

## Docker App

### Prerequisites

- Unraid server with Docker support
- Remote Samba shares already mounted on Unraid (e.g. under `/mnt/remotes/`)

### Setup

1. Clone this repo on your Unraid server
2. Edit `docker-compose.yml` to add your source volume mounts under `/sources/`:
   ```yaml
   volumes:
     - /mnt/user/data:/sources/local:ro
     - /mnt/remotes/friend1:/sources/friend1:ro
     - /mnt/remotes/friend2:/sources/friend2:ro
     - /mnt/user/appdata/retroshare:/config
   ```
3. Start the container:
   ```bash
   docker compose up -d --build
   ```

### Usage

1. Open the web UI at `http://<unraid-ip>:7868`
2. Add sources — for each, browse to the folder that contains system folders (e.g. `snes/`, `gba/`)
3. The app builds a merged directory using symlinks and shares it via Samba

The merged share is available at `smb://<unraid-ip>:7867/roms`.

### Ports

| Port | Service |
|------|---------|
| 7867 | SMB (merged ROM share) |
| 7868 | Web UI |

## R36S Sync Scripts

### Prerequisites

- **R36S** running [ArkOS](https://github.com/christianhaitian/arkos/wiki) (Ubuntu 19.10 arm64)
- **USB Wi-Fi dongle** — the R36S has no built-in Wi-Fi. Common compatible chipsets include RTL8188, RTL8192, and MT7601U.

### Setup

1. Copy the scripts to the `tools/` folder on the GAMES SD card:
   - `scripts/Setup ROM Sync.sh`
   - `scripts/Sync ROMs.sh`
2. Copy `scripts/romsync.cfg.example` to `tools/romsync.cfg` and edit it:
   ```
   SERVER_IP=192.168.178.101
   SHARE_NAME=roms
   ROM_PATH=
   SMB_PORT=7867
   ```
3. Insert the SD card into the R36S and boot into EmulationStation
4. Go to **Tools** and run **Setup ROM Sync** (one time — installs `smbclient`)

### Usage

From the EmulationStation **Tools** menu, run **Sync ROMs**.

The script connects to the server, discovers matching system folders, and downloads any new ROMs. EmulationStation restarts automatically when new files are added.

### Configuration

| Key | Description |
|-----|-------------|
| `SERVER_IP` | IP address of the server |
| `SHARE_NAME` | Samba share name |
| `ROM_PATH` | Subfolder within the share where system folders live (leave empty if at root) |
| `SMB_PORT` | SMB port (default 445, use 7867 for the Docker app) |

## How it works

### Docker app
1. Sources are bind-mounted read-only into the container at `/sources/`
2. The merger scans each source for system folders and creates a symlink tree at `/merged/`
3. Overlapping system folders are merged — duplicate filenames use first-source-wins
4. Samba shares `/merged/` as a guest-accessible read-only share

### Sync script
1. **Connect** — connects to the Samba share using `smbclient` with guest access
2. **Discover** — lists system folders on the server and matches them with folders on the console
3. **Sync** — downloads only files not already present on the console
4. **Restart** — restarts EmulationStation if new files were added

## Good to know

- **No files are ever deleted from the console.** The script only adds new files.
- **Only matching systems are synced.** If a system folder exists on the server but not on the console (or vice versa), it is skipped.
- **Safe to run repeatedly.** Re-running the sync is a no-op if nothing changed.
- **Dual SD card support.** The sync script auto-detects whether ROMs live at `/roms/` or `/roms2/`.
- **No kernel CIFS dependency.** Uses `smbclient` (userspace) instead of `mount -t cifs`.
