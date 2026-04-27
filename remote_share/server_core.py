from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .pathutil import PathEscapeError, resolve_under
from .protocol import (
    FRAME_ERROR,
    FRAME_REQUEST,
    FRAME_RESPONSE,
    MAX_CHUNK_SIZE,
    ProtocolError,
    async_recv_message,
    async_send_message,
)

READONLY = "readonly"
READWRITE = "readwrite"


class RemoteShareError(RuntimeError):
    code = "ERROR"


class NotFoundError(RemoteShareError):
    code = "NOT_FOUND"


class PermissionDeniedError(RemoteShareError):
    code = "PERMISSION_DENIED"


class BadRequestError(RemoteShareError):
    code = "BAD_REQUEST"


@dataclass(frozen=True)
class Share:
    name: str
    path: Path
    permission: str = READONLY

    def validate(self) -> None:
        if not self.name or "/" in self.name or "\\" in self.name:
            raise ValueError(f"invalid share name: {self.name!r}")
        if self.permission not in (READONLY, READWRITE):
            raise ValueError(f"invalid permission for {self.name}: {self.permission}")
        if not self.path.exists() or not self.path.is_dir():
            raise ValueError(f"share path does not exist or is not a directory: {self.path}")


@dataclass(frozen=True)
class UserAccount:
    username: str
    password: str
    shares: frozenset[str]

    def validate(self, available_shares: set[str]) -> None:
        if not self.username:
            raise ValueError("user name cannot be empty")
        unknown = self.shares - available_shares - {"*"}
        if unknown:
            raise ValueError(f"user {self.username} references unknown shares: {', '.join(sorted(unknown))}")


@dataclass
class ClientInfo:
    session_id: str
    peer: str
    username: str
    connected_at: float
    last_seen: float


def stat_to_dict(path: Path) -> dict[str, Any]:
    st = path.stat()
    return {
        "is_dir": path.is_dir(),
        "is_file": path.is_file(),
        "size": st.st_size,
        "mtime": st.st_mtime,
        "mtime_ns": st.st_mtime_ns,
        "mode": st.st_mode,
    }


