"""CLI entry point for svcctl."""
import sys
import time
from pathlib import Path

import click

from .config import get_config_path, get_log_dir, load_config_raw, save_config
from .ipc import daemon_request, daemon_running, ensure_daemon
from .utils import fmt_uptime

STARTER_CONFIG = """\
# svcctl — services configuration

root: ~/projects

services:
  api:
    dir: api
    cmd: ./run.sh
    auto_restart: true
    restart_delay: 2
"""


@click.group()
def cli() -> None:
    """svcctl — multi-app service manager"""


@cli.command()
def init() -> None:
    """Generate a starter services.yaml in the current directory."""
    p = Path("services.yaml")
    if p.exists():
        click.echo("[warn] services.yaml already exists — not overwriting.")
        return
    p.write_text(STARTER_CONFIG)
    click.echo(f"[ok] Created {p.resolve()}")
    click.echo("     Edit it to match your apps, then run: svcctl start all")


@cli.command()
@click.argument("name", default="all")
def start(name: str) -> None:
    """Start one or all services."""
    ensure_daemon()
    resp = daemon_request({"action": "start", "name": name})
    if not resp:
        click.echo("[error] Could not reach daemon.", err=True)
        raise SystemExit(1)
    for r in resp.get("results", []):
        icon = "✓" if r["ok"] else "✗"
        click.echo(f"  {icon}  {r['msg']}")


@cli.command()
@click.argument("name", default="all")
def stop(name: str) -> None:
    """Stop one or all services."""
    if not daemon_running():
        click.echo("[info] Daemon is not running.")
        return
    resp = daemon_request({"action": "stop", "name": name})
    if not resp:
        click.echo("[error] Could not reach daemon.", err=True)
        raise SystemExit(1)
    for r in resp.get("results", []):
        icon = "✓" if r["ok"] else "✗"
        click.echo(f"  {icon}  {r['msg']}")


@cli.command()
@click.argument("name")
def restart(name: str) -> None:
    """Restart a service."""
    ensure_daemon()
    resp = daemon_request({"action": "restart", "name": name})
    if not resp:
        click.echo("[error] Could not reach daemon.", err=True)
        raise SystemExit(1)
    for r in resp.get("results", []):
        icon = "✓" if r["ok"] else "✗"
        click.echo(f"  {icon}  {r['msg']}")


@cli.command()
def status() -> None:
    """Show status of all services."""
    if not daemon_running():
        click.echo("  daemon  stopped")
        return
    resp = daemon_request({"action": "status"})
    if not resp:
        click.echo("[error] No response from daemon.", err=True)
        raise SystemExit(1)

    svcs = resp.get("services", [])
    if not svcs:
        click.echo("  (no services)")
        return

    name_w = max(len(s["name"]) for s in svcs)
    click.echo(
        f"\n  {'NAME':<{name_w}}  {'STATUS':<10}  {'PID':<8}  {'UPTIME':<12}  RESTARTS"
    )
    click.echo(f"  {'─' * name_w}  {'─' * 10}  {'─' * 8}  {'─' * 12}  ────────")
    for s in svcs:
        st = s["status"]
        color = {"running": "green", "stopped": "white", "crashed": "red"}.get(st, "white")
        st_col = click.style(f"{st:<10}", fg=color)
        pid_str = str(s["pid"]) if s["pid"] else "—"
        up_str = fmt_uptime(s["uptime"]) if s["uptime"] is not None else "—"
        click.echo(
            f"  {s['name']:<{name_w}}  {st_col}  {pid_str:<8}  {up_str:<12}  {s['restart_count']}"
        )
    click.echo()


@cli.command()
@click.argument("name")
@click.option("-f", "--follow", is_flag=True, help="Follow log output.")
@click.option("-n", "--lines", default=50, show_default=True, type=click.IntRange(min=1), help="Lines to show.")
def logs(name: str, follow: bool, lines: int) -> None:
    """Show logs for a service (colorized)."""
    log_path = get_log_dir() / f"{name}.log"
    if not log_path.exists():
        click.echo(f"[error] No log file for '{name}'", err=True)
        click.echo(f"        Expected: {log_path}", err=True)
        raise SystemExit(1)

    def emit(line: str) -> None:
        line = line.rstrip("\n")
        if "[svcctl]" in line or ("─" * 10) in line:
            click.echo(click.style(line, fg="cyan", bold=True))
        else:
            click.echo(line)

    try:
        with open(log_path, "r", errors="replace") as f:
            all_lines = f.readlines()
            for line in all_lines[-lines:]:
                emit(line)
            if follow:
                while True:
                    line = f.readline()
                    if line:
                        emit(line)
                    else:
                        time.sleep(0.05)
    except KeyboardInterrupt:
        pass


