# CLAUDE.md

This file provides guidance to AI agents working with this repository.

## Project Overview

**svcctl** is a lightweight multi-app service manager for local development. It runs a short-lived daemon (alive only while the TUI is open) that keeps services alive, with a Textual-based TUI for monitoring and control.

## Setup & Installation

```sh
python -m venv .venv
.venv/bin/pip install -e .
export PATH="$HOME/projects/svcctl/.venv/bin:$PATH"
```

There are no test suites, lint configs, or Makefiles in this project.

## Critical Operational Notes

- **Always set `SVCCTL_CONFIG`** before running. If it is not set, `config.py` falls back to `CWD/services.yaml` then `~/.svcctl/services.yaml`. A stale daemon started from the wrong CWD will load zero services and show an empty TUI with no error.
  ```sh
  export SVCCTL_CONFIG=~/projects/svcctl/services.yaml
  ```
- **The daemon is not a persistent background service.** `run_tui()` starts it via `ensure_daemon()` and kills it via `stop_daemon()` (SIGTERM) when the TUI exits. Do not expect the daemon to survive a TUI quit.
- **Killing a stale daemon:** `kill $(cat ~/.svcctl/daemon.pid)` then relaunch the TUI.

## Architecture

**Daemon-Client model via Unix socket IPC:**

1. `__main__.py` — Entry point. Routes the `daemon` subcommand to `Daemon(...).run()`; all other invocations call `run_tui()`.
2. `daemon.py` — `Daemon` class. Loads YAML config at startup, manages `ServiceProcess` objects, listens on `~/.svcctl/daemon.sock`. Handles SIGTERM/SIGINT by stopping all services then exiting.
3. `service.py` — `ServiceProcess` class. Spawns subprocess via `shell=True, start_new_session=True` (own process group for `killpg`). Uses generation counters to prevent stale watch threads from re-starting after an intentional stop. Each service runs two daemon threads: `_pipe_writer` (stdout → log file) and `_watch` (process exit → auto-restart).
4. `ipc.py` — JSON messages with 4-byte big-endian length prefix. `daemon_request()` is synchronous client call (5s timeout). `ensure_daemon()` spawns daemon if not running; `stop_daemon()` sends SIGTERM.
5. `config.py` — Resolves config path: `$SVCCTL_CONFIG` → `CWD/services.yaml` → `~/.svcctl/services.yaml`. Provides `get_runtime_dir()`, `get_socket_path()`, `get_pid_file()`, `get_log_dir()`, `load_config_raw()`, `save_config()`.
6. `tui.py` — Textual app. Polls daemon every 2s. Uses `batch_update()` for all table mutations to avoid flicker. Key bindings: `a` start, `s` stop, `r` restart, `n` add, `d` delete, `f` toggle follow, `c` clear log, `q` quit.
7. `utils.py` — `fmt_uptime(seconds)` → human-readable string.

**Runtime files:** `~/.svcctl/daemon.pid`, `~/.svcctl/daemon.sock`, `~/.svcctl/logs/<name>.log`

## Threading Model

Each running service has:

- **`_pipe_writer` thread** — reads `proc.stdout` line-by-line, writes timestamped lines to the log file, flushes every 100ms or 20 lines.
- **`_watch` thread** — blocks on `proc.wait()`, then either marks service stopped (if `_stop_flag`) or auto-restarts after `restart_delay` seconds. Generation counter (`_gen`) prevents a stale watch thread from restarting a service that has already been explicitly stopped or restarted.

The `Daemon` runs a `ThreadPoolExecutor(max_workers=12)` for IPC handlers. The `_lock` on both `Daemon` and `ServiceProcess` protects state mutations. Long-blocking operations (stop, restart) are performed outside the lock or in spawned threads to avoid blocking IPC polling.

## IPC Protocol

Messages: JSON object with 4-byte big-endian length prefix. Max receive size: 1 MB.

| Action    | Request keys                                             | Response keys                                                             |
| --------- | -------------------------------------------------------- | ------------------------------------------------------------------------- |
| `status`  | —                                                        | `services: [{name, status, pid, uptime, restart_count}]`, `revision: int` |
| `start`   | `name` (or `"all"`)                                      | `results: [{ok, msg}]`                                                    |
| `stop`    | `name` (or `"all"`)                                      | `results: [{ok, msg}]`                                                    |
| `restart` | `name` (or `"all"`)                                      | `results: [{ok, msg}]`                                                    |
| `ping`    | —                                                        | `{pong: true}`                                                            |
| `reload`  | —                                                        | `{ok, added, removed, updated}`                                           |
| `add`     | `name`, `entry: {dir, cmd, auto_restart, restart_delay}` | `{ok}` or `{ok: false, error}`                                            |
| `remove`  | `name`                                                   | `{ok}` or `{ok: false, error}`                                            |

The `revision` integer on `status` is incremented whenever the service list or any status/pid/restart_count changes. The TUI skips a full table re-render when revision is unchanged (uptime-only update path).

## Config Format

```yaml
root: ~/projects # optional; all service dirs resolved relative to this

services:
  api:
    dir: api # relative to root (or services.yaml dir if root not set)
    cmd: yarn start
    env_file: .env # optional; .env file relative to dir
    env: # optional; list of KEY=VALUE pairs (merged over env_file)
      - NODE_ENV=development
    auto_restart: true # default: true
    restart_delay: 2 # seconds; default: 2
    use_nvm: true # optional; if omitted, auto-detected via .nvmrc in dir
```

`add` and `remove` IPC actions write back to `config_path` using `yaml.dump`. Field order may not be preserved.

## Service Lifecycle

```
start() ──► _do_start() ──► Popen ─┬─► _pipe_writer thread (stdout → log)
                                    └─► _watch thread ──► proc.wait()
                                                              │
                                          _stop_flag=True ◄──┤── stop()
                                                              │
                                          auto_restart ───────┘── delay → _do_start()
```

`stop()` sends SIGTERM to the process group (`killpg`), then uses `psutil` to catch orphaned child processes (e.g. NX daemon that called `setsid`). Escalates to SIGKILL after 5s.

## Known Constraints & Anti-patterns

- **Log files grow unless `max_log_bytes` is set.** Without it, logs in `~/.svcctl/logs/` are truncated only on each service start. Set `max_log_bytes: 10485760` (10 MB) in your config to cap size.
- **Restart backoff is enabled by default but uncapped at 60s.** A crashing service backs off exponentially (`restart_delay * 2^n`, max 60s). Set `max_restarts: N` to stop after N failures. Reset by stopping and starting the service manually.
- **No config file watching.** Changes to `services.yaml` while the TUI is running do NOT take effect automatically. Use the `reload` IPC action (not yet exposed in TUI) or restart the TUI.
- **`yaml.dump` on save** normalises the config (may reorder keys, change quoting). Do not rely on formatting being preserved after `add`/`remove`.
- **`shell=True` subprocess.** Commands run via `bash -c`. Complex quoting in `cmd` can cause issues.
