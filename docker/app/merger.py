"""
merger.py — Symlink tree builder for retroshare.

Merges ROM sources into a single flat symlink tree under merged_dir.
First-source-wins: if two sources have the same system/filename, the first
source's file is used and subsequent ones are skipped. The local source
(local_source_path) is always promoted to the front of the source list so a
local copy of a game always wins over a remote one — this lets "promote to
local" work by simply copying a file into /sources/local/ and rebuilding.
"""

import json
import os
import re
import logging

import romident

logger = logging.getLogger(__name__)

_REGION_CODES = {"USA", "Japan", "Europe", "World", "France", "Germany", "Spain", "Italy", "UK", "Australia", "Brazil", "Canada", "China", "Korea", "Russia", "Netherlands", "Sweden", "Denmark", "Norway", "Finland", "Belgium", "Switzerland", "Austria"}

# File extensions that should never appear in the merged ROM tree. Save data
# pushed from R36S devices lands in /sources/local/<system>/<stem>.srm (and
# .sav / .state variants) next to its ROM — convenient for the user, but we
# must not symlink these files into /merged/ or they would show up on the
# roms SMB share as fake games.
_SAVE_EXTENSIONS = (".srm", ".sav", ".state")


def _is_save_file(filename):
    """True if filename is a save file (.srm / .sav / .state / .stateN / .state.auto)."""
    for ext in _SAVE_EXTENSIONS:
        idx = filename.rfind(ext)
        if idx <= 0:
            continue
        tail = filename[idx + len(ext):]
        if tail == "" or tail.isdigit() or tail == ".auto":
            return True
    return False


def _clean_name(name):
    """Clean up a ROM name from OpenVGDB/No-Intro naming conventions.

    - Strips verbose language tags like (En,Fr,De,Es,It)
    - Strips publisher and date tags
    - Keeps standard region codes like (USA), (Europe), (Japan)
    - Capitalizes "The" properly (No-Intro convention)
    - Replaces SMB/Windows-reserved characters (: < > " | ? *) with safe
      equivalents. OpenVGDB's releaseTitleName keeps the original title
      punctuation (e.g. "Summon Night: Swordcraft Story"), but Samba
      refuses to serve filenames containing reserved characters, so the
      R36S sync fails with "Download failed" when the canonical name is
      used verbatim. No-Intro's own filename convention already replaces
      ": " with " - ", which matches most existing source filenames.
    """
    segments = re.split(r"(\s*\([^)]+\))", name)
    kept = []
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        m = re.match(r"^\(([^)]+)\)$", seg)
        if m:
            inner = m.group(1)
            if inner not in _REGION_CODES:
                continue
            if kept and not kept[-1].endswith(" "):
                kept.append(" ")
            kept.append(m.group(0))
        else:
            kept.append(seg)

    result = "".join(kept)
    result = re.sub(r"(?<!\w)[Tt]he(?!\w)", "The", result)
    result = _sanitize_reserved(result)
    return result if result else name


def _sanitize_reserved(name):
    """Replace SMB/Windows-reserved filename characters with safe forms."""
    # Colon: No-Intro convention is ": " → " - ", bare ":" → " - ".
    name = re.sub(r":\s*", " - ", name)
    # Other reserved chars: drop or replace. These are rare in game titles.
    name = name.replace('"', "'")
    name = re.sub(r"[<>|?*]", "", name)
    # Collapse any double spaces introduced by the above.
    name = re.sub(r" {2,}", " ", name)
    return name.strip()


def _maybe_rename_file(src_file, dest_name, local_source_path):
    """Rename src_file to dest_name if it is on the local source and names differ.

    Returns the (possibly updated) source file path after renaming.
    """
    if local_source_path is None:
        return src_file
    if not os.path.dirname(src_file).startswith(local_source_path):
        return src_file
    if dest_name == os.path.basename(src_file):
        return src_file

    new_src = os.path.join(os.path.dirname(src_file), dest_name)
    if os.path.exists(new_src):
        logger.warning(
            "Rename skipped (target exists): %s -> %s",
            os.path.basename(src_file),
            dest_name,
        )
        return src_file

    try:
        os.rename(src_file, new_src)
        logger.info("Renamed: %s -> %s", os.path.basename(src_file), dest_name)
        return new_src
    except OSError as exc:
        logger.warning("Could not rename %s: %s", os.path.basename(src_file), exc)
        return src_file


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


