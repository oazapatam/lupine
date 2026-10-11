"""System tray client for LUPINE on Linux.

Qt exports the icon as a StatusNotifierItem over D-Bus, so it sits in the KDE
Plasma system tray natively (and in any other desktop with an SNI host). The
icon is a GPU package drawn like Plasma's own monochrome tray icons; as on the
Windows server tray, its die is green when every selected server answers,
grey when none is selected and red when one stops answering or its client
cannot be fetched.

The tray sweeps the local network for LUPINE servers at start and every few
minutes, and picks the best one on its own when none has been chosen. Its
menu lists the selected server's GPUs, switches servers, shows the remote
``nvidia-smi``, opens a terminal whose programs run on the selected GPUs, and
controls start at login. While it runs, every program of the session sees the
selected servers' GPUs beside this machine's (see ``settings.native_dir``). Clicking the icon opens a window with every
server's GPUs, memory and load.

GPU details come from a server's ``/metrics``. Servers without it (the
Windows server) are asked through the client's NVML shim instead, which also
tells which GPUs the server's CUDA driver enumerates.
"""

from __future__ import annotations

import os
import signal
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QFontDatabase,
    QGuiApplication,
    QIcon,
    QImageReader,
    QPalette,
    QPixmap,
)
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import discovery, gpuinfo
from .settings import (
    ClientError,
    Settings,
    autostart_enabled,
    client_environment,
    instance_path,
    native_active,
    set_autostart,
    set_native_environment,
    set_session_env,
    sync_native,
)

HEALTH_INTERVAL_MS = 20_000
SCAN_INTERVAL_MS = 5 * 60_000
# Each NVML query is a connection of its own to the server; once a minute is
# plenty for a tray.
INVENTORY_INTERVAL_S = 60
MENU_SERVERS = 12

STATES = ("idle", "ok", "error")
# lupine-tray-<state>.svg, also installed in the hicolor icon theme.
_ICONS = Path(__file__).resolve().parent / "icons"

# Konsole first: --separate gives the new window its own process, so it
# keeps the environment it is started with.
_TERMINALS = (
    ("konsole", "--separate"),
    ("x-terminal-emulator",),
    ("gnome-terminal",),
    ("xfce4-terminal",),
    ("kitty",),
    ("alacritty",),
    ("xterm",),
)


def _status_icon(state: str) -> QIcon:
    """The tray icon for ``state``.

    In Plasma the icon goes to the panel by its theme name, so Plasma draws
    it in the panel's colors as it does its own tray icons. Elsewhere, or
    when the icons are not installed (a checkout), the SVG is drawn here in
    the application palette's text color.
    """

    name = f"lupine-tray-{state}"
    desktops = os.environ.get("XDG_CURRENT_DESKTOP", "").split(":")
    if "KDE" in desktops and QIcon.hasThemeIcon(name):
        return QIcon.fromTheme(name)
    text = QGuiApplication.palette().color(QPalette.ColorRole.WindowText).name()
    svg = (_ICONS / f"{name}.svg").read_bytes().replace(b"#232629", text.encode())
    icon = QIcon()
    for size in (16, 22, 24, 32, 48, 64):
        buffer = QBuffer()
        buffer.setData(QByteArray(svg))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer, b"svg")
        reader.setScaledSize(QSize(size, size))
        icon.addPixmap(QPixmap.fromImage(reader.read()))
    return icon


def _terminal_command() -> list[str] | None:
    for command in _TERMINALS:
        if shutil.which(command[0]):
            return list(command)
    return None


def _gib(value: int) -> str:
    return f"{value / (1 << 30):.1f}"


def _normalized(values: list[str], port: int) -> list[str]:
    result: dict[str, None] = {}
    for value in values:
        try:
            result.setdefault(str(discovery.Endpoint.parse(value, port)))
        except ValueError:
            continue
    return list(result)


def _probe_address(address: str) -> discovery.Server | None:
    try:
        return discovery.probe(discovery.Endpoint.parse(address))
    except ValueError:
        return None


