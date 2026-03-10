"""Textual-based TUI for svcctl."""
from __future__ import annotations

import threading
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from .config import get_log_dir
from .ipc import daemon_request, daemon_running
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
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._watching: str | None = None
        self._tail_stop = threading.Event()
        self._svc_data: dict[str, dict] = {}
        self._known_rows: set[str] = set()

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
                    wrap=False,
                    auto_scroll=True,
                )
        yield Footer()

    def on_mount(self) -> None:
        self.title = "svcctl"
        self.sub_title = "service manager"
        table = self.query_one("#service-table", DataTable)
        table.add_column("", key="dot", width=2)
        table.add_column("service", key="name")
        table.add_column("uptime", key="uptime", width=7)
        self._poll_status()
        self.set_interval(_POLL_SECS, self._poll_status)

    # ── status polling ────────────────────────────────────────────────────────

    def _poll_status(self) -> None:
        resp = daemon_request({"action": "status"})
        if not resp:
            return
        services: list[dict] = resp.get("services", [])

        table = self.query_one("#service-table", DataTable)
        current_names = {s["name"] for s in services}

        for svc in services:
            name = svc["name"]
            self._svc_data[name] = svc
            dot = Text(_STATUS_DOT.get(svc["status"], "?"), style=_STATUS_STYLE.get(svc["status"], ""))
            uptime = Text(fmt_uptime(svc["uptime"]) if svc["uptime"] is not None else "—", style="dim")
            if name in self._known_rows:
                table.update_cell(name, "dot", dot)
                table.update_cell(name, "uptime", uptime)
            else:
                table.add_row(dot, name, uptime, key=name)
                self._known_rows.add(name)
                if table.row_count == 1:
                    self._switch_log(name)

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
                # Seed with last _INIT_BYTES
                f.seek(0, 2)
                f.seek(max(0, f.tell() - _INIT_BYTES))
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
                    self._switch_log(name)
                    return

    def _write_log_line(self, log: RichLog, line: str) -> None:
        if "[svcctl]" in line or ("─" * 10) in line:
            self.call_from_thread(log.write, Text(line, style="cyan bold"))
        else:
            self.call_from_thread(log.write, line)

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

    def action_service_start(self) -> None:
        if name := self._selected_name():
            resp = daemon_request({"action": "start", "name": name})
            if resp is None:
                self.notify("Could not reach daemon.", severity="error")
            else:
                for r in resp.get("results", []):
                    self.notify(r["msg"], severity="information" if r["ok"] else "warning")
            self._poll_status()

    def action_service_stop(self) -> None:
        if name := self._selected_name():
            resp = daemon_request({"action": "stop", "name": name})
            if resp is None:
                self.notify("Could not reach daemon.", severity="error")
            else:
                for r in resp.get("results", []):
                    self.notify(r["msg"], severity="information" if r["ok"] else "warning")
            self._poll_status()

    def action_service_restart(self) -> None:
        if name := self._selected_name():
            resp = daemon_request({"action": "restart", "name": name})
            if resp is None:
                self.notify("Could not reach daemon.", severity="error")
            else:
                for r in resp.get("results", []):
                    self.notify(r["msg"], severity="information" if r["ok"] else "warning")
            self._poll_status()


def run_tui() -> None:
    if not daemon_running():
        import sys
        print("[error] Daemon is not running. Run: svcctl start all", file=sys.stderr)
        sys.exit(1)
    SvcctlApp().run()
