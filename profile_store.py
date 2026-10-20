"""Validated, atomic profile storage with a last-good backup."""
import json
import os
import tempfile
from pathlib import Path


def validate_profiles(data):
    if not isinstance(data, dict) or data.get("release") != 2:
        raise ValueError("Unsupported profile format (expected release 2)")
    if not isinstance(data.get("global"), dict) or not isinstance(data.get("profiles"), list):
        raise ValueError("Invalid profile structure")
    names = set()
    for profile in data["profiles"]:
        if not isinstance(profile, dict) or not isinstance(profile.get("name"), str):
            raise ValueError("Invalid profile name")
        if profile["name"] in names or not isinstance(profile.get("data"), list):
            raise ValueError("Duplicate profile name or invalid settings")
        names.add(profile["name"])
        for item in profile["data"]:
            if not isinstance(item, dict) or not all(k in item for k in ("fullname", "cls", "instance", "cmd", "value")):
                raise ValueError("Invalid profile parameter")
            if not all(isinstance(item[k], str) for k in ("fullname", "cls", "cmd")):
                raise ValueError("Invalid parameter name")
            if type(item["instance"]) is not int or not 0 <= item["instance"] <= 255:
                raise ValueError("Invalid parameter instance")
            if not isinstance(item["value"], (str, int, float)):
                raise ValueError("Invalid parameter value")
    return data


def atomic_write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, str(path))
        if os.name == "posix":
            directory = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def read_profiles(path):
    return validate_profiles(json.loads(Path(path).read_text(encoding="utf-8")))


def load_profiles(path):
    """Return (data, recovered). Preserve damaged files if recovery is impossible."""
    try:
        return read_profiles(path), False
    except (OSError, ValueError, TypeError) as original:
        try:
            data = read_profiles(str(path) + ".bak")
        except (OSError, ValueError, TypeError):
            raise ValueError("Profiles could not be loaded; original and backup preserved: " + str(original))
        # Preserve the damaged original for manual recovery before restoring.
        if Path(path).exists():
            atomic_write(str(path) + ".corrupt", Path(path).read_bytes())
        atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
        return data, True


def save_profiles(path, data):
    payload = json.dumps(validate_profiles(data), ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    if Path(path).exists():
        # Never replace a good backup with a truncated or invalid original.
        read_profiles(path)
        atomic_write(str(path) + ".bak", Path(path).read_bytes())
    atomic_write(path, payload)