@cli.command()
def ui() -> None:
    """Interactive TUI — live logs and service controls."""
    from .tui import run_tui
    run_tui()


def _pick_directory(initialdir: str | None = None, title: str = "Select service directory") -> str | None:
    """Open a native folder-picker dialog. Returns the chosen path or None."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", True)
        path = filedialog.askdirectory(title=title, initialdir=initialdir or Path.home())
        root.destroy()
        return path or None
    except Exception:
        return None


def _get_root(cfg: dict) -> Path | None:
    raw = cfg.get("root")
    return Path(raw).expanduser().resolve() if raw else None


@cli.command(name="add")
@click.argument("name")
@click.option("--dir", "svc_dir", default=None, help="Directory for the service (opens picker if omitted).")
@click.option("--cmd", default=None, help="Command to run (default: ./run.sh).")
@click.option("--env-file", default=None, help="Path to env file (relative to dir).")
@click.option("--no-auto-restart", is_flag=True, default=False, help="Disable auto-restart.")
@click.option("--restart-delay", default=2, show_default=True, type=int, help="Restart delay in seconds.")
def add_service(name: str, svc_dir: str | None, cmd: str | None, env_file: str | None, no_auto_restart: bool, restart_delay: int) -> None:
    """Add a new service to services.yaml and start it."""
    cfg = load_config_raw()
    root = _get_root(cfg)

    if not svc_dir:
        picked = _pick_directory(initialdir=str(root) if root else None)
        if not picked:
            click.echo("[error] No directory selected.", err=True)
            raise SystemExit(1)
        picked_path = Path(picked).resolve()
        if root and picked_path.is_relative_to(root):
            svc_dir = str(picked_path.relative_to(root))
        else:
            svc_dir = str(picked_path)
        click.echo(f"  dir  {svc_dir}")

    if name in cfg["services"]:
        click.echo(f"[error] Service '{name}' already exists.", err=True)
        raise SystemExit(1)

    entry: dict = {"dir": svc_dir, "cmd": cmd or "./run.sh", "auto_restart": not no_auto_restart, "restart_delay": restart_delay}
    if env_file:
        entry["env_file"] = env_file
    cfg["services"][name] = entry
    save_config(cfg)
    if daemon_running():
        resp = daemon_request({"action": "reload"})
        if resp and resp.get("ok"):
            click.echo(f"  ✓  Added and started '{name}'")
        else:
            click.echo(f"  ✓  Added '{name}' to config (daemon reload failed: {resp})", err=True)
    else:
        ensure_daemon()
        click.echo(f"  ✓  Added '{name}' and started daemon")


@cli.command(name="remove")
@click.argument("name")
def remove_service(name: str) -> None:
    """Remove a service from services.yaml and stop it."""
    if daemon_running():
        resp = daemon_request({"action": "remove", "name": name})
        if not resp:
            click.echo("[error] Could not reach daemon.", err=True)
            raise SystemExit(1)
        if resp.get("ok"):
            click.echo(f"  ✓  Stopped and removed '{name}'")
        else:
            click.echo(f"[error] {resp.get('error', 'Unknown error')}", err=True)
            raise SystemExit(1)
    else:
        cfg = load_config_raw()
        if name not in cfg["services"]:
            click.echo(f"[error] Service '{name}' not found in config.", err=True)
            raise SystemExit(1)
        del cfg["services"][name]
        save_config(cfg)
        click.echo(f"  ✓  Removed '{name}' from config (daemon not running)")


@cli.command(name="reload")
def reload_cmd() -> None:
    """Reload services.yaml in the running daemon."""
    if not daemon_running():
        click.echo("[info] Daemon is not running.")
        return
    resp = daemon_request({"action": "reload"})
    if not resp:
        click.echo("[error] Could not reach daemon.", err=True)
        raise SystemExit(1)
    if not resp.get("ok"):
        click.echo(f"[error] Reload failed: {resp.get('error')}", err=True)
        raise SystemExit(1)
    added = resp.get("added", [])
    removed = resp.get("removed", [])
    click.echo(f"  ✓  Reloaded — added: {added or '(none)'}, removed: {removed or '(none)'}")


@cli.command(hidden=True)
@click.option("--config", default=None)
def daemon(config: str | None) -> None:
    """Run the background daemon (internal use)."""
    from .daemon import Daemon
    config_path = config or str(get_config_path())
    Daemon(config_path).run()


def main() -> None:
    cli()
