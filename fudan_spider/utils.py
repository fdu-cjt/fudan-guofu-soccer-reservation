"""Small utilities shared by the reservation clients."""
import json
import os
from datetime import timedelta, timezone
from pathlib import Path

SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")


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
