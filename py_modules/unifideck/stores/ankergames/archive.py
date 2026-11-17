"""AnkerGames archive extraction.

Every download observed during reverse-engineering was a plain ``.zip``
(``Slotbound-AnkerGames.zip``, served with a matching
``Content-Disposition``). This still detects by magic bytes and falls back
to the external-tool ladder for anything else, the same way
``stores/gamevault/archive.py`` does (adapted from it, trimmed to this
store's needs — same repo, same license, not imported across stores per
``shared/store_base.py``'s self-containment note).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import zipfile
from pathlib import Path

from unifideck.stores.shared.cli_install_helpers import terminate_process_tree
from unifideck.stores.shared.zip_extract import extract_zip_interruptible

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[AnkerGames archive]"
_ARCH_ZIP = "zip"
_ARCH_LIBARCHIVE = "libarchive"
_EXTRACTORS: tuple[str, ...] = ("bsdtar", "7z")

_STDLIB_ZIP_METHODS: frozenset[int] = frozenset(
    m for m in (
        zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA,
        getattr(zipfile, "ZIP_ZSTANDARD", None),
    )
    if m is not None
)


def detect_format(path: Path) -> str | None:
    """Detect archive format from magic bytes (zip, or anything bsdtar/7z read)."""
    try:
        with path.open("rb") as fh:
            header = fh.read(8)
    except OSError:
        return None
    if header[:2] == b"PK":
        return _ARCH_ZIP
    if header[:6] == b"7z\xbc\xaf'\x1c":
        return _ARCH_LIBARCHIVE
    if header[:3] == b"Rar":
        return _ARCH_LIBARCHIVE
    for magic in (b"\xfd7zXZ\x00", b"\x1f\x8b", b"BZh"):
        if header.startswith(magic):
            return _ARCH_LIBARCHIVE
    return None


async def extract_archive(archive: Path, dest: Path) -> None:
    """Unpack *archive* into *dest*; raises on an unknown/unsupported format."""
    fmt = detect_format(archive)
    if fmt is None:
        raise RuntimeError(f"AnkerGames: unknown archive format: {archive.name}")
    await asyncio.to_thread(dest.mkdir, parents=True, exist_ok=True)
    if fmt == _ARCH_ZIP and not await asyncio.to_thread(_zip_needs_tool, archive):
        await extract_zip_interruptible(archive, dest)
        return
    await _extract_with_tool(archive, dest)


def _zip_needs_tool(archive: Path) -> bool:
    with zipfile.ZipFile(archive, "r") as zf:
        return any(info.compress_type not in _STDLIB_ZIP_METHODS for info in zf.infolist())


async def _extract_with_tool(archive: Path, dest: Path) -> None:
    tried: list[str] = []
    for tool in _EXTRACTORS:
        if not shutil.which(tool):
            continue
        tried.append(tool)
        cmd = (
            ["bsdtar", "-xf", str(archive), "-C", str(dest)] if tool == "bsdtar"
            else ["7z", "x", str(archive), f"-o{dest}", "-y"]
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await proc.communicate()
        except BaseException:
            await terminate_process_tree(proc, _LOG_PREFIX)
            raise
        if proc.returncode == 0:
            return
        logger.warning("%s %s failed: %s", _LOG_PREFIX, tool, stderr.decode(errors="replace")[:200])
    if not tried:
        raise RuntimeError(f"AnkerGames: no extractor available (looked for {_EXTRACTORS})")
    raise RuntimeError(f"AnkerGames: could not extract {archive.name}; tried {tried}")
