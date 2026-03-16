from __future__ import annotations

import argparse
import concurrent.futures
import csv
import io
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

DEFAULT_QUERY_COMMAND = (
    "nvidia-smi "
    "--query-gpu=index,name,memory.used,memory.total,utilization.gpu "
    "--format=csv,noheader,nounits"
)

paramiko = None
pystray = None
PILImage = None
PILImageDraw = None
PILImageFont = None
MIN_PYTHON = (3, 10)
AUTOSTART_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_VALUE_NAME = "GPUTray"


def ensure_ssh_dependencies() -> None:
    global paramiko
    if paramiko is not None:
        return

    try:
        import paramiko as imported_paramiko
    except ImportError as exc:
        raise RuntimeError(
            "Missing SSH dependency. Run: pip install -r requirements.txt"
        ) from exc

    paramiko = imported_paramiko


def ensure_ui_dependencies() -> None:
    global pystray, PILImage, PILImageDraw, PILImageFont
    if (
        pystray is not None
        and PILImage is not None
        and PILImageDraw is not None
        and PILImageFont is not None
    ):
        return

    try:
        import pystray as imported_pystray
        from PIL import Image as imported_image
        from PIL import ImageDraw as imported_image_draw
        from PIL import ImageFont as imported_image_font
    except ImportError as exc:
        raise RuntimeError(
            "Missing tray UI dependencies. Run: pip install -r requirements.txt"
        ) from exc

    pystray = imported_pystray
    PILImage = imported_image
    PILImageDraw = imported_image_draw
    PILImageFont = imported_image_font


@dataclass(frozen=True)
class GPUStat:
    index: int
    name: str
    memory_used_mib: int
    memory_total_mib: int
    utilization_percent: int | None

    @property
    def memory_percent(self) -> float:
        if self.memory_total_mib <= 0:
            return 0.0
        return (self.memory_used_mib / self.memory_total_mib) * 100.0

    @property
    def memory_used_gib(self) -> float:
        return self.memory_used_mib / 1024.0

    @property
    def memory_total_gib(self) -> float:
        return self.memory_total_mib / 1024.0


@dataclass(frozen=True)
class ServerConfig:
    name: str
    host: str
    username: str
    port: int = 22
    password: str | None = None
    password_env: str | None = None
    key_filename: str | None = None
    passphrase_env: str | None = None
    timeout_seconds: int = 8
    verify_host_key: bool = False
    gpu_query_command: str = DEFAULT_QUERY_COMMAND


@dataclass
class ServerStatus:
    server: ServerConfig
    gpus: list[GPUStat] = field(default_factory=list)
    error: str | None = None
    fetched_at: float | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def max_memory_percent(self) -> float:
        if not self.gpus:
            return 0.0
        return max(gpu.memory_percent for gpu in self.gpus)

    @property
    def total_used_mib(self) -> int:
        return sum(gpu.memory_used_mib for gpu in self.gpus)

    @property
    def total_capacity_mib(self) -> int:
        return sum(gpu.memory_total_mib for gpu in self.gpus)


@dataclass(frozen=True)
class AppConfig:
    refresh_interval_seconds: int
    ssh_timeout_seconds: int
    servers: list[ServerConfig]
    config_path: Path
    settings_window_position: tuple[int, int] | None = None


def create_placeholder_config(path: Path) -> AppConfig:
    return AppConfig(
        refresh_interval_seconds=15,
        ssh_timeout_seconds=8,
        servers=[
            ServerConfig(
                name="gpu-server",
                host="",
                username="",
                port=22,
                timeout_seconds=8,
                verify_host_key=False,
            )
        ],
        config_path=path.resolve(),
        settings_window_position=None,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show remote GPU memory usage in the Windows notification area."
    )
    parser.add_argument(
        "--config",
        default=str(default_config_path()),
        help="Path to the JSON config file. Default: config.json next to the script.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Fetch once and print the result to stdout without starting the tray app.",
    )
    return parser.parse_args()


def default_config_path() -> Path:
    return Path(sys.argv[0]).resolve().parent / "config.json"


