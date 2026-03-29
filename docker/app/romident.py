"""
romident.py — ROM identification via OpenVGDB for retroshare.

Downloads OpenVGDB (a freely available SQLite database mapping ROM CRC32
hashes to canonical No-Intro names), hashes ROM files, and looks them up
so that custom-named ROMs can be matched to Libretro thumbnails.

OpenVGDB releases: https://github.com/OpenVGDB/OpenVGDB/releases
"""

import json
import logging
import os
import sqlite3
import urllib.error
import urllib.request
import zipfile
import zlib

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GITHUB_API_URL = "https://api.github.com/repos/OpenVGDB/OpenVGDB/releases/latest"
_DB_FILENAME = "openvgdb.sqlite"
_USER_AGENT = "retroshare/1.0"
_TIMEOUT = 60  # seconds — the zip is ~30 MB
_CHUNK_SIZE = 1 << 20  # 1 MiB read chunks for hashing and downloading

# NES iNES header size (bytes to skip before hashing)
_INES_HEADER_SIZE = 16


# ---------------------------------------------------------------------------
# 1. ensure_db — download OpenVGDB if not present
# ---------------------------------------------------------------------------


def ensure_db(config_dir):
    """Ensure openvgdb.sqlite exists in config_dir, downloading it if needed.

    Args:
        config_dir: Directory where openvgdb.sqlite should be stored.

    Returns:
        Absolute path to openvgdb.sqlite, or None if the download failed.
    """
    db_path = os.path.join(config_dir, _DB_FILENAME)

    if os.path.isfile(db_path):
        logger.debug("OpenVGDB already present at %s", db_path)
        return db_path

    logger.info("OpenVGDB not found — fetching latest release info from GitHub")

    # --- Resolve the download URL via the GitHub releases API ---
    try:
        zip_url = _get_latest_zip_url()
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to fetch OpenVGDB release info: %s", exc)
        return None

    if zip_url is None:
        logger.error("Could not find a .zip asset in the latest OpenVGDB release")
        return None

    # --- Download the zip to a temp file, then extract ---
    zip_tmp = db_path + ".download.zip"
    os.makedirs(config_dir, exist_ok=True)

    try:
        logger.info("Downloading OpenVGDB from %s", zip_url)
        _download_file(zip_url, zip_tmp)
        logger.info("Extracting %s", _DB_FILENAME)
        _extract_db(zip_tmp, db_path)
        logger.info("OpenVGDB ready at %s", db_path)
        return db_path
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to download/extract OpenVGDB: %s", exc)
        # Clean up partial DB file so next run retries the download
        if os.path.isfile(db_path):
            try:
                os.unlink(db_path)
            except OSError:
                pass
        return None
    finally:
        # Always clean up the temp zip, even on failure
        if os.path.isfile(zip_tmp):
            try:
                os.unlink(zip_tmp)
            except OSError:
                pass


def _get_latest_zip_url():
    """Query the GitHub API and return the download URL for the .zip asset.

    Returns:
        URL string, or None if no .zip asset was found.

    Raises:
        urllib.error.URLError / urllib.error.HTTPError on network failure.
        json.JSONDecodeError if the response is not valid JSON.
    """
    req = urllib.request.Request(
        _GITHUB_API_URL,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    for asset in payload.get("assets", []):
        name = asset.get("name", "")
        if name.lower().endswith(".zip"):
            return asset.get("browser_download_url")

    return None


def _download_file(url, dest_path):
    """Download url to dest_path in chunks.

    Raises:
        urllib.error.URLError / urllib.error.HTTPError on network failure.
        OSError on write failure.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        with open(dest_path, "wb") as fh:
            while True:
                chunk = resp.read(_CHUNK_SIZE)
                if not chunk:
                    break
                fh.write(chunk)


def _extract_db(zip_path, dest_path):
    """Extract openvgdb.sqlite from zip_path to dest_path.

    Raises:
        zipfile.BadZipFile if the archive is corrupt.
        KeyError if openvgdb.sqlite is not found in the archive.
        OSError on write failure.
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extract(_DB_FILENAME, path=os.path.dirname(dest_path))


# ---------------------------------------------------------------------------
# 2. hash_rom — CRC32 hash a ROM file
# ---------------------------------------------------------------------------


def hash_rom(filepath):
    """Compute the CRC32 hash of a ROM file.

    NES ROMs (.nes) have a 16-byte iNES header that is skipped before
    hashing, matching the convention used by No-Intro and OpenVGDB.
    All other formats are hashed in full.

    Args:
        filepath: Path to the ROM file.

    Returns:
        Uppercase hex CRC32 string, e.g. "A1B2C3D4".

    Raises:
        OSError if the file cannot be read.
    """
    _, ext = os.path.splitext(filepath)
    skip_bytes = _INES_HEADER_SIZE if ext.lower() == ".nes" else 0

    crc = 0
    with open(filepath, "rb") as fh:
        if skip_bytes:
            fh.read(skip_bytes)  # discard iNES header
        while True:
            chunk = fh.read(_CHUNK_SIZE)
            if not chunk:
                break
            crc = zlib.crc32(chunk, crc)

    # zlib.crc32 may return a signed integer on some platforms; mask to uint32.
    return format(crc & 0xFFFFFFFF, "08X")


# ---------------------------------------------------------------------------
# 3. lookup_crc — look up a CRC32 in OpenVGDB
# ---------------------------------------------------------------------------


def lookup_crc(db_path, crc_hex):
    """Look up a CRC32 hash in the OpenVGDB SQLite database.

    Args:
        db_path: Path to openvgdb.sqlite.
        crc_hex: Uppercase hex CRC32 string, e.g. "A1B2C3D4".

    Returns:
        Canonical No-Intro filename string (e.g.
        "Kirby - Nightmare in Dream Land (USA).gba"), or None if not found.
    """
    uri = "file:{}?mode=ro".format(urllib.request.pathname2url(os.path.abspath(db_path)))
    with sqlite3.connect(uri, uri=True) as conn:
        cur = conn.execute(
            "SELECT romFileName FROM ROMs WHERE romHashCRC = ? LIMIT 1",
            (crc_hex.upper(),),
        )
        row = cur.fetchone()

    return row[0] if row is not None else None


# ---------------------------------------------------------------------------
# 4. identify_rom — convenience: hash + lookup
# ---------------------------------------------------------------------------


def identify_rom(db_path, filepath):
    """Hash a ROM file and look it up in OpenVGDB.

    Args:
        db_path:  Path to openvgdb.sqlite.
        filepath: Path to the ROM file.

    Returns:
        Canonical No-Intro filename string, or None if not found or on error.
    """
    try:
        crc = hash_rom(filepath)
    except OSError as exc:
        logger.warning("Could not hash %s: %s", filepath, exc)
        return None

    return lookup_crc(db_path, crc)
