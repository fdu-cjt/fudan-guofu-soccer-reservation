"""Small utilities shared by the reservation clients."""
import json
import os
from datetime import timedelta, timezone

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
