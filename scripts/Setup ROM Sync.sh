#!/bin/bash
# Setup ROM Sync
# One-time setup: installs dependencies and creates a default config file.
# Safe to run multiple times (idempotent).

CONFIG_FILE="/roms/tools/romsync.cfg"

echo "=== ROM Sync Setup ==="
echo ""

# ---------------------------------------------------------------------------
# Helper: fix apt sources for Ubuntu 19.10 (EOL) if needed
# ---------------------------------------------------------------------------
fix_apt_sources_if_needed() {
    # Try a quick apt-get update; if it fails, redirect to old-releases
    echo "Checking apt repositories..."
    if sudo apt-get update -qq 2>/dev/null; then
        return 0
    fi

    echo "apt-get update failed — Ubuntu 19.10 is EOL. Redirecting to old-releases.ubuntu.com..."
    sudo sed -i \
        's|http://ports.ubuntu.com/ubuntu-ports|http://old-releases.ubuntu.com/ubuntu|g;
         s|http://archive.ubuntu.com/ubuntu|http://old-releases.ubuntu.com/ubuntu|g;
         s|http://security.ubuntu.com/ubuntu|http://old-releases.ubuntu.com/ubuntu|g' \
        /etc/apt/sources.list

    echo "Retrying apt-get update..."
    if ! sudo apt-get update -qq; then
        echo "ERROR: apt-get update still failed after fixing sources. Check your network connection."
        return 1
    fi

    echo "apt sources updated successfully."
    return 0
}

# ---------------------------------------------------------------------------
# Install a package if not already present
# ---------------------------------------------------------------------------
ensure_package() {
    local pkg="$1"
    if dpkg -s "$pkg" &>/dev/null; then
        echo "  [OK] $pkg is already installed."
    else
        echo "  Installing $pkg..."
        if sudo apt-get install -y "$pkg" -qq; then
            echo "  [OK] $pkg installed successfully."
        else
            echo "  [ERROR] Failed to install $pkg."
            return 1
        fi
    fi
}

# ---------------------------------------------------------------------------
# 1. Install smbclient
# ---------------------------------------------------------------------------
echo "--- Checking smbclient ---"
if ! dpkg -s smbclient &>/dev/null; then
    fix_apt_sources_if_needed || { sleep 3; exit 1; }
fi
ensure_package smbclient
echo ""

# ---------------------------------------------------------------------------
# 2. Create default config file (only if it doesn't exist)
# ---------------------------------------------------------------------------
echo "--- Config file ---"
if [ -f "$CONFIG_FILE" ]; then
    echo "  [OK] Config file already exists: $CONFIG_FILE"
    echo "       (Leaving existing config untouched.)"
else
    # Ensure the directory exists
    sudo mkdir -p "$(dirname "$CONFIG_FILE")"
    sudo tee "$CONFIG_FILE" > /dev/null <<'EOF'
SERVER_IP=192.168.178.101
SHARE_NAME=data
ROM_PATH=media/roms
SMB_PORT=445
EOF
    echo "  [OK] Created default config: $CONFIG_FILE"
fi
echo ""

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo "=== Setup complete ==="
echo ""
echo "IMPORTANT: If your server details differ from the defaults, edit:"
echo "  $CONFIG_FILE"
echo ""
echo "Default values:"
echo "  SERVER_IP  = 192.168.178.101"
echo "  SHARE_NAME = data"
echo "  ROM_PATH   = media/roms"
echo "  SMB_PORT   = 445"
echo ""

sleep 3
