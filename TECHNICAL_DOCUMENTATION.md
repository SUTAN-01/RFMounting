# Remote Share Mount 技术文档

## 1. 项目概述

Remote Share Mount 是一个跨平台远程目录共享和挂载工具。一个程序同时提供服务端和客户端能力：

- 服务端把本机目录发布为远程共享目录。
- 客户端连接服务端，列出可访问共享目录。
- Linux 客户端通过 FUSE 把远程目录挂载到本地挂载点。
- Windows 客户端通过本地 WebDAV 桥接服务和系统 `WebClient` 服务，把远程目录映射成盘符。

项目入口是 `remote_share.cli:main`，执行 `python -m remote_share.cli` 时，如果没有传入子命令，会默认启动统一 GUI。

## 2. 代码模块与职责

| 模块 | 主要职责 | 使用的核心组件 |
| --- | --- | --- |
| `remote_share/gui.py` | 统一图形界面、服务端配置、客户端连接、挂载和卸载控制 | `tkinter`、`ttk`、`threading`、`asyncio`、`subprocess`、`socket`、Windows `sc` / `net use` |
| `remote_share/cli.py` | 命令行入口和子命令分发 | `argparse`、`asyncio`、`platform` |
| `remote_share/server_core.py` | TCP 服务端、权限校验、文件读写、并发写入控制 | `asyncio.start_server`、`Path`、`os`、文件锁、后台扫描任务 |
| `remote_share/client_core.py` | TCP 客户端、请求封装、自动重连、错误转换 | Python `socket`、`threading.RLock` |
| `remote_share/protocol.py` | 自定义 TCP 帧协议编码和解码 | `struct`、`json`、同步/异步 socket IO |
| `remote_share/fuse_mount.py` | Linux FUSE 文件系统适配层 | `fusepy`、Linux FUSE、`RemoteShareClient` |
| `remote_share/webdav_bridge.py` | Windows 本地 WebDAV 桥接服务 | `http.server.ThreadingHTTPServer`、WebDAV 方法、`RemoteShareClient` |
| `remote_share/pathutil.py` | 远程路径标准化和目录逃逸防护 | `pathlib`、`os.path.commonpath` |
| `scripts/build_windows.ps1` | Windows 打包脚本 | `pip`、`PyInstaller` |
| `scripts/build_linux.sh` / `scripts/build_deb.sh` | Linux tar 包和 deb 包构建 | `PyInstaller`、`fuse3`、`python3-tk`、`dpkg-deb` |

## 3. 启动与运行流程

### 3.1 GUI 启动流程

1. 用户执行 `python -m remote_share.cli` 或 `python -m remote_share.cli gui`。
2. `remote_share/__main__.py` 调用 `remote_share.cli.main()`。
3. `cli.py` 使用 `argparse` 解析命令；无参数时自动设置为 `gui`。
4. `cmd_gui()` 导入并调用 `remote_share.gui.run_gui()`。
5. `UnifiedGUI` 创建 `tk.Tk()` 主窗口，窗口标题为 `Remote Share Mount`。
6. GUI 使用 `ttk.Notebook` 创建两个标签页：
   - `Server`：配置共享目录、用户和启动服务。
   - `Client`：连接远程服务、刷新共享、挂载和卸载。
7. GUI 配置通过 JSON 文件保存到用户目录：
   - 路径：`~/.remote_share_mount/config.json`
   - 读写函数：`_load_config()`、`_save_config()`
   - 项目没有使用数据库保存配置或用户信息，不依赖 SQLite、MySQL、PostgreSQL 等数据库服务。

### 3.2 服务端启动流程

1. 用户在 `Server` 页添加共享目录。
2. GUI 把每个共享目录转换为 `Share`：
   - `name`：共享名。
   - `path`：本机真实目录。
   - `permission`：`readonly` 或 `readwrite`。
   - `allow_create_delete`：是否允许删除、重命名、新建目录等管理操作。
3. 可选添加用户，每个用户转换为 `UserAccount`：
   - `username`
   - `password`
   - `shares`：允许访问的共享集合，或 `*` 表示全部共享。
