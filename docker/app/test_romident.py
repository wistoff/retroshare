"""
Tests for romident.py — focused on the streaming hash path added to fix the
large-file memory issue.

Run with:  python -m pytest docker/app/test_romident.py  (from repo root)
       or:  python docker/app/test_romident.py
"""

import hashlib
import io
import os
import sys
import tempfile
import zipfile
import zlib

# Allow running directly from this directory or from the repo root.
sys.path.insert(0, os.path.dirname(__file__))

import romident  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reference_hashes(data: bytes, skip: int) -> dict:
    """Compute expected hashes in-memory for comparison."""
    payload = data[skip:]
    return {
        "crc32": format(zlib.crc32(payload) & 0xFFFFFFFF, "08X"),
        "md5": hashlib.md5(payload).hexdigest().upper(),
        "sha1": hashlib.sha1(payload).hexdigest().upper(),
    }


# ---------------------------------------------------------------------------
# _compute_hashes_stream
# ---------------------------------------------------------------------------

class TestComputeHashesStream:
    """_compute_hashes_stream must produce the same output as _compute_hashes."""

    def test_no_skip(self):
        data = b"Hello, ROM world!" * 1000
        expected = romident._compute_hashes(data, 0)
        fh = io.BytesIO(data)
        assert romident._compute_hashes_stream(fh, 0) == expected

    def test_with_skip(self):
        header = b"\x00" * 512
        payload = b"ROM payload data" * 500
        data = header + payload
        expected = romident._compute_hashes(data, 512)
        fh = io.BytesIO(data)
        assert romident._compute_hashes_stream(fh, 512) == expected

    def test_multi_chunk(self):
        """Data larger than _CHUNK_SIZE exercises the chunked read loop."""
        # 3 MiB — larger than the 1 MiB _CHUNK_SIZE
        data = bytes(range(256)) * (3 * 1024 * 4)
        expected = romident._compute_hashes(data, 0)
        fh = io.BytesIO(data)
        assert romident._compute_hashes_stream(fh, 0) == expected

    def test_empty_after_skip(self):
        """Skipping past all data should hash an empty payload."""
        data = b"header only"
        expected = romident._compute_hashes(data, len(data))
        fh = io.BytesIO(data)
        assert romident._compute_hashes_stream(fh, len(data)) == expected


# ---------------------------------------------------------------------------
# _snes_header_skip
# ---------------------------------------------------------------------------

class TestSnesHeaderSkip:
    def test_no_header(self):
        # Exact multiple of 1024 — no copier header
        assert romident._snes_header_skip(512 * 1024) == 0

    def test_with_header(self):
        # 512 extra bytes — copier header present
        assert romident._snes_header_skip(512 * 1024 + 512) == 512

    def test_small_rom_no_header(self):
        assert romident._snes_header_skip(256 * 1024) == 0

    def test_small_rom_with_header(self):
        assert romident._snes_header_skip(256 * 1024 + 512) == 512


# ---------------------------------------------------------------------------
# hash_rom — non-zip path (streaming)
# ---------------------------------------------------------------------------

class TestHashRomNonZip:
    """hash_rom on a plain file must stream and produce correct hashes."""

    def _write_tmp(self, suffix: str, data: bytes) -> str:
        fd, path = tempfile.mkstemp(suffix=suffix)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        return path

    def test_plain_rom_no_header(self):
        data = b"cartridge data" * 2000
        path = self._write_tmp(".gba", data)
        try:
            result = romident.hash_rom(path)
        finally:
            os.unlink(path)
        assert result == _reference_hashes(data, 0)

    def test_nes_header_skipped(self):
        header = b"\x4e\x45\x53\x1a" + b"\x00" * 12  # 16-byte iNES header
        payload = b"NES ROM data" * 1000
        data = header + payload
        path = self._write_tmp(".nes", data)
        try:
            result = romident.hash_rom(path)
        finally:
            os.unlink(path)
        assert result == _reference_hashes(data, 16)

    def test_snes_copier_header_detected_via_getsize(self):
        """SNES header detection must use os.path.getsize, not read()."""
        header = b"\x00" * 512
        payload = b"SNES ROM" * (256 * 128)  # 256 KiB payload
        data = header + payload  # total size % 1024 == 512 → header present
        assert len(data) % 1024 == 512  # sanity-check the fixture
        path = self._write_tmp(".sfc", data)
        try:
            result = romident.hash_rom(path)
        finally:
            os.unlink(path)
        assert result == _reference_hashes(data, 512)

    def test_snes_no_copier_header(self):
        payload = b"SNES ROM" * (256 * 128)  # exact 256 KiB, no header
        assert len(payload) % 1024 == 0  # sanity-check
        path = self._write_tmp(".sfc", payload)
        try:
            result = romident.hash_rom(path)
        finally:
            os.unlink(path)
        assert result == _reference_hashes(payload, 0)

    def test_large_file_streamed(self):
        """A file larger than _CHUNK_SIZE must hash correctly."""
        data = bytes(range(256)) * (2 * 1024 * 4)  # 2 MiB
        path = self._write_tmp(".bin", data)
        try:
            result = romident.hash_rom(path)
        finally:
            os.unlink(path)
        assert result == _reference_hashes(data, 0)


# ---------------------------------------------------------------------------
# hash_rom — zip path (unchanged, in-memory)
# ---------------------------------------------------------------------------

class TestHashRomZip:
    """Zip path must still work correctly after the refactor."""

    def _make_zip(self, inner_name: str, inner_data: bytes) -> str:
        fd, path = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(inner_name, inner_data)
        return path

    def test_zip_plain_rom(self):
        data = b"GBA ROM data" * 500
        path = self._make_zip("game.gba", data)
        try:
            result = romident.hash_rom(path)
        finally:
            os.unlink(path)
        assert result == _reference_hashes(data, 0)

    def test_zip_nes_header_skipped(self):
        header = b"\x00" * 16
        payload = b"NES data" * 500
        data = header + payload
        path = self._make_zip("game.nes", data)
        try:
            result = romident.hash_rom(path)
        finally:
            os.unlink(path)
        assert result == _reference_hashes(data, 16)

    def test_zip_empty_returns_none(self):
        fd, path = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        with zipfile.ZipFile(path, "w"):
            pass  # empty archive
        try:
            result = romident.hash_rom(path)
        finally:
            os.unlink(path)
        assert result is None

    def test_zip_corrupt_returns_none(self):
        fd, path = tempfile.mkstemp(suffix=".zip")
        try:
            os.write(fd, b"this is not a zip file")
        finally:
            os.close(fd)
        try:
            result = romident.hash_rom(path)
        finally:
            os.unlink(path)
        assert result is None


# ---------------------------------------------------------------------------
# Entry point for plain `python test_romident.py`
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import unittest

    # Collect all test classes defined above and run them.
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in [
        TestComputeHashesStream,
        TestSnesHeaderSkip,
        TestHashRomNonZip,
        TestHashRomZip,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
