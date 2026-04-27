from __future__ import annotations

import errno
import os
import stat as statmod
import time
from itertools import count
from typing import Any

from .client_core import RemoteIOError, RemoteShareClient
from .protocol import MAX_CHUNK_SIZE

try:
    from fuse import Operations as FuseOperationsBase
except Exception:
    class FuseOperationsBase:  # type: ignore[no-redef]
        pass


def _remote_path(path: str) -> str:
    return path.lstrip("/")


def _errno_for(exc: RemoteIOError) -> int:
    return {
        "NOT_FOUND": errno.ENOENT,
        "PERMISSION_DENIED": errno.EACCES,
        "BAD_REQUEST": errno.EINVAL,
    }.get(exc.code, errno.EIO)


class RemoteFuseOperations(FuseOperationsBase):
    def __init__(self, client: RemoteShareClient, share: str) -> None:
        self.client = client
        self.share = share
        self._fh_counter = count(1)
        self._fh_paths: dict[int, str] = {}
        self._fh_versions: dict[int, int] = {}

    def _call(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except RemoteIOError as exc:
            from fuse import FuseOSError

            raise FuseOSError(_errno_for(exc)) from exc
        except OSError:
            raise
        except Exception as exc:
            from fuse import FuseOSError

            raise FuseOSError(errno.EIO) from exc

    def _attrs(self, info: dict[str, Any]) -> dict[str, Any]:
        is_dir = bool(info.get("is_dir"))
        mode = int(info.get("mode") or 0)
        if not mode:
            mode = (statmod.S_IFDIR | 0o755) if is_dir else (statmod.S_IFREG | 0o644)
        return {
            "st_mode": mode,
            "st_nlink": 2 if is_dir else 1,
            "st_size": int(info.get("size") or 0),
            "st_ctime": float(info.get("mtime") or time.time()),
            "st_mtime": float(info.get("mtime") or time.time()),
            "st_atime": float(info.get("mtime") or time.time()),
            "st_uid": os.getuid() if hasattr(os, "getuid") else 0,
            "st_gid": os.getgid() if hasattr(os, "getgid") else 0,
        }

    def getattr(self, path: str, fh: int | None = None) -> dict[str, Any]:
        info = self._call(self.client.stat, self.share, _remote_path(path))
        return self._attrs(info)

    def readdir(self, path: str, fh: int | None) -> list[str]:
        entries = self._call(self.client.list_dir, self.share, _remote_path(path))
        return [".", "..", *[entry["name"] for entry in entries]]

    def open(self, path: str, flags: int) -> int:
        remote = _remote_path(path)
        info = self._call(self.client.stat, self.share, remote)
        fh = next(self._fh_counter)
        self._fh_paths[fh] = remote
        self._fh_versions[fh] = int(info.get("mtime_ns") or 0)
        return fh

    def create(self, path: str, mode: int, fi: Any = None) -> int:
        remote = _remote_path(path)
        info = self._call(self.client.create_file, self.share, remote, True)
        fh = next(self._fh_counter)
        self._fh_paths[fh] = remote
        self._fh_versions[fh] = int(info.get("mtime_ns") or 0)
        return fh

    def read(self, path: str, size: int, offset: int, fh: int | None) -> bytes:
        remote = self._fh_paths.get(fh or -1, _remote_path(path))
        read_size = min(size, MAX_CHUNK_SIZE)
        return self._call(self.client.read_file, self.share, remote, offset, read_size)

    def write(self, path: str, data: bytes, offset: int, fh: int | None) -> int:
        remote = self._fh_paths.get(fh or -1, _remote_path(path))
        expected = self._fh_versions.get(fh or -1)
        written = 0
        while written < len(data):
            chunk = data[written : written + MAX_CHUNK_SIZE]
            meta = self._call(self.client.write_file, self.share, remote, offset + written, chunk, expected)
            expected = int(meta.get("mtime_ns") or expected or 0)
            if fh is not None:
                self._fh_versions[fh] = expected
            written += len(chunk)
        return written

    def truncate(self, path: str, length: int, fh: int | None = None) -> int:
        self._call(self.client.truncate, self.share, _remote_path(path), length)
        return 0

    def flush(self, path: str, fh: int) -> int:
        return 0

    def release(self, path: str, fh: int) -> int:
        self._fh_paths.pop(fh, None)
        self._fh_versions.pop(fh, None)
        return 0

    def mkdir(self, path: str, mode: int) -> int:
        self._call(self.client.create_dir, self.share, _remote_path(path))
        return 0

    def mknod(self, path: str, mode: int, dev: int) -> int:
        self._call(self.client.create_file, self.share, _remote_path(path), False)
        return 0

    def unlink(self, path: str) -> int:
        self._call(self.client.delete, self.share, _remote_path(path))
        return 0

    def rmdir(self, path: str) -> int:
        self._call(self.client.delete, self.share, _remote_path(path))
        return 0

    def rename(self, old: str, new: str) -> int:
        self._call(self.client.rename, self.share, _remote_path(old), _remote_path(new))
        return 0

    def utimens(self, path: str, times: tuple[float, float] | None = None) -> int:
        if times is None:
            times = (time.time(), time.time())
        self._call(self.client.utime, self.share, _remote_path(path), times[0], times[1])
        return 0


def mount_fuse(
    host: str,
    port: int,
    share: str,
    mountpoint: str,
    foreground: bool = True,
    username: str = "",
    password: str = "",
) -> None:
    try:
        from fuse import FUSE
    except ImportError as exc:
        raise SystemExit("fusepy is not installed. Run: python -m pip install fusepy") from exc

    client = RemoteShareClient(host, port, username=username, password=password)
    client.on_warning = lambda message: print(f"warning: {message}", flush=True)
    client.connect()
    operations = RemoteFuseOperations(client, share)
    FUSE(operations, mountpoint, foreground=foreground, nothreads=False)
