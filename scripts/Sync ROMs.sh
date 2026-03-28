#!/bin/bash
# Sync ROMs
# Uses smbclient to list and download new ROMs from a Samba share.
# Skips files already present on the console (by filename).

CONFIG_FILE="/roms/tools/romsync.cfg"
TEMP_DIR="/tmp/romsync_tmp"

# Auto-detect ROM destination: /roms2/ (GAMES SD card) or /roms/ (single SD)
if [ -d "/roms2" ] && [ "$(ls -A /roms2 2>/dev/null)" ]; then
    LOCAL_ROMS="/roms2"
else
    LOCAL_ROMS="/roms"
fi

SYSTEMS_SYNCED=0
FILES_TRANSFERRED=0
ERRORS=0

echo "=== ROM Sync ==="
echo ""

# ---------------------------------------------------------------------------
# Cleanup trap — remove temp dir on exit
# ---------------------------------------------------------------------------
cleanup() {
    if [ -d "$TEMP_DIR" ]; then
        rm -rf "$TEMP_DIR" 2>/dev/null
    fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# 1. Read config
# ---------------------------------------------------------------------------
if [ ! -f "$CONFIG_FILE" ]; then
    echo "ERROR: Config file not found: $CONFIG_FILE"
    echo "       Please run 'Setup ROM Sync' first."
    sleep 5
    exit 1
fi

# shellcheck source=/dev/null
source "$CONFIG_FILE"

if [ -z "$SERVER_IP" ] || [ -z "$SHARE_NAME" ]; then
    echo "ERROR: Config file is incomplete."
    echo "       Expected: SERVER_IP, SHARE_NAME"
    echo "       File: $CONFIG_FILE"
    sleep 5
    exit 1
fi

# Defaults for optional config values
ROM_PATH="${ROM_PATH:-}"
SMB_PORT="${SMB_PORT:-445}"

# Build port flag for smbclient
SMB_PORT_FLAG=""
if [ "$SMB_PORT" != "445" ]; then
    SMB_PORT_FLAG="-p $SMB_PORT"
fi

echo "Server : $SERVER_IP"
echo "Share  : $SHARE_NAME"
echo "Port   : $SMB_PORT"
echo "Path   : $ROM_PATH"
echo "Dest   : $LOCAL_ROMS"
echo ""

# ---------------------------------------------------------------------------
# 2. Pre-flight checks
# ---------------------------------------------------------------------------
echo "--- Pre-flight checks ---"

if ! command -v smbclient &>/dev/null; then
    echo "ERROR: smbclient not found. Run 'Setup ROM Sync' to install it."
    sleep 5
    exit 1
fi
echo "  [OK] smbclient found"

echo "  [OK] Pre-flight checks passed"
echo ""

# ---------------------------------------------------------------------------
# smbclient helpers
# ---------------------------------------------------------------------------

# SMB_AUTH: try -N (no password / guest) first; fall back to -U guest%
# We probe once at startup and reuse the working auth flag.
SMB_AUTH=""

_probe_auth() {
    local share="//$SERVER_IP/$SHARE_NAME"
    if smbclient "$share" $SMB_PORT_FLAG -N -c "ls" &>/dev/null; then
        SMB_AUTH="-N"
    elif smbclient "$share" $SMB_PORT_FLAG -U "guest%" -c "ls" &>/dev/null; then
        SMB_AUTH="-U guest%"
    else
        return 1
    fi
}

# smb_ls <remote-path>
# Lists entries at <remote-path> on the share.
# Outputs lines of the form:  <type> <name>
#   type = "d" for directory, "f" for file
# Skips . and ..
smb_ls() {
    local remote_path="$1"
    local share="//$SERVER_IP/$SHARE_NAME"

    # smbclient ls output format:
    #   "  gba                                 D        0  Sat Mar 28 11:29:16 2026"
    # Parse with awk compatible with mawk (no POSIX character classes).
    smbclient "$share" $SMB_PORT_FLAG $SMB_AUTH -c "ls \"$remote_path/*\"" 2>/dev/null \
    | awk '
        # Skip blank lines
        /^[ \t]*$/ { next }
        # Skip smbclient status lines
        /^Domain=|^session|^Server|^Workgroup|^Unable/ { next }
        # Skip "blocks of size" summary line (starts with tabs/spaces then digits)
        /blocks of size/ { next }

        # Entry lines start with whitespace then the filename
        /^[ \t]/ {
            line = $0
            # Remove leading whitespace
            sub(/^[ \t]+/, "", line)

            # Match the attribute+size+date suffix
            # Using basic chars instead of POSIX classes for mawk compatibility
            if (match(line, /[ \t]+[ADHNSRadhnsr]+[ \t]+[0-9]+[ \t]+[A-Z][a-z][a-z] [A-Z][a-z][a-z] [0-9 ][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9] [0-9][0-9][0-9][0-9]/)) {
                name = substr(line, 1, RSTART - 1)
                rest = substr(line, RSTART)
                # Extract attribute chars
                sub(/^[ \t]+/, "", rest)
                split(rest, parts, " ")
                attr_field = parts[1]

                # Skip . and ..
                if (name == "." || name == "..") next

                # Classify
                if (index(attr_field, "D") > 0) {
                    print "d " name
                } else {
                    print "f " name
                }
            }
        }
    '
}

# smb_get <remote-dir> <filename> <local-dest-path>
# Downloads a single file from the share.
smb_get() {
    local remote_dir="$1"
    local filename="$2"
    local local_dest="$3"
    local share="//$SERVER_IP/$SHARE_NAME"

    smbclient "$share" $SMB_PORT_FLAG $SMB_AUTH \
        -c "cd \"$remote_dir\"; get \"$filename\" \"$local_dest\"" \
        2>/dev/null
}

# ---------------------------------------------------------------------------
# 3. Probe authentication
# ---------------------------------------------------------------------------
echo "--- Connecting to server ---"
echo "  Probing //$SERVER_IP/$SHARE_NAME ..."

if ! _probe_auth; then
    echo ""
    echo "ERROR: Cannot connect to //$SERVER_IP/$SHARE_NAME"
    echo "       Tried: -N (guest/no-password) and -U guest%"
    echo "       Check that the server is reachable and the share allows guest access."
    sleep 5
    exit 1
fi

echo "  [OK] Connected (auth: $SMB_AUTH)"
echo ""

# ---------------------------------------------------------------------------
# 4. Discover matching system folders
# ---------------------------------------------------------------------------
echo "--- Discovering systems ---"

# List directories under ROM_PATH on the server
REMOTE_SYSTEMS=()
while IFS= read -r entry; do
    type="${entry:0:1}"
    name="${entry:2}"
    if [ "$type" = "d" ] && [ -n "$name" ]; then
        REMOTE_SYSTEMS+=("$name")
    fi
done < <(smb_ls "$ROM_PATH")

if [ ${#REMOTE_SYSTEMS[@]} -eq 0 ]; then
    echo "ERROR: No system folders found under $ROM_PATH on the server."
    echo "       Check ROM_PATH in $CONFIG_FILE"
    sleep 5
    exit 1
fi

# Match with local system folders
MATCHING_SYSTEMS=()
for system in "${REMOTE_SYSTEMS[@]}"; do
    if [ -d "$LOCAL_ROMS/$system" ]; then
        MATCHING_SYSTEMS+=("$system")
    fi
done

if [ ${#MATCHING_SYSTEMS[@]} -eq 0 ]; then
    echo "No matching system folders found."
    echo ""
    echo "Server has these folders under $ROM_PATH:"
    for s in "${REMOTE_SYSTEMS[@]}"; do echo "    - $s"; done
    echo ""
    echo "Console has these folders under $LOCAL_ROMS/:"
    ls "$LOCAL_ROMS/" 2>/dev/null || echo "  (none)"
    echo ""
    echo "Tip: Check ROM_PATH in $CONFIG_FILE"
    sleep 5
    exit 0
fi

echo "  Found ${#MATCHING_SYSTEMS[@]} matching system(s):"
for system in "${MATCHING_SYSTEMS[@]}"; do
    echo "    - $system"
done
echo ""

# ---------------------------------------------------------------------------
# 5. Sync each matching system
# ---------------------------------------------------------------------------
echo "--- Syncing ROMs ---"
echo ""

mkdir -p "$TEMP_DIR"

for system in "${MATCHING_SYSTEMS[@]}"; do
    echo ">> $system"
    remote_dir="$ROM_PATH/$system"
    local_dir="$LOCAL_ROMS/$system"
    system_new=0
    system_errors=0

    # List files in this system folder on the server
    while IFS= read -r entry; do
        type="${entry:0:1}"
        filename="${entry:2}"

        # Only process files (not subdirectories)
        [ "$type" = "f" ] || continue
        [ -n "$filename" ] || continue

        local_file="$local_dir/$filename"

        # Skip if already present locally
        if [ -f "$local_file" ]; then
            continue
        fi

        echo "  Downloading: $filename"
        tmp_file="$TEMP_DIR/$filename"

        if smb_get "$remote_dir" "$filename" "$tmp_file" && [ -s "$tmp_file" ]; then
            # Move into place
            if mv "$tmp_file" "$local_file" 2>/dev/null; then
                echo "    [OK] $filename"
                system_new=$((system_new + 1))
                FILES_TRANSFERRED=$((FILES_TRANSFERRED + 1))
            else
                echo "    [ERROR] Could not move $filename to $local_file"
                rm -f "$tmp_file" 2>/dev/null
                system_errors=$((system_errors + 1))
                ERRORS=$((ERRORS + 1))
            fi
        else
            echo "    [ERROR] Download failed: $filename"
            rm -f "$tmp_file" 2>/dev/null
            system_errors=$((system_errors + 1))
            ERRORS=$((ERRORS + 1))
        fi

    done < <(smb_ls "$remote_dir")

    if [ $system_new -eq 0 ] && [ $system_errors -eq 0 ]; then
        echo "  (no new files)"
    else
        echo "  New files: $system_new  Errors: $system_errors"
    fi

    [ $system_errors -eq 0 ] && SYSTEMS_SYNCED=$((SYSTEMS_SYNCED + 1))
    echo ""
done

# ---------------------------------------------------------------------------
# 6. Report results
# ---------------------------------------------------------------------------
echo "=== Sync complete ==="
echo ""
echo "  Systems synced : $SYSTEMS_SYNCED / ${#MATCHING_SYSTEMS[@]}"
echo "  Files added    : $FILES_TRANSFERRED"
if [ $ERRORS -gt 0 ]; then
    echo "  Errors         : $ERRORS (check output above)"
fi
echo ""

# ---------------------------------------------------------------------------
# 7. Restart EmulationStation if new files were added
# ---------------------------------------------------------------------------
if [ $FILES_TRANSFERRED -gt 0 ]; then
    echo "Restarting EmulationStation to refresh game lists..."
    sleep 3
    sudo systemctl restart emulationstation
else
    echo "No new files — skipping restart."
    sleep 5
fi
