"""
scraper.py — Libretro thumbnail scraper for retroshare.

Downloads box art thumbnails from the Libretro thumbnail CDN at
https://thumbnails.libretro.com/ and maintains a JSON cache mapping
"system/filename" keys to title + thumbnail path metadata.

URL pattern:
    https://thumbnails.libretro.com/{Libretro System Name}/Named_Boxarts/{ROM name}.png

ROM filenames follow No-Intro naming (e.g. "Super Mario World (USA).sfc").
Libretro thumbnail filenames use the same base name without extension, with
"&" replaced by "_" before URL-encoding.

When the direct thumbnail lookup fails, fuzzy matching tries alternative
name variants (stripping metadata, trying alternate region codes) to find
a match in the Libretro database.
"""

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System name mapping
# ---------------------------------------------------------------------------

# Maps common ROM folder names (lower-case) to Libretro system names.
SYSTEM_MAP = {
    # Nintendo
    "snes": "Nintendo - Super Nintendo Entertainment System",
    "sfc": "Nintendo - Super Nintendo Entertainment System",
    "nes": "Nintendo - Nintendo Entertainment System",
    "famicom": "Nintendo - Nintendo Entertainment System",
    "gba": "Nintendo - Game Boy Advance",
    "gb": "Nintendo - Game Boy",
    "gbc": "Nintendo - Game Boy Color",
    "n64": "Nintendo - Nintendo 64",
    "nds": "Nintendo - Nintendo DS",
    # Sega
    "megadrive": "Sega - Mega Drive - Genesis",
    "genesis": "Sega - Mega Drive - Genesis",
    "mastersystem": "Sega - Master System - Mark III",
    "sms": "Sega - Master System - Mark III",
    "gamegear": "Sega - Game Gear",
    "gg": "Sega - Game Gear",
    # Sony
    "psx": "Sony - PlayStation",
    "ps1": "Sony - PlayStation",
    "playstation": "Sony - PlayStation",
    "psp": "Sony - PlayStation Portable",
    # NEC
    "pce": "NEC - PC Engine - TurboGrafx 16",
    "tg16": "NEC - PC Engine - TurboGrafx 16",
    "pcengine": "NEC - PC Engine - TurboGrafx 16",
    # Atari
    "atari2600": "Atari - 2600",
    "atari7800": "Atari - 7800",
    "lynx": "Atari - Lynx",
    # SNK
    "ngp": "SNK - Neo Geo Pocket Color",
    "ngpc": "SNK - Neo Geo Pocket Color",
    # Bandai
    "wonderswan": "Bandai - WonderSwan",
    "ws": "Bandai - WonderSwan",
    "wonderswancolor": "Bandai - WonderSwan Color",
    "wsc": "Bandai - WonderSwan Color",
    # Arcade
    "arcade": "MAME",
    "mame": "MAME",
    "fba": "FBNeo - Arcade Games",
    "fbneo": "FBNeo - Arcade Games",
    # Other
    "vectrex": "GCE - Vectrex",
    "coleco": "Coleco - ColecoVision",
    "colecovision": "Coleco - ColecoVision",
}

# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

_CDN_BASE = "https://thumbnails.libretro.com"
_USER_AGENT = "retroshare/1.0"
_TIMEOUT = 10  # seconds

_REGION_CODES = {
    "USA", "Japan", "Europe", "World",
    "France", "Germany", "Spain", "Italy", "UK", "Australia",
    "Brazil", "Canada", "China", "Korea", "Russia",
    "Netherlands", "Sweden", "Denmark", "Norway", "Finland",
    "Belgium", "Switzerland", "Austria",
}

_REGION_ALIASES = {
    "Europe": ["(E)"],
    "USA": ["(U)"],
    "Japan": ["(J)"],
    "World": ["(W)"],
    "France": ["(F)", "(Fr)"],
    "Germany": ["(D)", "(De)"],
    "Italy": ["(I)", "(It)"],
    "Spain": ["(S)", "(Es)"],
    "UK": ["(UK)", "(En)"],
    "Australia": ["(A)", "(Au)"],
    "Brazil": ["(B)", "(Br)"],
    "Netherlands": ["(NL)", "(Du)"],
    "Sweden": ["(Sw)"],
    "Denmark": ["(Da)"],
    "Norway": ["(No)"],
    "Finland": ["(Fi)"],
    "Belgium": ["(Be)"],
    "Switzerland": ["(Sz)", "(Ch)"],
    "Austria": ["(At)"],
    "Canada": ["(Ca)"],
    "China": ["(Cn)"],
    "Korea": ["(K)"],
    "Russia": ["(R)"],
}


