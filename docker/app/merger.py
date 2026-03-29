"""
merger.py — Symlink tree builder for retroshare.

Merges ROM sources into a single flat symlink tree under merged_dir.
First-source-wins: if two sources have the same system/filename, the first
source's file is used and subsequent ones are skipped.
"""

import os
import logging

import romident

logger = logging.getLogger(__name__)


def _clear_merged(merged_dir):
    """Remove all symlinks and empty directories under merged_dir (bottom-up).

    Does not follow symlinks when traversing, so existing symlink targets are
    never touched.
    """
    if not os.path.isdir(merged_dir):
        return

    for dirpath, dirnames, filenames in os.walk(merged_dir, topdown=False, followlinks=False):
        # Remove symlinks (files and directory symlinks)
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                try:
                    os.unlink(full)
                except OSError as exc:
                    logger.warning("Could not remove symlink %s: %s", full, exc)

        # Also remove any symlinks that os.walk reported as dirs
        for name in dirnames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                try:
                    os.unlink(full)
                except OSError as exc:
                    logger.warning("Could not remove dir-symlink %s: %s", full, exc)

        # Remove the directory itself if it is now empty (and not the root)
        if dirpath != merged_dir:
            try:
                os.rmdir(dirpath)
            except OSError:
                # Not empty — leave it; not fatal
                pass


def rebuild(sources, merged_dir, config_dir=None):
    """Rebuild the symlink tree in merged_dir from sources.

    Args:
        sources: list of dicts with keys 'name', 'path'
        merged_dir: absolute path to the merge destination directory
        config_dir: optional path to the config directory; if provided,
            OpenVGDB is used to rename symlinks to canonical No-Intro names.

    Returns:
        dict with keys:
            'systems': sorted list of system folder names that have at least one file
            'total_files': total number of symlinks created
    """
    _clear_merged(merged_dir)

    # Resolve the ROM identification DB once per rebuild (not per file).
    db_path = None
    if config_dir is not None:
        db_path = romident.ensure_db(config_dir)
        if db_path is None:
            logger.warning("OpenVGDB unavailable — ROM identification disabled for this rebuild")

    # Track which destination paths already exist (first-source-wins)
    seen = set()
    systems_with_files = set()
    total_files = 0

    for source in sources:
        src_path = source.get("path", "")
        src_name = source.get("name", src_path)

        # The source path points directly to the folder containing system folders
        rom_root_path = src_path

        if not os.path.isdir(rom_root_path):
            logger.warning(
                "Source '%s': path does not exist or is not a directory: %s",
                src_name,
                rom_root_path,
            )
            continue

        # List system folders (top-level subdirectories of the ROM root)
        try:
            entries = os.listdir(rom_root_path)
        except PermissionError as exc:
            logger.warning("Source '%s': cannot list %s: %s", src_name, rom_root_path, exc)
            continue

        for system in sorted(entries):
            if system.startswith("."):
                continue  # skip hidden

            system_path = os.path.join(rom_root_path, system)
            if not os.path.isdir(system_path):
                continue  # only process directories as system folders

            # List files inside the system folder (flat, no recursion)
            try:
                file_entries = os.listdir(system_path)
            except PermissionError as exc:
                logger.warning(
                    "Source '%s': cannot list system dir %s: %s", src_name, system_path, exc
                )
                continue

            for filename in file_entries:
                if filename.startswith("."):
                    continue  # skip hidden files

                src_file = os.path.join(system_path, filename)

                # Skip subdirectories within system folders (flat listing only)
                if os.path.isdir(src_file) and not os.path.islink(src_file):
                    continue

                # Attempt ROM identification to get a canonical symlink name.
                dest_name = filename
                if db_path is not None:
                    canonical = romident.identify_rom(db_path, src_file)
                    if canonical is not None and canonical != filename:
                        logger.debug("Renamed: %s -> %s", filename, canonical)
                        dest_name = canonical

                dest_system_dir = os.path.join(merged_dir, system)
                dest_file = os.path.join(dest_system_dir, dest_name)

                # First-source-wins (keyed on destination path, which uses the
                # canonical name so deduplication works correctly after renaming)
                if dest_file in seen:
                    continue

                seen.add(dest_file)

                # Create system directory if needed
                os.makedirs(dest_system_dir, exist_ok=True)

                # Create the symlink
                try:
                    os.symlink(src_file, dest_file)
                    systems_with_files.add(system)
                    total_files += 1
                except OSError as exc:
                    logger.warning(
                        "Source '%s': could not create symlink %s -> %s: %s",
                        src_name,
                        dest_file,
                        src_file,
                        exc,
                    )

    return {
        "systems": sorted(systems_with_files),
        "total_files": total_files,
    }
