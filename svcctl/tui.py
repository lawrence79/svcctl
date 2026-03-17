"""Textual-based TUI for svcctl."""
from __future__ import annotations

import asyncio
import re
import threading
import time
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, DirectoryTree, Footer, Header, Input, Label, RichLog, Static

from .config import get_log_dir, load_config_raw
from .ipc import daemon_request
from .utils import fmt_uptime

# ── constants ──────────────────────────────────────────────────────────────────

_INIT_BYTES = 32768
_POLL_SECS = 2.0

_STATUS_DOT = {"running": "●", "crashed": "✗", "stopped": "○"}
_STATUS_STYLE = {
    "running": "bold green",
    "crashed": "bold red",
    "stopped": "dim",
}

_ERROR_PATTERN = re.compile(r"(?:\s|\[|:)ERROR", re.IGNORECASE)
_WARN_PATTERN = re.compile(r"(?:\s|\[|:)WARN", re.IGNORECASE)
_INFO_PATTERN = re.compile(r"(?:\s|\[|:)INFO", re.IGNORECASE)


# ── modals ────────────────────────────────────────────────────────────────────


class DirectoryPickerModal(ModalScreen):
    """Browse and select a directory."""

    DEFAULT_CSS = """
    DirectoryPickerModal {
        align: center middle;
    }
    #dir-picker-container {
        background: $surface;
        border: thick $accent;
        padding: 1 2;
        width: 60;
        height: 28;
    }
    #dir-picker-container Label {
        margin-bottom: 1;
    }
    DirectoryTree {
        height: 1fr;
        border: solid $surface-darken-2;
    }
    #dir-picker-selected {
        height: 1;
        margin-top: 1;
        color: $text-muted;
    }
    #dir-picker-buttons {
        margin-top: 1;
        height: 3;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, start_path: str = "~") -> None:
        super().__init__()
        self._start = str(Path(start_path).expanduser().resolve())
        self._selected: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="dir-picker-container"):
            yield Label("Select Directory")
            yield DirectoryTree(self._start, id="dir-tree")
            yield Static("", id="dir-picker-selected")
            with Horizontal(id="dir-picker-buttons"):
                yield Button("Select", variant="primary", id="btn-select")
                yield Button("Cancel", id="btn-cancel")

    @on(DirectoryTree.DirectorySelected)
    def _on_dir_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        self._selected = str(event.path)
        self.query_one("#dir-picker-selected", Static).update(f"  {self._selected}")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-select":
            self.dismiss(self._selected)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class AddServiceModal(ModalScreen):
    """Modal form to add a new service."""

    DEFAULT_CSS = """
    AddServiceModal {
        align: center middle;
    }
    #modal-container {
        background: $surface;
        border: thick $accent;
        padding: 1 2;
        width: 56;
    }
    #modal-container Label {
        margin-top: 1;
    }
    #dir-row {
        height: 3;
    }
    #dir-row Input {
        width: 1fr;
    }
    #btn-browse {
        width: 10;
        margin-left: 1;
    }
    #modal-buttons {
        margin-top: 1;
        height: 3;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-container"):
            yield Label("Add Service", id="modal-title")
            yield Label("Name")
            yield Input(placeholder="myapp", id="input-name")
            yield Label("Dir")
            with Horizontal(id="dir-row"):
                yield Input(placeholder="./apps/myapp", id="input-dir")
                yield Button("Browse", id="btn-browse")
            yield Label("Cmd")
            yield Input(placeholder="yarn start", id="input-cmd")
            yield Checkbox("Auto-restart", value=True, id="input-auto-restart")
            yield Label("Restart Delay (seconds)")
            yield Input(value="2", id="input-restart-delay")
            with Horizontal(id="modal-buttons"):
                yield Button("Add", variant="primary", id="btn-add")
                yield Button("Cancel", id="btn-cancel")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
            return

        if event.button.id == "btn-browse":
            current = self.query_one("#input-dir", Input).value.strip()
            if current:
                start = str(Path(current).expanduser().resolve()) if Path(current).expanduser().exists() else "~"
            else:
                try:
                    cfg = load_config_raw()
                    root = cfg.get("root")
                    start = str(Path(root).expanduser().resolve()) if root else str(Path.home())
                except Exception:
                    start = str(Path.home())

            def _set_dir(path: str | None) -> None:
                if not path:
                    return
                # Try to make path relative to config root
                try:
                    cfg = load_config_raw()
                    root = cfg.get("root")
                    if root:
                        rel = Path(path).relative_to(Path(root).expanduser().resolve())
                        display = str(rel)
                    else:
                        display = path
                except ValueError:
                    display = path
                self.query_one("#input-dir", Input).value = display
                # Auto-fill name from folder basename if empty
                name_input = self.query_one("#input-name", Input)
                if not name_input.value.strip():
                    name_input.value = Path(path).name

            self.app.push_screen(DirectoryPickerModal(start), _set_dir)
            return

        name = self.query_one("#input-name", Input).value.strip()
        svc_dir = self.query_one("#input-dir", Input).value.strip()
        cmd = self.query_one("#input-cmd", Input).value.strip()
        auto_restart = self.query_one("#input-auto-restart", Checkbox).value
        try:
            restart_delay = int(self.query_one("#input-restart-delay", Input).value.strip())
        except ValueError:
            restart_delay = 2

        if not name or not svc_dir or not cmd:
            self.app.notify("Name, Dir, and Cmd are required.", severity="error")
            return

        entry: dict = {"dir": svc_dir, "cmd": cmd, "auto_restart": auto_restart, "restart_delay": restart_delay}
        resp = await asyncio.to_thread(daemon_request, {"action": "add", "name": name, "entry": entry})
        if not resp or not resp.get("ok"):
            self.app.notify(
                (resp.get("error") if resp else None) or "Failed to add service",
                severity="error",
            )
            return
        self.dismiss({"name": name})

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmRemoveModal(ModalScreen):
    """Confirmation dialog to remove a service."""

    DEFAULT_CSS = """
    ConfirmRemoveModal {
        align: center middle;
    }
    #modal-container {
        background: $surface;
        border: thick $accent;
        padding: 1 2;
        width: 50;
    }
    #modal-buttons {
        margin-top: 1;
        height: 3;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-container"):
            yield Label(f"Remove '{self._name}'?")
            with Horizontal(id="modal-buttons"):
                yield Button("Remove", variant="error", id="btn-remove")
                yield Button("Cancel", id="btn-cancel")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-remove":
            resp = await asyncio.to_thread(daemon_request, {"action": "remove", "name": self._name})
            if not resp or not resp.get("ok"):
                self.app.notify(
                    (resp.get("error") if resp else None) or "Failed to remove service",
                    severity="error",
                )
                self.dismiss(False)
            else:
                self.dismiss(True)
        else:
            self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


# ── widgets ───────────────────────────────────────────────────────────────────


class StatusBar(Static):
    """Compact one-line status shown above the log panel."""

    def update_svc(self, info: dict | None) -> None:
        if info is None:
            self.update("")
            return
        st = info["status"]
        style = _STATUS_STYLE.get(st, "")
        t = Text()
        t.append(f" {info['name']}", style="bold")
        t.append("  ")
        t.append(st, style=style)
        pid = info.get("pid")
        if pid:
            t.append(f"  pid {pid}", style="dim")
        restarts = info.get("restart_count", 0)
        if restarts:
            t.append(f"  restarts {restarts}", style="dim yellow")
        self.update(t)


# ── main app ──────────────────────────────────────────────────────────────────


class SvcctlApp(App[None]):
    CSS = """
    #sidebar {
        width: 32;
        border-right: solid $surface-darken-2;
        padding: 0;
    }

    #sidebar-header {
        background: $surface-darken-1;
        color: $text-muted;
        padding: 0 1;
        height: 1;
        text-style: bold;
    }

    DataTable {
        height: 1fr;
        border: none;
        padding: 0;
    }

    DataTable > .datatable--cursor {
        background: $accent 25%;
        color: $text;
    }

    #log-container {
        height: 1fr;
    }

    #status-bar {
        background: $surface-darken-1;
        height: 1;
        padding: 0 1;
    }

    #log {
        height: 1fr;
        border: none;
        padding: 0 1;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("a", "service_start", "Start"),
        Binding("s", "service_stop", "Stop"),
        Binding("r", "service_restart", "Restart"),
        Binding("n", "add_service", "Add"),
        Binding("d", "remove_service", "Delete"),
        Binding("f", "toggle_follow", "Follow"),
        Binding("c", "clear_log", "Clear"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._watching: str | None = None
        self._tail_stop = threading.Event()
        self._svc_data: dict[str, dict] = {}
        self._known_rows: set[str] = set()
        self._daemon_connected: bool = True
        self._follow: bool = True
        self._last_status_revision: int | None = None

    # ── layout ────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Static(" SERVICES", id="sidebar-header")
                yield DataTable(id="service-table", show_header=False, cursor_type="row")
            with Vertical(id="log-container"):
                yield StatusBar("", id="status-bar")
                yield RichLog(
                    id="log",
                    highlight=False,
                    markup=False,
                    wrap=True,
                    auto_scroll=True,
                    max_lines=5000,
                )
        yield Footer()

    def on_mount(self) -> None:
        self.title = "svcctl"
        self.sub_title = "service manager"
        table = self.query_one("#service-table", DataTable)
        table.add_column("", key="dot", width=2)
        table.add_column("service", key="name")
        table.add_column("uptime", key="uptime", width=7)
        self.set_interval(_POLL_SECS, self._poll_status)
        self.call_after_refresh(self._poll_status)

    # ── status polling ────────────────────────────────────────────────────────

    async def _poll_status(self) -> None:
        resp = await asyncio.to_thread(daemon_request, {"action": "status"})
        if not resp:
            if self._daemon_connected:
                self._daemon_connected = False
                self._apply_daemon_error()
            return
        reconnected = not self._daemon_connected
        self._daemon_connected = True
        services: list[dict] = resp.get("services", [])
        revision = resp.get("revision")
        if isinstance(revision, int) and not reconnected and revision == self._last_status_revision:
            self._apply_uptime_only(services)
            return
        if isinstance(revision, int):
            self._last_status_revision = revision
        self._apply_status(services, reconnected)

    def _apply_daemon_error(self) -> None:
        self.sub_title = "daemon unreachable"
        self.notify("Cannot connect to daemon. Run: svcctl daemon", severity="error", timeout=5)

    def _apply_status(self, services: list[dict], reconnected: bool = False) -> None:
        if reconnected:
            self.sub_title = "service manager"

        table = self.query_one("#service-table", DataTable)
        current_names = {s["name"] for s in services}
        first_added: str | None = None

        with self.batch_update():
            for svc in services:
                name = svc["name"]
                prev = self._svc_data.get(name)
                self._svc_data[name] = svc
                uptime = Text(fmt_uptime(svc["uptime"]) if svc["uptime"] is not None else "—", style="dim")
                if name in self._known_rows:
                    if not prev or prev.get("status") != svc["status"]:
                        dot = Text(_STATUS_DOT.get(svc["status"], "?"), style=_STATUS_STYLE.get(svc["status"], ""))
                        table.update_cell(name, "dot", dot)
                    table.update_cell(name, "uptime", uptime)
                else:
                    dot = Text(_STATUS_DOT.get(svc["status"], "?"), style=_STATUS_STYLE.get(svc["status"], ""))
                    table.add_row(dot, name, uptime, key=name)
                    self._known_rows.add(name)
                    if first_added is None:
                        first_added = name

            for name in list(self._known_rows):
                if name not in current_names:
                    self._known_rows.discard(name)
                    self._svc_data.pop(name, None)
                    table.remove_row(name)

        if first_added is not None and self._watching is None:
            self._switch_log(first_added)
        self.query_one("#status-bar", StatusBar).update_svc(self._selected_info())

    def _apply_uptime_only(self, services: list[dict]) -> None:
        table = self.query_one("#service-table", DataTable)
        current_names = {s["name"] for s in services}
        status_changed = False

        with self.batch_update():
            for svc in services:
                name = svc["name"]
                prev = self._svc_data.get(name)
                self._svc_data[name] = svc
                if name not in self._known_rows:
                    dot = Text(_STATUS_DOT.get(svc["status"], "?"), style=_STATUS_STYLE.get(svc["status"], ""))
                    uptime = Text(fmt_uptime(svc["uptime"]) if svc["uptime"] is not None else "—", style="dim")
                    table.add_row(dot, name, uptime, key=name)
                    self._known_rows.add(name)
                    continue

                if prev and prev.get("status") != svc["status"]:
                    dot = Text(_STATUS_DOT.get(svc["status"], "?"), style=_STATUS_STYLE.get(svc["status"], ""))
                    table.update_cell(name, "dot", dot)
                    status_changed = True
                uptime = Text(fmt_uptime(svc["uptime"]) if svc["uptime"] is not None else "—", style="dim")
                table.update_cell(name, "uptime", uptime)

            for name in list(self._known_rows):
                if name not in current_names:
                    self._known_rows.discard(name)
                    self._svc_data.pop(name, None)
                    table.remove_row(name)

        self.query_one("#status-bar", StatusBar).update_svc(self._selected_info())

    # ── log panel ─────────────────────────────────────────────────────────────

    @on(DataTable.RowHighlighted)
    def _on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key and event.row_key.value:
            name = event.row_key.value
            self._switch_log(name)
            self.query_one("#status-bar", StatusBar).update_svc(self._svc_data.get(name))

    def _switch_log(self, name: str) -> None:
        if name == self._watching:
            return
        self._watching = name
        self._tail_stop.set()
        self._tail_stop = threading.Event()
        log = self.query_one("#log", RichLog)
        log.clear()
        stop = self._tail_stop
        t = threading.Thread(target=self._tail_log, args=(name, log, stop), daemon=True)
        t.start()

    def _tail_log(self, name: str, log: RichLog, stop: threading.Event) -> None:
        log_path = get_log_dir() / f"{name}.log"
        try:
            with open(log_path, "r", errors="replace") as f:
                # Seed with last _INIT_BYTES; discard partial first line from seek
                f.seek(0, 2)
                if f.tell() > _INIT_BYTES:
                    f.seek(f.tell() - _INIT_BYTES)
                    f.readline()  # discard partial line at seek boundary
                for line in f.read().splitlines():
                    if stop.is_set():
                        return
                    self._write_log_line(log, line)
                # Follow
                while not stop.is_set():
                    line = f.readline()
                    if line:
                        self._write_log_line(log, line.rstrip("\n"))
                    else:
                        stop.wait(0.05)
        except FileNotFoundError:
            self.call_from_thread(log.write, Text(f"  [no log file yet for {name}]", style="dim"))
            while not stop.is_set():
                stop.wait(0.5)
                if (get_log_dir() / f"{name}.log").exists():
                    self.call_from_thread(self._switch_log, name)
                    return

    def _write_log_line(self, log: RichLog, line: str) -> None:
        if "[svcctl]" in line or ("─" * 10) in line:
            self.call_from_thread(log.write, Text(line, style="cyan bold"))
            return
        t = Text()
        rest = line
        # Dim the leading timestamp bracket [HH:MM:SS]
        if rest.startswith("[") and "]" in rest[:11]:
            bracket_end = rest.index("]") + 1
            t.append(rest[:bracket_end], style="dim")
            rest = rest[bracket_end:]
        if _ERROR_PATTERN.search(rest):
            t.append(rest, style="bold red")
        elif _WARN_PATTERN.search(rest):
            t.append(rest, style="yellow")
        elif _INFO_PATTERN.search(rest):
            t.append(rest, style="")
        elif rest.startswith("    ") or rest.startswith("\t"):
            t.append(rest, style="dim")  # stack trace / indented continuation
        else:
            t.append(rest)
        self.call_from_thread(log.write, t)

    # ── actions ───────────────────────────────────────────────────────────────

    def _selected_name(self) -> str | None:
        table = self.query_one("#service-table", DataTable)
        if not table.row_count:
            return None
        cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
        return cell_key.row_key.value

    def _selected_info(self) -> dict | None:
        name = self._selected_name()
        return self._svc_data.get(name) if name else None

    async def action_service_start(self) -> None:
        if name := self._selected_name():
            resp = await asyncio.to_thread(daemon_request, {"action": "start", "name": name})
            if resp is None:
                self.notify("Could not reach daemon.", severity="error")
            else:
                for r in resp.get("results", []):
                    self.notify(r["msg"], severity="information" if r["ok"] else "warning")
            await self._poll_status()

    async def action_service_stop(self) -> None:
        if name := self._selected_name():
            resp = await asyncio.to_thread(daemon_request, {"action": "stop", "name": name})
            if resp is None:
                self.notify("Could not reach daemon.", severity="error")
            else:
                for r in resp.get("results", []):
                    self.notify(r["msg"], severity="information" if r["ok"] else "warning")
            await self._poll_status()

    async def action_service_restart(self) -> None:
        if name := self._selected_name():
            resp = await asyncio.to_thread(daemon_request, {"action": "restart", "name": name})
            if resp is None:
                self.notify("Could not reach daemon.", severity="error")
            else:
                for r in resp.get("results", []):
                    self.notify(r["msg"], severity="information" if r["ok"] else "warning")
            await self._poll_status()

    def action_add_service(self) -> None:
        def _on_dismiss(result: dict | None) -> None:
            if result:
                self.notify(f"Added '{result['name']}'")
                self.call_after_refresh(self._poll_status)

        self.push_screen(AddServiceModal(), _on_dismiss)

    def action_remove_service(self) -> None:
        name = self._selected_name()
        if not name:
            return

        def _on_dismiss(confirmed: bool) -> None:
            if confirmed:
                self.notify(f"Removed '{name}'")
                self.call_after_refresh(self._poll_status)

        self.push_screen(ConfirmRemoveModal(name), _on_dismiss)

    def action_toggle_follow(self) -> None:
        log = self.query_one("#log", RichLog)
        self._follow = not self._follow
        log.auto_scroll = self._follow
        if self._follow:
            log.scroll_end(animate=False)
        label = "on" if self._follow else "off"
        self.notify(f"Follow {label}", timeout=1)

    def action_clear_log(self) -> None:
        self.query_one("#log", RichLog).clear()


def run_tui() -> None:
    from .ipc import ensure_daemon, stop_daemon
    ensure_daemon()
    SvcctlApp().run()
    stop_daemon()