4. 点击 `Start Service` 后，`ServerPage.start()` 会：
   - 校验共享目录必须存在且是目录。
   - 校验权限值是否合法。
   - 校验用户授权中的共享名存在。
   - 保存配置到 `config.json`。
   - 创建新的 `asyncio` event loop。
   - 创建后台线程运行服务端，避免阻塞 Tkinter 主线程。
5. 后台线程中执行：
   - `asyncio.set_event_loop(self.loop)`
   - `RemoteShareServer.start()`
   - `asyncio.start_server(self.handle_client, host, port)`
6. 服务端开始监听 TCP 端口，默认端口为 `18888`。
7. 服务端同时启动 `_scan_local_changes()` 后台任务，每 2 秒扫描共享目录的文件修改时间，用于发现服务端本地直接修改导致的版本变化。

### 3.3 服务端运行时配置更新

服务端启动后，用户仍然可以编辑共享目录或用户。GUI 调用：

```python
self.loop.call_soon_threadsafe(self.server.update_config, shares, users)
```

实现原理是从 Tkinter 主线程安全地把配置更新任务投递到服务端所在的 asyncio 线程。`RemoteShareServer.update_config()` 会重新校验共享和用户，并替换内存中的 `self.shares` 与 `self.users`。后续客户端请求立即使用新配置。

因此，程序运行期间可以直接修改共享目录权限或用户授权，不需要重启服务端，也不需要重新挂载客户端。已经挂载的 Linux FUSE 挂载点或 Windows 盘符仍会保持挂载状态，但它们后续发出的 `stat`、`list_dir`、`read`、`write`、`delete`、`rename` 等请求都会在服务端按最新权限重新校验。如果某个用户原本有写权限，运行中被改成只读，那么该用户已经挂载的目录不会立刻断开，但下一次写入、删除或重命名会被服务端拒绝，并通过错误响应返回到 FUSE 或 WebDAV 层。

需要注意的是，当前实现不会主动向客户端推送“权限已变化”的事件，也不会主动关闭已挂载连接。权限变化的生效点是“下一次远程请求到达服务端时”。客户端界面上的共享列表如果已经显示旧权限，需要点击 `Refresh` 才会重新拉取并显示最新共享信息。

## 4. 用户界面实现原理

GUI 使用 Python 标准库 `tkinter` 和 `tkinter.ttk` 实现，不依赖 Web 前端框架。

### 4.1 主窗口

- `tk.Tk()` 创建主窗口。
- `ttk.Notebook` 实现 `Server` 和 `Client` 双标签页。
- `ttk.Frame` 组织布局。
- `ttk.Entry` 输入主机、端口、用户名、密码、挂载点等。
- `ttk.Button` 触发启动、停止、刷新、挂载、卸载等操作。
- `ttk.Treeview` 展示共享目录、用户列表、远程共享列表、已挂载列表。
- `tk.Text` 展示服务端和客户端日志。
- `filedialog.askdirectory()` 选择本地共享目录或 Linux 挂载点。
- `messagebox` 展示错误和警告。

### 4.2 GUI 与后台任务的线程模型

Tkinter 主循环必须运行在主线程，否则容易出现界面卡死或崩溃。项目中：

- GUI 事件处理运行在 Tkinter 主线程。
- 服务端运行在后台 `threading.Thread`。
- 服务端内部使用独立 `asyncio` event loop。
- 日志从服务端线程写入 `queue.Queue`。
- GUI 使用 `after(200, self._drain_logs)` 每 200 毫秒从队列取日志并刷新 `Text` 控件。

这种设计让服务端可以持续监听网络连接，同时 GUI 仍然保持响应。

## 5. 网络通信协议

### 5.1 底层网络组件

项目的服务端和客户端直接使用 TCP socket 通信：

- 服务端：`asyncio.start_server()` 创建异步 TCP 服务。
- 客户端：`socket.create_connection()` 创建同步 TCP 连接。
- Windows WebDAV 桥和 Linux FUSE 进程内部也都是通过 `RemoteShareClient` 访问远程 TCP 服务。

### 5.2 自定义帧格式

