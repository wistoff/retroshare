"""
server.py — HTTP API server for retroshare.

Serves the web UI and provides a JSON API for managing ROM sources and
triggering symlink tree rebuilds. Runs on port 8080, single-threaded.

Endpoints:
    GET  /                      → static/index.html
    GET  /static/*              → files from static/
    GET  /api/sources           → list configured sources
    POST /api/sources           → save sources + rebuild
    GET  /api/browse?path=<p>   → list subdirectories under /sources/
    POST /api/rebuild           → rebuild symlink tree
    GET  /api/status            → current status summary
"""

import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

import merger

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PORT = 8080
CONFIG_FILE = "/config/sources.json"
MERGED_DIR = "/merged"
SOURCES_ROOT = "/sources"

# Directory containing this script — used to locate static/
APP_DIR = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

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
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/sources":
            self._api_post_sources()
        elif path == "/api/rebuild":
            self._api_rebuild()
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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _do_rebuild(self, sources):
        """Run merger.rebuild and return the stats dict."""
        try:
            result = merger.rebuild(sources, MERGED_DIR)
            logger.info(
                "Rebuild complete: %d systems, %d files",
                len(result["systems"]),
                result["total_files"],
            )
            return result
        except Exception as exc:  # noqa: BLE001
            logger.error("Rebuild failed: %s", exc)
            return {"systems": [], "total_files": 0, "error": str(exc)}

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


def main():
    server = HTTPServer(("", PORT), Handler)
    logger.info("retroshare backend listening on port %d", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
