"""Deployment checks with no Gamecore requests, plugin imports or game input."""
import ast
import json
from pathlib import Path
import platform
import subprocess
import sys

from .protocol import SDK_COMMIT
from .runtime import validate, worker_environment


class DeploymentError(RuntimeError):
    """Safe, structured setup failure, before an execution slot is consumed."""

    def __init__(self, checks):
        super().__init__("Deployment checks failed; run doctor --runtime before execution")
        self.checks = checks


def inspect_runtime(config=None):
    checks = []

    def check(name, passed, remedy):
        checks.append({"check": name, "passed": bool(passed), "remedy": None if passed else remedy})

    result = {"host_python": platform.python_version(), "host_platform": sys.platform,
              "sdk_commit": SDK_COMMIT, "native_worker_requirement": "Linux Python 3.8/3.9",
              "gamecore_started": False, "gameplay_checked": False,
              "scope": "local_deployment_only", "checks": checks, "ready": False,
              "unverified": ["Gamecore server build and authorization", "predictor quality",
                             "playable game-window recording", "independent live evidence issuer"]}
    if config is None:
        check("runtime_configuration", False, "Provide --runtime with a sealed private runtime configuration.")
        return result
    try:
        validate(config, verify_files=False)
        check("runtime_schema", True, "")
    except (ValueError, TypeError, KeyError, AttributeError):
        check("runtime_schema", False, "Use the runtime template, explicit private IPv4 addresses and valid budgets.")
        return result
    try:
        validate(config)
        check("frozen_files", True, "")
    except (OSError, ValueError, TypeError, KeyError):
        check("frozen_files", False, "Restore missing or changed frozen files; create a new experiment for a new runtime.")
        return result

    # Only stdlib interpreter metadata is executed. Do not load a predictor,
    # recorder, native SDK, license or model during this readiness check.
    probe = "import json,sys; print(json.dumps({'platform':sys.platform,'version':list(sys.version_info[:2])}))"
    try:
        process = subprocess.run([config["sdk_python"], "-I", "-c", probe],
                                 stdin=subprocess.DEVNULL, capture_output=True, timeout=8,
                                 env=worker_environment(config), check=False)
        if process.returncode or len(process.stdout) > 4096:
            raise ValueError("Interpreter probe failed")
        native = json.loads(process.stdout)
        supported = native.get("platform") == "linux" and native.get("version") in ([3, 8], [3, 9])
        result["native_interpreter"] = {k: native.get(k) for k in ("platform", "version")}
        check("native_interpreter", supported, "Run the host and frozen SDK interpreter in Linux/WSL using Python 3.8 or 3.9 for the worker.")
    except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
        native = {}
        check("native_interpreter", False, "Provide a runnable, frozen SDK Python executable; the probe must finish in 8 seconds.")

    sdk = Path(config["sdk_root"]) / "hok" / "hok1v1"
    required = [sdk / "env1v1.py", sdk / "config.dat", sdk / "hero_config.py", sdk / "lib" / "__init__.py"]
    check("sdk_layout", all(p.is_file() for p in required), "sdk_root must contain the pinned hok/hok1v1 package, config.dat and native lib directory.")
    version = native.get("version", [])
    tag = "{}{}".format(*version) if len(version) == 2 else "unknown"
    libraries = list((sdk / "lib").glob("interface*.so"))
    compatible = [p for p in libraries if "cpython-" not in p.name or ("cpython-" + tag + "-") in p.name]
    check("native_extension", bool(compatible), "Provide the official Linux interface .so matching the worker Python ABI; no native load is attempted by doctor.")
    for field, symbol in (("predictor", "create_predictor"), ("recorder", "create_recorder")):
        try:
            source = Path(config[field]).read_text(encoding="utf-8")
            tree = ast.parse(source, feature_version=(3, 8))
            available = any(isinstance(n, ast.FunctionDef) and n.name == symbol for n in tree.body)
            check(field + "_factory", available, "Provide a Python 3.8-compatible file defining " + symbol + ".")
        except (OSError, ValueError, SyntaxError, UnicodeError):
            check(field + "_factory", False, "Fix the frozen plugin syntax and factory definition, then register a new runtime.")
    result["ready"] = all(item["passed"] for item in checks)
    return result


def ensure_ready(config):
    report = inspect_runtime(config)
    if not report["ready"]:
        raise DeploymentError([c for c in report["checks"] if not c["passed"]])
    return report