协议定义在 `remote_share/protocol.py`。每个 TCP 消息都是一帧：

```text
1 byte frame_type + 4 bytes body_length + frame_body
```

其中 `frame_body` 又包含：

```text
4 bytes json_length + JSON metadata + binary data
```

字段说明：

- `frame_type`：帧类型。
  - `1`：`FRAME_REQUEST`，客户端请求。
  - `2`：`FRAME_RESPONSE`，服务端正常响应。
  - `3`：`FRAME_ERROR`，服务端错误响应。
  - `4`：`FRAME_EVENT`，预留事件帧，目前代码中没有作为主流程使用。
- `body_length`：帧体总长度，网络字节序。
- `json_length`：JSON 元数据长度，网络字节序。
- `JSON metadata`：请求操作、路径、偏移、大小、session 等结构化信息。
- `binary data`：读写文件时携带的二进制内容。

大小限制：

- `MAX_FRAME_SIZE = 16 * 1024 * 1024`，单帧最大 16 MB。
- `MAX_CHUNK_SIZE = 64 * 1024`，单次文件读写块最大 64 KB。

实际文件内容按块传输，避免一次性把大文件读入内存。

### 5.3 客户端连接与认证

`RemoteShareClient` 首次发送请求前会自动连接服务端：

1. `socket.create_connection((host, port), timeout=15.0)` 建立 TCP 连接。
2. 自动发送 `hello` 请求，携带用户名和密码。
3. 服务端返回 `session_id`、认证状态和最大块大小。
4. 客户端保存 `session_id`，后续请求都会带上该 ID。

如果请求时发生 `OSError`、`EOFError` 或协议错误，客户端会关闭 socket 并尝试重新连接，然后重发一次请求。

### 5.4 服务端请求分发

服务端在 `handle_client()` 中循环读取帧：

1. 调用 `async_recv_message(reader)` 读取请求帧。
2. 检查帧类型必须是 `FRAME_REQUEST`。
3. 从 JSON 元数据里读取 `op`。
4. 调用 `dispatch(session_id, request, data)` 分发操作。
5. 正常结果发送 `FRAME_RESPONSE`。
6. 业务错误或协议错误发送 `FRAME_ERROR`。

支持的主要操作：

| op | 功能 |
| --- | --- |
| `hello` | 登录认证并建立 session |
| `list_shares` | 列出当前用户可访问的共享 |
| `clients` | 列出连接中的客户端 |
| `list_dir` | 列出目录 |
| `stat` | 查询文件或目录属性 |
| `read` | 按 offset 和 size 读取文件块 |
| `write` | 按 offset 写入文件块 |
| `create` | 新建文件或目录 |
| `truncate` | 截断文件 |
| `delete` | 删除文件或空目录 |
| `rename` | 重命名或移动 |
| `utime` | 修改访问时间和修改时间 |

## 6. 权限与路径安全

### 6.1 用户认证

服务端支持两种模式：

- 没有配置用户：所有客户端以 `anonymous` 身份访问。
- 配置了用户：客户端必须在 `hello` 请求中提交正确用户名和密码。

认证逻辑位于：

- `_authenticate()`
- `_session_user()`
- `_user_can_access()`

密码目前以明文保存在 `~/.remote_share_mount/config.json` 的 `user_passwords` 字段中，适合可信局域网原型使用。生产环境建议改成哈希存储，并为 TCP 通信增加 TLS。

### 6.2 用户信息存储方式

项目没有使用数据库保存用户信息，也没有连接任何外部数据库服务。用户信息由 GUI 写入本机用户目录下的 JSON 配置文件：

```text
~/.remote_share_mount/config.json
```

具体读写代码在 `remote_share/gui.py`：

- `_load_config()`：启动 GUI 时读取 `config.json`，并用 `json.loads()` 转成 Python 字典。
- `_save_config()`：配置变化时创建 `~/.remote_share_mount` 目录，并用 `json.dumps()` 把配置写回 JSON 文件。
- `ServerPage._current_users()`：从界面上的用户列表读取用户名和授权共享，再从 `config["user_passwords"]` 读取密码，组装成 `UserAccount`。
- `ServerPage._persist()`：把用户列表保存到 `config["users"]`，把密码保存到 `config["user_passwords"]`。

