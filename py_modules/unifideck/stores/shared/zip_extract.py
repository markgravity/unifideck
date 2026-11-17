"""Cancellable zip extraction, shared by stores whose archives can be plain zips.

``gamevault/archive.py`` and ``ankergames/archive.py`` both needed this same
extractor body (``_extract_zip_interruptible`` + its worker
``_extract_zip``), byte-identical apart from naming — a genuine convergence,
not two stores that happen to look alike (see ``installed_path.py``'s
docstring for the same distinction applied to a different duplicate). Pulling
it out here is what ``validate_architecture.py``'s cross-module body-shape
check asks for instead of a baseline exemption.
"""
from __future__ import annotations

import asyncio
import contextlib
import threading
import zipfile
from collections.abc import Callable
from pathlib import Path

ZipWorker = Callable[[Path, Path, threading.Event], None]


def default_extract_zip(archive: Path, dest: Path, stop: threading.Event) -> None:
    """Member-by-member unpack so *stop* is checked between files.

    No logging of its own — the plain default for a store with nothing
    store-specific to say about a stopped extraction. A store that wants to
    log the stop (GameVault does) passes its own worker to
    :func:`extract_zip_interruptible` instead of this one.
    """
    with zipfile.ZipFile(archive, "r") as zf:
        for member in zf.infolist():
            if stop.is_set():
                return
            zf.extract(member, str(dest))


async def extract_zip_interruptible(
    archive: Path, dest: Path, *, worker: ZipWorker = default_extract_zip,
) -> None:
    """Unpack a zip in a worker thread that cancellation can actually stop.

    A plain ``await asyncio.to_thread(...)`` cannot be interrupted: the
    executor future refuses cancellation once the thread is running, so the
    task only sees ``CancelledError`` *after* the whole archive has been
    written — for a large game, minutes of ignoring the user's Cancel while
    the extract runs to completion.

    ``shield`` lets this coroutine take the cancellation immediately, so the
    stop flag is set while the thread is still between members. The inner
    task is then awaited so the thread is not left running loose behind a
    returned coroutine, which costs at most one member's write.

    ``worker`` is injectable so a store can keep its own member-loop body
    (GameVault logs an early stop; AnkerGames does not) while still sharing
    this cancellation plumbing — only the plumbing was ever a literal
    duplicate across stores, not the loop itself.
    """
    stop = threading.Event()
    task = asyncio.ensure_future(
        asyncio.to_thread(worker, archive, dest, stop),
    )
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        stop.set()
        with contextlib.suppress(BaseException):
            await task
        raise
