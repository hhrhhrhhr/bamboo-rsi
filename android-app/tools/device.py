"""Explicit USB deployment and diagnostics; never controls game input or permissions."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

APP = "dev.bamboo.rsi"
GAME = "com.tencent.tmgp.sgame"
RUNNER = "dev.bamboo.rsi.test/androidx.test.runner.AndroidJUnitRunner"


class DeviceError(RuntimeError):
    pass


class Adb:
    def __init__(self, executable: str, serial: str | None = None):
        self.executable, self.serial = executable, serial

    def call(self, *args: str, timeout: int = 20) -> str:
        command = [self.executable]
        if self.serial:
            command += ["-s", self.serial]
        try:
            result = subprocess.run(command + list(args), capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DeviceError("ADB unavailable or operation timed out") from error
        if result.returncode:
            # Device output may include identifiers; do not echo it into public logs.
            raise DeviceError("ADB operation failed: " + args[0])
        return result.stdout

    def select(self, requested: str | None) -> None:
        devices = {}
        for line in self.call("devices").splitlines():
            parts = line.split()
            if len(parts) >= 2 and not line.startswith(("List of", "*")):
                devices[parts[0]] = parts[1]
        if requested:
            if devices.get(requested) != "device":
                raise DeviceError("Selected device missing, offline, or awaiting on-device USB authorization")
            self.serial = requested
        elif len(devices) == 1 and next(iter(devices.values())) == "device":
            self.serial = next(iter(devices))
        else:
            raise DeviceError("Connect one authorized device, or select one explicitly with --serial")


def package_version(text: str) -> dict:
    code = re.search(r"\bversionCode=(\d+)", text)
    version = re.search(r"\bversionName=([^\s]+)", text)
    return {"installed": code is not None, "version_code": int(code[1]) if code else None,
            "version_name": version[1] if version else None}


def data_free_bytes(text: str) -> int | None:
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 6 and parts[-1] in ("/data", "/"):
            try:
                return int(parts[-3]) * 1024  # df -k: available, percent, mount
            except ValueError:
                pass
    return None


def inspect(adb: Adb) -> dict:
    api = adb.call("shell", "getprop", "ro.build.version.sdk").strip()
    if not api.isdigit():
        raise DeviceError("Cannot read Android API level")
    app = package_version(adb.call("shell", "dumpsys", "package", APP))
    game = package_version(adb.call("shell", "dumpsys", "package", GAME))
    services = adb.call("shell", "settings", "get", "secure", "enabled_accessibility_services").strip().split(":")
    accessibility = any(value in (APP + "/.GameAccessibility", APP + "/" + APP + ".GameAccessibility") for value in services)
    free = data_free_bytes(adb.call("shell", "df", "-k", "/data"))
    checks = []
    if int(api) < 30:
        checks.append("android_11_or_newer_required")
    if not app["installed"]:
        checks.append("install_bamboo_app")
    if not game["installed"]:
        checks.append("game_not_installed")
    if not accessibility:
        checks.append("accessibility_not_enabled_by_user")
    if free is None or free < 1024 ** 3:
        checks.append("recording_storage_unknown_or_below_1_gib")
    return {"device_id_sha256": hashlib.sha256(adb.serial.encode()).hexdigest(),
            "api": int(api), "model": adb.call("shell", "getprop", "ro.product.model").strip(),
            "abi": adb.call("shell", "getprop", "ro.product.cpu.abi").strip(),
            "emulator": adb.call("shell", "getprop", "ro.kernel.qemu").strip() == "1",
            "app": app, "game": game, "accessibility_enabled": accessibility,
            "data_free_bytes": free, "pending": checks,
            "live_game_acceptance": False, "recording_permission": "requested_fresh_in_app"}


def require_idle(adb: Adb) -> None:
    # Installation/instrumentation can kill the app. Refuse while either process is alive.
    output = adb.call("shell", "ps", "-A", "-o", "NAME")
    names = {line.strip() for line in output.splitlines()}
    if not names:
        raise DeviceError("Could not verify device process state")
    if any(name == package or name.startswith(package + ":") for name in names for package in (APP, GAME)):
        raise DeviceError("Close Bamboo RSI and the game normally before deployment or smoke tests; evidence is preserved")


def install(adb: Adb, apk: Path) -> str:
    if not apk.is_file() or apk.suffix.lower() != ".apk":
        raise DeviceError("Provide a built APK file")
    with apk.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    output = adb.call("install", "-r", str(apk.resolve()), timeout=180)
    if "Success" not in output.splitlines():
        raise DeviceError("APK installation was not confirmed")
    return digest


def smoke(adb: Adb) -> int:
    output = adb.call("shell", "am", "instrument", "-w", "-e", "class",
                      APP + ".DeviceSmokeTest", RUNNER, timeout=180)
    match = re.search(r"(?m)^OK \((\d+) tests?\)\s*$", output)
    if not match or int(match[1]) != 2 or any(token in output for token in (
            "FAILURES!!!", "INSTRUMENTATION_FAILED", "Process crashed", "shortMsg=")):
        raise DeviceError("Device smoke tests did not pass; keep this failure record")
    return int(match[1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("doctor", "install", "smoke"))
    parser.add_argument("--adb", default=None)
    parser.add_argument("--serial")
    parser.add_argument("--apk", type=Path)
    parser.add_argument("--test-apk", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    report = {"schema": 1, "at": datetime.now(timezone.utc).isoformat(), "action": args.action,
              "game_input": False, "real_matches": 0, "real_promotions": 0}
    exit_code = 0
    try:
        sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
        executable = args.adb or shutil.which("adb")
        if not executable and sdk:
            executable = str(Path(sdk) / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb"))
        if not executable:
            raise DeviceError("ADB not found; use --adb or ANDROID_HOME")
        adb = Adb(executable)
        adb.select(args.serial)
        report["device"] = inspect(adb)
        if args.action != "doctor":
            if report["device"]["api"] < 30:
                raise DeviceError("Android 11 or newer required")
            require_idle(adb)
            if args.action == "install":
                if args.apk is None:
                    raise DeviceError("install requires --apk")
                report["apk_sha256"] = install(adb, args.apk)
            else:
                if not report["device"]["app"]["installed"] or args.test_apk is None:
                    raise DeviceError("smoke requires the installed development app and --test-apk")
                report["test_apk_sha256"] = install(adb, args.test_apk)
                report["device_tests_passed"] = smoke(adb)
        report["status"] = "checked" if args.action == "doctor" else "completed"
    except DeviceError as error:
        report.update(status="blocked_or_failed", reason=str(error))
        exit_code = 2
    destination = args.report or (Path(__file__).resolve().parents[2] / ".runtime" / "android-device" /
                                 (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex + ".json"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Never overwrite earlier device failures or test results.
    with destination.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("Report: " + str(destination.resolve()))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
