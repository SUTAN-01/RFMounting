from __future__ import annotations

import email.utils
import html
import mimetypes
import posixpath
import time
import urllib.parse
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .client_core import RemoteIOError, RemoteShareClient
from .protocol import MAX_CHUNK_SIZE


def _quote_href(path: str) -> str:
    return urllib.parse.quote(path, safe="/")


def _clean_url_path(url_path: str, share: str) -> str:
    parsed = urllib.parse.urlparse(url_path)
    decoded = urllib.parse.unquote(parsed.path)
    parts = [part for part in decoded.split("/") if part]
    if parts and parts[0].lower() == "davwwwroot":
        parts = parts[1:]
    if parts and parts[0].lower() == share.lower():
        parts = parts[1:]
    return "/".join(parts)


def _uses_davwwwroot(url_path: str) -> bool:
    parsed = urllib.parse.urlparse(url_path)
    parts = [part for part in urllib.parse.unquote(parsed.path).split("/") if part]
    return bool(parts and parts[0].lower() == "davwwwroot")


def _href_for(share: str, remote_path: str, is_dir: bool, davwwwroot: bool = False) -> str:
    path = "/"
    if davwwwroot:
        path += "DavWWWRoot/"
    path += share
    if remote_path:
        path += "/" + remote_path.strip("/")
    if is_dir and not path.endswith("/"):
        path += "/"
    return _quote_href(path)


def _http_date(timestamp: float) -> str:
    return email.utils.formatdate(timestamp, usegmt=True)