def ensure_supported_python() -> None:
    if sys.version_info < MIN_PYTHON:
        min_version = ".".join(str(part) for part in MIN_PYTHON)
        current_version = sys.version.split()[0]
        raise RuntimeError(
            f"Python {min_version}+ is required. Current version: {current_version}. "
            "Please recreate the virtual environment with Python 3.10 or newer."
        )


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}\n"
            f"Copy config.example.json to config.json and edit it first."
        )

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in config file: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError("Config root must be a JSON object.")

    refresh_interval_seconds = int(raw.get("refresh_interval_seconds", 15))
    ssh_timeout_seconds = int(raw.get("ssh_timeout_seconds", 8))
    settings_window_position = parse_settings_window_position(raw.get("ui"))

    if refresh_interval_seconds <= 0:
        raise ValueError("refresh_interval_seconds must be > 0.")
    if ssh_timeout_seconds <= 0:
        raise ValueError("ssh_timeout_seconds must be > 0.")

    servers_raw = raw.get("servers")
    if not isinstance(servers_raw, list) or not servers_raw:
        raise ValueError("Config must contain a non-empty 'servers' array.")

    servers: list[ServerConfig] = []
    for index, item in enumerate(servers_raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"servers[{index - 1}] must be a JSON object.")

        name = require_string(item, "name", index)
        host = require_string(item, "host", index)
        username = require_string(item, "username", index)

        port = int(item.get("port", 22))
        timeout_seconds = int(item.get("timeout_seconds", ssh_timeout_seconds))
        verify_host_key = bool(item.get("verify_host_key", False))
        gpu_query_command = str(item.get("gpu_query_command", DEFAULT_QUERY_COMMAND))

        servers.append(
            ServerConfig(
                name=name,
                host=host,
                username=username,
                port=port,
                password=optional_string(item, "password"),
                password_env=optional_string(item, "password_env"),
                key_filename=optional_string(item, "key_filename"),
                passphrase_env=optional_string(item, "passphrase_env"),
                timeout_seconds=timeout_seconds,
                verify_host_key=verify_host_key,
                gpu_query_command=gpu_query_command,
            )
        )

    return AppConfig(
        refresh_interval_seconds=refresh_interval_seconds,
        ssh_timeout_seconds=ssh_timeout_seconds,
        servers=servers,
        config_path=path.resolve(),
        settings_window_position=settings_window_position,
    )


def require_string(data: dict[str, Any], key: str, index: int) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"servers[{index - 1}].{key} must be a non-empty string.")
    return value.strip()


