#!/bin/bash
# Sync Saves
# Pushes save files (.srm, .sav, .state*) from the R36S to the retroshare
# server's writable [saves] share. Before uploading each save, asks the
# server to promote the matching ROM from any remote-friend source into
# /sources/local/, so a save never lives on the server without its ROM.
#
# Upload is atomic: put to .tmp then rename. Local files are never deleted.
#
# Requires: smbclient, curl (both shipped on ArkOS).

# Auto-detect ROM destination: /roms2/ (GAMES SD card) or /roms/ (single SD)
if [ -d "/roms2" ] && [ "$(ls -A /roms2 2>/dev/null)" ]; then
    LOCAL_ROMS="/roms2"
else
    LOCAL_ROMS="/roms"
fi

CONFIG_FILE="$LOCAL_ROMS/tools/romsync.cfg"
STATE_FILE="$LOCAL_ROMS/tools/savesync.state"

SAVES_UPLOADED=0
SAVES_SKIPPED=0
SAVES_ERRORS=0
ROMS_PROMOTED=0

echo "=== Save Sync ==="
echo ""

# ---------------------------------------------------------------------------
# 1. Read config (shared with Sync ROMs.sh)
# ---------------------------------------------------------------------------
if [ ! -f "$CONFIG_FILE" ]; then
    echo "ERROR: Config file not found: $CONFIG_FILE"
    echo "       Please run 'Setup ROM Sync' first."
    sleep 5
    exit 1
fi

# shellcheck source=/dev/null
source "$CONFIG_FILE"

if [ -z "$SERVER_IP" ]; then
    echo "ERROR: SERVER_IP not set in $CONFIG_FILE"
    sleep 5
    exit 1
fi

SMB_PORT="${SMB_PORT:-445}"
SAVES_SHARE="${SAVES_SHARE:-saves}"
HTTP_PORT="${HTTP_PORT:-7868}"

SMB_PORT_FLAG=""
if [ "$SMB_PORT" != "445" ]; then
    SMB_PORT_FLAG="-p $SMB_PORT"
fi

echo "Server     : $SERVER_IP"
echo "Saves share: $SAVES_SHARE"
echo "SMB port   : $SMB_PORT"
echo "HTTP port  : $HTTP_PORT"
echo "Source     : $LOCAL_ROMS"
echo ""

# ---------------------------------------------------------------------------
# 2. Pre-flight checks
# ---------------------------------------------------------------------------
for cmd in smbclient curl; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: $cmd not found. Install it and retry."
        sleep 5
        exit 1
    fi
done

# ---------------------------------------------------------------------------
# 3. Probe SMB auth against the saves share
# ---------------------------------------------------------------------------
SMB_AUTH=""
_probe_auth() {
    local share="//$SERVER_IP/$SAVES_SHARE"
    if smbclient "$share" $SMB_PORT_FLAG -N -c "ls" &>/dev/null; then
        SMB_AUTH="-N"
    elif smbclient "$share" $SMB_PORT_FLAG -U "guest%" -c "ls" &>/dev/null; then
        SMB_AUTH="-U guest%"
    else
        return 1
    fi
}

echo "--- Connecting to //$SERVER_IP/$SAVES_SHARE ---"
if ! _probe_auth; then
    echo "ERROR: Cannot connect to //$SERVER_IP/$SAVES_SHARE"
    echo "       Check the retroshare container has the [saves] share enabled."
    sleep 5
    exit 1
fi
echo "  [OK] Connected (auth: $SMB_AUTH)"
echo ""

# ---------------------------------------------------------------------------
# 4. State file helpers (skip unchanged saves between runs)
# ---------------------------------------------------------------------------
mkdir -p "$(dirname "$STATE_FILE")"
touch "$STATE_FILE"

# get_state <abs-path>  → prints recorded mtime or empty
get_state() {
    awk -v p="$1" '$0 ~ "\t"p"$" { sub(/\t.*/, "", $0); print; exit }' "$STATE_FILE"
}

# set_state <abs-path> <mtime>  → rewrites STATE_FILE without the old line, appends new
set_state() {
    local p="$1" m="$2"
    local tmp="${STATE_FILE}.tmp"
    awk -v p="$p" '$0 !~ "\t"p"$" { print }' "$STATE_FILE" > "$tmp"
    echo -e "${m}\t${p}" >> "$tmp"
    mv "$tmp" "$STATE_FILE"
}

# ---------------------------------------------------------------------------
# 5. Promote ROM via HTTP API
#    promote <system> <rom-filename>   → 0 on success
# ---------------------------------------------------------------------------
promote() {
    local system="$1"
    local rom="$2"
    local url="http://$SERVER_IP:$HTTP_PORT/api/promote"
    local body
    body=$(printf '{"system":"%s","rom":"%s"}' "$system" "$rom")

    local response
    response=$(curl -sS -m 600 -X POST -H "Content-Type: application/json" \
        -d "$body" "$url" 2>&1)
    local rc=$?
    if [ $rc -ne 0 ]; then
        echo "    [WARN] promote request failed: $response"
        return 1
    fi
    # Response is JSON. Cheap check for "status":"ok".
    case "$response" in
        *'"status": "ok"'*|*'"status":"ok"'*) return 0 ;;
        *) echo "    [WARN] promote response: $response" ; return 1 ;;
    esac
}