def _etag_for(info: dict[str, Any]) -> str:
    return f'"{int(info.get("mtime_ns") or 0):x}-{int(info.get("size") or 0):x}"'


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

    def _href_for(self, remote_path: str, is_dir: bool) -> str:
        return _href_for(self.share, remote_path, is_dir, _uses_davwwwroot(self.path))

    def _send_empty(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_request_body(self) -> bytes:
        chunks: list[bytes] = []
        for chunk in self._iter_request_body():
            chunks.append(chunk)
        return b"".join(chunks)

    def _iter_request_body(self) -> Any:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            while True:
                line = self.rfile.readline()
                if not line:
                    break
                size_text = line.split(b";", 1)[0].strip()
                if not size_text:
                    continue
                size = int(size_text, 16)
                if size == 0:
                    while True:
                        trailer = self.rfile.readline()
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    break
                data = self.rfile.read(size)
                self.rfile.read(2)
                if data:
                    yield data
            return
        remaining = int(self.headers.get("Content-Length") or "0")
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, MAX_CHUNK_SIZE))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk

    def _send_error_for(self, exc: RemoteIOError) -> None:
        status = {
            "NOT_FOUND": HTTPStatus.NOT_FOUND,
            "PERMISSION_DENIED": HTTPStatus.FORBIDDEN,
            "BAD_REQUEST": HTTPStatus.BAD_REQUEST,
        }.get(exc.code, HTTPStatus.INTERNAL_SERVER_ERROR)
        self.server.log(f"{self.command} {self.path} remote error {exc.code}: {exc}")
        self._send_empty(int(status))

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("DAV", "1,2")
        self.send_header("MS-Author-Via", "DAV")
        self.send_header("Allow", "OPTIONS, PROPFIND, PROPPATCH, GET, HEAD, PUT, DELETE, MKCOL, COPY, MOVE, LOCK, UNLOCK")
        self.send_header("Public", "OPTIONS, PROPFIND, PROPPATCH, GET, HEAD, PUT, DELETE, MKCOL, COPY, MOVE, LOCK, UNLOCK")
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
            self.send_header("ETag", _etag_for(info))
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
            self._send_empty(HTTPStatus.OK, {"Content-Type": "httpd/unix-directory"})
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Length", str(int(info.get("size") or 0)))
        self.send_header("Last-Modified", _http_date(float(info.get("mtime") or 0)))
        self.send_header("ETag", _etag_for(info))
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
            href = html.escape(self._href_for(remote_path, is_dir))
            name = html.escape(posixpath.basename(remote_path.rstrip("/")) or self.share)
            modified = html.escape(_http_date(float(info.get("mtime") or 0)))
            size = int(info.get("size") or 0)
            content_type = "httpd/unix-directory" if is_dir else (mimetypes.guess_type(remote_path)[0] or "application/octet-stream")
            resource_type = "<D:collection/>" if is_dir else ""
            length_tag = "" if is_dir else f"<D:getcontentlength>{size}</D:getcontentlength>"
            etag_tag = "" if is_dir else f"<D:getetag>{html.escape(_etag_for(info))}</D:getetag>"
            creation = html.escape(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(info.get("mtime") or 0))))
            responses.append(
                "<D:response>"
                f"<D:href>{href}</D:href>"
                "<D:propstat><D:prop>"
                f"<D:displayname>{name}</D:displayname>"
                f"<D:creationdate>{creation}</D:creationdate>"
                f"<D:getlastmodified>{modified}</D:getlastmodified>"
                f"<D:getcontenttype>{html.escape(content_type)}</D:getcontenttype>"
                f"{length_tag}"
                f"{etag_tag}"
                f"<D:resourcetype>{resource_type}</D:resourcetype>"
                "<D:supportedlock>"
                "<D:lockentry><D:lockscope><D:exclusive/></D:lockscope><D:locktype><D:write/></D:locktype></D:lockentry>"
                "</D:supportedlock>"
                "<D:lockdiscovery/>"
                "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>"
                "</D:response>"
            )
        return '<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">' + "".join(responses) + "</D:multistatus>"

    def do_PUT(self) -> None:
        remote_path = self._remote_path()
        try:
            created = False
            try:
                self.client.stat(self.share, remote_path)
            except RemoteIOError as exc:
                if exc.code != "NOT_FOUND":
                    raise
                created = True
            self.client.create_file(self.share, remote_path, truncate=True)
            offset = 0
            for data in self._iter_request_body():
                chunk_offset = 0
                while chunk_offset < len(data):
                    chunk = data[chunk_offset : chunk_offset + MAX_CHUNK_SIZE]
                    self.client.write_file(self.share, remote_path, offset, chunk)
                    offset += len(chunk)
                    chunk_offset += len(chunk)
            headers = {}
            try:
                headers["ETag"] = _etag_for(self.client.stat(self.share, remote_path))
            except RemoteIOError:
                pass
            self._send_empty(HTTPStatus.CREATED if created else HTTPStatus.NO_CONTENT, headers)
        except RemoteIOError as exc:
            self._send_error_for(exc)

    def do_MKCOL(self) -> None:
        self._read_request_body()
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

    def do_COPY(self) -> None:
        destination = self.headers.get("Destination")
        if not destination:
            self._send_empty(HTTPStatus.BAD_REQUEST)
            return
        src_path = self._remote_path()
        dst_path = _clean_url_path(destination, self.share)
        try:
            info = self.client.stat(self.share, src_path)
            if info.get("is_dir"):
                self.client.create_dir(self.share, dst_path)
                self._send_empty(HTTPStatus.CREATED)
                return
            self.client.create_file(self.share, dst_path, truncate=True)
            size = int(info.get("size") or 0)
            offset = 0
            while offset < size:
                data = self.client.read_file(self.share, src_path, offset, min(MAX_CHUNK_SIZE, size - offset))
                if not data:
                    break
                self.client.write_file(self.share, dst_path, offset, data)
                offset += len(data)
            self._send_empty(HTTPStatus.CREATED)
        except RemoteIOError as exc:
            self._send_error_for(exc)

    def do_LOCK(self) -> None:
        self._read_request_body()
        remote_path = self._remote_path()
        is_dir_hint = urllib.parse.urlparse(self.path).path.endswith("/")
        status = HTTPStatus.OK
        try:
            stat_info = self.client.stat(self.share, remote_path)
            is_dir_hint = bool(stat_info.get("is_dir"))
        except RemoteIOError as exc:
            if exc.code == "NOT_FOUND":
                # Do not materialize lock-null resources as real files/directories here.
                # Some Windows flows LOCK an unmapped path before deciding whether MKCOL or PUT follows.
                status = HTTPStatus.CREATED
            else:
                self._send_error_for(exc)
                return
        token = f"opaquelocktoken:{uuid.uuid4()}"
        timeout = time.time() + 1800
        self.server.locks[token] = {"path": remote_path, "expires": timeout}
        href = html.escape(self._href_for(remote_path, is_dir_hint))
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<D:prop xmlns:D="DAV:">'
            "<D:lockdiscovery>"
            "<D:activelock>"
            "<D:locktype><D:write/></D:locktype>"
            "<D:lockscope><D:exclusive/></D:lockscope>"
            "<D:depth>infinity</D:depth>"
            f"<D:timeout>Second-1800</D:timeout>"
            f"<D:locktoken><D:href>{html.escape(token)}</D:href></D:locktoken>"
            f"<D:lockroot><D:href>{href}</D:href></D:lockroot>"
            "</D:activelock>"
            "</D:lockdiscovery>"
            "</D:prop>"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("DAV", "1,2")
        self.send_header("Lock-Token", f"<{token}>")
        self.send_header("Content-Type", 'application/xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_UNLOCK(self) -> None:
        token = self.headers.get("Lock-Token", "").strip()
        if token.startswith("<") and token.endswith(">"):
            token = token[1:-1]
        if token:
            self.server.locks.pop(token, None)
        self._send_empty(HTTPStatus.NO_CONTENT)

    def do_PROPPATCH(self) -> None:
        self._read_request_body()
        remote_path = self._remote_path()
        try:
            stat_info = self.client.stat(self.share, remote_path)
            href = html.escape(self._href_for(remote_path, bool(stat_info.get("is_dir"))))
            body = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<D:multistatus xmlns:D="DAV:">'
                "<D:response>"
                f"<D:href>{href}</D:href>"
                "<D:propstat><D:prop/>"
                "<D:status>HTTP/1.1 200 OK</D:status>"
                "</D:propstat>"
                "</D:response>"
                "</D:multistatus>"
            ).encode("utf-8")
            self.send_response(207, "Multi-Status")
            self.send_header("DAV", "1,2")
            self.send_header("Content-Type", 'application/xml; charset="utf-8"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
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
        self.locks: dict[str, dict[str, Any]] = {}

    def log(self, message: str) -> None:
        line = f"[webdav] {time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
        print(line, flush=True)
        try:
            log_dir = Path.home() / ".remote_share_mount"
            log_dir.mkdir(parents=True, exist_ok=True)
            with (log_dir / "webdav.log").open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception:
            pass


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
    # Fail fast if credentials/share access are invalid.
    client.stat(share, "")
    server = WebDAVBridgeServer(listen_host, listen_port, client, share)
    url = f"http://{listen_host}:{listen_port}/{share}"
    print(f"WebDAV bridge listening: {url}", flush=True)
    print(f"Windows map example: net use Z: {url} /persistent:no", flush=True)
    server.serve_forever()
