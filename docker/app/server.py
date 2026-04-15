"""
server.py — HTTP API server for retroshare.

Serves the web UI and provides a JSON API for managing ROM sources and
triggering symlink tree rebuilds. Runs on port 8080, single-threaded.

On startup a rebuild is performed immediately so /merged/ is populated after
a container restart. A SourceWatcher then monitors the configured source
directories in the background and triggers an automatic rebuild after 30 s of
filesystem quiet (debounced). All rebuild code paths — HTTP-triggered and
watcher-triggered — are serialised through a module-level threading.Lock so
concurrent rebuilds cannot occur.

Endpoints:
    GET  /                                  → static/index.html
    GET  /static/*                          → files from static/
    GET  /api/sources                       → list configured sources
    POST /api/sources                       → save sources + rebuild
    GET  /api/browse?path=<p>              → list subdirectories under /sources/
    POST /api/rebuild                       → rebuild symlink tree
    GET  /api/status                        → current status summary
    GET  /api/games                         → all games grouped by system with metadata
    POST /api/scrape                        → trigger full thumbnail scrape (synchronous)
    POST /api/promote                       → copy a remote-source ROM into /sources/local/ then rebuild
    GET  /api/thumbnails/<system>/<file>    → serve cached thumbnail image
    GET  /api/identify?path=<p>             → debug: CRC32/MD5/SHA1 hashes + OpenVGDB lookup for a file
"""

import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs, quote, unquote

import merger
import romident
import scraper
from watcher import SourceWatcher

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PORT = 8080
CONFIG_FILE = "/config/sources.json"
OWNERSHIP_FILE = "/config/ownership.json"
MERGED_DIR = "/merged"
SOURCES_ROOT = "/sources"
SOURCES_LOCAL = "/sources/local"
CACHE_FILE = "/config/gamecache.json"
CACHE_DIR = "/config/thumbnails"

# Directory containing this script — used to locate static/
APP_DIR = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level rebuild lock and watcher
# ---------------------------------------------------------------------------

# Serialises all rebuild calls regardless of whether they originate from an
# HTTP handler or the background watcher thread.
_rebuild_lock = threading.Lock()

# Single SourceWatcher instance shared across the process lifetime.
_watcher = SourceWatcher()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}


def _load_sources():
    """Load sources from CONFIG_FILE. Returns [] on any error."""
    try:
        with open(CONFIG_FILE, "r") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return data
        logger.warning("sources.json is not a list; resetting to []")
        return []
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to load %s: %s", CONFIG_FILE, exc)
        return []


def _get_local_source_path(sources):
    """Return the path of the local source from sources, or None."""
    for s in sources:
        if s.get("path") == SOURCES_LOCAL:
            return s["path"]
    return None


def _save_sources(sources):
    """Persist sources list to CONFIG_FILE."""
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w") as fh:
        json.dump(sources, fh, indent=2)


