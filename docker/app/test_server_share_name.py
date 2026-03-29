"""
Tests for the share_name resolution logic in _api_get_games().

The logic is extracted here as a pure function so it can be tested without
spinning up an HTTP server or touching the filesystem.

Run with:  python -m pytest docker/app/test_server_share_name.py
       or:  python docker/app/test_server_share_name.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))


# ---------------------------------------------------------------------------
# Pure helper that mirrors the logic in _api_get_games()
# ---------------------------------------------------------------------------

def _resolve_share_name(target, sources):
    """Return the human-readable share name for a symlink *target*.

    Mirrors the logic added to server.py:_api_get_games():
      1. Sort sources by path length descending (longest prefix wins).
      2. If the target starts with a source path, return that source's name.
      3. Fall back to extracting the segment after "sources"/"roms"/"media".
    """
    if not target:
        return None

    sources_by_path_len = sorted(
        sources, key=lambda s: len(s.get("path", "")), reverse=True
    )

    share_name = None

    for src in sources_by_path_len:
        src_path = src.get("path", "")
        if src_path and (
            target == src_path
            or target.startswith(src_path + "/")
        ):
            share_name = src.get("name")
            break

    if share_name is None:
        parts = target.split(os.sep)
        for i, p in enumerate(parts):
            if p in ("sources", "roms", "media"):
                if i + 1 < len(parts):
                    share_name = parts[i + 1]
                    break

    return share_name


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestResolveShareName:

    def test_exact_source_path_match(self):
        """Target equal to source path returns that source's name."""
        sources = [{"name": "cnrd", "path": "/sources/100.112.135.3_arr/roms"}]
        result = _resolve_share_name("/sources/100.112.135.3_arr/roms", sources)
        assert result == "cnrd"

    def test_prefix_match(self):
        """Target that starts with source path returns that source's name."""
        sources = [{"name": "cnrd", "path": "/sources/100.112.135.3_arr/roms"}]
        result = _resolve_share_name(
            "/sources/100.112.135.3_arr/roms/snes/Mario.sfc", sources
        )
        assert result == "cnrd"

    def test_longest_prefix_wins(self):
        """When two sources share a common prefix, the longer one wins."""
        sources = [
            {"name": "short", "path": "/sources/foo/roms"},
            {"name": "long",  "path": "/sources/foo/roms/extra"},
        ]
        result = _resolve_share_name("/sources/foo/roms/extra/game.rom", sources)
        assert result == "long"

    def test_no_partial_segment_match(self):
        """A source path must match at a '/' boundary, not mid-segment."""
        sources = [{"name": "tricky", "path": "/sources/foo"}]
        # /sources/foobar starts with /sources/foo but not /sources/foo/
        result = _resolve_share_name("/sources/foobar/roms/game.rom", sources)
        # Should NOT match "tricky"; falls back to path-segment extraction
        assert result == "foobar"

    def test_fallback_when_no_source_matches(self):
        """Falls back to path-segment extraction when no source matches."""
        sources = [{"name": "other", "path": "/sources/other/roms"}]
        result = _resolve_share_name(
            "/sources/unknown_host/roms/nes/Contra.nes", sources
        )
        # Fallback: segment after "sources" → "unknown_host"
        assert result == "unknown_host"

    def test_fallback_empty_sources(self):
        """With no sources configured, falls back to path-segment extraction."""
        result = _resolve_share_name("/sources/myshare/roms/gba/game.gba", [])
        assert result == "myshare"

    def test_empty_target_returns_none(self):
        """An empty symlink target produces None."""
        sources = [{"name": "cnrd", "path": "/sources/foo/roms"}]
        assert _resolve_share_name("", sources) is None

    def test_source_missing_name_key(self):
        """A source dict without a 'name' key falls back to path-segment extraction."""
        sources = [{"path": "/sources/foo/roms"}]  # no "name"
        result = _resolve_share_name("/sources/foo/roms/game.rom", sources)
        # src.get("name") → None, so share_name stays None and the fallback
        # extracts the segment after "sources" → "foo".
        assert result == "foo"

    def test_source_missing_path_key(self):
        """A source dict without a 'path' key is skipped gracefully."""
        sources = [
            {"name": "nope"},  # no "path"
            {"name": "yes", "path": "/sources/bar/roms"},
        ]
        result = _resolve_share_name("/sources/bar/roms/game.rom", sources)
        assert result == "yes"

    def test_multiple_sources_correct_one_chosen(self):
        """Only the matching source's name is returned."""
        sources = [
            {"name": "alpha", "path": "/sources/alpha/roms"},
            {"name": "beta",  "path": "/sources/beta/roms"},
        ]
        assert _resolve_share_name("/sources/alpha/roms/game.rom", sources) == "alpha"
        assert _resolve_share_name("/sources/beta/roms/game.rom", sources) == "beta"

    def test_fallback_roms_segment(self):
        """Fallback extracts segment after 'roms' when 'sources' is absent."""
        result = _resolve_share_name("/mnt/roms/myshare/game.rom", [])
        assert result == "myshare"

    def test_fallback_media_segment(self):
        """Fallback extracts segment after 'media' when 'sources'/'roms' absent."""
        result = _resolve_share_name("/mnt/media/myshare/game.rom", [])
        assert result == "myshare"


# ---------------------------------------------------------------------------
# Entry point for plain `python test_server_share_name.py`
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import unittest

    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestResolveShareName)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
