"""Host-owned watchdog and immutable runtime inventory; no game launch on import."""
import os
from pathlib import Path
import re
import subprocess
import psutil

from bamboo_rsi.storage import confined, read, write
from .protocol import SDK_COMMIT, file_hash, identity, terminal_outcome
from .worker import private_address, release
from .runtime_leases import RuntimeOwnership, release_unstarted_claim

FIELDS = {"server", "server_build", "ai_ip", "ai_ports", "sdk_python", "sdk_root", "model_root",
          "predictor", "recorder", "max_frames", "max_calls", "episode_seconds", "sdk_commit", "frozen_files"}


def inventory(config):
    paths = set()
    for field in ("sdk_root", "model_root"):
        root = Path(config[field])
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("Missing runtime directory: " + field)
        selected = []
        for path in root.rglob("*"):
            if any(part in (".git", "__pycache__") for part in path.relative_to(root).parts):
                continue
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise ValueError("Runtime links must be resolved before freezing")
            if path.is_file():
                if path.name in ("license.dat", ".env") or path.suffix in (".key", ".pem", ".dpapi"):
                    raise ValueError("Keep private credentials outside predictor and SDK directories")
                selected.append(path.resolve())
        if not selected:
            raise ValueError("Empty frozen runtime: " + field)
        paths.update(selected)
    for field in ("predictor", "recorder", "sdk_python"):
        path = Path(config[field])
        if not path.is_absolute() or not path.is_file() or path.is_symlink():
            raise ValueError("Missing explicit runtime file: " + field)
        paths.add(path.resolve())
    return {str(path): file_hash(path) for path in sorted(paths)}


def validate(config, *, verify_files=True):
    if set(config) != FIELDS or config["sdk_commit"] != SDK_COMMIT:
        raise ValueError("Runtime schema or SDK revision mismatch")
    private_address(config["server"])
    private_address(config["ai_ip"] + ":1")
    if not isinstance(config["server_build"], str) or not re.fullmatch(r"[a-f0-9]{64}", config["server_build"]) or config["server_build"] == "0" * 64:
        raise ValueError("Trusted Gamecore server build digest required")
    ports = config["ai_ports"]
    if not isinstance(ports, list) or len(ports) != 2 or len(set(ports)) != 2 or any(type(p) is not int or not 1024 <= p <= 65535 for p in ports):
        raise ValueError("Two distinct unprivileged AI ports required")
    for name, low, high in (("max_frames", 100, 100_000), ("max_calls", 1, 40_000), ("episode_seconds", 30, 3600)):
        if type(config[name]) is not int or not low <= config[name] <= high:
            raise ValueError("Invalid execution budget: " + name)
    if verify_files and inventory(config) != config["frozen_files"]:
        raise ValueError("Frozen SDK, predictor, recorder or model files changed")
    return config


def seal(config):
    config = {**config, "frozen_files": {}}
    validate(config, verify_files=False)
    config["frozen_files"] = inventory(config)
    return config


def source_identity():
    directory = Path(__file__).parent
    return identity({p.name: file_hash(p) for p in sorted(directory.glob("*.py"))})


def worker_environment(config):
    environment = {key: os.environ[key] for key in ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LD_LIBRARY_PATH") if key in os.environ}
    environment.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1",
                       PYTHONPATH=os.pathsep.join((str(Path(__file__).parents[1]), config["sdk_root"])))
    return environment


def recover_worker(config, directory):
    directory = Path(directory).resolve()
    request = directory / "request.json"
    if not request.exists():
        # Reservation may have been persisted before execute ever created its
        # request. A worker cannot pass the startup handshake without this file.
        allowed = {"assignment.json", "host-failure.json", "interrupted.json"}
        if not stop_owned_worker(directory) or not all(p.name in allowed for p in directory.iterdir()):
            return False
        release_unstarted_claim(config, directory)
        return True
    try:
        with RuntimeOwnership(config, directory) as ownership:
            result = _recover_owned_worker(config, directory)
            if result:
                ownership.complete()
            return result
    except RuntimeError:
        return False