JSON 中与用户相关的字段大致如下：

```json
{
  "users": [
    {
      "username": "alice",
      "shares": ["Work", "Projects"]
    },
    {
      "username": "bob",
      "shares": ["*"]
    }
  ],
  "user_passwords": {
    "alice": "pass123",
    "bob": "pass456"
  }
}
```

其中：

- `users` 保存用户账号名和允许访问的共享目录列表。
- `user_passwords` 保存用户名到密码的映射。
- 密码是明文字符串，不是哈希值，也没有加盐。
- 服务端启动或配置更新时，会把 JSON 中的用户信息转换成 `UserAccount` 对象。
- 服务端运行期间，用户信息保存在内存字典 `RemoteShareServer.users` 中，用于认证和授权判断。

如果通过命令行 `serve --user alice=pass123:Work` 启动服务端，用户信息来自命令行参数，会被解析为 `UserAccount` 并保存在服务端内存中；这条命令行方式本身不会把用户写入 `config.json`。

### 6.3 共享权限

每个共享目录有两层权限：

1. `readonly` / `readwrite`
   - `readonly` 只允许列表、属性查询、读取。
   - `readwrite` 允许写入、创建文件、截断和修改时间。
2. `allow_create_delete`
   - 为 `True` 时允许删除、重命名、创建目录等管理操作。
   - 为 `False` 时即使是 `readwrite`，也主要用于修改已有文件内容。

服务端通过以下方法集中校验：

- `_require_write()`
- `_require_create_delete()`

### 6.4 运行中修改权限的生效机制

运行中修改用户权限或共享读写权限可以直接生效。实现上不是修改操作系统挂载状态，而是修改服务端内存中的权限配置：

1. 管理员在 GUI 的 `Server` 页编辑共享目录权限或用户可访问共享。
2. GUI 调用 `_persist_and_apply()`，先把配置保存到 `~/.remote_share_mount/config.json`。
3. 如果服务端正在运行，GUI 使用 `loop.call_soon_threadsafe()` 调用 `RemoteShareServer.update_config(shares, users)`。
4. 服务端把新的共享配置保存到 `self.shares`，把新的用户配置保存到 `self.users`。
5. 后续每个请求进入服务端后都会重新调用 `_get_share()`、`_user_can_access()`、`_require_write()` 或 `_require_create_delete()`。
6. 因为这些校验都读取最新的 `self.shares` 和 `self.users`，所以权限变化会影响下一次请求。

举例：

- 用户 `alice` 已经在 Windows 上把共享 `Projects` 映射为 `Z:`。
- 管理员在服务端 GUI 中把 `Projects` 从 `readwrite` 改为 `readonly`。
- `Z:` 不会自动消失，Windows WebDAV 桥进程也不会自动退出。
- 如果 `alice` 继续打开或读取文件，读请求仍然可以成功。
- 如果 `alice` 尝试保存文件、删除文件或重命名文件，服务端会在 `_require_write()` 或 `_require_create_delete()` 处拒绝，客户端会收到 `PERMISSION_DENIED`。

如果管理员把用户从某个共享的授权列表中移除，效果类似：已挂载项不会立即断开，但该用户下一次访问该共享时，`_get_share()` 会发现 `_user_can_access()` 不通过，然后返回权限错误。

### 6.5 挂载状态与权限状态的关系

挂载状态由客户端本机维护：

- Linux：由 FUSE 子进程和内核挂载点维护。
- Windows：由本地 WebDAV 桥进程、系统 `WebClient` 服务和 `net use` 盘符映射维护。

权限状态由服务端维护：

- 共享读写能力保存在 `RemoteShareServer.shares`。
- 用户授权范围保存在 `RemoteShareServer.users`。
- 每次文件操作请求都回到服务端做权限判断。

所以“权限已变更”和“挂载是否存在”是两件事。项目当前策略是保留挂载，但让服务端拒绝不再被允许的操作。

