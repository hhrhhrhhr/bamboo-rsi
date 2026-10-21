"""Reproduce the pinned BetterGI development integration without resetting files."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / "integrations" / "bettergi"
    metadata = json.loads((source / "source.json").read_text(encoding="utf-8"))
    patch = source / metadata["patch_file"]
    if hashlib.sha256(patch.read_bytes()).hexdigest() != metadata["patch_sha256"]:
        raise SystemExit("BetterGI integration patch hash mismatch")
    checkout = args.checkout.absolute()
    if not checkout.exists():
        checkout.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-c", "core.autocrlf=false", "clone", "--filter=blob:none", "--no-checkout", metadata["upstream"],
                        str(checkout)], check=True)
        subprocess.run(["git", "-c", "core.autocrlf=false", "checkout", "--detach", metadata["base_commit"]], cwd=checkout, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    if head not in {metadata["base_commit"], metadata["local_development_commit"]}:
        raise SystemExit("BetterGI checkout has an unexpected revision; no files were changed")
    reverse = subprocess.run(["git", "apply", "--reverse", "--check", str(patch)],
                             cwd=checkout, capture_output=True)
    if reverse.returncode == 0:
        print(json.dumps({"checkout": str(checkout), "status": "already_applied", "base_commit": metadata["base_commit"]}))
        return
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=checkout, text=True).strip()
    if dirty:
        raise SystemExit("BetterGI checkout contains local changes; no files were changed")
    subprocess.run(["git", "apply", "--check", str(patch)], cwd=checkout, check=True)
    if not args.check_only:
        subprocess.run(["git", "apply", str(patch)], cwd=checkout, check=True)
    print(json.dumps({"checkout": str(checkout), "status": "verified" if args.check_only else "applied",
                      "base_commit": metadata["base_commit"], "license": metadata["license"]}))


if __name__ == "__main__":
    main()
