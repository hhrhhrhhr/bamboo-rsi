"""Check Git-visible source paths for common credential and private artifact mistakes.

Findings show file and line only, never matched secret values. This is a baseline
pre-publication guard, not a guarantee that arbitrary sensitive data is absent.
"""
from pathlib import Path
import hashlib
import json
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TOKENS = re.compile(r"(?<![\w-])(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")
PRIVATE_PATH = re.compile(r"(?:^|/)(?:\.runtime|outputs|runs|recordings|logs|\.venv)(?:/|$)|(?:^|/)(?:credential[^/]*|secrets?\.json|settings\.json|api[-_]key\.txt)$|\.(?:dpapi|lnk|pem|key|pfx|mp4|avi|mkv|exe|zip)$", re.I)


def main():
    paths = subprocess.check_output(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT)
    files = sorted(set(p.decode("utf-8") for p in paths.split(b"\0") if p))
    findings = []
    pinned_root = "integrations/auto_commission/upstream/"
    pinned = json.loads((ROOT / "integrations/auto_commission/source.json").read_text(encoding="utf-8"))["files"]
    for name, expected in pinned.items():
        full_name = pinned_root + name
        path = ROOT / full_name
        if full_name not in files or not path.is_file():
            findings.append({"path": full_name, "rule": "missing_pinned_source"})
        elif hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            findings.append({"path": full_name, "rule": "changed_pinned_source"})
    for name in files:
        # This exact, hash-verified upstream file defines UI fields only.
        upstream_ui_schema = name == pinned_root + "settings.json"
        if (PRIVATE_PATH.search(name) and not upstream_ui_schema) or (Path(name).name.startswith('.env') and not name.endswith('.example')):
            findings.append({"path": name, "rule": "private_artifact"})
            continue
        path = ROOT / name
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8-sig")
        except UnicodeError:
            continue
        for line, value in enumerate(content.splitlines(), 1):
            if TOKENS.search(value):
                findings.append({"path": name, "line": line, "rule": "credential_pattern"})
    print(json.dumps({"checked_files": len(files), "findings": findings}, ensure_ascii=True))
    return bool(findings)


if __name__ == "__main__":
    sys.exit(main())
