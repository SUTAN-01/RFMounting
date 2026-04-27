from __future__ import annotations

import json
import socket
import struct
from dataclasses import dataclass
from typing import Any, BinaryIO

FRAME_REQUEST = 1
FRAME_RESPONSE = 2
FRAME_ERROR = 3
FRAME_EVENT = 4

HEADER_STRUCT = struct.Struct("!BI")
JSON_LEN_STRUCT = struct.Struct("!I")
MAX_FRAME_SIZE = 16 * 1024 * 1024
MAX_CHUNK_SIZE = 64 * 1024


class ProtocolError(RuntimeError):
    pass


@dataclass
class Message:
    frame_type: int
    meta: dict[str, Any]
    data: bytes = b""


def _encode_body(meta: dict[str, Any], data: bytes = b"") -> bytes:
    raw_meta = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return JSON_LEN_STRUCT.pack(len(raw_meta)) + raw_meta + data


def _decode_body(frame_type: int, body: bytes) -> Message:
    if len(body) < JSON_LEN_STRUCT.size:
        raise ProtocolError("frame body is too small")
    (json_len,) = JSON_LEN_STRUCT.unpack(body[: JSON_LEN_STRUCT.size])
    json_start = JSON_LEN_STRUCT.size
    json_end = json_start + json_len
    if json_end > len(body):
        raise ProtocolError("invalid json length in frame")
    try:
        meta = json.loads(body[json_start:json_end].decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid json metadata: {exc}") from exc
    return Message(frame_type=frame_type, meta=meta, data=body[json_end:])


def make_frame(frame_type: int, meta: dict[str, Any], data: bytes = b"") -> bytes:
    body = _encode_body(meta, data)
    if len(body) > MAX_FRAME_SIZE:
        raise ProtocolError(f"frame too large: {len(body)} bytes")
    return HEADER_STRUCT.pack(frame_type, len(body)) + body


def read_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise EOFError("connection closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def send_message(sock: socket.socket, frame_type: int, meta: dict[str, Any], data: bytes = b"") -> None:
    sock.sendall(make_frame(frame_type, meta, data))


def recv_message(sock: socket.socket) -> Message:
    header = read_exact(sock, HEADER_STRUCT.size)
    frame_type, length = HEADER_STRUCT.unpack(header)
    if length > MAX_FRAME_SIZE:
        raise ProtocolError(f"frame too large: {length} bytes")
    return _decode_body(frame_type, read_exact(sock, length))


async def async_send_message(writer: Any, frame_type: int, meta: dict[str, Any], data: bytes = b"") -> None:
    writer.write(make_frame(frame_type, meta, data))
    await writer.drain()


async def async_recv_message(reader: Any) -> Message:
    header = await reader.readexactly(HEADER_STRUCT.size)
    frame_type, length = HEADER_STRUCT.unpack(header)
    if length > MAX_FRAME_SIZE:
        raise ProtocolError(f"frame too large: {length} bytes")
    body = await reader.readexactly(length)
    return _decode_body(frame_type, body)


def copy_limited(src: BinaryIO, size: int) -> bytes:
    if size > MAX_CHUNK_SIZE:
        raise ProtocolError(f"chunk exceeds {MAX_CHUNK_SIZE} bytes")
    return src.read(size)
