# BACKLOG

Prioritized list of known issues and future work. Update this when work is completed or new issues are found.

---

## High — Real bugs / user-facing risk

### ~~H1 · Double-daemon spawn race in `ensure_daemon()`~~ ✅ DONE

**Implemented:** `fcntl.flock` on `daemon.lock` serializes concurrent callers. Re-check inside lock before spawning.

### ~~H2 · `ensure_daemon()` does not verify the running process is actually `svcctl`~~ ✅ DONE

**Implemented:** `daemon_running()` now calls `psutil.Process(pid).cmdline()` and checks for `svcctl` or `__main__` in the args.

### H3 · Daemon starts silently with zero services when config is missing

**File:** `daemon.py` `_parse_config()` ~line 31  
**Description:** If `config_path` doesn't exist, `_parse_config()` returns `{}` without any log output or error. The daemon runs normally but the TUI shows an empty service list with no explanation.  
**Fix direction:** Log a warning to stderr when the config file is not found at startup. Optionally surface it as a TUI notification.

### H4 · Log file not re-opened after external rotation

**File:** `tui.py` `_tail_log()` ~line 300  
**Description:** If a log file is externally rotated (moved aside, new file created), the tail thread continues reading from the old inode. New output never appears.  
**Fix direction:** Periodically stat the log path; if inode changes, re-open and restart tailing. Alternatively, use `watchfiles` or similar.

---

## Medium — Edge cases / robustness

### M1 · No validation that `dir` exists when adding a service

**File:** `tui.py` `AddServiceModal` ~line 120  
**Description:** The "Add" form checks that name/dir/cmd are non-empty but doesn't verify the directory exists. The daemon will fail silently when it tries to `chdir` before spawning.  
**Fix direction:** Check `Path(dir).expanduser().exists()` in the modal before sending the IPC request; show an inline validation error.

### M2 · `_do_add()` window between config write and in-memory update

**File:** `daemon.py` `_do_add()` ~line 245  
**Description:** After writing the service to disk, `_parse_config()` is called outside the lock to resolve the full config. A concurrent `reload` action in another thread could read the same config and attempt to create the same service. The second lock-check at line 259 catches this, but a duplicate-service error is returned to the original caller rather than it being treated as a no-op.  
**Fix direction:** Low-risk as the second lock-check guards state correctly; document this as expected behavior.

### ~~M3 · Daemon signal handler blocks on service shutdown~~ ✅ DONE

**Implemented:** `_shutdown()` now stops all services in parallel via `ThreadPoolExecutor` with a 10s total timeout.

### M4 · `_watch` thread writes to `log_f` after auto-restart replaces it

**File:** `service.py` `_watch()` ~line 242  
**Description:** After `proc.wait()` returns, `_watch` grabs the lock and writes an exit/crash message to `log_f` (the file handle it was given at start). If `gen == self._gen` and auto-restart triggers `_do_start()`, the new start creates a new `log_f`. The exit message is written to the old handle (now closed). This is safe — `close()` flushes and subsequent writes raise, caught by the `except` block — but the exit message may not appear in the new log session.  
**Fix direction:** Write the exit message before closing the old handle in `_do_start()`, or pass the exit message to the new log session.

### M5 · Env file comment stripping breaks values containing ` #`

**File:** `service.py` `_build_env()` ~line 165  
**Description:** Unquoted env values are stripped of inline comments with `v.split(" #")[0]`. A value like `DATABASE_URL=postgres://host/db#fragment` would be truncated.  
**Fix direction:** Only strip inline comments that are preceded by whitespace AND where the `#` is not inside a URL-like token. Or require quoting for values containing `#`.

### M6 · IPC handler pool saturation

**File:** `daemon.py` ~line 22  
**Description:** `ThreadPoolExecutor(max_workers=12)` means at most 12 concurrent IPC handlers. A slow `stop()` (blocking up to 10s) ties up a worker. The TUI polls every 2s; under normal use this is fine, but a pathological sequence of stop/restart requests could starve status polls.  
**Fix direction:** Run stop/restart in a dedicated background thread from the handler (already done for remove), returning immediately to the caller. Or increase pool size and document the limit.

---

## Low — Polish / nice-to-have

### ~~L1 · No restart backoff~~ ✅ DONE

**Implemented:** Exponential backoff: `min(restart_delay * 2^crash_streak, 60)`. Streak resets if service ran ≥ 10s. New config key `max_restarts` (0 = unlimited) stops the service after N consecutive crashes.

### ~~L2 · No log rotation~~ ✅ DONE

**Implemented:** New config key `max_log_bytes`. If set and the log file exceeds the limit, it is deleted before the next service start. The TUI tail handles `FileNotFoundError` gracefully.

### L3 · `reload` not exposed in the TUI

**File:** `tui.py`  
**Description:** The `reload` IPC action exists but there is no key binding in the TUI. Users must restart the TUI to pick up config changes.  
**Fix direction:** Add a `reload` key binding (e.g. `R` or `ctrl+r`) that sends the `reload` IPC action and shows a notification with the result (added/removed/updated counts).

### L4 · `yaml.dump` on add/remove re-orders config keys

**File:** `daemon.py` `_do_add()` / `_do_remove()`  
**Description:** Round-tripping through `yaml.dump` loses key ordering and may change quoting style.  
**Fix direction:** Use `ruamel.yaml` for round-trip-safe YAML parsing that preserves formatting.

### L5 · No env-var expansion in config values

**File:** `daemon.py` `_parse_config()`  
**Description:** Values like `cmd: $HOME/bin/start.sh` are passed literally to the shell, which expands them. But `dir` is resolved via `Path`, which doesn't expand `$VAR` — only `~`. Users may expect `dir: $HOME/projects/api` to work.  
**Fix direction:** Call `os.path.expandvars()` on `dir` values before `Path.resolve()`.

### L6 · TUI action keys not debounced

**File:** `tui.py` action handlers  
**Description:** Pressing `a`/`s`/`r` rapidly sends multiple IPC requests. The daemon is idempotent (starting an already-running service returns an error), so no corruption occurs, but it produces noise.  
**Fix direction:** Track the last-request timestamp per service and ignore rapid duplicates.

### L7 · `use_nvm` auto-detection is file-existence only

**File:** `service.py` `_build_cmd()` ~line 133  
**Description:** `use_nvm` is inferred from whether `.nvmrc` exists in the service dir. If `.nvmrc` exists but `nvm` is not installed, the service fails to start with a confusing error.  
**Fix direction:** Check `NVM_DIR` is set and `nvm.sh` exists; fall back gracefully with a warning in the log.
