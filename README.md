# Remote Share Mount

跨平台远程目录共享与挂载系统。一个程序同时包含服务端和客户端页面：服务端共享本机目录，客户端选择远端共享后可在 GUI 中一键挂载/卸载。

## 已实现功能

- 单一 GUI 程序，标签页切换 `Server` / `Client`
- 多共享目录，每个共享目录支持 `readonly` / `readwrite`
- 可选多用户隔离：服务端添加用户后，客户端必须输入用户名和密码
- 每个用户可授权访问指定共享，或用 `*` 授权全部共享
- 多 IP / 多客户端可同时访问同一共享和同一文件
- Last-Write-Wins 并发写入策略，并向覆盖方返回冲突警告
- TCP 自定义协议：`1 byte type + 4 bytes length + frame body`
- 文件按块传输，单块最大 64KB，支持 offset/size 读取和 offset 写入
- Linux：FUSE 挂载
- Windows 11：GUI 自动启动本地 WebDAV 桥接并执行 `net use` 映射盘符
- 客户端不把远程文件内容缓存到本地磁盘

## 运行

开发环境：

```powershell
python -m pip install -r requirements.txt
python -m remote_share.cli
```

也可以显式启动统一 GUI：(推荐优先使用)

```powershell
python -m remote_share.cli gui
```

打开后：

1. 在 `Server` 页添加共享目录和权限。
2. 可选：添加用户，设置密码和允许访问的共享名，例如 `Work,Projects` 或 `*`。
3. 点击 `Start Service`。
4. 在 `Client` 页输入服务端 IP、端口、用户名、密码。
5. 点击 `Refresh`，选择共享。
6. Windows 输入盘符如 `Z:`，Linux 输入挂载点，点击 `Mount Selected`。
7. 选中已挂载列表中的条目，点击 `Unmount Selected` 卸载。

## 命令行

匿名服务端：

```powershell
python -m remote_share.cli serve --host 0.0.0.0 --port 18888 --share Work=D:\Work:readonly
```

带用户隔离：

```powershell
python -m remote_share.cli serve --host 0.0.0.0 --port 18888 --share Work=D:\Work:readonly --share Projects=D:\Projects:readwrite --user alice=pass123:Work --user bob=pass456:Work,Projects
```

客户端列共享：

```powershell
python -m remote_share.cli list --host 192.168.1.100 --port 18888 --username alice --password pass123
```

Linux FUSE 挂载：

```bash
mkdir -p ~/remote_projects
python -m remote_share.cli mount-fuse --host 192.168.1.100 --port 18888 --share Projects --mount ~/remote_projects --username bob --password pass456
```

Windows WebDAV 桥接：

```powershell
python -m remote_share.cli webdav --remote-host 192.168.1.100 --remote-port 18888 --share Projects --listen-port 18080 --username bob --password pass456
net use Z: http://127.0.0.1:18080/Projects /persistent:no
```

## Windows 11 安装包

在 Windows 11 上构建：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
```

生成目录：

```text
dist\windows-package
```

其中包含 `remote-share-mount.exe` 和启动脚本。安装依赖会由构建脚本自动安装到构建用 Python 环境中；最终程序由 PyInstaller 封装，目标机器不需要额外安装 Python 包。

Windows 盘符挂载依赖系统 `WebClient` 服务。若映射失败，请在服务管理器中启用 `WebClient`。

## Linux 安装包

在 Linux 上构建 tar 包：

```bash
bash scripts/build_linux.sh
```

生成：

```text
dist/remote-share-mount-linux.tar.gz
```

安装：

```bash
tar -xzf dist/remote-share-mount-linux.tar.gz -C /tmp/remote-share-mount
cd /tmp/remote-share-mount
./install.sh
remote-share-mount
```

构建 `.deb`：

```bash
bash scripts/build_deb.sh
sudo apt install ./dist/remote-share-mount_0.2.0_amd64.deb
remote-share-mount
```

Linux 运行依赖 `fuse3` 和 `python3-tk`，安装脚本或 `.deb` 会声明/安装这些系统依赖。

## 注意

- 配置文件保存在用户目录：`~/.remote_share_mount/config.json`。
- 当前版本密码以明文保存，适合可信局域网原型使用；生产环境建议改成哈希存储和 TLS。
- 客户端不落盘缓存文件内容，但操作系统、编辑器或应用程序自身可能生成本地临时文件。
- Windows 端采用 WebDAV 桥接映射盘符；Linux 端采用 FUSE。

 挂在后的文件可以复制到本地，但大小会受到Windows WebClient(WebDAV Redirector) 的默认限制，FileSizeLimitInBytes = 50,000,000 字节（约 47.7 MiB），不是本项目代码直接限制。微软文档里有这个默认值：

https://learn.microsoft.com/en-us/iis/publish/using-webdav/using-the-webdav-redirector

项目有两个“传输层限制”：
单次数据块上限：64 KB（MAX_CHUNK_SIZE = 64 * 1024）
单帧上限：16 MB（MAX_FRAME_SIZE = 16 * 1024 * 1024）
如果要改“项目内限制”，改 MAX_CHUNK_SIZE/MAX_FRAME_SIZE；如果要改你现在复制时的上限，更可能要改 Windows WebClient 的 FileSizeLimitInBytes（注册表）并重启服务/系统。
