from __future__ import annotations

import socket
import threading
from typing import Any, Callable

from .protocol import (
    FRAME_ERROR,
    FRAME_REQUEST,
    FRAME_RESPONSE,
    MAX_CHUNK_SIZE,
    Message,
    ProtocolError,
    recv_message,
    send_message,
)


class RemoteIOError(OSError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RemoteShareClient:
    def __init__(
        self,
        host: str,
        port: int,
        timeout: float = 15.0,
        username: str = "",
        password: str = "",
    ) -> None:
        self.host = host
        self.port = int(port)
        self.timeout = timeout
        self.username = username
        self.password = password
        self.session_id: str | None = None
        self._sock: socket.socket | None = None
        self._lock = threading.RLock()
        self.on_warning: Callable[[str], None] | None = None

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                finally:
                    self._sock = None

    def connect(self) -> None:
        with self._lock:
            self._connect_locked()

    def _connect_locked(self) -> None:
        if self._sock is not None:
            return
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        self._sock = sock
        response, _ = self._request_locked(
            "hello",
            {"username": self.username, "password": self.password},
            b"",
            retry=False,
        )
        self.session_id = str(response["session_id"])

    def request(self, op: str, params: dict[str, Any] | None = None, data: bytes = b"") -> tuple[dict[str, Any], bytes]:
        with self._lock:
            return self._request_locked(op, params or {}, data, retry=True)

    def _request_locked(
        self,
        op: str,
        params: dict[str, Any],
        data: bytes,
        retry: bool,
    ) -> tuple[dict[str, Any], bytes]:
        if len(data) > MAX_CHUNK_SIZE:
            raise ValueError(f"data chunk exceeds {MAX_CHUNK_SIZE} bytes")
        if self._sock is None:
            self._connect_locked()
        request = {"op": op, **params}
        if self.session_id and op != "hello":
            request["session_id"] = self.session_id
        try:
            assert self._sock is not None
            send_message(self._sock, FRAME_REQUEST, request, data)
            message = recv_message(self._sock)
            return self._handle_message(message)
        except (OSError, EOFError, ProtocolError):
            self.close()
            if not retry:
                raise
            self._connect_locked()
            assert self._sock is not None
            send_message(self._sock, FRAME_REQUEST, request, data)
            message = recv_message(self._sock)
            return self._handle_message(message)

    def _handle_message(self, message: Message) -> tuple[dict[str, Any], bytes]:
        meta = message.meta
        if message.frame_type == FRAME_ERROR:
            raise RemoteIOError(str(meta.get("code", "ERROR")), str(meta.get("message", "remote error")))
        if message.frame_type != FRAME_RESPONSE:
            raise ProtocolError(f"unexpected frame type {message.frame_type}")
        warning = meta.get("warning")
        if warning and self.on_warning:
            self.on_warning(str(warning))
        return meta, message.data

    def list_shares(self) -> list[dict[str, Any]]:
        meta, _ = self.request("list_shares")
        return list(meta.get("shares", []))

    def list_dir(self, share: str, path: str = "") -> list[dict[str, Any]]:
        meta, _ = self.request("list_dir", {"share": share, "path": path})
        return list(meta.get("entries", []))

    def stat(self, share: str, path: str = "") -> dict[str, Any]:
        meta, _ = self.request("stat", {"share": share, "path": path})
        return dict(meta.get("stat", {}))

    def read_file(self, share: str, path: str, offset: int, size: int) -> bytes:
        _, data = self.request("read", {"share": share, "path": path, "offset": offset, "size": size})
        return data

    def write_file(
        self,
        share: str,
        path: str,
        offset: int,
        data: bytes,
        expected_mtime_ns: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"share": share, "path": path, "offset": offset}
        if expected_mtime_ns is not None:
            params["expected_mtime_ns"] = expected_mtime_ns
        meta, _ = self.request("write", params, data)
        return meta

    def create_file(self, share: str, path: str, truncate: bool = False) -> dict[str, Any]:
        meta, _ = self.request("create", {"share": share, "path": path, "kind": "file", "truncate": truncate})
        return meta

    def create_dir(self, share: str, path: str) -> dict[str, Any]:
        meta, _ = self.request("create", {"share": share, "path": path, "kind": "dir"})
        return meta

    def truncate(self, share: str, path: str, size: int) -> dict[str, Any]:
        meta, _ = self.request("truncate", {"share": share, "path": path, "size": size})
        return meta

    def delete(self, share: str, path: str) -> dict[str, Any]:
        meta, _ = self.request("delete", {"share": share, "path": path})
        return meta

    def rename(self, share: str, old_path: str, new_path: str) -> dict[str, Any]:
        meta, _ = self.request("rename", {"share": share, "path": old_path, "new_path": new_path})
        return meta

    def utime(self, share: str, path: str, atime: float, mtime: float) -> dict[str, Any]:
        meta, _ = self.request("utime", {"share": share, "path": path, "atime": atime, "mtime": mtime})
        return meta