def _generate_candidates(name):
    """Generate candidate thumbnail names from a ROM name.

    Tries progressively more stripped-down versions to match Libretro's
    thumbnail naming conventions. Includes alternate region code variants.
    """
    candidates = set()

    segments = re.split(r"(\s*\([^)]+\))", name)
    parts = [s.strip() for s in segments if s.strip()]

    title_parts = []
    region_parts = []

    for p in parts:
        m = re.match(r"^\(([^)]+)\)$", p)
        if m and m.group(1) in _REGION_CODES:
            region_parts.append(p)
        elif m:
            continue
        else:
            title_parts.append(p)

    base_title = " ".join(title_parts).strip()

    if not base_title:
        return []

    if region_parts:
        candidates.add(base_title + " " + " ".join(region_parts))
        for rp in region_parts:
            m2 = re.match(r"^\(([^)]+)\)$", rp)
            if m2:
                canonical = m2.group(1)
                for alias in _REGION_ALIASES.get(canonical, []):
                    alias_paren = alias if alias.startswith("(") else f"({alias})"
                    if alias_paren != rp:
                        candidates.add(f"{base_title} {alias_paren}")
    candidates.add(base_title)

    return list(candidates)


def _try_url(url, dest_file, dest_dir):
    """Attempt to download a thumbnail from a URL.

    Returns (rel_path, debug_msg) on success, (None, debug_msg) on failure.
    """
    os.makedirs(dest_dir, exist_ok=True)

    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = resp.read()
        with open(dest_file, "wb") as fh:
            fh.write(data)
        return (dest_file, None)
    except urllib.error.HTTPError as exc:
        msg = f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        msg = f"Network error: {exc.reason}"
    except OSError as exc:
        msg = f"OS error: {exc}"
    return (None, msg)


def rom_name_to_thumbnail_name(filename):
    """Strip the file extension from a ROM filename.

    Args:
        filename: ROM filename, e.g. "Super Mario World (USA).sfc"

    Returns:
        Thumbnail lookup name, e.g. "Super Mario World (USA)"
    """
    name, _ = os.path.splitext(filename)
    return name