### 6.6 路径逃逸防护

客户端传入的是共享内相对路径。`pathutil.py` 做两步保护：

1. `normalize_remote_path()`：
   - 把反斜杠替换成 `/`。
   - 去掉空段和 `.`。
   - 拒绝 `..` 父目录引用。
2. `resolve_under()`：
   - 把共享根目录和相对路径解析成绝对路径。
   - 使用 `os.path.commonpath()` 确认目标路径仍在共享根目录下。

这样可以防止客户端通过 `../` 访问共享目录之外的文件。

## 7. 文件访问流程

### 7.1 刷新共享列表

1. 用户在客户端页输入服务端 IP、端口、用户名和密码。
2. 点击 `Refresh`。
3. `ClientPage.refresh()` 创建 `RemoteShareClient`。
4. 客户端自动发送 `hello` 认证。
5. 客户端发送 `list_shares` 请求。
6. 服务端按用户权限过滤共享列表。
7. GUI 使用 `Treeview` 展示共享名、权限和是否允许删除/重命名。

### 7.2 读取文件流程

以挂载后的读取为例：

1. 操作系统或应用程序读取本地挂载路径中的文件。
2. Linux 下进入 FUSE 回调 `RemoteFuseOperations.read()`；Windows 下进入 WebDAV `GET` 请求处理。
3. 挂载层把本地读取转换成 `RemoteShareClient.read_file()`。
4. 客户端发送 `read` 请求，包含：
   - `share`
   - `path`
   - `offset`
   - `size`
5. 服务端校验共享访问权限和路径安全。
6. 服务端用普通文件 IO：
   - `target.open("rb")`
   - `handle.seek(offset)`
   - `handle.read(size)`
7. 服务端返回二进制数据和文件 `mtime_ns`。
8. 挂载层把数据返回给操作系统。

### 7.3 写入文件流程

1. 操作系统或应用程序写入本地挂载路径。
2. Linux FUSE 的 `write()` 或 Windows WebDAV 的 `PUT` 把写入拆成不超过 64 KB 的块。
3. 客户端发送 `write` 请求，包含：
   - `share`
   - `path`
   - `offset`
   - 可选 `expected_mtime_ns`
   - 二进制数据块
4. 服务端校验：
   - 用户是否可访问该共享。
   - 共享是否为 `readwrite`。
   - 路径是否位于共享目录内。
   - 写入块是否超过 `MAX_CHUNK_SIZE`。
5. 服务端为目标文件创建或复用 `asyncio.Lock`，同一文件写入串行执行。
6. 服务端检查冲突：
   - 如果当前文件修改时间和客户端期望的 `expected_mtime_ns` 不一致，返回覆盖警告。
   - 如果另一个 session 正在写同一文件，返回 Last-Write-Wins 警告。
7. 服务端执行真实写入：
   - 文件存在时使用 `r+b`。
   - 文件不存在时创建父目录并使用 `wb`。
   - `seek(offset)` 后写入数据。
   - `flush()` 并 `os.fsync()`。
8. 服务端记录新的 `mtime_ns` 和写入 session。
9. 客户端收到响应，必要时通过 `on_warning` 输出冲突警告。

项目采用 Last-Write-Wins 策略，即并发写入时最后写入成功的一方覆盖旧内容，但服务端会尽量返回冲突警告。

## 8. Linux 挂载实现

### 8.1 使用的系统组件

Linux 挂载依赖：

- Linux 内核 FUSE 文件系统能力。
- 系统包 `fuse3`，提供 `/dev/fuse` 和 `fusermount3`。
- Python 包 `fusepy`，把 Python 类适配为 FUSE 文件系统回调。
- 卸载时优先调用 `fusermount3 -u`，如果没有则调用 `fusermount -u`。

Linux 安装包还声明依赖 `python3-tk`，用于 GUI。

### 8.2 GUI 挂载流程

1. 用户选择远程共享。
2. 输入 Linux 本地挂载点，例如 `~/remote_share`。
3. 点击 `Mount Selected`。
4. `ClientPage._mount_linux()` 创建挂载点目录。
5. GUI 通过 `_app_command()` 生成命令：