def _recover_owned_worker(config, directory):
    request = directory / "request.json"
    if not stop_owned_worker(directory):
        return False
    try:
        value = read(request)
        expected = {**config, "predictor_sha256": config["frozen_files"].get(str(Path(config["predictor"]).resolve())),
                    "recorder_sha256": config["frozen_files"].get(str(Path(config["recorder"]).resolve()))}
        if value["runtime"] != expected or value["assignment"]["run_id"] != directory.name:
            return False
        # Stop the remote game even when model files changed. No changed plugin
        # is imported, and no new game is launched by the maintenance path.
        released = release(config["server"], directory.name)
        stopped = False
        report_path = directory / "worker-report.json"
        if report_path.exists():
            report = read(report_path)
            stopped = (report.get("assignment") == value["assignment"]
                       and report.get("recording_complete") is True
                       and report.get("recording", {}).get("stopped") is True)
        if released and stopped:
            return True
        # For an unfinished recorder, all executable dependencies must still be
        # frozen. A changed model can be restored by the operator; never run a
        # modified recovery plugin just to make the experiment appear available.
        validate(config)
        result = subprocess.run([config["sdk_python"], "-m", "bamboo_hok.worker", "--recover", "--request", str(request), "--output", str(directory)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=40, env=worker_environment(config), check=False)
        return result.returncode == 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        return False


def stop_owned_worker(directory):
    """Do not kill by name or PID alone; creation time and exact argv must match."""
    directory = Path(directory)
    marker = directory / "worker-process.json"
    if not marker.exists():
        # Startup waits for the marker before any game or recorder interaction.
        # A parent crash before writing it leaves a harmless waiting process.
        expected_request = str(directory / "request.json")
        for process in psutil.process_iter(["cmdline"]):
            if "bamboo_hok.worker" in (process.info["cmdline"] or []) and expected_request in process.info["cmdline"]:
                return False
        return True
    value = read(marker)
    argv = value["argv"]
    expected_tail = ["-m", "bamboo_hok.worker", "--request", str(directory / "request.json"), "--output", str(directory)]
    if argv[1:] != expected_tail:
        return False
    try:
        process = psutil.Process(value["pid"])
        if process.create_time() != value["created"]:
            return True  # PID reused: the owned process is gone; never touch its replacement.
        if process.cmdline() != value["argv"] or not Path(process.exe()).samefile(value["argv"][0]):
            return False
        process.kill()
        process.wait(timeout=5)
        return True
    except psutil.NoSuchProcess:
        return True
    except (psutil.Error, OSError):
        return False


def completed_execution(directory, assignment):
    """Recognize an already sealed result after the host's final pointer write failed."""
    directory = Path(directory)
    try:
        manifest = read(directory / "manifest.json")
        if (manifest.get("assignment") != assignment or manifest.get("status") != "worker_finished"
                or any(manifest.get(k) is not True for k in ("artifacts_complete", "input_released", "recorder_stopped"))):
            return False
        required = {"worker-report.json", "events.jsonl", "recording-start.json", "cleanup.json"}
        if not required.issubset(manifest["artifacts"]):
            return False
        return all(file_hash(confined(directory, name)) == digest for name, digest in manifest["artifacts"].items())
    except (OSError, ValueError, KeyError, TypeError):
        return False


def execute(config, assignment, scenario, parameters, directory):
    """A killed worker never refunds a slot or becomes a successful match."""
    validate(config)
    from .diagnostics import ensure_ready
    ensure_ready(config)
    with RuntimeOwnership(config, directory) as ownership:
        result = _execute_owned(config, assignment, scenario, parameters, directory)
        if result.get("input_released") is True and result.get("recorder_stopped") is True:
            ownership.complete()
        return result


def _execute_owned(config, assignment, scenario, parameters, directory):
    directory = Path(directory).resolve()
    if (directory / "request.json").exists():
        raise ValueError("Existing run request cannot be executed again")
    request = {"runtime": {**config, "predictor_sha256": file_hash(config["predictor"]),
                            "recorder_sha256": file_hash(config["recorder"])},
               "assignment": assignment, "scenario": scenario, "policy": parameters}
    write(directory / "request.json", request, immutable=True)
    worker = [config["sdk_python"], "-m", "bamboo_hok.worker", "--request", str(directory / "request.json"), "--output", str(directory)]
    # Do not pass the optimizer key or unrelated provider secrets to native workers.
    environment = worker_environment(config)
    status, released = "worker_failed", False
    process = None
    try:
        with (directory / "worker.stdout.log").open("xb") as stdout, (directory / "worker.stderr.log").open("xb") as stderr:
            process = psutil.Popen(worker, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, env=environment)
            write(directory / "worker-process.json", {"pid": process.pid, "created": process.create_time(), "argv": worker}, immutable=True)
            returncode = process.wait(timeout=config["episode_seconds"] + 45)
        status = "worker_finished" if returncode == 0 else "worker_failed"
    except (subprocess.TimeoutExpired, psutil.TimeoutExpired):
        status = "worker_timeout"
    except (OSError, psutil.Error):
        status = "worker_start_failed"
    finally:
        # If saving the PID marker failed, this exact Popen handle still belongs
        # to us. Stop it before attempting the persisted-identity recovery path.
        if process is not None and not (directory / "worker-process.json").exists():
            try:
                process.kill()
                process.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass
            except (psutil.Error, OSError):
                pass
        exited = stop_owned_worker(directory)
        # Process death is not proof the remote Gamecore stopped receiving input.
        try:
            released = exited and release(config["server"], assignment["run_id"])
        except Exception:
            released = False
        stopped = False
        try:
            stopped = read(directory / "worker-report.json").get("recording_complete") is True
        except (OSError, ValueError):
            pass
        if not stopped:
            # Already holding the runtime lease; do not reacquire its OS lock.
            stopped = _recover_owned_worker(config, directory)
        write(directory / "cleanup.json", {"input_released": released, "recorder_stopped": stopped, "status": status}, immutable=True)
    artifacts = {}
    try:
        validate(config)
        report = read(directory / "worker-report.json")
        if report["assignment"] != assignment:
            raise ValueError("Worker assignment changed")
        recording = report.get("recording", {})
        names = ["worker-report.json", "events.jsonl", "recording-start.json", "cleanup.json", recording.get("video_path", "")]
        if assignment["mode"] == "prospective":
            names += ["terminal.json", recording.get("replay_path", "")]
        else:
            for name in ("terminal.json", recording.get("replay_path", "")):
                if name and confined(directory, name).is_file():
                    names.append(name)
        for name in names:
            path = confined(directory, name)
            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError("Missing nonempty execution artifact")
            artifacts[name] = file_hash(path)
        if Path(recording["video_path"]).suffix.lower() != ".mp4" or (assignment["mode"] == "prospective" and Path(recording["replay_path"]).suffix.lower() != ".abs"):
            raise ValueError("Expected MP4 recording and ABS replay")
        if not released or not stopped or not report["input_released"] or not report["recording_complete"] or status != "worker_finished":
            raise ValueError("Run did not close cleanly")
        if assignment["mode"] == "prospective" and terminal_outcome(read(directory / "terminal.json")) == "UNKNOWN":
            raise ValueError("Unknown terminal cannot complete a held-out slot")
        eligible = True
    except Exception:
        eligible = False
    manifest = {"assignment": assignment, "status": status, "input_released": released, "recorder_stopped": stopped,
                "artifacts_complete": eligible, "artifacts": artifacts,
                "independently_attested": False}
    write(directory / "manifest.json", manifest, immutable=True)
    return manifest
