"""Trusted source selector; runs before any candidate module is imported."""
import hashlib
import json
from pathlib import Path
import sys

def main():
    directory = Path(sys.argv.pop(1)).resolve()
    manifest = json.loads((directory / "manifest.json").read_bytes())
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if hashlib.sha256(encoded).hexdigest() != directory.name:
        raise ValueError("program manifest hash mismatch")
    files = {}
    for path in sorted((directory / "src/startlux").rglob("*")):
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError("program source contains a link")
        if path.is_file() and path.suffix == ".py":
            files[path.relative_to(directory / "src/startlux").as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.is_file():
            raise ValueError("sealed program contains unexpected executable/data files")
    if files != manifest["files"]:
        raise ValueError("program bytes changed before launch")
    sys.path.insert(0, str(directory / "src"))
    from startlux.scenarios.genshin.desktop import main as run, emit, error_fields
    try:
        run()
    except Exception as error:
        emit({"status": "error", **error_fields(error)})
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