```bash
python -m remote_share.cli mount-fuse \
  --host <server-ip> \
  --port <port> \
  --share <share> \
  --mount <mountpoint> \
  --username <username> \
  --password <password>
```

6. `_popen()` 使用 `subprocess.Popen()` 启动独立子进程。
7. GUI 在已挂载列表中记录子进程 pid。

### 8.3 FUSE 回调映射

`mount_fuse()` 创建 `RemoteShareClient`，认证成功后创建 `RemoteFuseOperations`，然后调用：

```python
FUSE(operations, mountpoint, foreground=foreground, nothreads=False)
```

主要 FUSE 操作映射：

| FUSE 回调 | 远程请求 |
| --- | --- |
| `getattr` | `stat` |
| `readdir` | `list_dir` |
| `open` | `stat` 并记录文件句柄 |
| `create` | `create_file(truncate=True)` |
| `read` | `read_file` |
| `write` | `write_file`，按 64 KB 分块 |
| `truncate` | `truncate` |
| `mkdir` | `create_dir` |
| `mknod` | `create_file` |
| `unlink` / `rmdir` | `delete` |
| `rename` | `rename` |
| `utimens` | `utime` |

FUSE 层会把远程错误码转换为 Linux errno：

- `NOT_FOUND` -> `ENOENT`
- `PERMISSION_DENIED` -> `EACCES`
- `BAD_REQUEST` -> `EINVAL`
- 其他错误 -> `EIO`

## 9. Windows 挂载实现

### 9.1 使用的系统组件

Windows 不直接使用 FUSE，而是走 WebDAV 网络驱动器映射：

- 本项目启动一个本地 WebDAV 桥接 HTTP 服务。
- Windows 系统 `WebClient` 服务负责把 WebDAV 地址挂载成网络驱动器。
- `net use` 命令负责映射和删除盘符。
- `sc query WebClient` 查询服务状态。
- `sc start WebClient` 启动服务。
- 映射失败且遇到 `System error 67` 时，代码会尝试 `sc stop WebClient` 和 `sc start WebClient` 重启服务后重试一次。
- `ctypes.windll.kernel32.GetLogicalDrives()` 用来检查盘符是否已被占用。

### 9.2 GUI 挂载流程

1. 用户选择远程共享。
2. 输入盘符，例如 `Z:`。
3. 输入本地 WebDAV 端口，默认 `18080`。
4. 点击 `Mount Selected`。
5. `ClientPage._mount_windows()` 校验盘符格式。
6. 使用 `GetLogicalDrives()` 检查盘符是否已占用。
7. 调用 `_ensure_webclient_running()` 确保 `WebClient` 服务正在运行。
8. 调用 `_next_webdav_endpoint()` 从默认端口开始寻找可用本地端口。
9. 启动本地 WebDAV 桥接子进程：

```powershell
python -m remote_share.cli webdav `
  --remote-host <server-ip> `
  --remote-port <port> `
  --share <share> `
  --listen-host 127.0.0.1 `
  --listen-port <local-port> `
  --username <username> `
  --password <password>