class RemoteShareServer:
    def __init__(self, host: str, port: int, shares: list[Share], users: list[UserAccount] | None = None) -> None:
        self.host = host
        self.port = port
        self.shares = {share.name: share for share in shares}
        for share in shares:
            share.validate()
        self.users = {user.username: user for user in (users or [])}
        for user in self.users.values():
            user.validate(set(self.shares))
        self.clients: dict[str, ClientInfo] = {}
        self._session_users: dict[str, str] = {}
        self._active_writers: dict[str, str] = {}
        self._file_locks: dict[str, asyncio.Lock] = {}
        self._write_versions: dict[str, tuple[int, str]] = {}
        self._server: asyncio.AbstractServer | None = None
        self._change_task: asyncio.Task[None] | None = None
        self._known_mtimes: dict[str, int] = {}
        self.on_log: Callable[[str], None] | None = None

    def log(self, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        if self.on_log:
            self.on_log(line)
        else:
            print(line, flush=True)

    async def start(self) -> None:
        self._server = await asyncio.start_server(self.handle_client, self.host, self.port)
        self._change_task = asyncio.create_task(self._scan_local_changes())
        addrs = ", ".join(str(sock.getsockname()) for sock in self._server.sockets or [])
        self.log(f"server listening on {addrs}")

    async def serve_forever(self) -> None:
        await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._change_task:
            self._change_task.cancel()
            self._change_task = None
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self.log("server stopped")

    def _session_user(self, session_id: str) -> str:
        if not self.users:
            return "anonymous"
        username = self._session_users.get(session_id)
        if not username:
            raise PermissionDeniedError("authentication required")
        return username

    def _authenticate(self, session_id: str, username: str, password: str) -> str:
        if not self.users:
            self._session_users[session_id] = "anonymous"
            return "anonymous"
        account = self.users.get(username)
        if account is None or account.password != password:
            raise PermissionDeniedError("invalid username or password")
        self._session_users[session_id] = username
        return username

    def _user_can_access(self, username: str, share_name: str) -> bool:
        if username == "anonymous" and not self.users:
            return True
        account = self.users.get(username)
        return bool(account and ("*" in account.shares or share_name in account.shares))

    def _get_share(self, session_id: str, name: str) -> Share:
        username = self._session_user(session_id)
        if not self._user_can_access(username, name):
            raise PermissionDeniedError(f"user {username} is not allowed to access share {name}")
        try:
            return self.shares[name]
        except KeyError as exc:
            raise NotFoundError(f"unknown share: {name}") from exc

    def _resolve(self, session_id: str, share_name: str, remote_path: str | None) -> Path:
        share = self._get_share(session_id, share_name)
        try:
            return resolve_under(share.path, remote_path)
        except PathEscapeError as exc:
            raise PermissionDeniedError(str(exc)) from exc

    def _require_write(self, session_id: str, share_name: str) -> None:
        share = self._get_share(session_id, share_name)
        if share.permission != READWRITE:
            raise PermissionDeniedError(f"share {share_name} is readonly")

    def _lock_for(self, path: Path) -> asyncio.Lock:
        key = str(path)
        lock = self._file_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._file_locks[key] = lock
        return lock

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        peer_text = f"{peer[0]}:{peer[1]}" if peer else "unknown"
        session_id = uuid.uuid4().hex
        self.clients[session_id] = ClientInfo(session_id, peer_text, "unauthenticated", time.time(), time.time())
        self.log(f"client connected {peer_text} session={session_id[:8]}")
        try:
            while True:
                msg = await async_recv_message(reader)
                if msg.frame_type != FRAME_REQUEST:
                    raise ProtocolError("expected request frame")
                request = msg.meta
                session_id = str(request.get("session_id") or session_id)
                username = self._session_users.get(session_id, "unauthenticated")
                self.clients[session_id] = ClientInfo(
                    session_id,
                    peer_text,
                    username,
                    self.clients.get(
                        session_id,
                        ClientInfo(session_id, peer_text, username, time.time(), time.time()),
                    ).connected_at,
                    time.time(),
                )
                try:
                    response, data = await self.dispatch(session_id, request, msg.data)
                except RemoteShareError as exc:
                    await self._send_error(writer, exc)
                    continue
                if request.get("op") == "hello":
                    self.clients[session_id].username = self._session_users.get(session_id, "anonymous")
                await async_send_message(writer, FRAME_RESPONSE, response, data)
        except (asyncio.IncompleteReadError, EOFError):
            pass
        except Exception as exc:
            await self._send_error(writer, exc)
        finally:
            self.clients.pop(session_id, None)
            self._session_users.pop(session_id, None)
            writer.close()
            await writer.wait_closed()
            self.log(f"client disconnected {peer_text} session={session_id[:8]}")

    async def _send_error(self, writer: asyncio.StreamWriter, exc: Exception) -> None:
        if isinstance(exc, RemoteShareError):
            code = exc.code
        elif isinstance(exc, ProtocolError):
            code = "PROTOCOL_ERROR"
        else:
            code = "INTERNAL_ERROR"
            self.log(f"internal error: {exc!r}")
        try:
            await async_send_message(writer, FRAME_ERROR, {"ok": False, "code": code, "message": str(exc)})
        except Exception:
            pass

    async def dispatch(self, session_id: str, request: dict[str, Any], data: bytes) -> tuple[dict[str, Any], bytes]:
        op = str(request.get("op") or "")
        if op == "hello":
            username = self._authenticate(
                session_id,
                str(request.get("username") or ""),
                str(request.get("password") or ""),
            )
            return {
                "ok": True,
                "session_id": session_id,
                "username": username,
                "auth_required": bool(self.users),
                "max_chunk": MAX_CHUNK_SIZE,
            }, b""
        if op == "list_shares":
            return {"ok": True, "shares": self.list_shares(session_id)}, b""
        if op == "clients":
            self._session_user(session_id)
            return {"ok": True, "clients": self.list_clients()}, b""

        share = str(request.get("share") or "")
        path = str(request.get("path") or "")
        if not share:
            raise BadRequestError("missing share")

        if op == "list_dir":
            return {"ok": True, "entries": await asyncio.to_thread(self.list_dir, session_id, share, path)}, b""
        if op == "stat":
            return {"ok": True, "stat": await asyncio.to_thread(self.stat, session_id, share, path)}, b""
        if op == "read":
            offset = int(request.get("offset", 0))
            size = int(request.get("size", MAX_CHUNK_SIZE))
            meta, payload = await asyncio.to_thread(self.read_file, session_id, share, path, offset, size)
            return {"ok": True, **meta}, payload
        if op == "write":
            offset = int(request.get("offset", 0))
            expected = request.get("expected_mtime_ns")
            return await self.write_file(session_id, share, path, offset, data, expected)
        if op == "create":
            kind = str(request.get("kind", "file"))
            truncate = bool(request.get("truncate", False))
            return {"ok": True, **await asyncio.to_thread(self.create, session_id, share, path, kind, truncate)}, b""
        if op == "truncate":
            size = int(request.get("size", 0))
            return {"ok": True, **await asyncio.to_thread(self.truncate, session_id, share, path, size)}, b""
        if op == "delete":
            return {"ok": True, **await asyncio.to_thread(self.delete, session_id, share, path)}, b""
        if op == "rename":
            new_path = str(request.get("new_path") or "")
            return {"ok": True, **await asyncio.to_thread(self.rename, session_id, share, path, new_path)}, b""
        if op == "utime":
            atime = float(request.get("atime", time.time()))
            mtime = float(request.get("mtime", time.time()))
            return {"ok": True, **await asyncio.to_thread(self.utime, session_id, share, path, atime, mtime)}, b""

        raise BadRequestError(f"unknown op: {op}")

    def list_shares(self, session_id: str) -> list[dict[str, Any]]:
        username = self._session_user(session_id)
        return [
            {"name": share.name, "permission": share.permission, "path": str(share.path)}
            for share in sorted(self.shares.values(), key=lambda item: item.name.lower())
            if self._user_can_access(username, share.name)
        ]

    def list_clients(self) -> list[dict[str, Any]]:
        now = time.time()
        return [
            {
                "session_id": info.session_id,
                "peer": info.peer,
                "username": info.username,
                "connected_at": info.connected_at,
                "idle_seconds": max(0.0, now - info.last_seen),
            }
            for info in self.clients.values()
        ]

    def list_dir(self, session_id: str, share_name: str, remote_path: str) -> list[dict[str, Any]]:
        target = self._resolve(session_id, share_name, remote_path)
        if not target.exists():
            raise NotFoundError(f"not found: {remote_path}")
        if not target.is_dir():
            raise BadRequestError(f"not a directory: {remote_path}")
        entries: list[dict[str, Any]] = []
        with os.scandir(target) as scan:
            for entry in scan:
                try:
                    st = entry.stat()
                    entries.append(
                        {
                            "name": entry.name,
                            "is_dir": entry.is_dir(),
                            "is_file": entry.is_file(),
                            "size": st.st_size,
                            "mtime": st.st_mtime,
                            "mtime_ns": st.st_mtime_ns,
                            "mode": st.st_mode,
                        }
                    )
                except FileNotFoundError:
                    continue
        entries.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
        return entries

    def stat(self, session_id: str, share_name: str, remote_path: str) -> dict[str, Any]:
        target = self._resolve(session_id, share_name, remote_path)
        if not target.exists():
            raise NotFoundError(f"not found: {remote_path}")
        return stat_to_dict(target)

    def read_file(self, session_id: str, share_name: str, remote_path: str, offset: int, size: int) -> tuple[dict[str, Any], bytes]:
        if offset < 0:
            raise BadRequestError("negative offset")
        size = min(max(size, 0), MAX_CHUNK_SIZE)
        target = self._resolve(session_id, share_name, remote_path)
        if not target.exists():
            raise NotFoundError(f"not found: {remote_path}")
        if not target.is_file():
            raise BadRequestError(f"not a file: {remote_path}")
        with target.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(size)
        st = target.stat()
        return {"size": len(data), "mtime_ns": st.st_mtime_ns}, data

    async def write_file(
        self,
        session_id: str,
        share_name: str,
        remote_path: str,
        offset: int,
        data: bytes,
        expected_mtime_ns: Any,
    ) -> tuple[dict[str, Any], bytes]:
        self._require_write(session_id, share_name)
        if offset < 0:
            raise BadRequestError("negative offset")
        if len(data) > MAX_CHUNK_SIZE:
            raise BadRequestError(f"write chunk exceeds {MAX_CHUNK_SIZE} bytes")
        target = self._resolve(session_id, share_name, remote_path)
        lock = self._lock_for(target)
        warning = self._write_warning(target, session_id, expected_mtime_ns)
        active = self._active_writers.get(str(target))
        if active and active != session_id:
            warning = "file is being modified by another client; this write uses last-write-wins"
        self._active_writers[str(target)] = session_id
        async with lock:
            try:
                meta = await asyncio.to_thread(self._write_file_sync, target, offset, data)
                self._write_versions[str(target)] = (meta["mtime_ns"], session_id)
            finally:
                if self._active_writers.get(str(target)) == session_id:
                    self._active_writers.pop(str(target), None)
        if warning:
            self.log(f"conflict warning for {target}: {warning}")
            meta["warning"] = warning
        return {"ok": True, **meta}, b""

    def _write_warning(self, target: Path, session_id: str, expected_mtime_ns: Any) -> str | None:
        if expected_mtime_ns is None:
            return None
        try:
            expected_int = int(expected_mtime_ns)
        except (TypeError, ValueError):
            return None
        if not target.exists():
            return None
        current = target.stat().st_mtime_ns
        if current == expected_int:
            return None
        last = self._write_versions.get(str(target))
        if last and last[1] == session_id:
            return None
        return "file has been modified by another client or on the server; this write will overwrite it"

    def _write_file_sync(self, target: Path, offset: int, data: bytes) -> dict[str, Any]:
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "r+b" if target.exists() else "wb"
        with target.open(mode) as handle:
            handle.seek(offset)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        return stat_to_dict(target)

    def create(self, session_id: str, share_name: str, remote_path: str, kind: str, truncate: bool) -> dict[str, Any]:
        self._require_write(session_id, share_name)
        target = self._resolve(session_id, share_name, remote_path)
        if kind == "dir":
            target.mkdir(parents=True, exist_ok=True)
        elif kind == "file":
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = "wb" if truncate else "ab"
            with target.open(mode):
                pass
        else:
            raise BadRequestError(f"invalid create kind: {kind}")
        return stat_to_dict(target)

    def truncate(self, session_id: str, share_name: str, remote_path: str, size: int) -> dict[str, Any]:
        self._require_write(session_id, share_name)
        if size < 0:
            raise BadRequestError("negative truncate size")
        target = self._resolve(session_id, share_name, remote_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("r+b" if target.exists() else "wb") as handle:
            handle.truncate(size)
        return stat_to_dict(target)

    def delete(self, session_id: str, share_name: str, remote_path: str) -> dict[str, Any]:
        self._require_write(session_id, share_name)
        target = self._resolve(session_id, share_name, remote_path)
        if not target.exists():
            raise NotFoundError(f"not found: {remote_path}")
        if target.is_dir():
            target.rmdir()
        else:
            target.unlink()
        return {"deleted": True}

    def rename(self, session_id: str, share_name: str, remote_path: str, new_path: str) -> dict[str, Any]:
        self._require_write(session_id, share_name)
        src = self._resolve(session_id, share_name, remote_path)
        dst = self._resolve(session_id, share_name, new_path)
        if not src.exists():
            raise NotFoundError(f"not found: {remote_path}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.replace(dst)
        return stat_to_dict(dst)

    def utime(self, session_id: str, share_name: str, remote_path: str, atime: float, mtime: float) -> dict[str, Any]:
        self._require_write(session_id, share_name)
        target = self._resolve(session_id, share_name, remote_path)
        if not target.exists():
            raise NotFoundError(f"not found: {remote_path}")
        os.utime(target, (atime, mtime))
        return stat_to_dict(target)

    async def _scan_local_changes(self) -> None:
        while True:
            try:
                await asyncio.sleep(2.0)
                await asyncio.to_thread(self._scan_once)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(f"local change scan error: {exc}")

    def _scan_once(self) -> None:
        for share in self.shares.values():
            for root, dirs, files in os.walk(share.path):
                names = dirs + files
                for name in names:
                    path = Path(root) / name
                    try:
                        mtime = path.stat().st_mtime_ns
                    except OSError:
                        continue
                    key = str(path.resolve())
                    old = self._known_mtimes.get(key)
                    self._known_mtimes[key] = mtime
                    if old is not None and old != mtime:
                        self._write_versions[key] = (mtime, "server-local")


def parse_share_spec(spec: str) -> Share:
    if "=" not in spec:
        raise ValueError("share spec must be NAME=PATH:permission")
    name, rest = spec.split("=", 1)
    if ":" in rest:
        raw_path, permission = rest.rsplit(":", 1)
        if permission not in (READONLY, READWRITE):
            raw_path = rest
            permission = READONLY
    else:
        raw_path, permission = rest, READONLY
    return Share(name=name, path=Path(raw_path).expanduser().resolve(), permission=permission)


def parse_user_spec(spec: str) -> UserAccount:
    if "=" not in spec:
        raise ValueError("user spec must be USER=PASSWORD:share1,share2")
    username, rest = spec.split("=", 1)
    if ":" in rest:
        password, shares_text = rest.split(":", 1)
        shares = frozenset(item.strip() for item in shares_text.split(",") if item.strip())
    else:
        password = rest
        shares = frozenset()
    return UserAccount(username=username.strip(), password=password, shares=shares)
