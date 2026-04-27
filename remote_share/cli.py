from __future__ import annotations

import argparse
import asyncio
import os
import platform
import sys

from .client_core import RemoteShareClient
from .server_core import RemoteShareServer, parse_share_spec, parse_user_spec


def cmd_serve(args: argparse.Namespace) -> int:
    shares = [parse_share_spec(spec) for spec in args.share]
    users = [parse_user_spec(spec) for spec in args.user]
    if not shares:
        print("at least one --share is required", file=sys.stderr)
        return 2
    server = RemoteShareServer(args.host, args.port, shares, users)
    try:
        asyncio.run(server.serve_forever())
    except KeyboardInterrupt:
        return 0
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    client = RemoteShareClient(args.host, args.port, username=args.username, password=args.password)
    for share in client.list_shares():
        print(f"{share['name']}\t{share['permission']}\t{share['path']}")
    client.close()
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    client = RemoteShareClient(args.host, args.port, username=args.username, password=args.password)
    for entry in client.list_dir(args.share, args.path):
        kind = "d" if entry.get("is_dir") else "-"
        print(f"{kind}\t{entry.get('size', 0)}\t{entry['name']}")
    client.close()
    return 0


def cmd_mount_fuse(args: argparse.Namespace) -> int:
    if platform.system() != "Linux":
        print("FUSE mount is supported by this command on Linux. Use 'webdav' on Windows.", file=sys.stderr)
        return 2
    from .fuse_mount import mount_fuse

    mount_fuse(
        args.host,
        args.port,
        args.share,
        os.path.abspath(args.mount),
        foreground=not args.background,
        username=args.username,
        password=args.password,
    )
    return 0


def cmd_webdav(args: argparse.Namespace) -> int:
    from .webdav_bridge import run_webdav_bridge

    run_webdav_bridge(
        args.remote_host,
        args.remote_port,
        args.share,
        args.listen_host,
        args.listen_port,
        args.username,
        args.password,
    )
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    from .gui import run_gui

    run_gui()
    return 0


def cmd_gui_server(args: argparse.Namespace) -> int:
    from .gui import run_server_gui

    run_server_gui()
    return 0


def cmd_gui_client(args: argparse.Namespace) -> int:
    from .gui import run_client_gui

    run_client_gui()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="remote-share", description="Remote directory sharing and mount tool")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="start remote share server")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=18888)
    serve.add_argument("--share", action="append", default=[], help="NAME=PATH:readonly|readwrite")
    serve.add_argument("--user", action="append", default=[], help="USER=PASSWORD:share1,share2 or USER=PASSWORD:*")
    serve.set_defaults(func=cmd_serve)

    list_cmd = sub.add_parser("list", help="list remote shares")
    list_cmd.add_argument("--host", required=True)
    list_cmd.add_argument("--port", type=int, default=18888)
    list_cmd.add_argument("--username", default="")
    list_cmd.add_argument("--password", default="")
    list_cmd.set_defaults(func=cmd_list)

    ls_cmd = sub.add_parser("ls", help="list a directory in a remote share")
    ls_cmd.add_argument("--host", required=True)
    ls_cmd.add_argument("--port", type=int, default=18888)
    ls_cmd.add_argument("--share", required=True)
    ls_cmd.add_argument("--path", default="")
    ls_cmd.add_argument("--username", default="")
    ls_cmd.add_argument("--password", default="")
    ls_cmd.set_defaults(func=cmd_ls)

    mount = sub.add_parser("mount-fuse", help="mount a share through Linux FUSE")
    mount.add_argument("--host", required=True)
    mount.add_argument("--port", type=int, default=18888)
    mount.add_argument("--share", required=True)
    mount.add_argument("--mount", required=True)
    mount.add_argument("--background", action="store_true")
    mount.add_argument("--username", default="")
    mount.add_argument("--password", default="")
    mount.set_defaults(func=cmd_mount_fuse)

    webdav = sub.add_parser("webdav", help="run local WebDAV bridge for Windows network-drive mapping")
    webdav.add_argument("--remote-host", required=True)
    webdav.add_argument("--remote-port", type=int, default=18888)
    webdav.add_argument("--share", required=True)
    webdav.add_argument("--listen-host", default="127.0.0.1")
    webdav.add_argument("--listen-port", type=int, default=18080)
    webdav.add_argument("--username", default="")
    webdav.add_argument("--password", default="")
    webdav.set_defaults(func=cmd_webdav)

    gui = sub.add_parser("gui", help="start unified GUI")
    gui.set_defaults(func=cmd_gui)

    gui_server = sub.add_parser("gui-server", help="start server GUI")
    gui_server.set_defaults(func=cmd_gui_server)

    gui_client = sub.add_parser("gui-client", help="start client GUI")
    gui_client.set_defaults(func=cmd_gui_client)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    if argv is None and len(sys.argv) == 1:
        argv = ["gui"]
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
