from __future__ import annotations

import email.utils
import html
import mimetypes
import posixpath
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .client_core import RemoteIOError, RemoteShareClient
from .protocol import MAX_CHUNK_SIZE


def _quote_href(path: str) -> str:
    return urllib.parse.quote(path, safe="/")


def _clean_url_path(url_path: str, share: str) -> str:
    parsed = urllib.parse.urlparse(url_path)
    decoded = urllib.parse.unquote(parsed.path)
    parts = [part for part in decoded.split("/") if part]
    if parts and parts[0] == share:
        parts = parts[1:]
    return "/".join(parts)


def _href_for(share: str, remote_path: str, is_dir: bool) -> str:
    path = "/" + share
    if remote_path:
        path += "/" + remote_path.strip("/")
    if is_dir and not path.endswith("/"):
        path += "/"
    return _quote_href(path)


def _http_date(timestamp: float) -> str:
    return email.utils.formatdate(timestamp, usegmt=True)


class WebDAVRequestHandler(BaseHTTPRequestHandler):
    server: "WebDAVBridgeServer"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        self.server.log(fmt % args)

    @property
    def client(self) -> RemoteShareClient:
        return self.server.remote_client

    @property
    def share(self) -> str:
        return self.server.share

    def _remote_path(self) -> str:
        return _clean_url_path(self.path, self.share)

    def _send_empty(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_error_for(self, exc: RemoteIOError) -> None:
        status = {
            "NOT_FOUND": HTTPStatus.NOT_FOUND,
            "PERMISSION_DENIED": HTTPStatus.FORBIDDEN,
            "BAD_REQUEST": HTTPStatus.BAD_REQUEST,
        }.get(exc.code, HTTPStatus.INTERNAL_SERVER_ERROR)
        self._send_empty(int(status))

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("DAV", "1,2")
        self.send_header("Allow", "OPTIONS, PROPFIND, GET, HEAD, PUT, DELETE, MKCOL, MOVE")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self) -> None:
        try:
            info = self.client.stat(self.share, self._remote_path())
            self._send_stat_headers(info, body=False)
        except RemoteIOError as exc:
            self._send_error_for(exc)

    def do_GET(self) -> None:
        remote_path = self._remote_path()
        try:
            info = self.client.stat(self.share, remote_path)
            if info.get("is_dir"):
                self._send_empty(HTTPStatus.METHOD_NOT_ALLOWED)
                return
            size = int(info.get("size") or 0)
            start, end = self._parse_range(size)
            length = max(0, end - start + 1)
            status = HTTPStatus.PARTIAL_CONTENT if start != 0 or end != size - 1 else HTTPStatus.OK
            self.send_response(status)
            self.send_header("Content-Type", mimetypes.guess_type(remote_path)[0] or "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Last-Modified", _http_date(float(info.get("mtime") or 0)))
            self.send_header("Content-Length", str(length))
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            offset = start
            remaining = length
            while remaining > 0:
                chunk = self.client.read_file(self.share, remote_path, offset, min(remaining, MAX_CHUNK_SIZE))
                if not chunk:
                    break
                self.wfile.write(chunk)
                offset += len(chunk)
                remaining -= len(chunk)
        except RemoteIOError as exc:
            self._send_error_for(exc)

    def _parse_range(self, size: int) -> tuple[int, int]:
        header = self.headers.get("Range")
        if not header or not header.startswith("bytes=") or size <= 0:
            return 0, max(0, size - 1)
        value = header.split("=", 1)[1].split(",", 1)[0].strip()
        if value.startswith("-"):
            count = int(value[1:] or "0")
            return max(0, size - count), size - 1
        start_text, _, end_text = value.partition("-")
        start = min(max(0, int(start_text or "0")), size - 1)
        end = min(size - 1, int(end_text) if end_text else size - 1)
        return start, max(start, end)

    def _send_stat_headers(self, info: dict[str, Any], body: bool) -> None:
        if info.get("is_dir"):
            self._send_empty(HTTPStatus.OK)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Length", str(int(info.get("size") or 0)))
        self.send_header("Last-Modified", _http_date(float(info.get("mtime") or 0)))
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()

    def do_PROPFIND(self) -> None:
        remote_path = self._remote_path()
        depth = self.headers.get("Depth", "1")
        try:
            items: list[tuple[str, dict[str, Any]]] = [(remote_path, self.client.stat(self.share, remote_path))]
            if depth != "0" and items[0][1].get("is_dir"):
                for entry in self.client.list_dir(self.share, remote_path):
                    child = posixpath.join(remote_path, entry["name"]) if remote_path else entry["name"]
                    items.append((child, entry))
            body = self._propfind_xml(items).encode("utf-8")
            self.send_response(207, "Multi-Status")
            self.send_header("DAV", "1,2")
            self.send_header("Content-Type", 'application/xml; charset="utf-8"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except RemoteIOError as exc:
            self._send_error_for(exc)

    def _propfind_xml(self, items: list[tuple[str, dict[str, Any]]]) -> str:
        responses: list[str] = []
        for remote_path, info in items:
            is_dir = bool(info.get("is_dir"))
            href = html.escape(_href_for(self.share, remote_path, is_dir))
            name = html.escape(posixpath.basename(remote_path.rstrip("/")) or self.share)
            modified = html.escape(_http_date(float(info.get("mtime") or 0)))
            size = int(info.get("size") or 0)
            content_type = "httpd/unix-directory" if is_dir else (mimetypes.guess_type(remote_path)[0] or "application/octet-stream")
            resource_type = "<D:collection/>" if is_dir else ""
            length_tag = "" if is_dir else f"<D:getcontentlength>{size}</D:getcontentlength>"
            responses.append(
                "<D:response>"
                f"<D:href>{href}</D:href>"
                "<D:propstat><D:prop>"
                f"<D:displayname>{name}</D:displayname>"
                f"<D:getlastmodified>{modified}</D:getlastmodified>"
                f"<D:getcontenttype>{html.escape(content_type)}</D:getcontenttype>"
                f"{length_tag}"
                f"<D:resourcetype>{resource_type}</D:resourcetype>"
                "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>"
                "</D:response>"
            )
        return '<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">' + "".join(responses) + "</D:multistatus>"

    def do_PUT(self) -> None:
        remote_path = self._remote_path()
        length = int(self.headers.get("Content-Length") or "0")
        try:
            self.client.create_file(self.share, remote_path, truncate=True)
            offset = 0
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, MAX_CHUNK_SIZE))
                if not chunk:
                    break
                self.client.write_file(self.share, remote_path, offset, chunk)
                offset += len(chunk)
                remaining -= len(chunk)
            self._send_empty(HTTPStatus.CREATED if length else HTTPStatus.NO_CONTENT)
        except RemoteIOError as exc:
            self._send_error_for(exc)

    def do_MKCOL(self) -> None:
        try:
            self.client.create_dir(self.share, self._remote_path())
            self._send_empty(HTTPStatus.CREATED)
        except RemoteIOError as exc:
            self._send_error_for(exc)

    def do_DELETE(self) -> None:
        try:
            self.client.delete(self.share, self._remote_path())
            self._send_empty(HTTPStatus.NO_CONTENT)
        except RemoteIOError as exc:
            self._send_error_for(exc)

    def do_MOVE(self) -> None:
        destination = self.headers.get("Destination")
        if not destination:
            self._send_empty(HTTPStatus.BAD_REQUEST)
            return
        try:
            new_path = _clean_url_path(destination, self.share)
            self.client.rename(self.share, self._remote_path(), new_path)
            self._send_empty(HTTPStatus.CREATED)
        except RemoteIOError as exc:
            self._send_error_for(exc)


class WebDAVBridgeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        listen_host: str,
        listen_port: int,
        remote_client: RemoteShareClient,
        share: str,
    ) -> None:
        super().__init__((listen_host, listen_port), WebDAVRequestHandler)
        self.remote_client = remote_client
        self.share = share

    def log(self, message: str) -> None:
        print(f"[webdav] {message}", flush=True)


def run_webdav_bridge(
    remote_host: str,
    remote_port: int,
    share: str,
    listen_host: str = "127.0.0.1",
    listen_port: int = 18080,
    username: str = "",
    password: str = "",
) -> None:
    client = RemoteShareClient(remote_host, remote_port, username=username, password=password)
    client.on_warning = lambda message: print(f"warning: {message}", flush=True)
    client.connect()
    server = WebDAVBridgeServer(listen_host, listen_port, client, share)
    url = f"http://{listen_host}:{listen_port}/{share}"
    print(f"WebDAV bridge listening: {url}", flush=True)
    print(f"Windows map example: net use Z: {url} /persistent:no", flush=True)
    server.serve_forever()
