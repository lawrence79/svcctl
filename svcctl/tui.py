"""Textual-based TUI for svcctl."""
from __future__ import annotations

import threading
import time
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Label, ListItem, ListView, RichLog, Static

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


class ServiceItem(ListItem):
    """One row in the sidebar service list."""

    def __init__(self, info: dict) -> None:
        super().__init__()
        self.svc_info = info

    @property
    def svc_name(self) -> str:
        return self.svc_info["name"]

    def compose(self) -> ComposeResult:
        yield Label(self._render_text(), id="svc-label")

    def refresh_info(self, info: dict) -> None:
        self.svc_info = info
        self.query_one("#svc-label", Label).update(self._render_text())

    def _render_text(self) -> Text:
        st = self.svc_info["status"]
        dot = _STATUS_DOT.get(st, "?")
        style = _STATUS_STYLE.get(st, "")
        uptime = self.svc_info.get("uptime")
        t = Text()
        t.append(f" {dot} ", style=style)
        t.append(self.svc_info["name"])
        if uptime is not None:
            t.append(f"  {fmt_uptime(uptime)}", style="dim")
        return t


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
        width: 28;
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

    ListView {
        height: 1fr;
        border: none;
        padding: 0;
    }

    ListItem {
        padding: 0;
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
        self._item_map: dict[str, ServiceItem] = {}

    # ── layout ────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Static(" SERVICES", id="sidebar-header")
                yield ListView(id="service-list")
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
        self._poll_status()
        self.set_interval(_POLL_SECS, self._poll_status)

    # ── status polling ────────────────────────────────────────────────────────

    def _poll_status(self) -> None:
        resp = daemon_request({"action": "status"})
        if not resp:
            return
        services: list[dict] = resp.get("services", [])

        lv = self.query_one("#service-list", ListView)
        current_names = [s["name"] for s in services]

        # Update existing items, add new ones
        for svc in services:
            name = svc["name"]
            if name in self._item_map:
                self._item_map[name].refresh_info(svc)
            else:
                item = ServiceItem(svc)
                self._item_map[name] = item
                lv.append(item)

        # Remove stale items
        for name in list(self._item_map):
            if name not in current_names:
                self._item_map.pop(name).remove()

        # Auto-select first item on startup
        if lv.index is None and services:
            lv.index = 0
            self._switch_log(services[0]["name"])

        # Keep status bar fresh for the currently selected service
        selected = self._selected_info()
        self.query_one("#status-bar", StatusBar).update_svc(selected)

    # ── log panel ─────────────────────────────────────────────────────────────

    @on(ListView.Selected)
    def _on_list_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, ServiceItem):
            self._switch_log(item.svc_name)
            self.query_one("#status-bar", StatusBar).update_svc(item.svc_info)

    @on(ListView.Highlighted)
    def _on_list_highlighted(self, event: ListView.Highlighted) -> None:
        item = event.item
        if isinstance(item, ServiceItem):
            self._switch_log(item.svc_name)
            self.query_one("#status-bar", StatusBar).update_svc(item.svc_info)

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
                seed = f.read().splitlines()
                for line in seed:
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
            # Poll until the log file appears, then switch to tailing it
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
        lv = self.query_one("#service-list", ListView)
        item = lv.highlighted_child
        if isinstance(item, ServiceItem):
            return item.svc_name
        return None

    def _selected_info(self) -> dict | None:
        lv = self.query_one("#service-list", ListView)
        item = lv.highlighted_child
        if isinstance(item, ServiceItem):
            return item.svc_info
        return None

    def action_service_start(self) -> None:
        if name := self._selected_name():
            resp = daemon_request({"action": "start", "name": name})
            if resp is None:
                self.notify("Could not reach daemon.", severity="error")
            else:
                results = resp.get("results", [])
                if results:
                    r = results[0]
                    self.notify(r["msg"], severity="information" if r["ok"] else "warning")
            self._poll_status()

    def action_service_stop(self) -> None:
        if name := self._selected_name():
            resp = daemon_request({"action": "stop", "name": name})
            if resp is None:
                self.notify("Could not reach daemon.", severity="error")
            else:
                results = resp.get("results", [])
                if results:
                    r = results[0]
                    self.notify(r["msg"], severity="information" if r["ok"] else "warning")
            self._poll_status()

    def action_service_restart(self) -> None:
        if name := self._selected_name():
            resp = daemon_request({"action": "restart", "name": name})
            if resp is None:
                self.notify("Could not reach daemon.", severity="error")
            else:
                results = resp.get("results", [])
                if results:
                    r = results[0]
                    self.notify(r["msg"], severity="information" if r["ok"] else "warning")
            self._poll_status()


def run_tui() -> None:
    if not daemon_running():
        import sys
        print("[error] Daemon is not running. Run: svcctl start all", file=sys.stderr)
        sys.exit(1)
    SvcctlApp().run()
