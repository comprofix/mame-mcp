"""Filesystem containment by identity, not by spelling.

One directory can be written several ways on Windows (8.3 short names, mixed case, forward slashes, the `\\\\?\\` extended
prefix) and `Path.resolve()` does not unify them: it leaves an extended prefix on an existing path, so two spellings of the
same folder compare unequal. These helpers resolve symlinks/junctions and `..` first (so an escape is judged by where the
path really ends up) and then compare directories with `os.path.samefile`, which asks the filesystem whether two paths are
the same object. A sibling whose name merely starts with the folder's name is a different directory and is refused."""
from __future__ import annotations

import os
from pathlib import Path


def same_directory(a: "str | os.PathLike[str]", b: "str | os.PathLike[str]") -> bool:
    """True when both paths exist and are the same directory (whatever their spelling or links)."""
    try:
        return os.path.isdir(a) and os.path.isdir(b) and os.path.samefile(a, b)
    except OSError:
        return False


def is_directly_in(path: "str | os.PathLike[str]", folder: "str | os.PathLike[str]") -> bool:
    """The real location of `path` (links and `..` followed) has `folder` as its immediate parent directory."""
    return same_directory(os.path.dirname(os.path.realpath(path)), folder)


def is_within(path: "str | os.PathLike[str]", folder: "str | os.PathLike[str]") -> bool:
    """The real location of `path` is `folder` itself or lies anywhere beneath it."""
    cur = Path(os.path.realpath(path))
    while True:
        if same_directory(cur, folder):
            return True
        if cur.parent == cur:
            return False
        cur = cur.parent