```

10. GUI 通过 `OPTIONS` 请求等待 WebDAV 桥接服务就绪。
11. 生成多个 WebDAV 目标地址并依次尝试 `net use`：

```text
\\127.0.0.1@<port>\DavWWWRoot\<share>
\\127.0.0.1@<port>\<share>
http://127.0.0.1:<port>/<share>/
\\localhost@<port>\DavWWWRoot\<share>
\\localhost@<port>\<share>
http://localhost:<port>/<share>/
```

12. 映射成功后在 GUI 已挂载列表记录：
   - 共享名
   - 盘符
   - WebDAV URL
   - WebDAV 桥进程 pid

### 9.3 WebDAV 桥接服务原理

`webdav_bridge.py` 使用 `ThreadingHTTPServer` 实现本地 HTTP 服务。它只监听本机地址，默认是 `127.0.0.1`。

WebDAV 桥接服务自身不保存远程文件内容。每个 WebDAV 请求都会转换为 `RemoteShareClient` 请求，再转发到远程 TCP 服务端。

支持的主要 WebDAV/HTTP 方法：

| 方法 | 实现功能 | 转换到远程操作 |
| --- | --- | --- |
| `OPTIONS` | 返回 DAV 能力 | 本地响应 |
| `HEAD` | 文件属性 | `stat` |
| `GET` | 文件读取，支持 Range | `stat` + `read_file` |
| `PROPFIND` | 目录枚举和属性查询 | `stat` + `list_dir` |
| `PUT` | 上传或覆盖文件 | `create_file` + 分块 `write_file` |
| `MKCOL` | 创建目录 | `create_dir` |
| `DELETE` | 删除文件或目录 | `delete` |
| `MOVE` | 重命名或移动 | `rename` |
| `COPY` | 复制文件或目录 | `stat` + `read_file` + `write_file` |
| `LOCK` / `UNLOCK` | WebDAV 锁兼容 | 本地维护 lock token |
| `PROPPATCH` | 属性修改兼容响应 | 本地响应并查询 `stat` |

Windows 资源管理器会大量使用 `PROPFIND`、`LOCK`、`PUT`、`MOVE` 等 WebDAV 方法，桥接层负责把这些请求翻译成项目自定义 TCP 协议。

## 10. 卸载流程

### 10.1 Linux 卸载

1. 用户在 GUI 已挂载列表中选择挂载项。
2. 点击 `Unmount Selected`。
3. 代码选择卸载工具：
   - 优先 `fusermount3`
   - 否则 `fusermount`
4. 执行：

```bash
fusermount3 -u <mountpoint>
```

5. 终止对应的 FUSE 子进程。
6. 从 GUI 列表移除挂载项。

### 10.2 Windows 卸载

1. 用户选择已映射盘符。
2. 点击 `Unmount Selected`。
3. 执行：

```powershell
net use <drive> /delete /y
```

4. 终止对应的 WebDAV 桥接子进程。
5. 从 GUI 列表移除挂载项。

关闭 GUI 时，`UnifiedGUI._on_close()` 会停止服务端并调用 `client_page.stop_all()` 尝试卸载所有客户端挂载。

## 11. 命令行运行流程

除了 GUI，项目也提供命令行子命令：

### 11.1 启动服务端

```powershell
python -m remote_share.cli serve --host 0.0.0.0 --port 18888 --share Work=D:\Work:readonly
```

流程：

1. `argparse` 解析 `serve` 子命令。
2. `parse_share_spec()` 把 `NAME=PATH:permission[:create]` 转为 `Share`。
3. `parse_user_spec()` 把 `USER=PASSWORD:share1,share2` 转为 `UserAccount`。
4. `RemoteShareServer` 初始化并校验配置。
5. `asyncio.run(server.serve_forever())` 启动异步 TCP 服务。

### 11.2 列出共享

```powershell
python -m remote_share.cli list --host 192.168.1.100 --port 18888 --username alice --password pass123
```

流程：

1. 创建 `RemoteShareClient`。
2. 自动 `hello` 登录。
3. 发送 `list_shares`。
4. 打印共享名、权限、创建/删除能力和服务端路径。

### 11.3 Linux FUSE 挂载

```bash
python -m remote_share.cli mount-fuse --host 192.168.1.100 --port 18888 --share Projects --mount ~/remote_projects --username bob --password pass456
```

`cmd_mount_fuse()` 会先检查当前系统必须是 Linux，然后调用 `fuse_mount.mount_fuse()`。

### 11.4 Windows WebDAV 桥接

```powershell
python -m remote_share.cli webdav --remote-host 192.168.1.100 --remote-port 18888 --share Projects --listen-port 18080 --username bob --password pass456
net use Z: http://127.0.0.1:18080/Projects /persistent:no
```

第一条命令启动本地 WebDAV 桥，第二条命令调用 Windows 系统网络驱动器映射。

## 12. 打包与安装

### 12.1 Python 依赖

`pyproject.toml` 声明项目本体不强制依赖额外包。`requirements.txt` 中只在 Linux 平台声明：

```text
fusepy>=3.0.1; platform_system == "Linux"
```

Tkinter 来自 Python 标准库，但 Linux 系统中经常需要通过系统包 `python3-tk` 安装。

### 12.2 Windows 打包

`scripts/build_windows.ps1` 流程：

1. 升级 pip。
2. 安装 `build_requirements.txt`。
3. 安装当前项目。
4. 执行 PyInstaller：

```powershell
python -m PyInstaller --noconfirm --clean remote_share.spec
```

5. 复制构建产物到 `dist\windows-package`。
6. 生成 `Launch Remote Share Mount.bat` 启动脚本。

`remote_share.spec` 使用 `collect_submodules("remote_share")` 收集项目子模块，并把 `README.md` 放进打包数据中。

### 12.3 Linux 打包

`scripts/build_linux.sh` 流程：

1. 升级 pip。
2. 安装构建依赖。
3. 安装当前项目。
4. 使用 PyInstaller 构建。
5. 复制产物到 `dist/linux-package/remote-share-mount`。
6. 生成安装脚本 `install.sh`。
7. 安装脚本会把程序复制到 `/opt/remote-share-mount`，并创建 `/usr/local/bin/remote-share-mount`。
8. 安装系统依赖：

```bash
sudo apt-get install -y fuse3 python3-tk
```

9. 打包成 `dist/remote-share-mount-linux.tar.gz`。

`scripts/build_deb.sh` 在 Linux tar 包构建基础上创建 deb 安装包，控制文件中声明：

```text
Depends: fuse3, python3-tk
```

## 13. 关键设计点

### 13.1 为什么服务端使用 asyncio

服务端需要同时处理多个客户端连接。`asyncio.start_server()` 可以让每个 TCP 连接以协程方式并发运行，避免为每个连接手动管理线程。阻塞文件系统操作通过 `_to_thread()` 放到线程池中执行，减少阻塞 event loop 的风险。

### 13.2 为什么客户端使用同步 socket

FUSE 回调和 `http.server` 请求处理都是同步调用模型。客户端使用同步 `socket` 可以直接嵌入 FUSE 和 WebDAV 处理流程，代码更简单。客户端内部使用 `threading.RLock` 保护 socket 请求，避免多线程场景下同一个连接上的帧交错。

### 13.3 为什么 Windows 需要 WebDAV 桥

Windows 资源管理器和系统盘符映射原生支持 WebDAV 网络位置。项目没有实现 Windows 文件系统驱动，而是：

1. 本地启动 WebDAV HTTP 服务。
2. WebDAV 服务把文件操作转发到自定义 TCP 协议。
3. Windows `WebClient` 服务把 WebDAV 地址映射为盘符。

这样避免编写内核驱动或安装第三方 Windows FUSE 兼容层。

### 13.4 为什么 Linux 使用 FUSE

Linux FUSE 允许用户态程序实现文件系统。项目通过 `fusepy` 把 Python 类方法注册为文件系统操作，操作系统访问挂载点时会回调 Python 方法，再由 Python 客户端访问远程 TCP 服务端。

### 13.5 为什么不缓存远程文件内容到本地磁盘

代码中的 FUSE 和 WebDAV 桥都是按需读取、按块写入。项目自身不把远程文件内容落盘缓存到本地。需要注意的是，操作系统、编辑器或应用程序可能自行创建临时文件或缓存，这不属于项目直接控制范围。

## 14. 当前限制与注意事项

- TCP 通信未加密，建议仅在可信局域网内使用。
- 用户密码明文保存在配置文件中。
- Windows 映射依赖 `WebClient` 服务，服务未安装或无法启动时不能映射盘符。
- Windows WebClient 对 WebDAV 复制大文件有系统默认限制，例如 `FileSizeLimitInBytes`，这不是项目代码直接限制。
- 项目单次文件传输块大小为 64 KB，单帧最大 16 MB。
- 并发写入采用 Last-Write-Wins，并提供冲突警告，但没有实现强一致锁或版本合并。
- 服务端本地变化扫描每 2 秒执行一次，主要用于发现外部修改后的版本变化，不是实时文件通知。
