#!/bin/sh
set -e

# Create default config if it doesn't exist
if [ ! -f /config/sources.json ]; then
    echo "[]" > /config/sources.json
fi

# Ensure the writable saves directory exists inside the local source.
# Samba's [saves] share points here; R36S devices push save data via SMB.
mkdir -p /sources/local/.saves
chmod 0775 /sources/local/.saves 2>/dev/null || true

# Trap to kill all background processes on exit
cleanup() {
    kill 0
    exit
}
trap cleanup EXIT INT TERM

# Start Samba
smbd --foreground --no-process-group &

# Start Python backend
python3 /app/server.py &

# Wait for all background processes (POSIX-compatible, no wait -n)
wait

# If we get here, something exited — trigger cleanup
exit 1