def get_thumbnail_url(system_folder, rom_filename):
    """Build the Libretro CDN URL for a ROM's box art thumbnail.

    Args:
        system_folder: ROM folder name, e.g. "snes"
        rom_filename:  ROM filename, e.g. "Super Mario World (USA).sfc"

    Returns:
        Full URL string, or None if system_folder is not in SYSTEM_MAP.
    """
    libretro_system = SYSTEM_MAP.get(system_folder.lower())
    if libretro_system is None:
        return None

    thumbnail_name = rom_name_to_thumbnail_name(rom_filename)

    # Libretro convention: replace "&" with "_" before URL-encoding.
    thumbnail_name = thumbnail_name.replace("&", "_")

    # URL-encode the thumbnail name (spaces → %20, etc.).
    encoded_name = urllib.parse.quote(thumbnail_name, safe="")
    encoded_system = urllib.parse.quote(libretro_system, safe="")

    url = f"{_CDN_BASE}/{encoded_system}/Named_Boxarts/{encoded_name}.png"
    logger.debug("Thumbnail URL for %s/%s: %s", system_folder, rom_filename, url)
    return url


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def download_thumbnail(system_folder, rom_filename, cache_dir, original_filename=None):
    """Download a thumbnail PNG to cache_dir/system_folder/thumbnail_name.png.

    Skips the download if the file already exists (cache hit).
    On 404, tries fuzzy-matched candidate names (from original filename) before giving up.

    Args:
        system_folder: ROM folder name, e.g. "snes"
        rom_filename:  Clean canonical ROM filename, e.g. "Super Mario World (USA).sfc"
        cache_dir:     Root directory for cached thumbnails
        original_filename: Optional original filename with full metadata, e.g.
                          "Super Mario World (USA) (En,Fr,De,Es,It).sfc"

    Returns:
        Relative path string (e.g. "snes/Super Mario World (USA).png") on
        success or cache hit, None on failure (404, network error, etc.).
    """
    libretro_system = SYSTEM_MAP.get(system_folder.lower())
    if libretro_system is None:
        logger.info("Skip %s/%s — system not in SYSTEM_MAP", system_folder, rom_filename)
        return None

    thumbnail_name = rom_name_to_thumbnail_name(rom_filename)
    encoded_system = urllib.parse.quote(libretro_system, safe="")

    dest_dir = os.path.join(cache_dir, system_folder)
    dest_file = os.path.join(dest_dir, thumbnail_name + ".png")
    rel_path = os.path.join(system_folder, thumbnail_name + ".png")

    if os.path.isfile(dest_file):
        logger.info("Cache hit: %s", rel_path)
        return rel_path

    candidates = [(thumbnail_name, thumbnail_name + ".png")]
    seen = {thumbnail_name}

    def _add(cand):
        if cand and cand not in seen:
            candidates.append((cand, cand + ".png"))
            seen.add(cand)

    _LIBRETRO_REGION_COMBOS = [
        "(Europe)", "(USA)", "(Japan)", "(World)",
        "(USA, Europe)", "(Europe) (En,Fr,De,Es,It)",
        "(Europe) (En,Fr,De)", "(USA) (En,Ja)",
        "(Japan) (En,Ja)", "(Europe) (En)",
        "(Europe) (En,Ja,Fr,De,Es,It)",
    ]

    if original_filename:
        orig_base = rom_name_to_thumbnail_name(original_filename)
        _add(orig_base)

        for cand in _generate_candidates(orig_base):
            _add(cand)

        for base in [orig_base] + _generate_candidates(orig_base):
            _add(base.replace("&", "And"))
            _add(base.replace("And", "&"))

        segments = re.split(r"(\s*\([^)]+\))", orig_base)
        base_title = " ".join(s.strip() for s in segments if s.strip() and not re.match(r"^\s*\([^)]+\)\s*$", s.strip())).strip()
        _add(base_title)
        _add(base_title.replace("&", "And"))
        _add(base_title.replace("And", "&"))

        for base in [orig_base, base_title]:
            for combo in _LIBRETRO_REGION_COMBOS:
                _add(f"{base} {combo}")
                _add(f"{base.replace('&', 'And')} {combo}")
                _add(f"{base.replace('And', '&')} {combo}")

    for cand in _generate_candidates(thumbnail_name):
        _add(cand)
    _add(thumbnail_name.replace("&", "And"))
    _add(thumbnail_name.replace("And", "&"))

    for thumb_name, thumb_file in candidates:
        thumb_name_encoded = thumb_name.replace("&", "_")
        thumb_name_encoded = urllib.parse.quote(thumb_name_encoded, safe="")
        url = f"{_CDN_BASE}/{encoded_system}/Named_Boxarts/{thumb_name_encoded}.png"

        logger.debug("Trying URL: %s", url)
        result, err = _try_url(url, dest_file, dest_dir)

        if result is not None:
            if thumb_file == thumbnail_name + ".png":
                logger.info("Downloaded: %s", rel_path)
            else:
                logger.info("Downloaded (fuzzy match): %s (tried '%s')", rel_path, thumb_name)
            return rel_path

        logger.debug("Failed '%s': %s", thumb_name, err)

    logger.warning(
        "All thumbnail attempts failed for %s/%s (tried %d candidates)",
        system_folder, rom_filename, len(candidates),
    )
    return None


# ---------------------------------------------------------------------------
# Cache persistence
# ---------------------------------------------------------------------------


