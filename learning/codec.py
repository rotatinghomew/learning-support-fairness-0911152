import hashlib
import json
import math


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def numeric(value, field):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError(f"{field} 不是有限数值")
    return float(value)


def unique(rows, fields):
    seen = set()
    for row in rows:
        key = tuple(row[field] for field in fields)
        if key in seen:
            raise ValueError(f"重复业务键：{fields} {key}")
        seen.add(key)


def require_fields(rows, fields):
    for row in rows:
        missing = set(fields) - row.keys()
        if missing:
            raise ValueError(f"缺少字段：{sorted(missing)}")