def rebuild(sources, merged_dir, config_dir=None, local_source_path=None):
    """Rebuild the symlink tree in merged_dir from sources.

    Args:
        sources: list of dicts with keys 'name', 'path'
        merged_dir: absolute path to the merge destination directory
        config_dir: optional path to the config directory; if provided,
            OpenVGDB is used to rename symlinks to canonical No-Intro names.
        local_source_path: optional path to the local source (e.g.
            "/sources/local"); if provided, identified ROMs in this source
            will have their files renamed on disk to canonical names.

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

    # Local source always wins on collision: move it to the front of the list
    # so first-source-wins naturally picks it. This is the mechanism behind
    # "promote to local" — once a file is copied into /sources/local/ and a
    # rebuild runs, the merged symlink automatically repoints at the local copy.
    if local_source_path:
        sources = sorted(
            sources,
            key=lambda s: 0 if s.get("path") == local_source_path else 1,
        )

    # Track which destination paths already exist (first-source-wins)
    seen = set()
    systems_with_files = set()
    total_files = 0
    # canonical_key ("<system>/<dest_name>") -> absolute source file path.
    # Consumed by server.py to implement /api/promote.
    ownership = {}

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

            # List files inside the system folder, recursing into
            # subdirectories (e.g. pico-8/carts/).
            try:
                file_entries = os.listdir(system_path)
            except PermissionError as exc:
                logger.warning(
                    "Source '%s': cannot list system dir %s: %s", src_name, system_path, exc
                )
                continue

            scan_queue = [(system_path, file_entries)]
            while scan_queue:
                current_dir, entries = scan_queue.pop()
                for filename in entries:
                    if filename.startswith("."):
                        continue
                    full_path = os.path.join(current_dir, filename)
                    if os.path.isdir(full_path) and not os.path.islink(full_path):
                        try:
                            sub_entries = os.listdir(full_path)
                        except PermissionError:
                            continue
                        scan_queue.append((full_path, sub_entries))
                        continue
                    if _is_save_file(filename):
                        continue

                    src_file = full_path

                    # Attempt ROM identification to get a canonical symlink name.
                    dest_name = filename
                    if db_path is not None:
                        canonical = romident.identify_rom(db_path, src_file)
                        if canonical is not None:
                            canonical = _clean_name(canonical)
                            src_base, src_ext = os.path.splitext(filename)
                            canon_base, canon_ext = os.path.splitext(canonical)
                            if canon_base != src_base:
                                dest_name = canon_base + (src_ext if src_ext else canon_ext)
                            if dest_name != filename:
                                logger.debug("Identified: %s -> %s", filename, dest_name)

                    # Rename the source file on disk if it is in the local source.
                    actual_src = _maybe_rename_file(src_file, dest_name, local_source_path)

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
                        os.symlink(actual_src, dest_file)
                        systems_with_files.add(system)
                        total_files += 1
                        ownership[f"{system}/{dest_name}"] = actual_src
                    except OSError as exc:
                        logger.warning(
                            "Source '%s': could not create symlink %s -> %s: %s",
                            src_name,
                            dest_file,
                            src_file,
                            exc,
                        )

    if config_dir is not None:
        _write_ownership(config_dir, ownership, local_source_path)

    return {
        "systems": sorted(systems_with_files),
        "total_files": total_files,
    }


def _write_ownership(config_dir, ownership, local_source_path):
    """Persist the canonical-path → source-file-path mapping to ownership.json.

    Consumed by server.py's /api/promote endpoint, which needs to know which
    source file backs a given merged entry so it can copy that file into
    /sources/local/ on demand.
    """
    path = os.path.join(config_dir, "ownership.json")
    tmp = path + ".tmp"
    payload = {
        "local_source_path": local_source_path,
        "entries": ownership,
    }
    try:
        with open(tmp, "w") as fh:
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.rename(tmp, path)
    except OSError as exc:
        logger.warning("Could not write %s: %s", path, exc)