@dataclass
class Row:
    address: str
    server: discovery.Server | None
    selected: bool
    manual: bool
    offline: bool

    @property
    def label(self) -> str:
        return self.server.label if self.server else self.address


class Tray(QObject):
    scan_finished = pyqtSignal(object)
    scan_progressed = pyqtSignal(int, int)
    health_finished = pyqtSignal(object)
    client_finished = pyqtSignal(object, str)
    inventory_finished = pyqtSignal(object, str)
    smi_finished = pyqtSignal(object)

    def __init__(self, executable: str) -> None:
        super().__init__()
        self.executable = executable
        self.settings = Settings.load()
        self.settings.servers = _normalized(self.settings.servers, self.settings.port)
        self.settings.manual = _normalized(self.settings.manual, self.settings.port)
        self.known: dict[str, discovery.Server] = {}
        self.offline: set[str] = set()
        self.announced: set[str] = set()
        self.scanned_once = False
        self.auto_select = not self.settings.servers
        self.scanning = False
        self.progress = (0, 0)
        self.last_scan: float | None = None
        self.health_running = False
        self.health_again = False
        self.client_error = ""
        # GPUs reported through NVML for servers without /metrics, by address.
        self.inventory: dict[str, list[discovery.Gpu]] = {}
        self.inventory_error: dict[str, str] = {}
        self.inventory_at: dict[str, float] = {}
        self.inventory_running: set[str] = set()
        # UUIDs the server's CUDA driver enumerates; checked once per address.
        self.cuda_seen: dict[str, set[str]] = {}
        self.window: ServersWindow | None = None
        self.smi_dialog: SmiDialog | None = None
        # The client directory for the current selection, once fetched.
        self.native_libdir: str | None = None

        self.icons = {state: _status_icon(state) for state in STATES}
        self.menu = QMenu()
        self.icon = QSystemTrayIcon(self.icons["idle"])
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)

        self.scan_finished.connect(self._scan_finished)
        self.scan_progressed.connect(self._scan_progressed)
        self.health_finished.connect(self._health_finished)
        self.client_finished.connect(self._client_finished)
        self.inventory_finished.connect(self._inventory_finished)
        self.smi_finished.connect(self._smi_finished)

        self.health_timer = QTimer(self)
        self.health_timer.setInterval(HEALTH_INTERVAL_MS)
        self.health_timer.timeout.connect(self.check_health)
        self.scan_timer = QTimer(self)
        self.scan_timer.setInterval(SCAN_INTERVAL_MS)
        self.scan_timer.timeout.connect(self.scan)

    def start(self) -> None:
        self.refresh()
        self.icon.show()
        self.apply_native_environment()
        self.health_timer.start()
        self.scan_timer.start()
        self.scan()
        self.check_health()
        if self.settings.servers:
            self.prepare_client("prefetch")

    # Background work -------------------------------------------------------

    def _run(self, work: Callable[[], object], done, *args: object) -> None:
        def target() -> None:
            try:
                result = work()
            except Exception as exc:  # reported in the UI thread
                result = exc
            done.emit(result, *args)

        threading.Thread(target=target, daemon=True).start()

    def scan(self) -> None:
        if self.scanning:
            return
        self.scanning = True
        self.progress = (0, 0)
        self.refresh()
        port = self.settings.port
        extra = [*self.settings.manual, *self.settings.servers]
        networks = list(self.settings.networks)
        self._run(
            lambda: discovery.discover(
                port=port, extra=extra, networks=networks, progress=self.scan_progressed.emit
            ),
            self.scan_finished,
        )

    def _scan_progressed(self, done: int, total: int) -> None:
        self.progress = (done, total)
        if self.window is not None and self.window.isVisible():
            self.window.update_status()

    def _usable(self, server: discovery.Server) -> bool:
        return server.client_available is not False or bool(self.settings.libdir)

    def _scan_finished(self, result: object) -> None:
        self.scanning = False
        self.last_scan = time.time()
        if isinstance(result, Exception):
            self.notify("Network scan failed", str(result), error=True)
            self.refresh()
            return
        found = {server.address: server for server in result}
        pinned = {*self.settings.servers, *self.settings.manual}
        previous = self.known
        # Servers that vanished are forgotten unless the user chose them.
        self.known = {a: s for a, s in previous.items() if a in pinned}
        for address, server in found.items():
            if address in previous and not server.name:
                server.name = previous[address].name
            if address not in pinned:
                self.known[address] = server
        for address in pinned:
            self._update(address, found.get(address))

        usable = [server for server in result if self._usable(server)]
        if self.auto_select and not self.settings.servers and usable:
            chosen = discovery.best(usable)
            self.select([chosen.address])
            self.notify(
                f"Using {chosen.label}",
                f"{chosen.gpu_summary()} at {chosen.address}. "
                "Choose another server from the LUPINE tray icon.",
            )
        elif self.scanned_once:
            new = [s for s in result if s.address not in self.announced]
            if new:
                self.notify(
                    "LUPINE server found" if len(new) == 1 else f"{len(new)} LUPINE servers found",
                    "\n".join(f"{s.label}: {s.gpu_summary()}" for s in new),
                )
        elif result and not usable:
            self.notify(
                "No usable LUPINE server",
                "The servers found publish no client for this machine.",
                error=True,
            )
        self.announced |= set(found)
        self.scanned_once = True
        self.refresh()

    def check_health(self) -> None:
        targets = list(dict.fromkeys([*self.settings.servers, *self.settings.manual]))
        if not targets:
            return
        if self.health_running:
            self.health_again = True
            return
        self.health_running = True

        def work() -> dict[str, discovery.Server | None]:
            with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
                return dict(zip(targets, pool.map(_probe_address, targets)))

        self._run(work, self.health_finished)

    def _update(self, address: str, server: discovery.Server | None) -> None:
        """Record a probe of a chosen server, announcing when it goes or returns."""

        selected = address in self.settings.servers
        if server is None:
            if selected and address not in self.offline:
                self.notify(
                    f"{self._label(address)} is not answering",
                    "Programs using it fail until it is back.",
                    error=True,
                )
            self.offline.add(address)
            return
        previous = self.known.get(address)
        if previous and not server.name:
            server.name = previous.name
        if not server.metrics and address in self.inventory:
            server.gpus = self.inventory[address]
        if selected and address in self.offline:
            self.notify(f"{server.label} is back", server.gpu_summary())
        self.offline.discard(address)
        self.known[address] = server

    def _health_finished(self, result: object) -> None:
        self.health_running = False
        if isinstance(result, dict):
            for address, server in result.items():
                self._update(address, server)
        if self.health_again:
            self.health_again = False
            self.check_health()
        self._refresh_inventory()
        self.refresh()

    def _refresh_inventory(self, force: bool = False) -> None:
        """Ask selected servers without /metrics for their GPUs over NVML."""

        now = time.monotonic()
        libdir = self.settings.libdir
        for address in self.settings.servers:
            server = self.known.get(address)
            if (
                server is None
                or server.metrics
                or address in self.offline
                or address in self.inventory_running
                or not self._usable(server)
            ):
                continue
            if not force and now - self.inventory_at.get(address, -1e9) < INVENTORY_INTERVAL_S:
                continue
            self.inventory_running.add(address)
            self.inventory_at[address] = now
            cuda = address not in self.cuda_seen

            def work(address: str = address, cuda: bool = cuda) -> dict:
                env = client_environment([address], libdir=libdir)
                return gpuinfo.query(env, cuda=cuda)

            self._run(work, self.inventory_finished, address)

    def _inventory_finished(self, result: object, address: str) -> None:
        self.inventory_running.discard(address)
        if isinstance(result, Exception):
            self.inventory_error[address] = str(result)
            self.refresh()
            return
        self.inventory_error.pop(address, None)
        if result.get("cuda") is not None:
            self.cuda_seen[address] = set(result["cuda"])
        visible = self.cuda_seen.get(address)
        gpus = gpuinfo.gpus({**result, "cuda": None if visible is None else list(visible)})
        self.inventory[address] = gpus
        server = self.known.get(address)
        if server is not None and not server.metrics:
            server.gpus = gpus
        self.refresh()

    def prepare_client(self, purpose: str) -> None:
        """Fetch the selected servers' client, then act on ``purpose``."""

        servers = list(self.settings.servers)
        if not servers:
            if purpose == "terminal":
                self.notify("No LUPINE server selected", "Pick a server first.", error=True)
            return
        libdir = self.settings.libdir

        def work() -> tuple[list[str], object]:
            try:
                return servers, client_environment(servers, libdir=libdir)
            except ClientError as exc:
                return servers, exc

        self._run(work, self.client_finished, purpose)

    def _client_finished(self, result: object, purpose: str) -> None:
        if isinstance(result, Exception):
            self.notify("LUPINE client unavailable", str(result), error=True)
            return
        servers, env = result
        current = servers == self.settings.servers
        if isinstance(env, ClientError):
            if current:
                self.client_error = str(env)
                self.refresh()
            self.notify("LUPINE client unavailable", str(env), error=True)
            return
        if current:
            self.native_libdir = env["LUPINE_LIBDIR"]
        if current and self.client_error:
            self.client_error = ""
        if current:
            self.refresh()
            self._refresh_inventory(force=True)
        if purpose == "terminal":
            down = [a for a in servers if a in self.offline]
            if down:
                self.notify(
                    f"{', '.join(self._label(a) for a in down)} is not answering",
                    "Programs in a LUPINE terminal would wait for it; try again once it is back.",
                    error=True,
                )
                return
            self._launch_terminal(env)

    def _launch_terminal(self, env: dict[str, str]) -> None:
        command = _terminal_command()
        if command is None:
            self.notify(
                "No terminal emulator found",
                "Install Konsole, or run lupine-run from a terminal.",
                error=True,
            )
            return
        subprocess.Popen(
            command,
            env=env,
            cwd=str(Path.home()),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    # Actions ---------------------------------------------------------------

    def select(self, addresses: list[str]) -> None:
        addresses = list(dict.fromkeys(addresses))
        if addresses == self.settings.servers:
            self.refresh()  # undo the check mark a click on it removed
            return
        self.auto_select = False
        self.settings.servers = addresses
        self.settings.save()
        if self._exports_server():
            set_session_env(addresses)
        self.client_error = ""
        self.native_libdir = None
        self.refresh()
        if addresses:
            self.prepare_client("prefetch")
            self.check_health()

    def add_server(self) -> None:
        text, ok = QInputDialog.getText(
            self.window,
            "Add LUPINE server",
            "Server address (host, host:port, or https://… behind a TLS proxy):",
        )
        if not ok or not text.strip():
            return
        try:
            address = str(discovery.Endpoint.parse(text, self.settings.port))
        except ValueError as exc:
            QMessageBox.warning(self.window, "Add LUPINE server", str(exc))
            return
        if address not in self.settings.manual:
            self.settings.manual.append(address)
            self.settings.save()
        self.select([address])
        self.check_health()

    def remove_server(self, address: str) -> None:
        if address in self.settings.manual:
            self.settings.manual.remove(address)
            self.settings.save()
        self.known.pop(address, None)
        self.offline.discard(address)
        self.select([a for a in self.settings.servers if a != address])

    def _exports_server(self) -> bool:
        # With every program on the client, LUPINE_SERVER in the session would
        # make the CUDA client take this machine's own device opens (the
        # display's, say) for the server's, and wait out a server that is off.
        return self.settings.session_env and not self.settings.native_gpus

    def toggle_session_env(self, enabled: bool) -> None:
        self.settings.session_env = enabled
        self.settings.save()
        set_session_env(self.settings.servers if self._exports_server() else None)
        if enabled:
            self.notify(
                "LUPINE_SERVER exported",
                "Programs started after your next login use the selected server.",
            )

    def toggle_autostart(self, enabled: bool) -> None:
        set_autostart(enabled, self.executable)

    def toggle_native(self, enabled: bool) -> None:
        self.settings.native_gpus = enabled
        self.settings.save()
        self.apply_native_environment()
        self.refresh()

    def apply_native_environment(self) -> None:
        """Keep the session's ``LD_LIBRARY_PATH`` in step with the setting."""

        enabled = self.settings.native_gpus
        set_session_env(self.settings.servers if self._exports_server() else None)
        if not set_native_environment(enabled):
            self.notify(
                "Remote GPUs only through LUPINE",
                "This session has no XDG_RUNTIME_DIR; use lupine-run or the "
                "LUPINE terminal.",
                error=True,
            )
        elif enabled and not native_active():
            self.notify(
                "Log out and back in once",
                "From then on every program sees the remote GPUs beside this "
                "machine's while LUPINE runs.",
            )

    def _sync_native(self) -> None:
        # Only a selection that answers: programs would otherwise wait for it.
        ready = self.settings.native_gpus and self.state() == "ok"
        servers = self.settings.servers if ready else []
        try:
            sync_native(servers, self.native_libdir if ready else None)
        except OSError as exc:
            print(f"lupine-tray: {exc}", file=sys.stderr)

    def stop_native(self) -> None:
        """With the tray gone, programs go back to the system's libraries."""

        try:
            sync_native([], None)
        except OSError:
            pass

    def show_smi(self) -> None:
        """Show ``nvidia-smi`` as run against the selected servers."""

        servers = list(self.settings.servers)
        if self.smi_dialog is None:
            self.smi_dialog = SmiDialog(self)
        self.smi_dialog.show_text(
            f"Running nvidia-smi on {', '.join(servers)}…" if servers else "No server selected."
        )
        if not servers:
            return
        libdir = self.settings.libdir

        def work() -> str:
            down = discovery.unreachable(servers)
            if down:
                return (
                    f"{', '.join(down)} is not answering, so there are no remote "
                    "GPUs to show.\n\nCheck that the LUPINE server is running on that "
                    "machine, that it is awake, and that its firewall lets port "
                    f"{discovery.DEFAULT_PORT} in."
                )
            # The remote GPUs alone, not this machine's beside them.
            env = {**client_environment(servers, libdir=libdir), "LUPINE_LOCAL_GPUS": "0"}
            smi = shutil.which("nvidia-smi")
            if smi is None:
                return _format_report(gpuinfo.query(env))
            result = subprocess.run(
                [smi], env=env, capture_output=True, text=True, timeout=90
            )
            return (result.stdout + result.stderr).rstrip()

        self._run(work, self.smi_finished)

    def _smi_finished(self, result: object) -> None:
        if self.smi_dialog is not None:
            self.smi_dialog.show_text(str(result))

    def show_window(self) -> None:
        if self.window is None:
            self.window = ServersWindow(self)
        self.window.update_from_tray()
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            if self.window is not None and self.window.isVisible():
                self.window.hide()
            else:
                self.show_window()

    def notify(self, title: str, text: str, error: bool = False) -> None:
        icon = (
            QSystemTrayIcon.MessageIcon.Warning
            if error
            else QSystemTrayIcon.MessageIcon.Information
        )
        self.icon.showMessage(title, text, icon, 8000)

    # Presentation ----------------------------------------------------------

    def _label(self, address: str) -> str:
        server = self.known.get(address)
        return server.label if server else address

    def rows(self) -> list[Row]:
        selected = self.settings.servers
        order = list(dict.fromkeys([*selected, *self.known, *self.settings.manual]))
        rows = [
            Row(
                address,
                self.known.get(address),
                address in selected,
                address in self.settings.manual,
                address in self.offline,
            )
            for address in order
        ]
        rows.sort(key=lambda row: not row.selected)
        return rows

    def state(self) -> str:
        selected = self.settings.servers
        if not selected:
            return "idle"
        if self.client_error or any(a in self.offline for a in selected):
            return "error"
        if all(a in self.known for a in selected):
            return "ok"
        return "idle"

    def status_text(self) -> str:
        selected = self.settings.servers
        if not selected:
            return "Searching for servers…" if self.scanning else "No server selected"
        names = ", ".join(self._label(a) for a in selected)
        offline = [self._label(a) for a in selected if a in self.offline]
        if offline:
            return f"{', '.join(offline)} not answering"
        if self.client_error:
            return f"No client for {names}"
        servers = [self.known[a] for a in selected if a in self.known]
        if len(servers) < len(selected):
            return f"Checking {names}…"
        gpus = [gpu for s in servers for gpu in s.gpus]
        if not gpus:
            return f"Using {names}"
        text = f"Using {names} · {len(gpus)} GPU{'s' if len(gpus) != 1 else ''}"
        hidden = sum(1 for gpu in gpus if gpu.cuda is False)
        if hidden:
            text += f" ({hidden} not visible to CUDA)"
        return text

    def _row_text(self, row: Row) -> str:
        if row.server is None:
            return f"{row.address} — {'not answering' if row.offline else 'checking…'}"
        text = f"{row.server.label} — {row.server.gpu_summary()}"
        if row.offline:
            text += " (not answering)"
        elif not self._usable(row.server):
            text += " (no client)"
        return text

    def _add(self, text: str, slot: Callable[..., object] | None = None, enabled: bool = True) -> QAction:
        action = self.menu.addAction(text)
        action.setEnabled(enabled)
        if slot is not None:
            action.triggered.connect(slot)
        return action

    def _rebuild_menu(self) -> None:
        self.menu.clear()
        self._add(self.status_text(), enabled=False)
        self.menu.addSeparator()
        rows = self.rows()
        if not rows:
            self._add("Searching the network…" if self.scanning else "No servers found", enabled=False)
        for row in rows[:MENU_SERVERS]:
            action = self._add(self._row_text(row))
            action.setCheckable(True)
            action.setChecked(row.selected)
            action.triggered.connect(lambda _checked=False, a=row.address: self.select([a]))
            if row.selected and row.server is not None:
                for gpu in row.server.gpus:
                    self._add(f"      GPU {gpu.index}: {gpu.details()}", enabled=False)
        if len(rows) > MENU_SERVERS:
            self._add(f"{len(rows) - MENU_SERVERS} more…", self.show_window)
        self._add("Scanning the network…" if self.scanning else "Scan network", self.scan, not self.scanning)
        self._add("Add server…", self.add_server)
        self._add("Show servers…", self.show_window)
        self.menu.addSeparator()
        self._add(
            "Open terminal on remote GPU",
            lambda: self.prepare_client("terminal"),
            bool(self.settings.servers),
        )
        self._add("Remote nvidia-smi…", self.show_smi, bool(self.settings.servers))
        self.menu.addSeparator()
        native = self._add("Use remote GPUs in every program")
        native.setCheckable(True)
        native.setChecked(self.settings.native_gpus)
        native.toggled.connect(self.toggle_native)
        # Every program already finds the server; see _exports_server.
        export = self._add(
            "Export LUPINE_SERVER to my session", enabled=not self.settings.native_gpus
        )
        export.setCheckable(True)
        export.setChecked(self._exports_server())
        export.toggled.connect(self.toggle_session_env)
        autostart = self._add("Start at login")
        autostart.setCheckable(True)
        autostart.setChecked(autostart_enabled())
        autostart.toggled.connect(self.toggle_autostart)
        self.menu.addSeparator()
        self._add("Quit", QApplication.quit)

    def refresh(self) -> None:
        self.icon.setIcon(self.icons[self.state()])
        tooltip = [f"LUPINE — {self.status_text()}"]
        for address in self.settings.servers:
            server = self.known.get(address)
            for gpu in server.gpus if server else []:
                tooltip.append(f"GPU {gpu.index}: {gpu.details()}")
        self.icon.setToolTip("\n".join(tooltip))
        self._sync_native()
        self._rebuild_menu()
        if self.window is not None and self.window.isVisible():
            self.window.update_from_tray()


class ServersWindow(QWidget):
    COLUMNS = ("Use", "Server", "Address", "GPUs", "Memory (GiB)", "Load", "Clients", "Status")

    def __init__(self, tray: Tray) -> None:
        super().__init__()
        self.tray = tray
        self.filling = False
        self.setWindowTitle("Servers")
        self.resize(940, 320)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self._item_changed)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.itemSelectionChanged.connect(self._update_details)
        self.details = QLabel()
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details.setWordWrap(True)

        self.scan_button = QPushButton("Scan network")
        self.scan_button.clicked.connect(tray.scan)
        add = QPushButton("Add server…")
        add.clicked.connect(tray.add_server)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._remove)
        self.copy_button = QPushButton("Copy address")
        self.copy_button.clicked.connect(self._copy)
        self.smi_button = QPushButton("nvidia-smi…")
        self.smi_button.clicked.connect(tray.show_smi)
        close = QPushButton("Close")
        close.clicked.connect(self.hide)

        buttons = QHBoxLayout()
        for button in (self.scan_button, add, self.remove_button, self.copy_button, self.smi_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(self.table)
        layout.addWidget(self.details)
        layout.addLayout(buttons)

    def _current(self) -> str | None:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def update_status(self) -> None:
        tray = self.tray
        if tray.scanning:
            done, total = tray.progress
            text = f"Scanning the network… {done}/{total}" if total else "Scanning the network…"
        else:
            count = sum(1 for row in tray.rows() if row.server)
            when = time.strftime("%H:%M", time.localtime(tray.last_scan)) if tray.last_scan else "never"
            text = f"{count} server{'s' if count != 1 else ''} answering · last scan {when}"
        text += (
            ". Checked servers are the ones lupine-run and “Open terminal on "
            "remote GPU” use."
        )
        self.status.setText(text)
        self.scan_button.setEnabled(not tray.scanning)

    def update_from_tray(self) -> None:
        current = self._current()
        rows = self.tray.rows()
        self.filling = True
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            server = row.server
            use = QTableWidgetItem()
            use.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            use.setCheckState(Qt.CheckState.Checked if row.selected else Qt.CheckState.Unchecked)
            use.setData(Qt.ItemDataRole.UserRole, row.address)
            memory = server.memory() if server else None
            load = server.utilization() if server else None
            if row.offline:
                status = "not answering"
            elif server is None:
                status = "checking…"
            else:
                status = f"online · {server.latency_ms:.0f} ms"
                if not self.tray._usable(server):
                    status += " · no client published"
            cells = (
                use,
                QTableWidgetItem(row.label + (" (added)" if row.manual else "")),
                QTableWidgetItem(row.address),
                QTableWidgetItem(server.gpu_summary() if server else ""),
                QTableWidgetItem(f"{_gib(memory[0])} / {_gib(memory[1])}" if memory else "–"),
                QTableWidgetItem("–" if load is None else f"{load}%"),
                QTableWidgetItem(str(server.clients) if server and server.metrics else "–"),
                QTableWidgetItem(status),
            )
            if server and server.cuda_version:
                cells[1].setToolTip(f"CUDA {server.cuda_version}")
            for column, item in enumerate(cells):
                self.table.setItem(index, column, item)
            if row.address == current:
                self.table.selectRow(index)
        self.filling = False
        self.update_status()
        self._update_buttons()
        self._update_details()

    def _update_details(self) -> None:
        """The GPUs of the highlighted server, or of the first selected one."""

        tray = self.tray
        address = self._current() or next(iter(tray.settings.servers), None)
        server = tray.known.get(address) if address else None
        if server is None:
            self.details.setText("")
            return
        lines = [f"{server.label} ({server.address})" + (
            f" · CUDA {server.cuda_version}" if server.cuda_version else ""
        )]
        for gpu in server.gpus:
            lines.append(f"  GPU {gpu.index}: {gpu.details()}  {gpu.uuid}".rstrip())
        if not server.gpus:
            if address in tray.inventory_error:
                lines.append(f"  GPU query failed: {tray.inventory_error[address]}")
            elif not server.metrics:
                lines.append(
                    "  This server has no /metrics; its GPUs are read over NVML "
                    "while it is selected."
                )
        if any(gpu.cuda is False for gpu in server.gpus):
            lines.append(
                "  A GPU marked “not visible to CUDA” is listed by the server's "
                "NVML but not enumerated by its CUDA driver (check "
                "CUDA_VISIBLE_DEVICES on the server)."
            )
        self.details.setText("\n".join(lines))

    def _update_buttons(self) -> None:
        current = self._current()
        self.copy_button.setEnabled(current is not None)
        self.remove_button.setEnabled(current in self.tray.settings.manual)
        self.smi_button.setEnabled(bool(self.tray.settings.servers))

    def _item_changed(self, item: QTableWidgetItem) -> None:
        if self.filling or item.column() != 0:
            return
        selected = [
            self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            for row in range(self.table.rowCount())
            if self.table.item(row, 0).checkState() == Qt.CheckState.Checked
        ]
        # select() repopulates the table; not from inside its own signal.
        QTimer.singleShot(0, lambda: self.tray.select(selected))

    def _remove(self) -> None:
        current = self._current()
        if current is not None:
            self.tray.remove_server(current)

    def _copy(self) -> None:
        current = self._current()
        if current is not None:
            QGuiApplication.clipboard().setText(current)


def _format_report(report: dict) -> str:
    """A table like nvidia-smi -L for machines without nvidia-smi."""

    lines = [f"GPU {gpu.index}: {gpu.details()}  ({gpu.uuid})" for gpu in gpuinfo.gpus(report)]
    return "\n".join(lines) or "No GPUs reported."


class SmiDialog(QDialog):
    def __init__(self, tray: Tray) -> None:
        super().__init__()
        self.setWindowTitle("Remote nvidia-smi")
        self.resize(820, 520)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(tray.show_smi)
        close = QPushButton("Close")
        close.clicked.connect(self.hide)
        buttons = QHBoxLayout()
        buttons.addWidget(refresh)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(self.text)
        layout.addLayout(buttons)

    def show_text(self, text: str) -> None:
        self.text.setPlainText(text)
        self.show()
        self.raise_()
        self.activateWindow()


def _hand_off(show: bool) -> bool:
    """Ask a running tray to show its window; False when none is running."""

    socket = QLocalSocket()
    socket.connectToServer(instance_path())
    if not socket.waitForConnected(500):
        return False
    socket.write(b"show\n" if show else b"\n")
    socket.waitForBytesWritten(500)
    socket.disconnectFromServer()
    return True


def _listen(tray: Tray) -> QLocalServer:
    server = QLocalServer()
    QLocalServer.removeServer(instance_path())
    server.listen(instance_path())

    def accept() -> None:
        connection = server.nextPendingConnection()
        if connection is None:
            return

        def read() -> None:
            if bytes(connection.readAll()).strip() == b"show":
                tray.show_window()
            connection.disconnectFromServer()

        connection.readyRead.connect(read)
        connection.disconnected.connect(connection.deleteLater)

    server.newConnection.connect(accept)
    return server


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv if argv is None else argv)
    show = "--show" in args[1:]
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = QApplication(args)
    app.setApplicationName("lupine-tray")
    app.setApplicationDisplayName("LUPINE")
    app.setDesktopFileName("lupine-tray")
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(QIcon.fromTheme("lupine", _status_icon("ok")))
    if _hand_off(show):
        return 0

    tray = Tray(executable=os.path.abspath(args[0]))
    instance = _listen(tray)  # noqa: F841 - lives as long as the event loop
    app.aboutToQuit.connect(tray.stop_native)
    # A plain kill (logout, an upgrade) also gives programs their libraries
    # back; the timer lets Python see the signal while Qt waits.
    signal.signal(signal.SIGTERM, lambda *_: app.quit())
    wake = QTimer()
    wake.timeout.connect(lambda: None)
    wake.start(1000)
    tray.start()
    if show or not QSystemTrayIcon.isSystemTrayAvailable():
        tray.show_window()
    return app.exec()
