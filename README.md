# Remote GPU Tray for Windows

This project shows remote NVIDIA GPU memory usage in the Windows notification area.

## What it does

- Connects to one or more remote servers over SSH.
- Runs `nvidia-smi` on a schedule.
- Updates a tray icon with the highest GPU memory usage percentage.
- Shows per-server and per-GPU details in the tray menu.
- Supports a `--once` mode for config testing from a terminal.
- Provides a connection settings window so you do not need to edit JSON manually.
- Lets you pause/resume monitoring from the tray menu.
- Lets you enable or disable "Start with Windows" from the tray menu.

## Assumptions

- The remote machine has `nvidia-smi` available in `PATH`.
- "Status bar" here means the Windows notification area / system tray.
- The default query command is:

```bash
nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits
```

## Files

- `gpu_tray.py`: main app
- `config.example.json`: example config
- `requirements.txt`: Python dependencies
- `requirements-build.txt`: build-time dependencies
- `gpu_tray.spec`: PyInstaller config
- `build_windows_exe.bat`: one-click Windows build script

## Setup

1. Install Python 3.10+ on Windows.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Copy `config.example.json` to `config.json`.
4. Edit `config.json` with your server details.

You can also launch the tray app first and use the settings window instead of editing
`config.json` by hand.
If `config.json` does not exist, the tray app still starts and opens the settings window automatically.

Do not commit `config.json` to Git. The repository should keep only `config.example.json`.

## WSL vs Windows

- You can keep this code in WSL while editing.
- But the tray app itself must run as a Windows process.
- Building a Windows `.exe` must also be done from Windows, not from Linux inside WSL.

If the project is still stored in WSL, open it from Windows using a path like:

```powershell
cd "\\wsl$\Ubuntu\home\yourname\PythonProject\gpu_use"
```

## Config fields

Top-level fields:

- `refresh_interval_seconds`: how often to poll the server
- `ssh_timeout_seconds`: default SSH timeout
- `servers`: list of remote servers

Per-server fields:

- `name`: label shown in the tray
- `host`: SSH host or IP
- `port`: SSH port, default `22`
- `username`: SSH username
- `password`: optional plaintext password
- `password_env`: optional environment variable containing the password
- `key_filename`: optional SSH private key path
- `passphrase_env`: optional environment variable for the SSH key passphrase
- `timeout_seconds`: optional per-server timeout override
- `verify_host_key`: set `true` to enforce known-host verification
- `gpu_query_command`: optional custom command

You usually want either:

- `key_filename`
- `password_env`
- SSH agent / default keys

## Usage

If your virtual environment has an old `pip`, upgrade it first:

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
```

Test the config once:

```bash
python gpu_tray.py --config config.json --once
```

`--once` still requires a valid `config.json`.

Run the tray app without a console window:

```bash
pythonw.exe gpu_tray.py --config config.json
```

Or with the Python launcher:

```bash
pyw -3 gpu_tray.py --config config.json
```

Tray menu controls:

- `Connection settings...`: open the editable SSH settings window
- `Monitoring enabled`: pause or resume polling
- `Start with Windows`: toggle Windows logon autostart
- `Refresh now`: force an immediate refresh
- `Quit`: exit the tray app

The settings window can update:

- host / IP
- port
- username
- SSH key path
- password
- refresh interval
- SSH timeout
- host key verification

After you click `Save`, the app writes the values back into `config.json` and refreshes immediately.
The settings window opens centered the first time, then remembers the last position you moved it to.

## Packaging

Build a single-file `.exe` from a Windows terminal:

```powershell
.\build_windows_exe.bat
```

Or run the equivalent commands manually:

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean gpu_tray.spec
copy config.example.json dist\config.json
```

Output file:

- `dist\gpu_tray.exe`

Before running the executable:

- Edit `dist\config.json`
- Set `key_filename` to a Windows path such as `C:/Users/you/.ssh/id_ed25519`
- Launch `dist\gpu_tray.exe` once, then use the tray menu to enable `Start with Windows` if needed

## Notes

- The tray icon shows the highest memory usage percentage across all successful servers.
- If a server refresh fails, the icon shows an error badge and the menu shows the error text.
- If you want true taskbar text instead of a tray icon, that is a different Windows integration path.

---

# README 中文版

## 项目简介

这个项目用于在 Windows 通知区域（系统托盘）显示远程 NVIDIA 服务器的显存占用情况。

## 功能

