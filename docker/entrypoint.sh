#!/bin/sh
set -e

# Create default config if it doesn't exist
if [ ! -f /config/sources.json ]; then
    echo "[]" > /config/sources.json
fi

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
