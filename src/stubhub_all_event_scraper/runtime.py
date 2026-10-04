"""CLI logging, bounded worker scheduling and atomic snapshot publication."""

import csv
import logging
import os
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile


def configure_logging(path: str, level: str = "INFO") -> None:
    """Create log directories only when a command actually runs."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler()],
        force=True,
    )


@contextmanager
def atomic_output(path: str | Path, *, newline=None):
    """Replace a destination only after the complete temporary file is flushed."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline=newline,
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_csv(path, rows, fields) -> None:
    with atomic_output(path, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def worker_results(function, items, workers):
    """Yield futures with at most twice the worker count queued (Python 3.11+)."""
    iterator = iter(items)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = set()
        exhausted = False
        while pending or not exhausted:
            while not exhausted and len(pending) < workers * 2:
                try:
                    item = next(iterator)
                except StopIteration:
                    exhausted = True
                else:
                    pending.add(pool.submit(function, item))
            if pending:
                completed, pending = wait(pending, return_when=FIRST_COMPLETED)
                yield from completed
