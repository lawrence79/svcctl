# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**svcctl** is a lightweight multi-app service manager for local development. It runs a background daemon that keeps services alive, with a Textual-based TUI for monitoring.

## Setup & Installation

```sh
python -m venv .venv
.venv/bin/pip install -e .
export PATH="$HOME/projects/svcctl/.venv/bin:$PATH"
```

There are no test suites, lint configs, or Makefiles in this project.

## Architecture

**Daemon-Client model via Unix socket IPC:**

1. `__main__.py` — Entry point (`svcctl.__main__:main`). Handles the `daemon` subcommand directly; all other invocations launch the TUI.
2. `daemon.py` — Long-running background process. Loads YAML config, instantiates `ServiceProcess` objects, listens on `~/.svcctl/daemon.sock` for JSON IPC requests.
3. `service.py` — `ServiceProcess` class. Spawns subprocess per service with `start_new_session=True` (for `killpg`), monitors with threads, implements auto-restart with generation tracking to prevent stale watch threads.
4. `ipc.py` — JSON messages with 4-byte length prefix. `daemon_request()` is the client-side call; `ensure_daemon()` spawns the daemon if not running.
5. `config.py` — Resolves config from `SVCCTL_CONFIG` env var → CWD → `~/projects/services.yaml`. Provides all runtime path helpers.
6. `tui.py` — Textual-based TUI. Polls daemon every 2s for status. Keys: `a` start, `s` stop, `r` restart, `q` quit.
7. `utils.py` — `fmt_uptime()` for human-readable uptime strings.

**Runtime files:** `~/.svcctl/daemon.pid`, `~/.svcctl/daemon.sock`, `~/.svcctl/logs/<name>.log`

## Config Format

```yaml
root: ~/projects # optional; all service dirs resolved relative to this

services:
  api:
    dir: api # relative to root (or services.yaml if root not set)
    cmd: yarn start
    env_file: .env # optional; relative to dir
    auto_restart: true # default: true
    restart_delay: 2 # seconds; default: 2
```

## IPC Protocol

Messages are JSON with a 4-byte big-endian length prefix. Requests include an `action` key (`start`, `stop`, `restart`, `status`, `ping`) and optional `name`. Daemon replies with a `status` key and action-specific payload.
