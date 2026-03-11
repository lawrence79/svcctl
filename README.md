# svcctl

A lightweight multi-app service manager for local development. Runs a background daemon that keeps your services alive, with a CLI for control and a terminal TUI for monitoring.

## Team Installation

Requires Python 3.11+ and either [pipx](https://pipx.pypa.io) or [uv](https://docs.astral.sh/uv/).

```sh
# pipx (SSH access to repo required)
pipx install git+ssh://git@github.com/your-org/svcctl.git

# uv (faster)
uv tool install git+ssh://git@github.com/your-org/svcctl.git
```

To upgrade:

```sh
pipx upgrade svcctl
# or
uv tool upgrade svcctl
```

Copy `services.example.yaml` to `~/.svcctl/services.yaml` and edit it to describe your services.

## Installation (local dev)

```sh
cd ~/projects/svcctl
python -m venv .venv
.venv/bin/pip install -e .
```

Add to your shell so `svcctl` is on your PATH:

```sh
export PATH="$HOME/projects/svcctl/.venv/bin:$PATH"
```

## Configuration

svcctl looks for `~/.svcctl/services.yaml` by default. Generate a starter file:

```sh
svcctl init
```

Edit it to describe your services:

```yaml
services:
  api:
    dir: ./api          # relative to services.yaml
    cmd: yarn start
    env_file: .env      # optional; loaded relative to dir
    auto_restart: true
    restart_delay: 2    # seconds before restart after crash

  web:
    dir: ./web
    cmd: yarn dev
    auto_restart: true
```

Each service requires `dir` and `cmd`. All other keys are optional.

To use a different config file, set `SVCCTL_CONFIG`:

```sh
SVCCTL_CONFIG=/path/to/services.yaml svcctl start all
```

## Usage

### Start services

```sh
svcctl start           # start all services
svcctl start api       # start one service
```

The daemon starts automatically on the first `start` if it isn't already running.

### Stop services

```sh
svcctl stop            # stop all services
svcctl stop api        # stop one service
```

### Restart

```sh
svcctl restart api
```

### Status

```sh
svcctl status
```

```
  NAME   STATUS      PID       UPTIME        RESTARTS
  ─────  ──────────  ────────  ────────────  ────────
  api    running     12345     2h 14m        0
  web    running     12346     2h 14m        1
```

### Logs

```sh
svcctl logs api             # last 50 lines
svcctl logs api -n 100      # last 100 lines
svcctl logs api -f          # follow (like tail -f)
```

svcctl-generated lines (start/stop events) are highlighted in cyan. Press `Ctrl+C` to exit follow mode.

Log files are written to `~/.svcctl/logs/<name>.log`.

### TUI

```sh
svcctl ui
```

An interactive terminal UI showing all services and live logs side by side.

| Key | Action |
|-----|--------|
| `↑` / `↓` | Select service |
| `a` | Start selected service |
| `s` | Stop selected service |
| `r` | Restart selected service |
| `q` | Quit |

## Daemon

The daemon runs as a background process and manages service lifecycles. It communicates with the CLI via a Unix socket at `~/.svcctl/daemon.sock`.

The daemon starts automatically when you run `svcctl start`. To stop it, send SIGTERM — it will stop all managed services cleanly before exiting:

```sh
kill $(cat ~/.svcctl/daemon.pid)
```

Runtime files:

| Path | Purpose |
|------|---------|
| `~/.svcctl/daemon.pid` | Daemon PID |
| `~/.svcctl/daemon.sock` | IPC socket |
| `~/.svcctl/logs/<name>.log` | Per-service log |

## Performance Notes

- The daemon now handles IPC requests with a bounded worker pool, which avoids unbounded thread growth during bursts of client polling.
- Status responses include a structural revision marker; the TUI uses it to skip expensive full table refreshes when only uptime changed.
- Service log forwarding batches flushes in short intervals instead of flushing every line, reducing I/O overhead for noisy services while keeping logs near-real-time.
