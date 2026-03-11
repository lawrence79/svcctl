import json
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

from .config import get_config_path, get_pid_file, get_runtime_dir, get_socket_path


def send_msg(sock: socket.socket, obj: dict) -> None:
    data = json.dumps(obj).encode()
    sock.sendall(struct.pack(">I", len(data)) + data)


_MAX_MSG_BYTES = 1 * 1024 * 1024  # 1 MB


def recv_msg(sock: socket.socket) -> dict | None:
    raw = _recv_exact(sock, 4)
    if not raw:
        return None
    length = struct.unpack(">I", raw)[0]
    if length > _MAX_MSG_BYTES:
        raise ValueError(f"Incoming message too large: {length} bytes")
    data = _recv_exact(sock, length)
    if not data:
        return None
    return json.loads(data)


def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def daemon_request(cmd: dict) -> dict | None:
    sp = get_socket_path()
    if not sp.exists():
        return None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(str(sp))
        try:
            send_msg(s, cmd)
            return recv_msg(s)
        finally:
            s.close()
    except Exception:
        return None


def daemon_running() -> bool:
    pid_file = get_pid_file()
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError, FileNotFoundError, PermissionError):
        return False


def ensure_daemon() -> None:
    if daemon_running():
        return
    config_path = get_config_path().resolve()
    env = os.environ.copy()
    env["SVCCTL_CONFIG"] = str(config_path)
    env["SVCCTL_RUNTIME"] = str(get_runtime_dir())
    subprocess.Popen(
        [sys.executable, "-m", "svcctl", "daemon", "--config", str(config_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
        env=env,
    )
    for _ in range(20):
        time.sleep(0.2)
        if daemon_running():
            return
    print("[error] Daemon failed to start.", file=sys.stderr)
    sys.exit(1)
