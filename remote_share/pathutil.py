from __future__ import annotations

import os
from pathlib import Path, PurePosixPath


class PathEscapeError(ValueError):
    pass


def normalize_remote_path(path: str | None) -> str:
    raw = (path or "").replace("\\", "/")
    posix = PurePosixPath("/" + raw)
    parts: list[str] = []
    for part in posix.parts:
        if part in ("", "/"):
            continue
        if part == ".":
            continue
        if part == "..":
            raise PathEscapeError("parent directory references are not allowed")
        parts.append(part)
    return "/".join(parts)


def resolve_under(root: str | Path, remote_path: str | None) -> Path:
    root_path = Path(root).resolve()
    rel = normalize_remote_path(remote_path)
    target = (root_path / Path(*rel.split("/"))).resolve() if rel else root_path
    try:
        common = os.path.commonpath([str(root_path), str(target)])
    except ValueError as exc:
        raise PathEscapeError("path escapes share root") from exc
    if common != str(root_path):
        raise PathEscapeError("path escapes share root")
    return target


def to_remote_child(parent: str, name: str) -> str:
    clean_parent = normalize_remote_path(parent)
    clean_name = normalize_remote_path(name)
    return f"{clean_parent}/{clean_name}" if clean_parent else clean_name
