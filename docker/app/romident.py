"""
romident.py — ROM identification via OpenVGDB for retroshare.

Downloads OpenVGDB (a freely available SQLite database mapping ROM CRC32
hashes to canonical No-Intro names), hashes ROM files, and looks them up
so that custom-named ROMs can be matched to Libretro thumbnails.

OpenVGDB releases: https://github.com/OpenVGDB/OpenVGDB/releases
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
import urllib.error
import urllib.parse
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

# Header sizes (bytes to skip before hashing) keyed by lowercase file extension.
# Extensions not listed here default to 0 (no header skip).
# .sfc and .smc use dynamic SNES copier-header detection instead (see hash_rom).
_HEADER_SKIP = {
    ".nes": 16,   # iNES header
    ".fds": 16,   # fwNES header
    ".lnx": 64,   # Atari Lynx header
    ".a78": 128,  # Atari 7800 header
}

_SCREENSCRAPER_BASE_URL = "https://api.screenscraper.fr/api2/jeuRecherche.php"

_SCREENSCRAPER_SYSTEM_MAP = {
    "gba": "52",
    "gbc": "41",
    "gb": "40",
    "nes": "7",
    "snes": "6",
    "sfc": "6",
    "n64": "3",
    "nds": "38",
    "psx": "1",
    "ps1": "1",
    "megadrive": "10",
    "genesis": "10",
    "mastersystem": "2",
    "sms": "2",
    "gamegear": "4",
    "gg": "4",
    "lynx": "13",
    "atari2600": "22",
    "atari7800": "23",
    "pce": "8",
    "tg16": "8",
    "ngp": "26",
    "ngpc": "26",
    "wonderswan": "36",
    "ws": "36",
    "wsc": "37",
    "arcade": "52",
    "mame": "52",
}

_SCREENSCRAPER_EXT_MAP = {
    "gba": ".gba",
    "gbc": ".gbc",
    "gb": ".gb",
    "nes": ".nes",
    "snes": ".sfc",
    "sfc": ".sfc",
    "n64": ".n64",
    "nds": ".nds",
    "psx": ".bin",
    "ps1": ".bin",
    "megadrive": ".md",
    "genesis": ".md",
    "mastersystem": ".sms",
    "sms": ".sms",
    "gamegear": ".gg",
    "gg": ".gg",
    "lynx": ".lnx",
    "atari2600": ".a26",
    "atari7800": ".a78",
    "pce": ".pce",
    "tg16": ".pce",
    "ngp": ".ngp",
    "ngpc": ".ngp",
    "wonderswan": ".ws",
    "ws": ".ws",
    "wsc": ".wsc",
    "arcade": "",
    "mame": "",
}


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


def _snes_header_skip(data_size):
    """Return the number of bytes to skip for a SNES ROM based on its size.

    SNES copier headers are 512 bytes and are present when the file size
    modulo 1024 equals 512.  Returns 512 if a copier header is detected,
    0 otherwise.
    """
    return 512 if data_size % 1024 == 512 else 0


def _compute_hashes(data, skip_bytes):
    """Compute CRC32, MD5, and SHA1 over data[skip_bytes:] in a single pass.

    Args:
        data:       bytes-like object containing the full ROM data.
        skip_bytes: number of leading bytes to ignore.

    Returns:
        dict with keys "crc32", "md5", "sha1" (all uppercase hex strings).
    """
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    crc = 0

    view = memoryview(data)[skip_bytes:]
    offset = 0
    total = len(view)
    while offset < total:
        chunk = bytes(view[offset : offset + _CHUNK_SIZE])
        crc = zlib.crc32(chunk, crc)
        md5.update(chunk)
        sha1.update(chunk)
        offset += _CHUNK_SIZE

    return {
        "crc32": format(crc & 0xFFFFFFFF, "08X"),
        "md5": md5.hexdigest().upper(),
        "sha1": sha1.hexdigest().upper(),
    }


def _compute_hashes_stream(fh, skip_bytes):
    """Compute CRC32, MD5, and SHA1 by streaming an open file handle.

    Seeks to skip_bytes first, then reads the remainder in _CHUNK_SIZE chunks
    so that arbitrarily large files (e.g. PS1 .bin disc images) are never
    fully loaded into memory.

    Args:
        fh:         A binary file handle opened for reading.
        skip_bytes: Number of leading bytes to skip (via seek).

    Returns:
        dict with keys "crc32", "md5", "sha1" (all uppercase hex strings).
    """
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    crc = 0

    fh.seek(skip_bytes)
    while True:
        chunk = fh.read(_CHUNK_SIZE)
        if not chunk:
            break
        crc = zlib.crc32(chunk, crc)
        md5.update(chunk)
        sha1.update(chunk)

    return {
        "crc32": format(crc & 0xFFFFFFFF, "08X"),
        "md5": md5.hexdigest().upper(),
        "sha1": sha1.hexdigest().upper(),
    }


def hash_rom(filepath):
    """Compute CRC32, MD5, and SHA1 hashes of a ROM file.

    Header stripping:
    - .nes / .fds: skip 16 bytes (iNES / fwNES header)
    - .lnx:        skip 64 bytes (Atari Lynx header)
    - .a78:        skip 128 bytes (Atari 7800 header)
    - .sfc / .smc: skip 512 bytes only when filesize % 1024 == 512
                   (SNES copier / SMC header detection)
    - all others:  no skip

    Zip support:
    - If the file extension is .zip, the first entry in the archive is
      extracted and hashed.  Header skipping uses the inner file's extension.
      Returns None (with a warning) if the zip is corrupt or unreadable.

    Args:
        filepath: Path to the ROM file (or .zip containing a ROM).

    Returns:
        dict {"crc32": ..., "md5": ..., "sha1": ...} with uppercase hex
        strings, or None if a zip could not be read.

    Raises:
        OSError if a non-zip file cannot be read.
    """
    _, ext = os.path.splitext(filepath)
    ext = ext.lower()

    if ext == ".zip":
        # --- Zip handling ---
        try:
            with zipfile.ZipFile(filepath, "r") as zf:
                names = zf.namelist()
                if not names:
                    logger.warning("Empty zip archive: %s", filepath)
                    return None
                inner_name = names[0]
                inner_info = zf.getinfo(inner_name)
                _, inner_ext = os.path.splitext(inner_name)
                inner_ext = inner_ext.lower()

                data = zf.read(inner_name)
        except zipfile.BadZipFile as exc:
            logger.warning("Corrupt zip archive %s: %s", filepath, exc)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read zip archive %s: %s", filepath, exc)
            return None

        # Determine header skip using the inner file's extension and size.
        if inner_ext in (".sfc", ".smc"):
            # Use the uncompressed size from zip metadata for SNES detection.
            skip_bytes = _snes_header_skip(inner_info.file_size)
        else:
            skip_bytes = _HEADER_SKIP.get(inner_ext, 0)

        return _compute_hashes(data, skip_bytes)

    # --- Regular file handling (streaming — never loads the whole file) ---
    # Determine the header skip before opening the file so we can seek past it.
    if ext in (".sfc", ".smc"):
        # Use the on-disk file size to detect a SNES copier header without
        # reading any file data.
        skip_bytes = _snes_header_skip(os.path.getsize(filepath))
    else:
        skip_bytes = _HEADER_SKIP.get(ext, 0)

    with open(filepath, "rb") as fh:
        return _compute_hashes_stream(fh, skip_bytes)


# ---------------------------------------------------------------------------
# 3. lookup_rom — look up a ROM hash in OpenVGDB
# ---------------------------------------------------------------------------

_HASH_COLUMN_MAP = {
    "crc32": "romHashCRC",
    "md5": "romHashMD5",
    "sha1": "romHashSHA1",
}


def lookup_rom(db_path, hash_type, hash_hex):
    """Look up a ROM hash in the OpenVGDB SQLite database.

    Args:
        db_path:    Path to openvgdb.sqlite.
        hash_type:  One of "crc32", "md5", or "sha1".
        hash_hex:   Uppercase hex hash string.

    Returns:
        Canonical No-Intro filename string (e.g.
        "Kirby - Nightmare in Dream Land (USA).gba"), or None if not found.
    """
    column = _HASH_COLUMN_MAP.get(hash_type)
    if column is None:
        return None

    uri = "file:{}?mode=ro".format(urllib.request.pathname2url(os.path.abspath(db_path)))
    with sqlite3.connect(uri, uri=True) as conn:
        cur = conn.execute(
            f"SELECT romFileName FROM ROMs WHERE {column} = ? LIMIT 1",
            (hash_hex.upper(),),
        )
        row = cur.fetchone()

    return row[0] if row is not None else None


def lookup_crc(db_path, crc_hex):
    """Look up a CRC32 hash in the OpenVGDB SQLite database.

    Alias for lookup_rom(db_path, "crc32", crc_hex).
    """
    return lookup_rom(db_path, "crc32", crc_hex)


# ---------------------------------------------------------------------------
# 4. ScreenScraper lookup
# ---------------------------------------------------------------------------


def screenScraper_lookup(hashes, system_name, debug_info=None):
    """Look up a ROM via the ScreenScraper API using hash or filename.

    Args:
        hashes:       dict with "crc32", "md5", "sha1" keys (uppercase hex).
        system_name: System folder name (e.g. "gba").
        debug_info:   Optional dict to populate with debug info.

    Returns:
        Canonical filename string (e.g. "Kirby & The Amazing Mirror.gba"),
        or None if not found or on error.
    """
    system_id = _SCREENSCRAPER_SYSTEM_MAP.get(system_name.lower())
    if system_id is None:
        if debug_info is not None:
            debug_info["ss_error"] = f"unknown system: {system_name}"
        return None

    params = {
        "devid": os.environ.get("SCREENSCRAPER_DEV_ID", ""),
        "devpassword": os.environ.get("SCREENSCRAPER_DEV_PASSWORD", ""),
        "ssid": os.environ.get("SCREENSCRAPAPER_SSID", ""),
        "sspassword": os.environ.get("SCREENSCRAPER_SS_PASSWORD", ""),
        "systemeid": system_id,
        "crc": hashes.get("crc32", ""),
        "md5": hashes.get("md5", ""),
        "sha1": hashes.get("sha1", ""),
    }

    url = _SCREENSCRAPER_BASE_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        if debug_info is not None:
            debug_info["ss_error"] = str(exc)
        return None

    if debug_info is not None:
        debug_info["ss_response"] = data

    games = data.get("response", {}).get("jeux", {}).get("jeu", [])
    if not games:
        if debug_info is not None:
            debug_info["ss_found"] = False
        return None

    game = games[0]
    rom_name = game.get("rom", {}).get("romNom", "")
    if not rom_name:
        if debug_info is not None:
            debug_info["ss_found"] = False
        return None

    rom_name = re.sub(r"&", "and", rom_name)

    ext = _SCREENSCRAPER_EXT_MAP.get(system_name.lower(), "")
    canonical = f"{rom_name}{ext}"

    if debug_info is not None:
        debug_info["ss_found"] = True
        debug_info["ss_canonical"] = canonical

    return canonical


# ---------------------------------------------------------------------------
# 5. identify_rom — hash + OpenVGDB + ScreenScraper fallback
# ---------------------------------------------------------------------------


def identify_rom(db_path, filepath, system_name=None, debug=False):
    """Hash a ROM file and look it up in OpenVGDB, then ScreenScraper.

    Tries CRC32 first, then MD5, then SHA1 via OpenVGDB.  If all fail and
    system_name is provided, falls back to ScreenScraper.

    Args:
        db_path:     Path to openvgdb.sqlite.
        filepath:    Path to the ROM file.
        system_name: Optional system folder name (e.g. "gba") for ScreenScraper
                     fallback and for filename-based lookup.
        debug:       If True, returns a debug dict as second return value.

    Returns:
        Canonical No-Intro filename string, or None if not found or on error.
        If debug=True, returns a (result, debug_info) tuple.
    """
    debug_info = {} if debug else None

    try:
        hashes = hash_rom(filepath)
    except OSError as exc:
        logger.warning("Could not hash %s: %s", filepath, exc)
        return (None, debug_info) if debug else None

    if hashes is None:
        return (None, debug_info) if debug else None

    if debug_info is not None:
        debug_info["hashes"] = hashes

    for hash_type in ("crc32", "md5", "sha1"):
        result = lookup_rom(db_path, hash_type, hashes[hash_type])
        if result is not None:
            logger.debug("Identified %s via OpenVGDB/%s: %s", filepath, hash_type, result)
            if debug_info is not None:
                debug_info["source"] = f"OpenVGDB/{hash_type}"
                debug_info["canonical"] = result
            return (result, debug_info) if debug else result

    if system_name is not None:
        result = screenScraper_lookup(hashes, system_name, debug_info=debug_info)
        if result is not None:
            logger.debug("Identified %s via ScreenScraper: %s", filepath, result)
            if debug_info is not None:
                debug_info["source"] = "ScreenScraper"
                debug_info["canonical"] = result
            return (result, debug_info) if debug else result

    return (None, debug_info) if debug else None
