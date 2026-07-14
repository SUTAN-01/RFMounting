from __future__ import annotations

import asyncio
import ctypes
import json
import os
import platform
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from tkinter import BOTH, END, LEFT, X, filedialog, messagebox, ttk
import tkinter as tk

from .client_core import RemoteShareClient
from .server_core import READONLY, READWRITE, RemoteShareServer, Share, UserAccount

CONFIG_PATH = Path.home() / ".remote_share_mount" / "config.json"
YES = "Yes"
NO = "No"


def _load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_config(config: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def _app_command(*args: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, "-m", "remote_share.cli", *args]


def _popen(cmd: list[str]) -> subprocess.Popen:
    kwargs = {}
    if platform.system() == "Windows":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(cmd, **kwargs)


def _bool_text(value: bool) -> str:
    return YES if value else NO


def _truthy(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "allow", "allowed"}


class ServerPage(ttk.Frame):
    def __init__(self, master: tk.Misc, config: dict) -> None:
        super().__init__(master, padding=10)
        self.config = config
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop_event: threading.Event | None = None
        self.thread: threading.Thread | None = None
        self.server: RemoteShareServer | None = None
        self._build()
        self._load()
        self.after(200, self._drain_logs)

    def _build(self) -> None:
        conn = ttk.Frame(self)
        conn.pack(fill=X)
        ttk.Label(conn, text="Host").pack(side=LEFT)
        self.host_var = tk.StringVar(value=self.config.get("server_host", "0.0.0.0"))
        ttk.Entry(conn, textvariable=self.host_var, width=16).pack(side=LEFT, padx=4)
        ttk.Label(conn, text="Port").pack(side=LEFT)
        self.port_var = tk.StringVar(value=str(self.config.get("server_port", 18888)))
        ttk.Entry(conn, textvariable=self.port_var, width=8).pack(side=LEFT, padx=4)
        ttk.Button(conn, text="Start Service", command=self.start).pack(side=LEFT, padx=6)
        ttk.Button(conn, text="Stop", command=self.stop).pack(side=LEFT)

        share_bar = ttk.Frame(self)
        share_bar.pack(fill=X, pady=(10, 4))
        ttk.Button(share_bar, text="Add Directory", command=self.add_share).pack(side=LEFT)
        ttk.Button(share_bar, text="Edit Directory", command=self.edit_share).pack(side=LEFT, padx=6)
        ttk.Button(share_bar, text="Remove Share", command=self.remove_share).pack(side=LEFT)

        self.share_tree = ttk.Treeview(
            self,
            columns=("name", "path", "permission", "allow_create_delete"),
            show="headings",
            height=7,
        )
        for name, text, width in (
            ("name", "Share", 120),
            ("path", "Path", 360),
            ("permission", "Permission", 100),
            ("allow_create_delete", "Delete/Rename", 110),
        ):
            self.share_tree.heading(name, text=text)
            self.share_tree.column(name, width=width)
        self.share_tree.pack(fill=BOTH, expand=True)
        self.share_tree.bind("<Double-1>", lambda _event: self.edit_share())

        user_bar = ttk.Frame(self)
        user_bar.pack(fill=X, pady=(10, 4))
        ttk.Button(user_bar, text="Add User", command=self.add_user).pack(side=LEFT)
        ttk.Button(user_bar, text="Edit User", command=self.edit_user).pack(side=LEFT, padx=6)
        ttk.Button(user_bar, text="Remove User", command=self.remove_user).pack(side=LEFT)

        self.user_tree = ttk.Treeview(self, columns=("username", "shares"), show="headings", height=5)
        self.user_tree.heading("username", text="User")
        self.user_tree.heading("shares", text="Allowed Shares")
        self.user_tree.column("username", width=160)
        self.user_tree.column("shares", width=520)
        self.user_tree.pack(fill=BOTH, expand=True)
        self.user_tree.bind("<Double-1>", lambda _event: self.edit_user())

        ttk.Label(self, text="Log").pack(anchor="w", pady=(10, 2))
        self.log_text = tk.Text(self, height=9)
        self.log_text.pack(fill=BOTH, expand=True)

    def _load(self) -> None:
        for item in self.config.get("shares", []):
            self.share_tree.insert(
                "",
                END,
                values=(
                    item["name"],
                    item["path"],
                    item["permission"],
                    _bool_text(bool(item.get("allow_create_delete", False))),
                ),
            )
        for item in self.config.get("users", []):
            shares = ",".join(item.get("shares", []))
            self.user_tree.insert("", END, values=(item["username"], shares))

    def _current_shares(self) -> list[Share]:
        shares: list[Share] = []
        for item_id in self.share_tree.get_children():
            values = list(self.share_tree.item(item_id, "values"))
            name, path, permission = values[:3]
            allow_create_delete = _truthy(values[3]) if len(values) > 3 else False
            shares.append(Share(str(name), Path(path), str(permission), allow_create_delete))
        return shares

    def _current_users(self) -> list[UserAccount]:
        users: list[UserAccount] = []
        passwords = self.config.setdefault("user_passwords", {})
        for item_id in self.user_tree.get_children():
            username, shares_text = self.user_tree.item(item_id, "values")
            shares = frozenset(part.strip() for part in str(shares_text).split(",") if part.strip())
            users.append(UserAccount(str(username), str(passwords.get(str(username), "")), shares))
        return users

    def _persist(self) -> None:
        self.config["server_host"] = self.host_var.get()
        self.config["server_port"] = int(self.port_var.get() or "18888")
        self.config["shares"] = [
            {
                "name": share.name,
                "path": str(share.path),
                "permission": share.permission,
                "allow_create_delete": share.allow_create_delete,
            }
            for share in self._current_shares()
        ]
        self.config["users"] = [
            {"username": user.username, "shares": sorted(user.shares)}
            for user in self._current_users()
        ]
        _save_config(self.config)

    def _persist_and_apply(self) -> bool:
        try:
            shares = self._current_shares()
            share_names = {share.name for share in shares}
            for share in shares:
                share.validate()
            users = self._current_users()
            for user in users:
                user.validate(share_names)
        except Exception as exc:
            messagebox.showerror("Remote Share", str(exc))
            return False
        self._persist()
        if self.server and self.loop and self.thread and self.thread.is_alive():
            self.loop.call_soon_threadsafe(self.server.update_config, shares, users)
        return True

    def _replace_user_share_name(self, old_name: str, new_name: str) -> None:
        if not old_name or old_name == new_name:
            return
        for item_id in self.user_tree.get_children():
            username, shares_text = self.user_tree.item(item_id, "values")
            parts = [part.strip() for part in str(shares_text).split(",") if part.strip()]
            if "*" in parts:
                continue
            changed = [new_name if part == old_name else part for part in parts]
            self.user_tree.item(item_id, values=(username, ",".join(changed)))

    def _remove_user_share_names(self, removed_names: set[str]) -> None:
        if not removed_names:
            return
        for item_id in self.user_tree.get_children():
            username, shares_text = self.user_tree.item(item_id, "values")
            parts = [part.strip() for part in str(shares_text).split(",") if part.strip()]
            if "*" in parts:
                continue
            kept = [part for part in parts if part not in removed_names]
            self.user_tree.item(item_id, values=(username, ",".join(kept)))

    def add_share(self) -> None:
        path = filedialog.askdirectory(title="Choose directory to share")
        if not path:
            return
        self._open_share_dialog(
            title="Add Share",
            item_id=None,
            name=Path(path).name or "Share",
            path=path,
            permission=READONLY,
            allow_create_delete=False,
        )

    def edit_share(self) -> None:
        selection = self.share_tree.selection()
        if not selection:
            messagebox.showwarning("Remote Share", "Select a shared directory first.")
            return
        item_id = selection[0]
        values = list(self.share_tree.item(item_id, "values"))
        self._open_share_dialog(
            title="Edit Share",
            item_id=item_id,
            name=str(values[0]),
            path=str(values[1]),
            permission=str(values[2]),
            allow_create_delete=_truthy(values[3]) if len(values) > 3 else False,
        )

    def _open_share_dialog(
        self,
        title: str,
        item_id: str | None,
        name: str,
        path: str,
        permission: str,
        allow_create_delete: bool,
    ) -> None:
        dialog = tk.Toplevel(self)
        dialog.title(title)
        name_var = tk.StringVar(value=name)
        path_var = tk.StringVar(value=path)
        perm_var = tk.StringVar(value=permission)
        create_var = tk.BooleanVar(value=allow_create_delete)

        def browse() -> None:
            selected = filedialog.askdirectory(title="Choose directory to share")
            if selected:
                path_var.set(selected)

        ttk.Label(dialog, text="Name").pack(padx=10, pady=(10, 2), anchor="w")
        ttk.Entry(dialog, textvariable=name_var).pack(padx=10, fill=X)
        ttk.Label(dialog, text="Path").pack(padx=10, pady=(10, 2), anchor="w")
        path_row = ttk.Frame(dialog)
        path_row.pack(padx=10, fill=X)
        ttk.Entry(path_row, textvariable=path_var, width=46).pack(side=LEFT, fill=X, expand=True)
        ttk.Button(path_row, text="Browse", command=browse).pack(side=LEFT, padx=(6, 0))
        ttk.Label(dialog, text="Permission").pack(padx=10, pady=(10, 2), anchor="w")
        ttk.Combobox(dialog, textvariable=perm_var, values=(READONLY, READWRITE), state="readonly").pack(padx=10, fill=X)
        ttk.Checkbutton(
            dialog,
            text="Allow deleting and renaming files/directories",
            variable=create_var,
        ).pack(padx=10, pady=(10, 0), anchor="w")

        def accept() -> None:
            share_name = name_var.get().strip()
            share_path = path_var.get().strip()
            if not share_name or not share_path:
                return
            values = (share_name, share_path, perm_var.get(), _bool_text(create_var.get()))
            if item_id:
                self.share_tree.item(item_id, values=values)
                self._replace_user_share_name(name, share_name)
            else:
                self.share_tree.insert("", END, values=values)
            if self._persist_and_apply():
                dialog.destroy()

        ttk.Button(dialog, text="Save", command=accept).pack(pady=10)
        dialog.transient(self)
        dialog.grab_set()

    def remove_share(self) -> None:
        removed_names: set[str] = set()
        for item_id in self.share_tree.selection():
            removed_names.add(str(self.share_tree.item(item_id, "values")[0]))
            self.share_tree.delete(item_id)
        self._remove_user_share_names(removed_names)
        self._persist_and_apply()

    def add_user(self) -> None:
        share_names = [self.share_tree.item(item_id, "values")[0] for item_id in self.share_tree.get_children()]
        self._open_user_dialog(
            title="Add User",
            item_id=None,
            username="",
            password="",
            shares_text=",".join(share_names),
        )

    def edit_user(self) -> None:
        selection = self.user_tree.selection()
        if not selection:
            messagebox.showwarning("Remote Share", "Select a user first.")
            return
        item_id = selection[0]
        username, shares_text = self.user_tree.item(item_id, "values")
        password = str(self.config.setdefault("user_passwords", {}).get(str(username), ""))
        self._open_user_dialog(
            title="Edit User",
            item_id=item_id,
            username=str(username),
            password=password,
            shares_text=str(shares_text),
        )

    def _open_user_dialog(
        self,
        title: str,
        item_id: str | None,
        username: str,
        password: str,
        shares_text: str,
    ) -> None:
        dialog = tk.Toplevel(self)
        dialog.title(title)
        username_var = tk.StringVar(value=username)
        password_var = tk.StringVar(value=password)
        shares_var = tk.StringVar(value=shares_text)
        ttk.Label(dialog, text="Username").pack(padx=10, pady=(10, 2), anchor="w")
        ttk.Entry(dialog, textvariable=username_var).pack(padx=10, fill=X)
        ttk.Label(dialog, text="Password").pack(padx=10, pady=(10, 2), anchor="w")
        ttk.Entry(dialog, textvariable=password_var, show="*").pack(padx=10, fill=X)
        ttk.Label(dialog, text="Allowed shares, comma separated, or *").pack(padx=10, pady=(10, 2), anchor="w")
        ttk.Entry(dialog, textvariable=shares_var, width=46).pack(padx=10, fill=X)

        def accept() -> None:
            next_username = username_var.get().strip()
            if not next_username:
                return
            next_shares_text = shares_var.get().strip() or "*"
            passwords = self.config.setdefault("user_passwords", {})
            if item_id and username and username != next_username:
                passwords.pop(username, None)
            passwords[next_username] = password_var.get()
            if item_id:
                self.user_tree.item(item_id, values=(next_username, next_shares_text))
            else:
                self.user_tree.insert("", END, values=(next_username, next_shares_text))
            if self._persist_and_apply():
                dialog.destroy()

        ttk.Button(dialog, text="Save", command=accept).pack(pady=10)
        dialog.transient(self)
        dialog.grab_set()

    def remove_user(self) -> None:
        passwords = self.config.setdefault("user_passwords", {})
        for item_id in self.user_tree.selection():
            username = str(self.user_tree.item(item_id, "values")[0])
            passwords.pop(username, None)
            self.user_tree.delete(item_id)
        self._persist_and_apply()

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        try:
            shares = self._current_shares()
            if not shares:
                messagebox.showwarning("Remote Share", "Add at least one shared directory.")
                return
            users = self._current_users()
            self._persist()
            self.loop = asyncio.new_event_loop()
            self.stop_event = threading.Event()
            self.server = RemoteShareServer(self.host_var.get(), int(self.port_var.get()), shares, users)
            self.server.on_log = self.log_queue.put
        except Exception as exc:
            messagebox.showerror("Remote Share", str(exc))
            return

        def run() -> None:
            assert self.loop is not None and self.stop_event is not None and self.server is not None
            asyncio.set_event_loop(self.loop)

            async def runner() -> None:
                await self.server.start()
                while not self.stop_event.is_set():
                    await asyncio.sleep(0.2)
                await self.server.stop()

            try:
                self.loop.run_until_complete(runner())
            except Exception as exc:
                self.log_queue.put(f"server error: {exc}")

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.loop and self.stop_event:
            self.loop.call_soon_threadsafe(self.stop_event.set)

    def _drain_logs(self) -> None:
        try:
            while True:
                self.log(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        self.after(200, self._drain_logs)

    def log(self, line: str) -> None:
        self.log_text.insert(END, line + "\n")
        self.log_text.see(END)


class ClientPage(ttk.Frame):
    def __init__(self, master: tk.Misc, config: dict) -> None:
        super().__init__(master, padding=10)
        self.config = config
        self.mounts: dict[str, dict] = {}
        self._build()

    def _build(self) -> None:
        conn = ttk.Frame(self)
        conn.pack(fill=X)
        ttk.Label(conn, text="Host").pack(side=LEFT)
        self.host_var = tk.StringVar(value=self.config.get("client_host", "127.0.0.1"))
        ttk.Entry(conn, textvariable=self.host_var, width=18).pack(side=LEFT, padx=4)
        ttk.Label(conn, text="Port").pack(side=LEFT)
        self.port_var = tk.StringVar(value=str(self.config.get("client_port", 18888)))
        ttk.Entry(conn, textvariable=self.port_var, width=8).pack(side=LEFT, padx=4)
        ttk.Label(conn, text="User").pack(side=LEFT)
        self.username_var = tk.StringVar(value=self.config.get("client_username", ""))
        ttk.Entry(conn, textvariable=self.username_var, width=14).pack(side=LEFT, padx=4)
        ttk.Label(conn, text="Password").pack(side=LEFT)
        self.password_var = tk.StringVar(value=self.config.get("client_password", ""))
        ttk.Entry(conn, textvariable=self.password_var, width=14, show="*").pack(side=LEFT, padx=4)
        ttk.Button(conn, text="Refresh", command=self.refresh).pack(side=LEFT, padx=6)

        self.share_tree = ttk.Treeview(self, columns=("name", "permission", "allow_create_delete"), show="headings", height=7)
        # self.share_tree = ttk.Treeview(self, columns=("name", "permission"), height=7)
        self.share_tree.heading("name", text="Share")
        self.share_tree.heading("permission", text="Permission")
        self.share_tree.heading("allow_create_delete", text="Delete/Rename")
        self.share_tree.column("name", width=220)
        self.share_tree.column("permission", width=120)
        self.share_tree.column("allow_create_delete", width=120)
        self.share_tree.pack(fill=BOTH, expand=True, pady=(10, 4))

        mount = ttk.Frame(self)
        mount.pack(fill=X)
        if platform.system() == "Windows":
            label = "Drive letter"
            default_target = self.config.get("mount_target", "Z:")
        else:
            label = "Mount point"
            default_target = self.config.get("mount_target", str(Path.home() / "remote_share"))
        ttk.Label(mount, text=label).pack(side=LEFT)
        self.mount_var = tk.StringVar(value=default_target)
        ttk.Entry(mount, textvariable=self.mount_var, width=24).pack(side=LEFT, padx=4)
        ttk.Button(mount, text="Browse", command=self.browse_mount).pack(side=LEFT)
        ttk.Label(mount, text="Local port").pack(side=LEFT, padx=(10, 0))
        self.local_port_var = tk.StringVar(value=str(self.config.get("local_webdav_port", 18080)))
        ttk.Entry(mount, textvariable=self.local_port_var, width=8).pack(side=LEFT, padx=4)
        ttk.Button(mount, text="Mount Selected", command=self.mount_selected).pack(side=LEFT, padx=6)
        ttk.Button(mount, text="Unmount Selected", command=self.unmount_selected).pack(side=LEFT)

        self.mount_tree = ttk.Treeview(self, columns=("share", "target", "status"), show="headings", height=5)
        self.mount_tree.heading("share", text="Mounted Share")
        self.mount_tree.heading("target", text="Target")
        self.mount_tree.heading("status", text="Status")
        self.mount_tree.column("share", width=180)
        self.mount_tree.column("target", width=260)
        self.mount_tree.column("status", width=220)
        self.mount_tree.pack(fill=BOTH, expand=True, pady=(10, 4))

        ttk.Label(self, text="Log").pack(anchor="w", pady=(8, 2))
        self.log_text = tk.Text(self, height=8)
        self.log_text.pack(fill=BOTH, expand=True)

    def log(self, line: str) -> None:
        self.log_text.insert(END, line + "\n")
        self.log_text.see(END)

    def _client(self) -> RemoteShareClient:
        return RemoteShareClient(
            self.host_var.get(),
            int(self.port_var.get()),
            username=self.username_var.get(),
            password=self.password_var.get(),
        )

    def _persist(self) -> None:
        self.config["client_host"] = self.host_var.get()
        self.config["client_port"] = int(self.port_var.get() or "18888")
        self.config["client_username"] = self.username_var.get()
        self.config["client_password"] = self.password_var.get()
        self.config["mount_target"] = self.mount_var.get()
        self.config["local_webdav_port"] = int(self.local_port_var.get() or "18080")
        _save_config(self.config)

    def refresh(self) -> None:
        try:
            client = self._client()
            shares = client.list_shares()
            client.close()
            self.share_tree.delete(*self.share_tree.get_children())
            for share in shares:
                self.share_tree.insert(
                    "",
                    END,
                    values=(
                        share["name"],
                        share["permission"],
                        _bool_text(bool(share.get("allow_create_delete", False))),
                    ),
                )
            self._persist()
            self.log(f"loaded {len(shares)} share(s)")
        except Exception as exc:
            messagebox.showerror("Remote Share", str(exc))

    def browse_mount(self) -> None:
        if platform.system() == "Windows":
            return
        path = filedialog.askdirectory(title="Choose mount point")
        if path:
            self.mount_var.set(path)

    def _selected_share(self) -> str | None:
        selection = self.share_tree.selection()
        if not selection:
            return None
        return str(self.share_tree.item(selection[0], "values")[0])

    def _selected_mount_id(self) -> str | None:
        selection = self.mount_tree.selection()
        if selection:
            return selection[0]
        share = self._selected_share()
        if share:
            for item_id, data in self.mounts.items():
                if data["share"] == share:
                    return item_id
        return None

    def mount_selected(self) -> None:
        share = self._selected_share()
        if not share:
            messagebox.showwarning("Remote Share", "Select a share first.")
            return
        self._persist()
        if platform.system() == "Windows":
            self._mount_windows(share)
        else:
            self._mount_linux(share)

    def _mount_linux(self, share: str) -> None:
        target = self.mount_var.get()
        Path(target).mkdir(parents=True, exist_ok=True)
        cmd = _app_command(
            "mount-fuse",
            "--host",
            self.host_var.get(),
            "--port",
            self.port_var.get(),
            "--share",
            share,
            "--mount",
            target,
            "--username",
            self.username_var.get(),
            "--password",
            self.password_var.get(),
        )
        try:
            proc = _popen(cmd)
            item_id = self.mount_tree.insert("", END, values=(share, target, f"FUSE pid={proc.pid}"))
            self.mounts[item_id] = {"share": share, "target": target, "proc": proc, "kind": "linux"}
            self.log(f"mounted {share} at {target}")
        except Exception as exc:
            messagebox.showerror("Remote Share", str(exc))

    def _normalize_windows_drive(self) -> str:
        drive = self.mount_var.get().strip().strip("\"'").upper()
        if len(drive) == 1 and drive.isalpha():
            return drive + ":"
        if len(drive) == 2 and drive[0].isalpha() and drive[1] == ":":
            return drive
        raise ValueError("Enter a Windows drive letter like Z: or Y:.")

    def _is_local_endpoint_available(self, host: str, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
            except OSError:
                return False
        return True

    def _next_webdav_endpoint(self, start_port: int) -> tuple[str, int]:
        if not 1 <= start_port <= 65535:
            raise ValueError("Local port must be between 1 and 65535.")
        # Keep the WebDAV bridge on 127.0.0.1 for best compatibility with Windows WebClient.
        host = "127.0.0.1"
        used_ports = {
            int(data.get("listen_port"))
            for data in self.mounts.values()
            if data.get("kind") == "windows" and data.get("listen_host") == host and data.get("listen_port") is not None
        }
        for port in range(start_port, 65536):
            if port not in used_ports and self._is_local_endpoint_available(host, port):
                return host, port
        raise RuntimeError("No available local WebDAV endpoint found.")

    def _wait_webdav_ready(self, url: str, timeout: float = 8.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            request = urllib.request.Request(url, method="OPTIONS")
            try:
                with urllib.request.urlopen(request, timeout=1.0):
                    return True
            except Exception:
                time.sleep(0.2)
        return False

    def _webdav_targets(self, share: str, listen_host: str, listen_port: int) -> list[str]:
        share_name = share.strip("/").replace("/", "\\")
        if not share_name:
            return []
        url_share = urllib.parse.quote(share.strip("/"), safe="")
        targets = [
            rf"\\{listen_host}@{listen_port}\DavWWWRoot\{share_name}",
            rf"\\{listen_host}@{listen_port}\{share_name}",
            f"http://{listen_host}:{listen_port}/{url_share}/",
        ]
        if listen_host.startswith("127."):
            targets.extend(
                [
                    rf"\\localhost@{listen_port}\DavWWWRoot\{share_name}",
                    rf"\\localhost@{listen_port}\{share_name}",
                    f"http://localhost:{listen_port}/{url_share}/",
                ]
            )
        unique: list[str] = []
        seen: set[str] = set()
        for target in targets:
            if target not in seen:
                seen.add(target)
                unique.append(target)
        return unique

    def _map_windows_drive(self, drive: str, targets: list[str]) -> tuple[str, list[str]]:
        errors: list[str] = []
        for target in targets:
            try:
                self._run_net_use([drive, target, "/persistent:no"])
                return target, errors
            except RuntimeError as exc:
                errors.append(f"{target}: {exc}")
        raise RuntimeError("\n".join(errors) if errors else "No WebDAV target generated for mapping.")

    def _run_net_use(self, args: list[str]) -> None:
        result = subprocess.run(["net", "use", *args], capture_output=True, text=True)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(detail or f"net use failed with exit code {result.returncode}")

    def _service_state(self, name: str) -> str | None:
        result = subprocess.run(["sc", "query", name], capture_output=True, text=True)
        if result.returncode != 0:
            return None
        text = f"{result.stdout}\n{result.stderr}"
        for line in text.splitlines():
            if "STATE" in line:
                upper = line.upper()
                if "RUNNING" in upper:
                    return "RUNNING"
                if "STOPPED" in upper:
                    return "STOPPED"
                return "OTHER"
        return None

    def _ensure_webclient_running(self) -> None:
        state = self._service_state("WebClient")
        if state == "RUNNING":
            return
        if state is None:
            raise RuntimeError("Windows WebClient service was not found. WebDAV drive mapping is unavailable on this system.")
        start = subprocess.run(["sc", "start", "WebClient"], capture_output=True, text=True)
        if start.returncode != 0:
            detail = (start.stderr or start.stdout or "").strip()
            raise RuntimeError(f"Failed to start WebClient service: {detail or start.returncode}")
        deadline = time.time() + 8.0
        while time.time() < deadline:
            if self._service_state("WebClient") == "RUNNING":
                return
            time.sleep(0.2)
        raise RuntimeError("WebClient service did not reach RUNNING state in time.")

    def _restart_webclient(self) -> None:
        subprocess.run(["sc", "stop", "WebClient"], capture_output=True, text=True)
        deadline = time.time() + 10.0
        while time.time() < deadline:
            state = self._service_state("WebClient")
            if state in ("STOPPED", None):
                break
            time.sleep(0.2)
        start = subprocess.run(["sc", "start", "WebClient"], capture_output=True, text=True)
        if start.returncode != 0:
            detail = (start.stderr or start.stdout or "").strip()
            raise RuntimeError(f"Failed to restart WebClient service: {detail or start.returncode}")
        deadline = time.time() + 8.0
        while time.time() < deadline:
            if self._service_state("WebClient") == "RUNNING":
                return
            time.sleep(0.2)
        raise RuntimeError("WebClient service did not become RUNNING after restart.")

    def _is_windows_drive_in_use(self, drive: str) -> bool:
        bit = ord(drive[0]) - ord("A")
        return bool(ctypes.windll.kernel32.GetLogicalDrives() & (1 << bit))

    def _mount_windows(self, share: str) -> None:
        proc = None
        try:
            drive = self._normalize_windows_drive()
            if any(data.get("kind") == "windows" and data.get("target") == drive for data in self.mounts.values()):
                raise ValueError(f"{drive} is already mounted in this window. Choose another drive letter.")
            if self._is_windows_drive_in_use(drive):
                raise ValueError(f"{drive} is already in use by Windows. Choose another drive letter or unmount it first.")
            self._ensure_webclient_running()
            listen_host, listen_port = self._next_webdav_endpoint(int(self.local_port_var.get() or "18080"))
            url_share = urllib.parse.quote(share.strip("/"), safe="")
            url = f"http://{listen_host}:{listen_port}/{url_share}/"
            cmd = _app_command(
                "webdav",
                "--remote-host",
                self.host_var.get(),
                "--remote-port",
                self.port_var.get(),
                "--share",
                share,
                "--listen-host",
                listen_host,
                "--listen-port",
                str(listen_port),
                "--username",
                self.username_var.get(),
                "--password",
                self.password_var.get(),
            )
            proc = _popen(cmd)
            if not self._wait_webdav_ready(url):
                raise RuntimeError("WebDAV bridge did not become ready in time.")
            if proc.poll() is not None:
                raise RuntimeError("WebDAV bridge exited before Windows could map the drive.")
            targets = self._webdav_targets(share, listen_host, listen_port)
            try:
                mapped_target, attempts = self._map_windows_drive(drive, targets)
            except RuntimeError as first_exc:
                if "System error 67" not in str(first_exc):
                    raise
                self.log("WebDAV mapping returned system error 67, restarting WebClient and retrying once.")
                self._restart_webclient()
                mapped_target, attempts = self._map_windows_drive(drive, targets)
            item_id = self.mount_tree.insert("", END, values=(share, drive, f"WebDAV {mapped_target} pid={proc.pid}"))
            self.mounts[item_id] = {
                "share": share,
                "target": drive,
                "proc": proc,
                "kind": "windows",
                "url": mapped_target,
                "listen_host": listen_host,
                "listen_port": listen_port,
            }
            self.log(f"mounted {share} to {drive} via {listen_host}:{listen_port}")
            if attempts:
                self.log(f"mount fallback succeeded after {len(attempts)} failed target(s)")
        except Exception as exc:
            if proc and proc.poll() is None:
                proc.terminate()
            details = str(exc)
            if "System error 67" in details:
                details += "\n\nHint: verify Windows WebClient service is running, then retry mount."
            messagebox.showerror("Remote Share", details)

    def unmount_selected(self) -> None:
        item_id = self._selected_mount_id()
        if not item_id:
            messagebox.showwarning("Remote Share", "Select a mounted share first.")
            return
        data = self.mounts.pop(item_id, None)
        if not data:
            return
        try:
            if data["kind"] == "windows":
                result = subprocess.run(["net", "use", data["target"], "/delete", "/y"], capture_output=True, text=True)
                if result.returncode != 0:
                    detail = (result.stderr or result.stdout or "").strip()
                    self.log(f"warning: failed to remove Windows drive mapping: {detail or result.returncode}")
            else:
                tool = "fusermount3" if shutil_which("fusermount3") else "fusermount"
                subprocess.run([tool, "-u", data["target"]], capture_output=True, text=True)
            proc = data.get("proc")
            if proc and proc.poll() is None:
                proc.terminate()
            self.mount_tree.delete(item_id)
            self.log(f"unmounted {data['share']} from {data['target']}")
        except Exception as exc:
            messagebox.showerror("Remote Share", str(exc))

    def stop_all(self) -> None:
        for item_id in list(self.mounts):
            self.mount_tree.selection_set(item_id)
            self.unmount_selected()


def shutil_which(name: str) -> str | None:
    paths = os.environ.get("PATH", "").split(os.pathsep)
    suffixes = [""] if platform.system() != "Windows" else os.environ.get("PATHEXT", ".EXE").split(os.pathsep)
    for folder in paths:
        for suffix in suffixes:
            candidate = Path(folder) / f"{name}{suffix}"
            if candidate.exists():
                return str(candidate)
    return None


class UnifiedGUI:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Remote Share Mount")
        self.root.geometry("860x680")
        self.config = _load_config()
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=BOTH, expand=True)
        self.server_page = ServerPage(notebook, self.config)
        self.client_page = ClientPage(notebook, self.config)
        notebook.add(self.server_page, text="Server")
        notebook.add(self.client_page, text="Client")

    def run(self) -> None:
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self) -> None:
        # self.unmount_selected()
        self.server_page.stop()
        self.client_page.stop_all()
        self.root.destroy()


def run_gui() -> None:
    UnifiedGUI().run()


def run_server_gui() -> None:
    app = UnifiedGUI()
    app.root.after(100, lambda: None)
    app.run()


def run_client_gui() -> None:
    app = UnifiedGUI()
    app.root.after(100, lambda: None)
    app.run()
