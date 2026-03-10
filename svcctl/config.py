import os
import sys
from pathlib import Path


def get_config_path() -> Path:
    if "SVCCTL_CONFIG" in os.environ:
        return Path(os.environ["SVCCTL_CONFIG"])
    cwd_config = Path.cwd() / "services.yaml"
    if cwd_config.exists():
        return cwd_config
    return Path.home() / ".svcctl" / "services.yaml"


def get_runtime_dir() -> Path:
    d = Path(os.environ.get("SVCCTL_RUNTIME", Path.home() / ".svcctl"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_socket_path() -> Path:
    return get_runtime_dir() / "daemon.sock"


def get_pid_file() -> Path:
    return get_runtime_dir() / "daemon.pid"


def get_log_dir() -> Path:
    d = get_runtime_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_config() -> dict:
    import yaml
    path = get_config_path()
    if not path.exists():
        print(f"[error] No services.yaml found at {path}", file=sys.stderr)
        print("        Run 'svcctl init' to generate one.", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f)


def load_config_raw() -> dict:
    import yaml
    path = get_config_path()
    if not path.exists():
        return {"services": {}}
    with open(path) as f:
        data = yaml.safe_load(f)
    if not data:
        return {"services": {}}
    if "services" not in data:
        data["services"] = {}
    return data


def save_config(cfg: dict) -> None:
    import yaml
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
