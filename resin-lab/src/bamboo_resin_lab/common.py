"""Small, independent readers; never import or change the live runtime."""
import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def read(path):
    def invalid(_):
        raise ValueError("Non-finite JSON")
    return json.loads(Path(path).read_text(encoding="utf-8-sig"), parse_constant=invalid)


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def is_hash(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def stamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Timezone required")
    return parsed


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def artifact(root, relative, expected):
    """Reject traversal, links and alternate streams; hash the original bytes."""
    if not isinstance(relative, str) or not is_hash(expected):
        return False
    parts = relative.split("/")
    if any(not p or p in {".", ".."} or p.endswith((".", " ")) or
           re.search(r'[\\:\x00-\x1f]', p) for p in parts):
        return False
    root = Path(root).resolve()
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root):
        return False
    if any(root.joinpath(*parts[:i]).is_symlink() for i in range(1, len(parts) + 1)):
        return False
    try:
        return path.is_file() and file_hash(path) == expected
    except OSError:
        return False


def fingerprint():
    return digest({p.name: file_hash(p) for p in sorted(Path(__file__).parent.glob("*.py"))})


def write_new(path, value, *, protected=()):
    """Results are new artifacts outside game homes/source, never replacements."""
    path = Path(path).resolve()
    if any(path.is_relative_to(Path(root).resolve()) for root in protected):
        raise ValueError("Output must be outside protected input directories")
    content = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(content)
