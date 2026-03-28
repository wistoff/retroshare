# retroshare

Sync ROMs from a home server to an R36S handheld running ArkOS over Wi-Fi.

## Prerequisites

- **R36S** running [ArkOS](https://github.com/christianhaitian/arkos/wiki) (Ubuntu 19.10 arm64)
- **USB Wi-Fi dongle** — the R36S has no built-in Wi-Fi. Common compatible chipsets include RTL8188, RTL8192, and MT7601U. Check the [ArkOS wiki](https://github.com/christianhaitian/arkos/wiki) for a tested list.
- **Samba share** on your home server with ROMs organised as `<share>/<rom-path>/<system>/`. The default config expects `//192.168.178.101/data` with ROMs under `media/roms/<system>/`.

## Setup

1. Copy both scripts from `scripts/` to `/roms/tools/` on the R36S EASYROMS SD card partition:
   - `Setup ROM Sync.sh`
   - `Sync ROMs.sh`
2. Insert the SD card back into the R36S and boot into EmulationStation.
3. Open the **Tools** menu and run **Setup ROM Sync**.

The setup script installs `smbclient` (handling Ubuntu 19.10 EOL apt sources automatically), then creates a default config file at `/roms/tools/romsync.cfg` if one does not already exist. It is safe to run more than once.

## Usage

From the EmulationStation **Tools** menu, run **Sync ROMs**.

The script mounts the Samba share, discovers which system folders exist on both the server and the console, copies any new ROMs across, then unmounts the share. A summary of systems synced and files added is shown at the end.

## Configuration

The config file lives at `/roms/tools/romsync.cfg` on the SD card. You can edit it from a PC by mounting the EASYROMS partition directly.

```
SERVER_IP=192.168.178.101
SHARE_NAME=data
ROM_PATH=media/roms
```

| Key | Description |
|---|---|
| `SERVER_IP` | IP address of the home server hosting the Samba share |
| `SHARE_NAME` | Samba share name (the part after `//server/`) |
| `ROM_PATH` | Path inside the share where system folders live |

With the defaults above, the script expects ROMs at `//192.168.178.101/data/media/roms/<system>/`.

## How it works

1. **Connect** — the script connects to the Samba share using `smbclient` with guest access. It auto-detects the correct authentication method.
2. **Discover** — the script lists every subfolder under `<share>/<ROM_PATH>/` and keeps only those that also exist under `/roms/` on the console. This means only systems you already have set up are synced.
3. **Sync** — for each matching system, the script lists remote files and downloads only those not already present on the console. Downloads go to a temp directory first, then are moved into place.
4. **Cleanup** — temporary files are removed on exit, even if the script errors out.

## Good to know

- **No files are ever deleted from the console.** The script only adds new files.
- **Only matching systems are synced.** If a system folder exists on the server but not on the console (or vice versa), it is skipped entirely.
- **Safe to run repeatedly.** Re-running the sync after it completes is a no-op — nothing is transferred a second time.
- **Pre-flight checks.** Before syncing, the script verifies that `smbclient` is installed and that the server is reachable. Clear error messages are shown if anything is missing.
- **No kernel CIFS dependency.** Uses `smbclient` (userspace) instead of `mount -t cifs`, so it works regardless of which SMB protocol version your server uses.
