from __future__ import annotations

import asyncio
import json
import os
import platform
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from tkinter import BOTH, END, LEFT, X, filedialog, messagebox, ttk
import tkinter as tk

from .client_core import RemoteShareClient
from .server_core import READONLY, READWRITE, RemoteShareServer, Share, UserAccount

CONFIG_PATH = Path.home() / ".remote_share_mount" / "config.json"


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
        ttk.Button(share_bar, text="Remove Share", command=self.remove_share).pack(side=LEFT, padx=6)

        self.share_tree = ttk.Treeview(self, columns=("name", "path", "permission"), show="headings", height=7)
        for name, text, width in (
            ("name", "Share", 120),
            ("path", "Path", 420),
            ("permission", "Permission", 100),
        ):
            self.share_tree.heading(name, text=text)
            self.share_tree.column(name, width=width)
        self.share_tree.pack(fill=BOTH, expand=True)

        user_bar = ttk.Frame(self)
        user_bar.pack(fill=X, pady=(10, 4))
        ttk.Button(user_bar, text="Add User", command=self.add_user).pack(side=LEFT)
        ttk.Button(user_bar, text="Remove User", command=self.remove_user).pack(side=LEFT, padx=6)

        self.user_tree = ttk.Treeview(self, columns=("username", "shares"), show="headings", height=5)
        self.user_tree.heading("username", text="User")
        self.user_tree.heading("shares", text="Allowed Shares")
        self.user_tree.column("username", width=160)
        self.user_tree.column("shares", width=520)
        self.user_tree.pack(fill=BOTH, expand=True)

        ttk.Label(self, text="Log").pack(anchor="w", pady=(10, 2))
        self.log_text = tk.Text(self, height=9)
        self.log_text.pack(fill=BOTH, expand=True)

    def _load(self) -> None:
        for item in self.config.get("shares", []):
            self.share_tree.insert("", END, values=(item["name"], item["path"], item["permission"]))
        for item in self.config.get("users", []):
            shares = ",".join(item.get("shares", []))
            self.user_tree.insert("", END, values=(item["username"], shares))

    def _current_shares(self) -> list[Share]:
        shares: list[Share] = []
        for item_id in self.share_tree.get_children():
            name, path, permission = self.share_tree.item(item_id, "values")
            shares.append(Share(str(name), Path(path), str(permission)))
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
            {"name": share.name, "path": str(share.path), "permission": share.permission}
            for share in self._current_shares()
        ]
        self.config["users"] = [
            {"username": user.username, "shares": sorted(user.shares)}
            for user in self._current_users()
        ]
        _save_config(self.config)

    def add_share(self) -> None:
        path = filedialog.askdirectory(title="Choose directory to share")
        if not path:
            return
        dialog = tk.Toplevel(self)
        dialog.title("Add Share")
        name_var = tk.StringVar(value=Path(path).name or "Share")
        perm_var = tk.StringVar(value=READONLY)
        ttk.Label(dialog, text="Name").pack(padx=10, pady=(10, 2), anchor="w")
        ttk.Entry(dialog, textvariable=name_var).pack(padx=10, fill=X)
        ttk.Label(dialog, text="Permission").pack(padx=10, pady=(10, 2), anchor="w")
        ttk.Combobox(dialog, textvariable=perm_var, values=(READONLY, READWRITE), state="readonly").pack(padx=10, fill=X)

        def accept() -> None:
            self.share_tree.insert("", END, values=(name_var.get().strip(), path, perm_var.get()))
            self._persist()
            dialog.destroy()

        ttk.Button(dialog, text="Add", command=accept).pack(pady=10)
        dialog.transient(self)
        dialog.grab_set()

    def remove_share(self) -> None:
        for item_id in self.share_tree.selection():
            self.share_tree.delete(item_id)
        self._persist()

    def add_user(self) -> None:
        share_names = [self.share_tree.item(item_id, "values")[0] for item_id in self.share_tree.get_children()]
        dialog = tk.Toplevel(self)
        dialog.title("Add User")
        username_var = tk.StringVar()
        password_var = tk.StringVar()
        shares_var = tk.StringVar(value=",".join(share_names))
        ttk.Label(dialog, text="Username").pack(padx=10, pady=(10, 2), anchor="w")
        ttk.Entry(dialog, textvariable=username_var).pack(padx=10, fill=X)
        ttk.Label(dialog, text="Password").pack(padx=10, pady=(10, 2), anchor="w")
        ttk.Entry(dialog, textvariable=password_var, show="*").pack(padx=10, fill=X)
        ttk.Label(dialog, text="Allowed shares, comma separated, or *").pack(padx=10, pady=(10, 2), anchor="w")
        ttk.Entry(dialog, textvariable=shares_var, width=46).pack(padx=10, fill=X)

        def accept() -> None:
            username = username_var.get().strip()
            if not username:
                return
            shares_text = shares_var.get().strip() or "*"
            self.config.setdefault("user_passwords", {})[username] = password_var.get()
            self.user_tree.insert("", END, values=(username, shares_text))
            self._persist()
            dialog.destroy()

        ttk.Button(dialog, text="Add", command=accept).pack(pady=10)
        dialog.transient(self)
        dialog.grab_set()

    def remove_user(self) -> None:
        passwords = self.config.setdefault("user_passwords", {})
        for item_id in self.user_tree.selection():
            username = str(self.user_tree.item(item_id, "values")[0])
            passwords.pop(username, None)
            self.user_tree.delete(item_id)
        self._persist()

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

        self.share_tree = ttk.Treeview(self, columns=("name", "permission"), show="headings", height=7)
        self.share_tree.heading("name", text="Share")
        self.share_tree.heading("permission", text="Permission")
        self.share_tree.column("name", width=220)
        self.share_tree.column("permission", width=120)
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
                self.share_tree.insert("", END, values=(share["name"], share["permission"]))
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

    def _mount_windows(self, share: str) -> None:
        drive = self.mount_var.get().strip().upper()
        if len(drive) == 1:
            drive += ":"
        listen_port = int(self.local_port_var.get() or "18080")
        url = f"http://127.0.0.1:{listen_port}/{share}"
        cmd = _app_command(
            "webdav",
            "--remote-host",
            self.host_var.get(),
            "--remote-port",
            self.port_var.get(),
            "--share",
            share,
            "--listen-port",
            str(listen_port),
            "--username",
            self.username_var.get(),
            "--password",
            self.password_var.get(),
        )
        try:
            proc = _popen(cmd)
            time.sleep(1.0)
            subprocess.run(["net", "use", drive, url, "/persistent:no"], check=True, capture_output=True, text=True)
            item_id = self.mount_tree.insert("", END, values=(share, drive, f"WebDAV {url} pid={proc.pid}"))
            self.mounts[item_id] = {"share": share, "target": drive, "proc": proc, "kind": "windows", "url": url}
            self.log(f"mounted {share} to {drive}")
        except Exception as exc:
            if "proc" in locals() and proc.poll() is None:
                proc.terminate()
            messagebox.showerror("Remote Share", str(exc))

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
                subprocess.run(["net", "use", data["target"], "/delete", "/y"], capture_output=True, text=True)
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
