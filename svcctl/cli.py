"""CLI entry point for svcctl."""
import sys
import time
from pathlib import Path

import click

from .config import get_config_path, get_log_dir
from .ipc import daemon_request, daemon_running, ensure_daemon
from .utils import fmt_uptime

STARTER_CONFIG = """\
# svcctl — services configuration

services:
  api:
    dir: ./apps/api
    cmd: yarn start
    env_file: .env
    auto_restart: true
    restart_delay: 2

  web:
    dir: ./apps/web
    cmd: yarn dev
    env_file: .env
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


@cli.command(hidden=True)
@click.option("--config", default=None)
def daemon(config: str | None) -> None:
    """Run the background daemon (internal use)."""
    from .daemon import Daemon
    config_path = config or str(get_config_path())
    Daemon(config_path).run()


def main() -> None:
    cli()
