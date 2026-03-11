import os
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


def _read_yaml(path: Path) -> dict | None:
    import yaml
    if not path.exists():
        return None
    with open(path) as f:
        return yaml.safe_load(f)


def load_config() -> dict:
    path = get_config_path()
    data = _read_yaml(path)
    if data is None:
        raise FileNotFoundError(f"No services.yaml found at {path}")
    return data


def load_config_raw() -> dict:
    path = get_config_path()
    data = _read_yaml(path)
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