def optional_string(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string when provided.")
    value = value.strip()
    return value or None


def parse_settings_window_position(ui_raw: Any) -> tuple[int, int] | None:
    if ui_raw is None:
        return None
    if not isinstance(ui_raw, dict):
        raise ValueError("ui must be a JSON object when provided.")

    position_raw = ui_raw.get("settings_window_position")
    if position_raw is None:
        return None
    if not isinstance(position_raw, dict):
        raise ValueError("ui.settings_window_position must be a JSON object.")

    try:
        x = int(position_raw["x"])
        y = int(position_raw["y"])
    except KeyError as exc:
        raise ValueError("ui.settings_window_position must contain x and y.") from exc
    except (TypeError, ValueError) as exc:
        raise ValueError("ui.settings_window_position.x and y must be integers.") from exc

    return (x, y)


def resolve_secret(direct_value: str | None, env_name: str | None) -> str | None:
    if env_name:
        env_value = os.getenv(env_name)
        if env_value is None:
            raise RuntimeError(f"Environment variable is not set: {env_name}")
        return env_value
    return direct_value


def parse_gpu_stats(output_text: str) -> list[GPUStat]:
    reader = csv.reader(io.StringIO(output_text))
    gpus: list[GPUStat] = []

    for row in reader:
        if not row or not any(cell.strip() for cell in row):
            continue
        if len(row) < 5:
            raise ValueError(f"Unexpected nvidia-smi row: {row}")

        gpus.append(
            GPUStat(
                index=parse_required_int(row[0]),
                name=row[1].strip(),
                memory_used_mib=parse_required_int(row[2]),
                memory_total_mib=parse_required_int(row[3]),
                utilization_percent=parse_optional_int(row[4]),
            )
        )

    if not gpus:
        raise ValueError("The command returned no GPU rows.")

    return gpus


def parse_required_int(value: str) -> int:
    return int(float(value.strip()))


def parse_optional_int(value: str) -> int | None:
    normalized = value.strip().upper()
    if normalized in {"N/A", "[N/A]"}:
        return None
    return int(float(normalized))


def fetch_server_status(server: ServerConfig) -> ServerStatus:
    ensure_ssh_dependencies()

    client = paramiko.SSHClient()
    try:
        if server.verify_host_key:
            client.load_system_host_keys()
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        password = resolve_secret(server.password, server.password_env)
        passphrase = resolve_secret(None, server.passphrase_env)

        connect_kwargs: dict[str, Any] = {
            "hostname": server.host,
            "port": server.port,
            "username": server.username,
            "timeout": server.timeout_seconds,
            "auth_timeout": server.timeout_seconds,
            "banner_timeout": server.timeout_seconds,
            "look_for_keys": not password and not server.key_filename,
            "allow_agent": not password,
        }

        if server.key_filename:
            connect_kwargs["key_filename"] = str(Path(server.key_filename).expanduser())
        if password:
            connect_kwargs["password"] = password
        if passphrase:
            connect_kwargs["passphrase"] = passphrase

        client.connect(**connect_kwargs)
        _, stdout, stderr = client.exec_command(
            server.gpu_query_command,
            timeout=server.timeout_seconds,
        )

        stdout_text = stdout.read().decode("utf-8", errors="replace").strip()
        stderr_text = stderr.read().decode("utf-8", errors="replace").strip()
        exit_code = stdout.channel.recv_exit_status()

        if exit_code != 0:
            raise RuntimeError(stderr_text or stdout_text or f"Command failed: {exit_code}")

        return ServerStatus(
            server=server,
            gpus=parse_gpu_stats(stdout_text),
            fetched_at=time.time(),
        )
    except Exception as exc:
        return ServerStatus(
            server=server,
            error=str(exc),
            fetched_at=time.time(),
        )
    finally:
        client.close()


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


class GPUTrayApp:
    def __init__(
        self,
        config: AppConfig,
        *,
        require_initial_setup: bool = False,
    ) -> None:
        self.config = config
        self.stop_event = threading.Event()
        self.refresh_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.statuses: list[ServerStatus] = []
        self.last_updated: float | None = None
        self.icon = None
        self.first_refresh_notification_sent = False
        self.needs_initial_setup = require_initial_setup
        self.monitoring_enabled = not require_initial_setup
        self.settings_window_open = False

    def run(self) -> None:
        ensure_ui_dependencies()
        self.icon = pystray.Icon(
            "gpu-memory-tray",
            self.render_icon([]),
            self.build_tooltip([]),
            self.build_menu([], None),
        )
        self.icon.run(self.on_icon_ready)

    def on_icon_ready(self, icon: Any) -> None:
        try:
            icon.visible = True
        except Exception:
            pass

        if self.needs_initial_setup:
            self.show_notification(
                "No config.json found. Opening connection settings."
            )
            self.on_open_settings_clicked(icon, None)
        else:
            self.show_notification(
                "GPU Tray started. If you do not see the icon, check the hidden icons arrow (^)."
            )
        worker = threading.Thread(target=self.poll_loop, name="gpu-poll-loop", daemon=True)
        worker.start()

    def poll_loop(self) -> None:
        while not self.stop_event.is_set():
            if self.monitoring_enabled:
                self.refresh_statuses()
            if self.stop_event.wait(self.config.refresh_interval_seconds):
                break

    def refresh_statuses(self) -> None:
        if not self.monitoring_enabled:
            self.update_tray_view()
            return

        if not self.refresh_lock.acquire(blocking=False):
            return

        try:
            statuses = self.collect_statuses()
            updated_at = time.time()

            with self.state_lock:
                self.statuses = statuses
                self.last_updated = updated_at

            self.update_tray_view()
            if not self.first_refresh_notification_sent:
                self.show_notification(self.build_tooltip(statuses))
                self.first_refresh_notification_sent = True
        finally:
            self.refresh_lock.release()

    def collect_statuses(self) -> list[ServerStatus]:
        max_workers = min(len(self.config.servers), 8)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            return list(executor.map(fetch_server_status, self.config.servers))

    def build_tooltip(self, statuses: list[ServerStatus]) -> str:
        if self.needs_initial_setup:
            return "Connection settings required"

        if not self.monitoring_enabled:
            return "GPU monitoring paused"

        if not statuses:
            return "GPU: waiting for first refresh..."

        parts: list[str] = []
        for status in statuses:
            if status.ok:
                parts.append(f"{status.server.name} {status.max_memory_percent:.0f}%")
            else:
                parts.append(f"{status.server.name} ERR")

        return truncate(" | ".join(parts), 120)

    def build_menu(self, statuses: list[ServerStatus], updated_at: float | None) -> Any:
        ensure_ui_dependencies()
        items: list[Any] = []

        items.append(
            pystray.MenuItem(
                "Connection settings...",
                self.on_open_settings_clicked,
                default=True,
            )
        )
        items.append(
            pystray.MenuItem(
                "Monitoring enabled",
                self.on_toggle_monitoring_clicked,
                checked=lambda item: self.monitoring_enabled,
                enabled=lambda item: not self.needs_initial_setup,
            )
        )
        if os.name == "nt":
            items.append(
                pystray.MenuItem(
                    "Start with Windows",
                    self.on_toggle_autostart_clicked,
                    checked=lambda item: self.is_autostart_enabled(),
                )
            )

        if updated_at is None:
            if self.needs_initial_setup:
                items.append(pystray.MenuItem("Config file missing. Open settings.", None, enabled=False))
            else:
                items.append(pystray.MenuItem("Waiting for first refresh...", None, enabled=False))
        else:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(updated_at))
            items.append(pystray.MenuItem(f"Last update: {timestamp}", None, enabled=False))

        if not statuses:
            items.append(pystray.MenuItem("No server data yet.", None, enabled=False))
        else:
            for status in statuses:
                if status.ok:
                    summary = (
                        f"{status.server.name}: "
                        f"max {status.max_memory_percent:.0f}% | "
                        f"{status.total_used_mib / 1024.0:.1f}/"
                        f"{status.total_capacity_mib / 1024.0:.1f} GiB"
                    )
                    items.append(pystray.MenuItem(summary, None, enabled=False))
                    for gpu in status.gpus:
                        util_text = (
                            f"{gpu.utilization_percent}%"
                            if gpu.utilization_percent is not None
                            else "n/a"
                        )
                        gpu_line = (
                            f"  GPU{gpu.index} {gpu.name}: "
                            f"{gpu.memory_used_gib:.1f}/{gpu.memory_total_gib:.1f} GiB "
                            f"({gpu.memory_percent:.0f}%), util {util_text}"
                        )
                        items.append(pystray.MenuItem(truncate(gpu_line, 120), None, enabled=False))
                else:
                    error_line = f"{status.server.name}: ERROR - {truncate(status.error or 'unknown error', 90)}"
                    items.append(pystray.MenuItem(error_line, None, enabled=False))

        items.append(
            pystray.MenuItem(
                "Refresh now",
                self.on_refresh_clicked,
                enabled=lambda item: not self.needs_initial_setup,
            )
        )
        items.append(pystray.MenuItem("Quit", self.on_quit_clicked))
        return pystray.Menu(*items)

    def render_icon(self, statuses: list[ServerStatus]) -> Any:
        ensure_ui_dependencies()

        image = PILImage.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = PILImageDraw.Draw(image)

        successful = [status for status in statuses if status.ok]
        failed_count = sum(1 for status in statuses if not status.ok)

        if not self.monitoring_enabled:
            text = "||"
            text_color = (95, 95, 95, 255)
        elif successful:
            percent = int(round(max(status.max_memory_percent for status in successful)))
            text = str(max(0, min(100, percent)))
            text_color = usage_color(percent)
        elif statuses:
            text = "!!"
            text_color = (190, 45, 45, 255)
        else:
            text = "00"
            text_color = (120, 120, 120, 255)

        self.draw_right_aligned_text(draw, text, text_color)

        if failed_count > 0:
            draw.ellipse((4, 5, 20, 21), fill=(190, 45, 45, 255))
            draw.rectangle((11, 9, 13, 15), fill=(255, 255, 255, 255))
            draw.rectangle((11, 17, 13, 19), fill=(255, 255, 255, 255))

        return image

    def on_refresh_clicked(self, icon: Any, item: Any) -> None:
        threading.Thread(target=self.refresh_statuses, name="gpu-refresh-click", daemon=True).start()

    def on_quit_clicked(self, icon: Any, item: Any) -> None:
        self.stop_event.set()
        icon.stop()

    def on_open_settings_clicked(self, icon: Any, item: Any) -> None:
        if self.settings_window_open:
            self.show_notification("Connection settings are already open.")
            return

        self.settings_window_open = True
        threading.Thread(target=self.open_settings_window, name="gpu-settings-window", daemon=True).start()

    def show_notification(self, message: str, title: str = "GPU Tray") -> None:
        if self.icon is None:
            return
        try:
            self.icon.notify(message, title)
        except Exception:
            pass

    def draw_right_aligned_text(
        self,
        draw: Any,
        text: str,
        fill: tuple[int, int, int, int],
    ) -> None:
        font = load_icon_font(text)
        bbox = draw.textbbox((0, 0), text, font=font, stroke_width=2)
        text_height = bbox[3] - bbox[1]
        x = 63 - bbox[2]
        y = (64 - text_height) / 2 - bbox[1] - 2

        # A subtle shadow keeps digits readable on both light and dark taskbars.
        draw.text(
            (x + 1, y + 1),
            text,
            font=font,
            fill=(0, 0, 0, 110),
            stroke_width=1,
            stroke_fill=(0, 0, 0, 110),
        )
        draw.text(
            (x, y),
            text,
            font=font,
            fill=fill,
            stroke_width=2,
            stroke_fill=(20, 20, 20, 255),
        )

    def update_tray_view(self) -> None:
        if self.icon is None:
            return

        with self.state_lock:
            statuses = list(self.statuses)
            updated_at = self.last_updated

        self.icon.icon = self.render_icon(statuses)
        self.icon.title = self.build_tooltip(statuses)
        self.icon.menu = self.build_menu(statuses, updated_at)
        self.icon.update_menu()

    def on_toggle_monitoring_clicked(self, icon: Any, item: Any) -> None:
        self.monitoring_enabled = not self.monitoring_enabled
        self.update_tray_view()
        if self.monitoring_enabled:
            self.show_notification("GPU monitoring resumed.")
            threading.Thread(target=self.refresh_statuses, name="gpu-refresh-resume", daemon=True).start()
        else:
            self.show_notification("GPU monitoring paused.")

    def on_toggle_autostart_clicked(self, icon: Any, item: Any) -> None:
        enabled = not self.is_autostart_enabled()
        self.set_autostart_enabled(enabled)
        self.update_tray_view()
        if enabled:
            self.show_notification("Start with Windows enabled.")
        else:
            self.show_notification("Start with Windows disabled.")

    def open_settings_window(self) -> None:
        try:
            import tkinter as tk
            from tkinter import filedialog
            from tkinter import messagebox
            from tkinter import ttk
        except ImportError:
            self.settings_window_open = False
            self.show_notification("Tkinter is not available in this Python installation.")
            return

        result: dict[str, Any] = {"saved": False}

        try:
            current_config = self.config
            server = current_config.servers[0]

            root = tk.Tk()
            root.title("GPU Tray Connection Settings")
            root.resizable(False, False)
            root.attributes("-topmost", True)

            padding = {"padx": 10, "pady": 6}
            frame = ttk.Frame(root, padding=12)
            frame.grid(row=0, column=0, sticky="nsew")

            server_name_var = tk.StringVar(value=server.name)
            host_var = tk.StringVar(value=server.host)
            port_var = tk.StringVar(value=str(server.port))
            username_var = tk.StringVar(value=server.username)
            key_filename_var = tk.StringVar(value=server.key_filename or "")
            password_var = tk.StringVar(value=server.password or "")
            refresh_interval_var = tk.StringVar(value=str(current_config.refresh_interval_seconds))
            timeout_var = tk.StringVar(value=str(server.timeout_seconds))
            verify_host_key_var = tk.BooleanVar(value=server.verify_host_key)

            ttk.Label(frame, text="Server name").grid(row=0, column=0, sticky="w", **padding)
            ttk.Entry(frame, textvariable=server_name_var, width=36).grid(row=0, column=1, sticky="ew", **padding)

            ttk.Label(frame, text="Host / IP").grid(row=1, column=0, sticky="w", **padding)
            ttk.Entry(frame, textvariable=host_var, width=36).grid(row=1, column=1, sticky="ew", **padding)

            ttk.Label(frame, text="Port").grid(row=2, column=0, sticky="w", **padding)
            ttk.Entry(frame, textvariable=port_var, width=12).grid(row=2, column=1, sticky="w", **padding)

            ttk.Label(frame, text="Username").grid(row=3, column=0, sticky="w", **padding)
            ttk.Entry(frame, textvariable=username_var, width=24).grid(row=3, column=1, sticky="ew", **padding)

            ttk.Label(frame, text="SSH key path").grid(row=4, column=0, sticky="w", **padding)
            key_frame = ttk.Frame(frame)
            key_frame.grid(row=4, column=1, sticky="ew", **padding)
            key_frame.columnconfigure(0, weight=1)
            ttk.Entry(key_frame, textvariable=key_filename_var, width=30).grid(row=0, column=0, sticky="ew")
            ttk.Button(
                key_frame,
                text="Browse...",
                command=lambda: self.fill_key_path(key_filename_var, filedialog),
            ).grid(row=0, column=1, padx=(8, 0))

            ttk.Label(frame, text="Password").grid(row=5, column=0, sticky="w", **padding)
            ttk.Entry(frame, textvariable=password_var, width=24, show="*").grid(row=5, column=1, sticky="ew", **padding)

            ttk.Label(frame, text="Refresh interval (s)").grid(row=6, column=0, sticky="w", **padding)
            ttk.Entry(frame, textvariable=refresh_interval_var, width=12).grid(row=6, column=1, sticky="w", **padding)

            ttk.Label(frame, text="SSH timeout (s)").grid(row=7, column=0, sticky="w", **padding)
            ttk.Entry(frame, textvariable=timeout_var, width=12).grid(row=7, column=1, sticky="w", **padding)

            ttk.Checkbutton(
                frame,
                text="Verify host key",
                variable=verify_host_key_var,
            ).grid(row=8, column=1, sticky="w", **padding)

            ttk.Label(
                frame,
                text="Leave password empty to use the SSH key or agent.",
            ).grid(row=9, column=0, columnspan=2, sticky="w", padx=10, pady=(6, 2))

            button_frame = ttk.Frame(frame)
            button_frame.grid(row=10, column=0, columnspan=2, sticky="e", padx=10, pady=(12, 4))

            def close_without_saving() -> None:
                self.persist_settings_window_position(self.read_window_position(root))
                root.destroy()

            def save_and_close() -> None:
                try:
                    self.config = build_updated_config(
                        current_config,
                        server_name=server_name_var.get(),
                        host=host_var.get(),
                        port=port_var.get(),
                        username=username_var.get(),
                        key_filename=key_filename_var.get(),
                        password=password_var.get(),
                        refresh_interval=refresh_interval_var.get(),
                        timeout_seconds=timeout_var.get(),
                        verify_host_key=verify_host_key_var.get(),
                        settings_window_position=self.read_window_position(root),
                    )
                    if self.needs_initial_setup:
                        self.needs_initial_setup = False
                        self.monitoring_enabled = True
                    self.first_refresh_notification_sent = False
                    result["saved"] = True
                    root.destroy()
                except Exception as exc:
                    messagebox.showerror("GPU Tray", str(exc), parent=root)

            ttk.Button(button_frame, text="Cancel", command=close_without_saving).grid(row=0, column=0, padx=(0, 8))
            ttk.Button(button_frame, text="Save", command=save_and_close).grid(row=0, column=1)

            self.place_settings_window(root, current_config.settings_window_position)
            root.protocol("WM_DELETE_WINDOW", close_without_saving)
            root.mainloop()
        finally:
            self.settings_window_open = False

        if result["saved"]:
            self.update_tray_view()
            threading.Thread(target=self.refresh_statuses, name="gpu-refresh-settings", daemon=True).start()
            self.show_notification("Connection settings saved.")

    def place_settings_window(
        self,
        root: Any,
        position: tuple[int, int] | None,
    ) -> None:
        root.update_idletasks()

        width = root.winfo_width() or root.winfo_reqwidth()
        height = root.winfo_height() or root.winfo_reqheight()
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()

        if position is None:
            x = max(0, (screen_width - width) // 2)
            y = max(0, (screen_height - height) // 2)
        else:
            max_x = max(0, screen_width - width)
            max_y = max(0, screen_height - height)
            x = max(0, min(position[0], max_x))
            y = max(0, min(position[1], max_y))

        root.geometry(f"+{x}+{y}")

    def read_window_position(self, root: Any) -> tuple[int, int] | None:
        try:
            root.update_idletasks()
            return (int(root.winfo_x()), int(root.winfo_y()))
        except Exception:
            return None

    def persist_settings_window_position(self, position: tuple[int, int] | None) -> None:
        if position is None:
            return
        if not self.can_persist_settings_window_position():
            return
        if self.config.settings_window_position == position:
            return

        updated_config = replace(self.config, settings_window_position=position)
        write_config(updated_config)
        self.config = updated_config

    def can_persist_settings_window_position(self) -> bool:
        if not self.config.config_path.exists():
            return False
        if not self.config.servers:
            return False
        server = self.config.servers[0]
        return bool(server.host.strip() and server.username.strip())

    def fill_key_path(self, key_filename_var: Any, filedialog: Any) -> None:
        path = filedialog.askopenfilename(
            title="Select SSH private key",
            filetypes=[("All files", "*.*")],
        )
        if path:
            key_filename_var.set(path.replace("\\", "/"))

    def is_autostart_enabled(self) -> bool:
        if os.name != "nt":
            return False

        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_REG_PATH) as key:
                value, _ = winreg.QueryValueEx(key, AUTOSTART_VALUE_NAME)
            return bool(value)
        except FileNotFoundError:
            return False
        except OSError:
            return False

    def set_autostart_enabled(self, enabled: bool) -> None:
        if os.name != "nt":
            raise RuntimeError("Start with Windows is only supported on Windows.")

        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            AUTOSTART_REG_PATH,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            if enabled:
                winreg.SetValueEx(
                    key,
                    AUTOSTART_VALUE_NAME,
                    0,
                    winreg.REG_SZ,
                    build_autostart_command(self.config.config_path),
                )
            else:
                try:
                    winreg.DeleteValue(key, AUTOSTART_VALUE_NAME)
                except FileNotFoundError:
                    pass


def usage_color(percent: float) -> tuple[int, int, int, int]:
    if percent < 50:
        return (58, 167, 109, 255)
    if percent < 80:
        return (223, 173, 35, 255)
    return (202, 66, 61, 255)


def load_icon_font(text: str) -> Any:
    ensure_ui_dependencies()

    if text in {"!!", "||", ".."}:
        font_size = 40
    elif len(text) == 1:
        font_size = 56
    elif len(text) == 2:
        font_size = 52
    else:
        font_size = 42
    candidate_paths: list[Path] = []
    if os.name == "nt":
        font_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        candidate_paths.extend(
            [
                font_dir / "segoeuib.ttf",
                font_dir / "arialbd.ttf",
                font_dir / "tahomabd.ttf",
                font_dir / "tahoma.ttf",
            ]
        )

    for path in candidate_paths:
        if path.exists():
            try:
                return PILImageFont.truetype(str(path), font_size)
            except OSError:
                continue

    return PILImageFont.load_default()


def build_autostart_command(config_path: Path) -> str:
    if getattr(sys, "frozen", False):
        return subprocess.list2cmdline([sys.executable])

    executable = Path(sys.executable)
    if executable.name.lower() == "python.exe":
        pythonw = executable.with_name("pythonw.exe")
        if pythonw.exists():
            executable = pythonw

    return subprocess.list2cmdline(
        [
            str(executable),
            str(Path(sys.argv[0]).resolve()),
            "--config",
            str(config_path),
        ]
    )


def server_config_to_dict(server: ServerConfig) -> dict[str, Any]:
    return {
        "name": server.name,
        "host": server.host,
        "port": server.port,
        "username": server.username,
        "password": server.password,
        "password_env": server.password_env,
        "key_filename": server.key_filename,
        "passphrase_env": server.passphrase_env,
        "timeout_seconds": server.timeout_seconds,
        "verify_host_key": server.verify_host_key,
        "gpu_query_command": server.gpu_query_command,
    }


def app_config_to_dict(config: AppConfig) -> dict[str, Any]:
    payload = {
        "refresh_interval_seconds": config.refresh_interval_seconds,
        "ssh_timeout_seconds": config.ssh_timeout_seconds,
        "servers": [server_config_to_dict(server) for server in config.servers],
    }
    if config.settings_window_position is not None:
        payload["ui"] = {
            "settings_window_position": {
                "x": config.settings_window_position[0],
                "y": config.settings_window_position[1],
            }
        }
    return payload


def build_updated_config(
    config: AppConfig,
    *,
    server_name: str,
    host: str,
    port: str,
    username: str,
    key_filename: str,
    password: str,
    refresh_interval: str,
    timeout_seconds: str,
    verify_host_key: bool,
    settings_window_position: tuple[int, int] | None,
) -> AppConfig:
    config_dict = app_config_to_dict(config)
    servers = config_dict["servers"]
    if not servers:
        raise ValueError("Config must contain at least one server.")

    try:
        parsed_port = int(port)
    except ValueError as exc:
        raise ValueError("Port must be an integer.") from exc

    try:
        parsed_refresh_interval = int(refresh_interval)
    except ValueError as exc:
        raise ValueError("Refresh interval must be an integer.") from exc

    try:
        parsed_timeout = int(timeout_seconds)
    except ValueError as exc:
        raise ValueError("SSH timeout must be an integer.") from exc

    if parsed_port <= 0:
        raise ValueError("Port must be > 0.")
    if parsed_refresh_interval <= 0:
        raise ValueError("Refresh interval must be > 0.")
    if parsed_timeout <= 0:
        raise ValueError("SSH timeout must be > 0.")
    if not host.strip():
        raise ValueError("Host / IP cannot be empty.")
    if not username.strip():
        raise ValueError("Username cannot be empty.")

    server = servers[0]
    server["name"] = server_name.strip() or "gpu-server"
    server["host"] = host.strip()
    server["port"] = parsed_port
    server["username"] = username.strip()
    server["key_filename"] = key_filename.strip() or None
    server["password"] = password if password else None
    if server["password"]:
        server["password_env"] = None
    server["timeout_seconds"] = parsed_timeout
    server["verify_host_key"] = bool(verify_host_key)

    config_dict["refresh_interval_seconds"] = parsed_refresh_interval
    config_dict["ssh_timeout_seconds"] = parsed_timeout
    if settings_window_position is not None:
        config_dict["ui"] = {
            "settings_window_position": {
                "x": settings_window_position[0],
                "y": settings_window_position[1],
            }
        }
    return load_config_from_raw(config.config_path, config_dict)


def load_config_from_raw(path: Path, raw: dict[str, Any]) -> AppConfig:
    path.write_text(json.dumps(raw, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    return load_config(path)


def write_config(config: AppConfig) -> None:
    config.config_path.write_text(
        json.dumps(app_config_to_dict(config), ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )

def run_once(config: AppConfig) -> int:
    statuses = [fetch_server_status(server) for server in config.servers]
    exit_code = 0

    for status in statuses:
        if status.ok:
            print(
                f"{status.server.name}: max {status.max_memory_percent:.1f}% "
                f"({status.total_used_mib / 1024.0:.1f}/{status.total_capacity_mib / 1024.0:.1f} GiB)"
            )
            for gpu in status.gpus:
                util_text = (
                    f"{gpu.utilization_percent}%"
                    if gpu.utilization_percent is not None
                    else "n/a"
                )
                print(
                    f"  GPU{gpu.index} {gpu.name}: "
                    f"{gpu.memory_used_gib:.1f}/{gpu.memory_total_gib:.1f} GiB "
                    f"({gpu.memory_percent:.1f}%), util {util_text}"
                )
        else:
            exit_code = 1
            print(f"{status.server.name}: ERROR - {status.error}")

    return exit_code


def write_fatal_error_log(message: str) -> None:
    try:
        log_path = Path(sys.argv[0]).resolve().parent / "gpu_tray_error.log"
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"[{timestamp}] {message}\n")
    except Exception:
        pass


def show_fatal_error(message: str) -> None:
    write_fatal_error_log(message)

    stderr_is_tty = False
    if sys.stderr is not None:
        try:
            sys.stderr.write(message + "\n")
            sys.stderr.flush()
            stderr_is_tty = bool(getattr(sys.stderr, "isatty", lambda: False)())
        except Exception:
            pass

    if os.name == "nt" and not stderr_is_tty:
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None,
                message,
                "GPU Tray",
                0x10,
            )
            return
        except Exception:
            pass

    sys.stderr.write(message + "\n")


def main() -> int:
    ensure_supported_python()
    args = parse_args()
    config_path = Path(args.config)

    if args.once:
        config = load_config(config_path)
        return run_once(config)

    require_initial_setup = False
    if config_path.exists():
        config = load_config(config_path)
    else:
        config = create_placeholder_config(config_path)
        require_initial_setup = True

    ensure_ssh_dependencies()
    ensure_ui_dependencies()
    app = GPUTrayApp(config, require_initial_setup=require_initial_setup)
    app.run()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        show_fatal_error(str(exc))
        raise SystemExit(1)
