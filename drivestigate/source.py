"""Descriptor-based source access: reject symlinks, devices and path escapes."""

import os
import stat
from contextlib import contextmanager
from pathlib import PurePosixPath


@contextmanager
def source_directory(root, relative, device):
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Invalid folder path.")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    try:
        for part in path.parts:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
            os.close(fd)
            fd = nxt
        if os.fstat(fd).st_dev != device:
            raise OSError("The drive identity has changed.")
        yield fd
    finally:
        os.close(fd)


@contextmanager
def source_file(root, relative, device):
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or any(p in (".", "..") for p in path.parts):
        raise ValueError("Invalid source path.")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    folder = os.open(root, os.O_RDONLY | os.O_DIRECTORY | nofollow)
    fd = None
    try:
        if os.fstat(folder).st_dev != device:
            raise OSError("The drive identity has changed.")
        for part in path.parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | nofollow, dir_fd=folder)
            os.close(folder)
            folder = nxt
            if os.fstat(folder).st_dev != device:
                raise OSError("Different filesystem skipped.")
        fd = os.open(path.name, os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0), dir_fd=folder)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_dev != device:
            raise ValueError("Not a regular file on the selected drive.")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            yield stream, info
    finally:
        os.close(folder)
        if fd is not None:
            os.close(fd)
