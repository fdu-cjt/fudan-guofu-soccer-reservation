"""Small utilities shared by the reservation clients."""
import json
import os
import subprocess
from datetime import timedelta, timezone
from pathlib import Path

SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")


def git_version():
    """Identify the code being run without exposing changed filenames or requiring Git."""
    options = dict(cwd=Path(__file__).resolve().parents[1], text=True, encoding="utf-8",
                   stderr=subprocess.DEVNULL, timeout=3,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], **options).strip()
        status = subprocess.check_output(
            ["git", "status", "--porcelain=v1", "--branch", "--untracked-files=normal"], **options).splitlines()
        return {"branch": status[0][3:].split("...")[0], "commit": commit, "dirty": len(status) > 1}
    except (OSError, subprocess.SubprocessError, IndexError, UnicodeError):
        return {"branch": "unknown", "commit": "unknown", "dirty": None}


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as out:
        json.dump(value, out, ensure_ascii=False)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)
    if os.name != "nt":
        path.chmod(0o600)


def load_env_file(path):
    """Read simple KEY=VALUE settings without turning the file into a shell script."""
    values = {}
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return values
    for line in raw.splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        name, value = text.split("=", 1)
        values[name.strip().upper()] = value.strip().strip("'\"")
    return values