def _merged_stats():
    """Return (systems, total_files) by inspecting the merged directory."""
    systems = []
    total_files = 0
    if not os.path.isdir(MERGED_DIR):
        return systems, total_files
    try:
        for entry in os.scandir(MERGED_DIR):
            if entry.is_dir(follow_symlinks=False):
                systems.append(entry.name)
                try:
                    total_files += sum(
                        1
                        for f in os.scandir(entry.path)
                        if f.is_symlink() or f.is_file(follow_symlinks=False)
                    )
                except OSError:
                    pass
    except OSError:
        pass
    return sorted(systems), total_files


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    """Single-threaded HTTP request handler."""

    # Silence default request logging — we do our own
    def log_message(self, fmt, *args):  # noqa: N802
        logger.info("HTTP %s %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self._serve_static_file("index.html")
        elif path.startswith("/static/"):
            rel = path[len("/static/"):]
            self._serve_static_file(rel)
        elif path == "/api/sources":
            self._api_get_sources()
        elif path == "/api/browse":
            qs = parse_qs(parsed.query)
            browse_path = qs.get("path", [None])[0]
            self._api_browse(browse_path)
        elif path == "/api/status":
            self._api_status()
        elif path == "/api/games":
            self._api_get_games()
        elif path == "/api/identify":
            qs = parse_qs(parsed.query)
            ident_path = qs.get("path", [None])[0]
            self._api_identify(ident_path)
        elif path.startswith("/api/thumbnails/"):
            self._api_serve_thumbnail(path[len("/api/thumbnails/"):])
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/sources":
            self._api_post_sources()
        elif path == "/api/rebuild":
            self._api_rebuild()
        elif path == "/api/scrape":
            self._api_scrape()
        elif path == "/api/promote":
            self._api_promote()
        else:
            self._send_json({"error": "not found"}, status=404)

    # ------------------------------------------------------------------
    # Static file serving
    # ------------------------------------------------------------------

    def _serve_static_file(self, rel_path):
        # Normalise and prevent path traversal
        safe_rel = os.path.normpath(rel_path).lstrip("/")
        full_path = os.path.join(APP_DIR, "static", safe_rel)
        real_full = os.path.realpath(full_path)
        static_root = os.path.realpath(os.path.join(APP_DIR, "static"))

        if not real_full.startswith(static_root + os.sep) and real_full != static_root:
            self._send_json({"error": "forbidden"}, status=403)
            return

        if not os.path.isfile(real_full):
            self._send_json({"error": "not found"}, status=404)
            return

        _, ext = os.path.splitext(real_full)
        content_type = MIME_TYPES.get(ext.lower(), "application/octet-stream")

        try:
            with open(real_full, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            logger.error("Cannot read static file %s: %s", real_full, exc)
            self._send_json({"error": "internal error"}, status=500)
            return

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ------------------------------------------------------------------
    # API handlers
    # ------------------------------------------------------------------

    def _api_get_sources(self):
        sources = _load_sources()
        self._send_json(sources)

    def _api_post_sources(self):
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, list):
            self._send_json({"error": "body must be a JSON array"}, status=400)
            return

        try:
            _save_sources(body)
        except OSError as exc:
            logger.error("Failed to save sources: %s", exc)
            self._send_json({"error": "failed to save config"}, status=500)
            return

        result = self._do_rebuild(body)
        _watcher.update_paths([s["path"] for s in body if s.get("path")])
        self._send_json({"status": "ok", **result})

    def _api_browse(self, browse_path):
        if not browse_path:
            self._send_json({"error": "path parameter required"}, status=400)
            return

        # Resolve to an absolute path and ensure it stays under SOURCES_ROOT
        # If the path already starts with SOURCES_ROOT, use it directly
        if browse_path.startswith(SOURCES_ROOT):
            candidate = os.path.realpath(browse_path)
        else:
            candidate = os.path.realpath(os.path.join(SOURCES_ROOT, browse_path.lstrip("/")))
        sources_real = os.path.realpath(SOURCES_ROOT)

        # Allow browsing SOURCES_ROOT itself or anything strictly under it
        if candidate != sources_real and not candidate.startswith(sources_real + os.sep):
            self._send_json({"error": "path outside /sources/ is not allowed"}, status=403)
            return

        if not os.path.isdir(candidate):
            self._send_json({"error": "path is not a directory"}, status=404)
            return

        try:
            dirs = sorted(
                e.name
                for e in os.scandir(candidate)
                if e.is_dir(follow_symlinks=True) and not e.name.startswith(".")
            )
        except PermissionError as exc:
            logger.warning("Browse permission denied for %s: %s", candidate, exc)
            self._send_json({"error": "permission denied"}, status=403)
            return

        self._send_json({"dirs": dirs, "path": candidate})

    def _api_rebuild(self):
        sources = _load_sources()
        result = self._do_rebuild(sources)
        self._send_json({"status": "ok", **result})

    def _api_status(self):
        sources = _load_sources()
        systems, total_files = _merged_stats()
        self._send_json(
            {
                "sources_count": len(sources),
                "systems": systems,
                "total_files": total_files,
            }
        )

    def _api_get_games(self):
        cache = scraper.load_cache(CACHE_FILE)

        # Load sources once and sort longest path first so the most-specific
        # prefix wins when source paths overlap (e.g. /sources/foo/roms/extra
        # before /sources/foo/roms).
        sources = _load_sources()
        sources_by_path_len = sorted(
            sources, key=lambda s: len(s.get("path", "")), reverse=True
        )

        systems_map = {}  # system_name → list of game dicts

        if os.path.isdir(MERGED_DIR):
            try:
                for system_entry in os.scandir(MERGED_DIR):
                    if not system_entry.is_dir(follow_symlinks=False):
                        continue
                    if system_entry.name.startswith("."):
                        continue
                    system_name = system_entry.name
                    games = []
                    try:
                        for file_entry in os.scandir(system_entry.path):
                            if file_entry.name.startswith("."):
                                continue
                            if file_entry.is_dir(follow_symlinks=False):
                                continue
                            filename = file_entry.name
                            cache_key = f"{system_name}/{filename}"
                            entry = cache.get(cache_key)
                            if entry is not None:
                                title = entry.get("title") or os.path.splitext(filename)[0]
                                thumb_rel = entry.get("thumbnail")
                                if thumb_rel:
                                    thumb_filename = os.path.basename(thumb_rel)
                                    thumbnail_url = (
                                        f"/api/thumbnails/{quote(system_name, safe='')}"
                                        f"/{quote(thumb_filename, safe='')}"
                                    )
                                else:
                                    thumbnail_url = None
                                scraped = True
                            else:
                                title = os.path.splitext(filename)[0]
                                thumbnail_url = None
                                scraped = False

                            share_name = None
                            try:
                                target = os.readlink(file_entry.path)
                                if target:
                                    # Match symlink target against known source
                                    # paths (longest first) to get the
                                    # human-readable source name.
                                    for src in sources_by_path_len:
                                        src_path = src.get("path", "")
                                        if src_path and (
                                            target == src_path
                                            or target.startswith(src_path + "/")
                                        ):
                                            share_name = src.get("name")
                                            break
                                    # Fallback: extract the segment after
                                    # "sources" / "roms" / "media" in the path.
                                    if share_name is None:
                                        parts = target.split(os.sep)
                                        for i, p in enumerate(parts):
                                            if p in ("sources", "roms", "media"):
                                                if i + 1 < len(parts):
                                                    share_name = parts[i + 1]
                                                    break
                            except OSError:
                                pass

                            games.append(
                                {
                                    "filename": filename,
                                    "title": title,
                                    "thumbnail": thumbnail_url,
                                    "scraped": scraped,
                                    "share": share_name,
                                }
                            )
                    except OSError as exc:
                        logger.warning(
                            "Cannot list system dir %s: %s", system_entry.path, exc
                        )
                    games.sort(key=lambda g: g["title"].lower())
                    systems_map[system_name] = games
            except OSError as exc:
                logger.warning("Cannot scan merged dir %s: %s", MERGED_DIR, exc)

        systems_list = [
            {"name": name, "games": games}
            for name, games in sorted(systems_map.items())
        ]
        total_games = sum(len(s["games"]) for s in systems_list)
        self._send_json({"systems": systems_list, "total_games": total_games})

    def _api_identify(self, file_path):
        """Debug endpoint: hash file_path and look it up in OpenVGDB.

        Only allows paths under /sources/ or /merged/.
        """
        if not file_path:
            self._send_json({"error": "path parameter required"}, status=400)
            return

        real_path = os.path.realpath(file_path)
        sources_real = os.path.realpath(SOURCES_ROOT)
        merged_real = os.path.realpath(MERGED_DIR)

        if not (
            real_path.startswith(sources_real + os.sep)
            or real_path == sources_real
            or real_path.startswith(merged_real + os.sep)
            or real_path == merged_real
        ):
            self._send_json({"error": "path must be under /sources/ or /merged/"}, status=403)
            return

        if not os.path.isfile(real_path):
            self._send_json({"error": "file not found"}, status=404)
            return

        db_path = romident.ensure_db(os.path.dirname(CACHE_FILE))
        if db_path is None:
            self._send_json({"error": "OpenVGDB unavailable"}, status=503)
            return

        try:
            hashes = romident.hash_rom(real_path)
        except OSError as exc:
            self._send_json({"error": f"hash failed: {exc}"}, status=500)
            return

        if hashes is None:
            self._send_json({"error": "hash failed: could not read file (corrupt zip?)"}, status=500)
            return

        canonical = romident.lookup_crc(db_path, hashes["crc32"])
        self._send_json({
            "path": real_path,
            "crc32": hashes["crc32"],
            "md5": hashes["md5"],
            "sha1": hashes["sha1"],
            "found": canonical is not None,
            "canonical": canonical,
        })

    def _api_scrape(self):
        try:
            stats = scraper.scrape_all(MERGED_DIR, CACHE_FILE, CACHE_DIR)
        except Exception as exc:  # noqa: BLE001
            logger.error("Scrape failed: %s", exc)
            self._send_json({"error": str(exc)}, status=500)
            return
        self._send_json({"status": "ok", **stats})

    def _api_promote(self):
        """Copy a ROM from a remote source into /sources/local/, then rebuild.

        Body: {"system": "<system>", "rom": "<canonical filename>"}

        Uses /config/ownership.json (written by merger.rebuild) to resolve the
        current source file backing the merged entry. If the ROM already lives
        in the local source, this is a no-op. On success the caller can safely
        upload a save file for this ROM to the [saves] share knowing the ROM
        is now durably in the user's own share.

        Copy is atomic-on-rename: data → <dest>.tmp → fsync → rename → fsync dir.
        """
        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict):
            self._send_json({"error": "body must be a JSON object"}, status=400)
            return

        system = body.get("system")
        rom = body.get("rom")
        if not system or not rom:
            self._send_json({"error": "system and rom required"}, status=400)
            return
        # Reject path separators in either component — keys are flat names.
        if "/" in system or "/" in rom or system.startswith(".") or rom.startswith("."):
            self._send_json({"error": "invalid system or rom"}, status=400)
            return

        try:
            with open(OWNERSHIP_FILE, "r") as fh:
                ownership = json.load(fh)
        except FileNotFoundError:
            self._send_json({"error": "ownership manifest missing — trigger a rebuild first"}, status=503)
            return
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to read ownership manifest: %s", exc)
            self._send_json({"error": "failed to read ownership manifest"}, status=500)
            return

        entries = ownership.get("entries", {}) if isinstance(ownership, dict) else {}
        key = f"{system}/{rom}"
        src_file = entries.get(key)
        if not src_file:
            self._send_json({"error": f"no ownership entry for {key}"}, status=404)
            return

        # If the source file already lives under /sources/local, nothing to do.
        # (A save upload can land immediately.)
        real_src = os.path.realpath(src_file)
        local_real = os.path.realpath(SOURCES_LOCAL)
        if real_src.startswith(local_real + os.sep) or real_src == local_real:
            self._send_json({"status": "ok", "promoted": False, "reason": "already local"})
            return

        if not os.path.isfile(real_src):
            logger.warning("Promote: source file missing on disk: %s", real_src)
            self._send_json({"error": "source file missing on disk"}, status=404)
            return

        dest_dir = os.path.join(SOURCES_LOCAL, system)
        dest_file = os.path.join(dest_dir, rom)
        tmp_file = dest_file + ".tmp"

        try:
            os.makedirs(dest_dir, exist_ok=True)
            with open(real_src, "rb") as s, open(tmp_file, "wb") as d:
                while True:
                    chunk = s.read(1024 * 1024)
                    if not chunk:
                        break
                    d.write(chunk)
                d.flush()
                os.fsync(d.fileno())
            os.rename(tmp_file, dest_file)
            dir_fd = os.open(dest_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as exc:
            logger.error("Promote failed for %s: %s", key, exc)
            try:
                if os.path.exists(tmp_file):
                    os.unlink(tmp_file)
            except OSError:
                pass
            self._send_json({"error": f"copy failed: {exc}"}, status=500)
            return

        logger.info("Promoted %s → %s", real_src, dest_file)

        # Rebuild so the merged symlink repoints at the new local copy.
        # local-wins ordering in merger.rebuild guarantees it takes precedence.
        sources = _load_sources()
        self._do_rebuild(sources)

        self._send_json({"status": "ok", "promoted": True, "dest": dest_file})

    def _api_serve_thumbnail(self, raw_path):
        """Serve a cached thumbnail from CACHE_DIR.

        raw_path is the URL suffix after /api/thumbnails/, e.g.
        "snes/Super%20Mario%20World%20(USA).png".  Both components are
        URL-decoded before resolving to a filesystem path.
        """
        # URL-decode the full path, then split into at most two components.
        decoded = unquote(raw_path)
        parts = decoded.split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            self._send_json({"error": "invalid thumbnail path"}, status=400)
            return

        system, filename = parts[0], parts[1]

        # Prevent path traversal: normalise each component independently and
        # reject anything that contains a separator after normalisation.
        safe_system = os.path.normpath(system)
        safe_filename = os.path.normpath(filename)
        if os.sep in safe_system or os.sep in safe_filename:
            self._send_json({"error": "forbidden"}, status=403)
            return

        full_path = os.path.join(CACHE_DIR, safe_system, safe_filename)
        real_full = os.path.realpath(full_path)
        cache_real = os.path.realpath(CACHE_DIR)

        if not real_full.startswith(cache_real + os.sep):
            self._send_json({"error": "forbidden"}, status=403)
            return

        if not os.path.isfile(real_full):
            self._send_json({"error": "not found"}, status=404)
            return

        try:
            with open(real_full, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            logger.error("Cannot read thumbnail %s: %s", real_full, exc)
            self._send_json({"error": "internal error"}, status=500)
            return

        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _do_rebuild(self, sources):
        """Run merger.rebuild under the module-level lock and return the stats dict.

        The lock prevents concurrent rebuilds from HTTP handlers and the
        background watcher thread. Callers see identical return-value semantics
        to the pre-lock version.
        """
        rebuild_ok = False
        with _rebuild_lock:
            try:
                result = merger.rebuild(
                    sources, MERGED_DIR,
                    config_dir="/config",
                    local_source_path=_get_local_source_path(sources),
                )
                logger.info(
                    "Rebuild complete: %d systems, %d files",
                    len(result["systems"]),
                    result["total_files"],
                )
                rebuild_ok = True
            except Exception as exc:  # noqa: BLE001
                logger.error("Rebuild failed: %s", exc)
                result = {"systems": [], "total_files": 0, "error": str(exc)}

        if rebuild_ok:
            scraper.prune_cache(CACHE_FILE, MERGED_DIR)

        return result

    def _read_json_body(self):
        """Read and parse the request body as JSON. Sends error response and
        returns None on failure."""
        length_str = self.headers.get("Content-Length", "0")
        try:
            length = int(length_str)
        except ValueError:
            self._send_json({"error": "invalid Content-Length"}, status=400)
            return None

        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            self._send_json({"error": f"invalid JSON: {exc}"}, status=400)
            return None

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _watcher_rebuild_callback():
    """Callback invoked by SourceWatcher after a debounce period.

    Loads the current sources from disk and runs a rebuild under the shared
    lock. Exceptions are caught so they cannot kill the background timer thread.
    """
    try:
        sources = _load_sources()
        rebuild_ok = False
        with _rebuild_lock:
            try:
                result = merger.rebuild(
                    sources, MERGED_DIR,
                    config_dir="/config",
                    local_source_path=_get_local_source_path(sources),
                )
                logger.info(
                    "Auto-rebuild complete: %d systems, %d files",
                    len(result["systems"]),
                    result["total_files"],
                )
                rebuild_ok = True
            except Exception as exc:  # noqa: BLE001
                logger.error("Auto-rebuild failed: %s", exc)

        if rebuild_ok:
            scraper.prune_cache(CACHE_FILE, MERGED_DIR)
            try:
                stats = scraper.scrape_all(MERGED_DIR, CACHE_FILE, CACHE_DIR)
                logger.info(
                    "Auto-scrape complete: %d scraped, %d failed, %d skipped",
                    stats.get("scraped", 0),
                    stats.get("failed", 0),
                    stats.get("skipped", 0),
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("Auto-scrape failed: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.error("Watcher callback error: %s", exc)


def main():
    # ------------------------------------------------------------------
    # Startup rebuild — populate /merged/ before accepting requests
    # ------------------------------------------------------------------
    logger.info("Running startup rebuild…")
    sources = _load_sources()
    try:
        result = merger.rebuild(
            sources, MERGED_DIR,
            config_dir="/config",
            local_source_path=_get_local_source_path(sources),
        )
        logger.info(
            "Startup rebuild complete: %d systems, %d files",
            len(result["systems"]),
            result["total_files"],
        )
        scraper.prune_cache(CACHE_FILE, MERGED_DIR)
        try:
            stats = scraper.scrape_all(MERGED_DIR, CACHE_FILE, CACHE_DIR)
            logger.info(
                "Startup scrape complete: %d scraped, %d failed, %d skipped",
                stats.get("scraped", 0),
                stats.get("failed", 0),
                stats.get("skipped", 0),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Startup scrape failed: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.error("Startup rebuild failed: %s", exc)

    # ------------------------------------------------------------------
    # Start filesystem watcher
    # ------------------------------------------------------------------
    source_paths = [s["path"] for s in sources if s.get("path")]
    _watcher.start(paths=source_paths, on_change_callback=_watcher_rebuild_callback)

    # ------------------------------------------------------------------
    # HTTP server
    # ------------------------------------------------------------------
    server = HTTPServer(("", PORT), Handler)
    logger.info("retroshare backend listening on port %d", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down")
    finally:
        _watcher.stop()
        server.server_close()


if __name__ == "__main__":
    main()
