"""Build a self-contained Windows preview, preserving corresponding GPL source."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VOICE_URL = "https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip"
VOICE_SHA256 = "3af8b0e7e0f835ae9d414ce5df580237a3cfb08d586c9fbbb0f7ff29ad5b14ba"
VOICE_BYTES = 43898754


def fetch_voice_archive(destination):
    # Official host supports HTTP ranges. Bound concurrency and total bytes so
    # a throttled connection does not dominate the complete Windows build.
    block = 1024 * 1024

    def fetch_part(index):
        start, end = index * block, min(VOICE_BYTES - 1, (index + 1) * block - 1)
        request = urllib.request.Request(VOICE_URL, headers={"Range": f"bytes={start}-{end}"})
        with urllib.request.urlopen(request, timeout=120) as response:
            if response.status != 206 or response.headers.get("Content-Range") != f"bytes {start}-{end}/{VOICE_BYTES}":
                raise ValueError("ASR server returned an unexpected range")
            data = response.read(end - start + 2)
        if len(data) != end - start + 1:
            raise ValueError("ASR archive range has the wrong size")
        return data

    temporary = destination.with_suffix(".partial")
    try:
        with ThreadPoolExecutor(max_workers=12) as pool, temporary.open("wb") as output:
            for part in pool.map(fetch_part, range((VOICE_BYTES + block - 1) // block)):
                output.write(part)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != VOICE_SHA256:
            raise ValueError("ASR download hash mismatch")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bettergi", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "genshin-desktop")
    parser.add_argument("--voice-archive", type=Path)
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("Build the Windows package on Windows")
    target = args.output.absolute()
    target.mkdir(parents=True, exist_ok=False)
    provenance = json.loads((ROOT / "integrations/auto_commission/source.json").read_text(encoding="utf-8"))
    for name, expected in provenance["files"].items():
        if hashlib.sha256((ROOT / "integrations/auto_commission/upstream" / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"AutoCommission source hash mismatch: {name}")
    subprocess.run(["dotnet", "publish", str(ROOT / "desktop/StartLux.Desktop/StartLux.Desktop.csproj"),
                    "-c", "Release", "-r", "win-x64", "--self-contained", "true", "-o", str(target),
                    "--nologo", "-v", "quiet"], check=True)
    subprocess.run(["dotnet", "publish", str(args.bettergi / "BetterGenshinImpact/BetterGenshinImpact.csproj"),
                    "-c", "Release", "-r", "win-x64", "--self-contained", "true", "-o", str(target / "bettergi"),
                    "--nologo", "-v", "quiet"], check=True)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--name", "genshin-agent",
                    "--paths", str(ROOT / "platform/src"), "--exclude-module", "startlux.optimization",
                    "--exclude-module", "harness2", "--exclude-module", "startlux.testing",
                    "--distpath", str(ROOT / "dist" / "frozen-agent"),
                    str(ROOT / "scripts/genshin_desktop_entry.py")], cwd=ROOT, check=True)
    shutil.copytree(ROOT / "dist/frozen-agent/genshin-agent", target / "agent")
    shutil.copytree(ROOT / "integrations/auto_commission/upstream", target / "task-source/autocommission")
    shutil.copyfile(ROOT / "integrations/auto_commission/agent-main.js", target / "task-source/agent-main.js")
    archive = args.voice_archive or ROOT / "dist/vosk-model-small-cn-0.22.zip"
    if not archive.exists():
        fetch_voice_archive(archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != VOICE_SHA256:
        raise ValueError("Chinese ASR model archive hash mismatch")
    with zipfile.ZipFile(archive) as voice:
        for info in voice.infolist():
            path = Path(info.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in info.filename:
                raise ValueError("unsafe ASR archive entry")
        voice.extractall(target / "voice-unpack")
    shutil.move(str(target / "voice-unpack/vosk-model-small-cn-0.22"), target / "voice-model")
    (target / "voice-unpack").rmdir()
    sources = target / "sources"
    sources.mkdir()
    # Supply the exact modified BetterGI sources alongside its binary (GPL v3).
    # git archive includes committed source; the current pinned patch is applied by CI.
    clean_base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=args.bettergi, text=True).strip()
    base_zip = sources / "bettergi-base.zip"
    subprocess.run(["git", "archive", "--format=zip", "--output", str(base_zip), clean_base], cwd=args.bettergi, check=True)
    lab_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    # Source distributions must not recursively embed previously published
    # installers. Git archive can expand LFS objects through its checkout filters.
    subprocess.run(["git", "archive", "--format=zip", "--output", str(sources / "game-harness-lab.zip"), lab_commit,
                    "--", ".", ":(exclude)windows/*.exe", ":(exclude)windows/*.zip"],
                   cwd=ROOT, check=True)
    shutil.copytree(ROOT / "integrations/bettergi", sources / "bettergi-integration")
    shutil.copyfile(args.bettergi / "LICENSE", sources / "BetterGI-LICENSE")
    shutil.copytree(ROOT / "desktop", sources / "desktop", ignore=shutil.ignore_patterns("bin", "obj"))
    shutil.copytree(ROOT / "platform", sources / "platform", ignore=shutil.ignore_patterns(
        "__pycache__", ".venv", ".pytest_cache", "*.egg-info"))
    (sources / "agent").mkdir()
    shutil.copyfile(ROOT / "scripts/genshin_desktop_entry.py", sources / "agent/entry.py")
    shutil.copyfile(ROOT / "requirements-desktop.lock", sources / "agent/requirements.lock")
    shutil.copyfile(ROOT / "scripts/build_genshin_desktop.py", sources / "build_genshin_desktop.py")
    shutil.copyfile(ROOT / "THIRD_PARTY_NOTICES.md", target / "THIRD_PARTY_NOTICES.md")
    notices = target / "licenses"
    notices.mkdir()
    # Python wheels' license files survive freezing regardless of PyInstaller hooks.
    for distribution in importlib.metadata.distributions():
        for file in distribution.files or []:
            if any(part.lower().startswith(("license", "copying", "notice")) for part in file.parts):
                source = Path(distribution.locate_file(file))
                if source.is_file():
                    destination = notices / distribution.metadata["Name"] / Path(file)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
    # .NET notices, exact package versions and source/source-license links.
    shutil.copyfile(ROOT / "desktop/THIRD_PARTY_NOTICES.txt", notices / "desktop.txt")
    shutil.copyfile(ROOT / "desktop/APACHE-2.0.txt", notices / "APACHE-2.0.txt")
    for assets_path in [ROOT / "desktop/StartLux.Desktop/obj/project.assets.json",
                        args.bettergi / "BetterGenshinImpact/obj/project.assets.json"]:
        assets = json.loads(assets_path.read_text(encoding="utf-8"))
        for library in assets["libraries"].values():
            if library.get("type") != "package":
                continue
            for folder in assets["packageFolders"]:
                package = Path(folder) / library["path"]
                if not package.is_dir():
                    continue
                for name in library.get("files", []):
                    if Path(name).name.lower().startswith(("license", "copying", "notice")):
                        source = package / name
                        if source.is_file():
                            destination = notices / "nuget" / library["path"] / name
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(source, destination)
    shutil.copyfile(args.bettergi / "BetterGenshinImpact/obj/project.assets.json", sources / "bettergi-packages.json")
    shutil.copyfile(ROOT / "desktop/StartLux.Desktop/obj/project.assets.json", sources / "desktop-packages.json")
    result = subprocess.run([str(target / "agent/genshin-agent.exe"), "self-test"], check=True, capture_output=True, text=True)
    print(result.stdout.strip())
    hashes = {path.relative_to(target).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(target.rglob("*")) if path.is_file()}
    (target / "build-manifest.json").write_text(json.dumps({"release_status": "development", "live_game_verified": False,
        "lab_commit": lab_commit, "bettergi_base_commit": clean_base,
        "asr_model_url": VOICE_URL, "asr_model_sha256": VOICE_SHA256, "files": hashes}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
