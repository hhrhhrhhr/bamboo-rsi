"""Configuration and deployment fingerprints, without credentials."""
import json
import os
from pathlib import Path
import shutil
import sys
from importlib.metadata import version, PackageNotFoundError

from bamboo_rsi.contracts import controller_fingerprint
from bamboo_rsi.storage import identity
from bamboo_delta import recording, windows, protocol as shared_protocol
from bamboo_delta.recording import digest
from bamboo_delta.protocol import ProbeError
from .protocol import scenario
from .decision import validate_provider, provider_endpoint

FILES = ("game_executable", "ffmpeg_executable", "riot_ca_certificate")


def configuration(path):
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    required = {*FILES, "decision_endpoint", "model", "call_budget", "scenario"}
    if not required <= set(value) or set(value) - required - {'decision_transport', 'decision_protocol'}:
        raise ProbeError("invalid_config_fields")
    if value.get('decision_transport', 'system') not in ('system', 'direct'):
        raise ProbeError('invalid_decision_transport')
    if any(not isinstance(value[k], str) for k in (*FILES, "decision_endpoint", "model")):
        raise ProbeError("invalid_config_types")
    provider_endpoint(value)
    if not 1 <= len(value["model"].strip()) <= 120:
        raise ProbeError("invalid_model")
    validate_provider(value)
    if type(value["call_budget"]) is not int or not 1 <= value["call_budget"] <= 20000:
        raise ProbeError("invalid_call_budget")
    scenario(value["scenario"])
    return value


def source_identity():
    own = {p.name: digest(p) for p in sorted(Path(__file__).parent.glob("*.py"))}
    capture_dependencies = {}
    for name in ("windows-capture", "numpy", "opencv-python"):
        try: capture_dependencies[name] = version(name)
        except PackageNotFoundError: capture_dependencies[name] = "unavailable"
    return identity({"lol": own, "rsi_core": controller_fingerprint(),
        "capture_dependencies": capture_dependencies,
        "dependencies": {name: version(name) for name in ("httpx", "httpcore", "h11", "anyio", "certifi", "idna", "psutil")},
        "python": sys.version,
        "capture": {m.__name__: digest(m.__file__) for m in (recording, windows, shared_protocol)}})


def freeze(config):
    result = {"config": config, "source": source_identity(), "files": {}}
    for key in FILES:
        path = Path(config[key])
        if not config[key] or not path.is_absolute() or not path.is_file():
            raise ProbeError(key + "_missing")
        result["files"][key] = digest(path)
    return result


def doctor(config, home, *, diagnostic=False):
    from .live import LiveClient
    from .protocol import check_scene
    from .window import GameWindow
    pending = []
    if sys.platform != "win32":
        pending.append("windows_required")
    for key in FILES:
        if not config[key] or not Path(config[key]).is_absolute() or not Path(config[key]).is_file():
            pending.append(key + "_missing")
    if validate_provider(config) != 'systemone' and not os.environ.get("BAMBOO_DECISION_KEY"):
        pending.append("model_key_missing")
    path = Path(home).resolve()
    while not path.exists():
        path = path.parent
    if shutil.disk_usage(path).free < 2 * 1024**3:
        pending.append("recording_storage_below_2_gib")
    target = None
    if "game_executable_missing" not in pending and sys.platform == "win32":
        try:
            target = GameWindow(config["game_executable"])
        except ProbeError as error:
            pending.append(str(error))
    if "riot_ca_certificate_missing" not in pending:
        try:
            client = LiveClient(config["riot_ca_certificate"])
            if target is not None:
                client.bind(target.pid)
            state, _ = client.fetch()
            check_scene(state, config["scenario"])
            if not diagnostic and state["game_time"] > config["scenario"]["start_time"]:
                pending.append("fresh_training_match_required")
        except Exception as error:
            pending.append(str(error) if isinstance(error, ProbeError) else "live_client_check_failed")
    return {"ready": not pending, "pending": pending, "source": source_identity(),
            "scope": "garen_fixed_bot_laning", "live_validation": "not_established_by_doctor"}