# Returns 0 if the promote response indicates promoted:true
promote_did_copy() {
    case "$1" in
        *'"promoted": true'*|*'"promoted":true'*) return 0 ;;
        *) return 1 ;;
    esac
}

# ---------------------------------------------------------------------------
# 6. Upload save atomically: put <local> to <name>.tmp, then rename.
#    upload_save <system> <local-file> <remote-name>
# ---------------------------------------------------------------------------
upload_save() {
    local system="$1"
    local local_file="$2"
    local remote_name="$3"
    local share="//$SERVER_IP/$SAVES_SHARE"
    local tmp_name="${remote_name}.tmp"

    # smbclient ignores individual command failures; mkdir will fail silently
    # if the system subdir already exists. cd must succeed for put/rename.
    local out
    out=$(smbclient "$share" $SMB_PORT_FLAG $SMB_AUTH -c "
        prompt OFF
        mkdir \"$system\"
        cd \"$system\"
        put \"$local_file\" \"$tmp_name\"
        rename \"$tmp_name\" \"$remote_name\"
    " 2>&1)
    local rc=$?
    if [ $rc -ne 0 ]; then
        echo "    [ERROR] smbclient rc=$rc: $out"
        return 1
    fi
    # smbclient returns 0 even if individual commands failed — scan output.
    if echo "$out" | grep -qiE "NT_STATUS|cannot|error"; then
        # "NT_STATUS_OBJECT_NAME_COLLISION" on mkdir is harmless.
        if echo "$out" | grep -qvE "NT_STATUS_OBJECT_NAME_COLLISION"; then
            echo "    [ERROR] smbclient: $out"
            return 1
        fi
    fi
    return 0
}

# ---------------------------------------------------------------------------
# 7. Walk local save files and sync
# ---------------------------------------------------------------------------
echo "--- Scanning for save files ---"
echo ""

# Find every save file across all system folders. ArkOS with
# savefiles_in_content_dir = true places .srm / .sav / .state* next to the ROM.
shopt -s nullglob
SAVE_FILES=()
for system_dir in "$LOCAL_ROMS"/*/; do
    [ -d "$system_dir" ] || continue
    system_name=$(basename "$system_dir")
    # Skip the tools folder and any dotted helpers.
    case "$system_name" in tools|.*) continue ;; esac
    for f in "$system_dir"*.srm "$system_dir"*.sav "$system_dir"*.state*; do
        [ -f "$f" ] || continue
        SAVE_FILES+=("$f")
    done
done
shopt -u nullglob

if [ ${#SAVE_FILES[@]} -eq 0 ]; then
    echo "  (no save files found)"
    echo ""
    echo "=== Save sync complete ==="
    sleep 3
    exit 0
fi

echo "  Found ${#SAVE_FILES[@]} save file(s)"
echo ""

for save_path in "${SAVE_FILES[@]}"; do
    system=$(basename "$(dirname "$save_path")")
    save_name=$(basename "$save_path")

    # Derive the ROM stem by stripping the save extension.
    case "$save_name" in
        *.srm)     stem="${save_name%.srm}" ;;
        *.sav)     stem="${save_name%.sav}" ;;
        *.state*)  stem="${save_name%.state*}" ;;
        *)         stem="" ;;
    esac

    # Skip if unchanged since last run
    mtime=$(stat -c %Y "$save_path" 2>/dev/null)
    recorded=$(get_state "$save_path")
    if [ -n "$recorded" ] && [ "$recorded" = "$mtime" ]; then
        SAVES_SKIPPED=$((SAVES_SKIPPED + 1))
        continue
    fi

    echo ">> $system/$save_name"

    # Promote every ROM in this system folder with the same stem. Usually
    # one match; if the canonicaliser has collapsed regional duplicates we
    # promote all candidates — storage is cheap, lost saves aren't.
    promoted_any=0
    if [ -n "$stem" ]; then
        shopt -s nullglob
        for candidate in "$LOCAL_ROMS/$system/$stem".*; do
            [ -f "$candidate" ] || continue
            cand_name=$(basename "$candidate")
            # Ignore the save files themselves.
            case "$cand_name" in
                *.srm|*.sav|*.state*) continue ;;
            esac
            echo "   promote $cand_name"
            if promote "$system" "$cand_name"; then
                promoted_any=1
                ROMS_PROMOTED=$((ROMS_PROMOTED + 1))
            fi
        done
        shopt -u nullglob
    fi

    if [ "$promoted_any" -eq 0 ]; then
        echo "   (no matching ROM found — uploading save anyway)"
    fi

    # Upload the save itself — atomic rename on the server.
    if upload_save "$system" "$save_path" "$save_name"; then
        echo "   [OK] uploaded"
        set_state "$save_path" "$mtime"
        SAVES_UPLOADED=$((SAVES_UPLOADED + 1))
    else
        SAVES_ERRORS=$((SAVES_ERRORS + 1))
    fi
done

# ---------------------------------------------------------------------------
# 8. Report
# ---------------------------------------------------------------------------
echo ""
echo "=== Save sync complete ==="
echo "  Uploaded   : $SAVES_UPLOADED"
echo "  Skipped    : $SAVES_SKIPPED (unchanged)"
echo "  Promoted   : $ROMS_PROMOTED ROM(s) to local share"
if [ $SAVES_ERRORS -gt 0 ]; then
    echo "  Errors     : $SAVES_ERRORS (check output above)"
fi
echo ""
sleep 3