def load_cache(cache_file):
    """Load the scrape cache from a JSON file.

    Args:
        cache_file: Path to the JSON cache file.

    Returns:
        Dict mapping "system/filename" → {"title": ..., "thumbnail": ...}.
        Returns {} if the file does not exist or cannot be parsed.
    """
    try:
        with open(cache_file, "r") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
        logger.warning("Cache file %s is not a JSON object; resetting to {}", cache_file)
        return {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to load cache %s: %s", cache_file, exc)
        return {}


def save_cache(cache_file, data):
    """Persist the scrape cache to a JSON file.

    Args:
        cache_file: Path to the JSON cache file.
        data:       Dict to serialise.
    """
    os.makedirs(os.path.dirname(os.path.abspath(cache_file)), exist_ok=True)
    with open(cache_file, "w") as fh:
        json.dump(data, fh, indent=2)


# ---------------------------------------------------------------------------
# Cache pruning
# ---------------------------------------------------------------------------


def prune_cache(cache_file, merged_dir):
    """Remove stale entries from the scraper cache.

    An entry is stale when its "system/filename" key no longer corresponds to
    any file currently present in *merged_dir*.  This happens after a rebuild
    renames symlinks (e.g. following ROM identification).

    Only runs when *cache_file* already exists; returns 0 immediately if it
    does not.

    Args:
        cache_file:  Path to the JSON cache file (e.g. "/config/gamecache.json").
        merged_dir:  Path to the merged ROM tree (system/filename layout).

    Returns:
        Number of entries removed from the cache.
    """
    if not os.path.isfile(cache_file):
        return 0

    cache = load_cache(cache_file)
    if not cache:
        return 0

    # Build the set of all current "system/filename" keys from merged_dir.
    current_keys = set()
    if os.path.isdir(merged_dir):
        try:
            for system_entry in os.scandir(merged_dir):
                if not system_entry.is_dir(follow_symlinks=False):
                    continue
                if system_entry.name.startswith("."):
                    continue
                try:
                    for file_entry in os.scandir(system_entry.path):
                        if file_entry.name.startswith("."):
                            continue
                        if file_entry.is_dir(follow_symlinks=False):
                            continue
                        current_keys.add(f"{system_entry.name}/{file_entry.name}")
                except OSError as exc:
                    logger.warning(
                        "prune_cache: cannot list system dir %s: %s",
                        system_entry.path,
                        exc,
                    )
        except OSError as exc:
            logger.warning("prune_cache: cannot scan merged dir %s: %s", merged_dir, exc)

    stale_keys = [key for key in cache if key not in current_keys]
    if not stale_keys:
        logger.info("prune_cache: no stale entries found")
        return 0

    for key in stale_keys:
        del cache[key]

    try:
        save_cache(cache_file, cache)
    except OSError as exc:
        logger.error("prune_cache: failed to save pruned cache: %s", exc)
        return 0

    logger.info("prune_cache: removed %d stale cache entries", len(stale_keys))
    return len(stale_keys)


# ---------------------------------------------------------------------------
# Bulk scrape
# ---------------------------------------------------------------------------


def scrape_all(merged_dir, cache_file, cache_dir, on_progress=None):
    """Walk merged_dir and download missing thumbnails for all games.

    Skips games already present in the cache. Rate-limits to 1 request per
    second between actual download attempts.

    Args:
        merged_dir:   Path to the merged ROM tree (system/filename layout).
        cache_file:   Path to the JSON cache file.
        cache_dir:    Root directory for cached thumbnails.
        on_progress:  Optional callable(current, total, game_name). Called
                      after each game is processed (including skips).

    Returns:
        Dict with keys: "scraped", "skipped", "failed", "total".
    """
    cache = load_cache(cache_file)

    # Collect all games first so we can report accurate totals.
    games = []  # list of (system_folder, rom_filename, original_filename)
    if os.path.isdir(merged_dir):
        try:
            for system_entry in sorted(os.scandir(merged_dir), key=lambda e: e.name):
                if not system_entry.is_dir(follow_symlinks=False):
                    continue
                if system_entry.name.startswith("."):
                    continue
                try:
                    for file_entry in sorted(
                        os.scandir(system_entry.path), key=lambda e: e.name
                    ):
                        if file_entry.name.startswith("."):
                            continue
                        if file_entry.is_dir(follow_symlinks=False):
                            continue
                        original_filename = None
                        if file_entry.is_symlink():
                            target = os.readlink(file_entry.path)
                            original_filename = os.path.basename(target)
                        games.append((system_entry.name, file_entry.name, original_filename))
                except OSError as exc:
                    logger.warning(
                        "Cannot list system dir %s: %s", system_entry.path, exc
                    )
        except OSError as exc:
            logger.warning("Cannot scan merged dir %s: %s", merged_dir, exc)

    total = len(games)
    scraped = 0
    skipped = 0
    failed = 0
    first_download = True

    for idx, (system_folder, rom_filename, original_filename) in enumerate(games, start=1):
        cache_key = f"{system_folder}/{rom_filename}"
        game_name = rom_name_to_thumbnail_name(rom_filename)

        if on_progress is not None:
            on_progress(idx, total, game_name)

        # Already in cache — skip entirely (no network request).
        if cache_key in cache:
            skipped += 1
            continue

        # Skip systems with no Libretro mapping (e.g. pico-8, ports).
        if SYSTEM_MAP.get(system_folder.lower()) is None:
            cache[cache_key] = {"title": game_name, "thumbnail": None}
            skipped += 1
            continue

        # Rate-limit: 1 second between download attempts.
        if not first_download:
            time.sleep(1)
        first_download = False

        rel_path = download_thumbnail(system_folder, rom_filename, cache_dir, original_filename)

        if rel_path is not None:
            cache[cache_key] = {"title": game_name, "thumbnail": rel_path}
            scraped += 1
        else:
            # Record a cache entry with no thumbnail so we don't retry on the
            # next run (avoids hammering the CDN for known-missing thumbnails).
            cache[cache_key] = {"title": game_name, "thumbnail": None}
            failed += 1

        # Persist after every entry so progress survives interruption.
        try:
            save_cache(cache_file, cache)
        except OSError as exc:
            logger.error("Failed to save cache after %s: %s", cache_key, exc)

    return {"scraped": scraped, "skipped": skipped, "failed": failed, "total": total}