- 通过 SSH 连接一台或多台远程服务器
- 定时执行 `nvidia-smi`
- 托盘图标显示当前最高显存占用百分比
- 托盘菜单显示每台服务器、每张 GPU 的详细信息
- 支持 `--once` 模式做一次性配置测试
- 提供可视化连接设置窗口，不需要手动编辑 JSON
- 可以在托盘菜单中暂停/恢复监控
- 可以在托盘菜单中开启/关闭开机自启动

## 前提

- 远程机器上已经安装并可执行 `nvidia-smi`
- 这里的“状态栏”指的是 Windows 通知区域 / 系统托盘
- 默认查询命令是：

```bash
nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits
```

## 文件说明

- `gpu_tray.py`：主程序
- `config.example.json`：配置示例
- `requirements.txt`：运行依赖
- `requirements-build.txt`：打包依赖
- `gpu_tray.spec`：PyInstaller 配置
- `build_windows_exe.bat`：Windows 一键打包脚本

## 安装与准备

1. 在 Windows 上安装 Python 3.10+
2. 安装依赖：

```bash
pip install -r requirements.txt
```

3. 将 `config.example.json` 复制为 `config.json`
4. 按你的服务器信息修改 `config.json`

你也可以先直接启动托盘程序，通过设置窗口填写信息，而不手动编辑 `config.json`。

如果 `config.json` 不存在，托盘程序仍然可以启动，并会自动弹出设置窗口。

不要把 `config.json` 提交到 Git 仓库。仓库中只应保留 `config.example.json`。

## WSL 与 Windows

- 你可以继续在 WSL 中编写和修改代码
- 但托盘程序本身必须作为 Windows 进程运行
- Windows 的 `.exe` 也必须在 Windows 环境中构建，不能在 WSL 的 Linux 环境里直接生成

如果项目仍然放在 WSL 中，可以在 Windows 里这样进入目录：

```powershell
cd "\\wsl$\Ubuntu\home\yourname\PythonProject\gpu_use"
```

## 配置字段

顶层字段：

- `refresh_interval_seconds`：轮询间隔秒数
- `ssh_timeout_seconds`：默认 SSH 超时时间
- `servers`：远程服务器列表

每台服务器的字段：

- `name`：托盘中显示的名称
- `host`：SSH 主机名或 IP
- `port`：SSH 端口，默认 `22`
- `username`：SSH 用户名
- `password`：可选，明文密码
- `password_env`：可选，从环境变量读取密码
- `key_filename`：可选，SSH 私钥路径
- `passphrase_env`：可选，私钥口令环境变量
- `timeout_seconds`：可选，单台服务器超时覆盖值
- `verify_host_key`：是否校验主机密钥
- `gpu_query_command`：可选，自定义 GPU 查询命令

通常你只需要以下几种方式之一：

- `key_filename`
- `password_env`
- SSH agent / 默认密钥

## 使用方式

如果你的虚拟环境里的 `pip` 比较旧，先升级：

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
```

一次性测试配置：

```bash
python gpu_tray.py --config config.json --once
```

注意：`--once` 模式仍然要求存在有效的 `config.json`。

无控制台运行托盘程序：

```bash
pythonw.exe gpu_tray.py --config config.json
```

或者使用 Python Launcher：

```bash
pyw -3 gpu_tray.py --config config.json
```

## 托盘菜单说明

- `Connection settings...`：打开连接设置窗口
- `Monitoring enabled`：暂停或恢复轮询
- `Start with Windows`：开启或关闭登录后自启动
- `Refresh now`：立即刷新一次
- `Quit`：退出程序

## 设置窗口支持修改

- 主机名 / IP
- 端口
- 用户名
- SSH 私钥路径
- 密码
- 刷新间隔
- SSH 超时时间
- 是否校验主机密钥

点击 `Save` 后，程序会把内容写回 `config.json`，然后立即刷新监控状态。

设置窗口第一次打开时会默认居中；如果你手动拖动过窗口，下次会记住并恢复到上次的位置。

## 打包为 EXE

在 Windows 终端中执行：

```powershell
.\build_windows_exe.bat
```

或者手动执行：

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean gpu_tray.spec
copy config.example.json dist\config.json
```

输出文件：

- `dist\gpu_tray.exe`

运行打包版之前：

- 修改 `dist\config.json`
- `key_filename` 需要写成 Windows 路径，例如 `C:/Users/you/.ssh/id_ed25519`
- 首次运行 `dist\gpu_tray.exe` 后，可以在托盘菜单中开启 `Start with Windows`

## 说明

- 托盘图标显示的是所有成功连接的服务器中“最高显存占用百分比”
- 如果某台服务器刷新失败，图标会出现错误标记，菜单里也会显示错误信息
- 如果你想做成真正的“任务栏文字”而不是托盘图标，那是另一条 Windows 集成路径
