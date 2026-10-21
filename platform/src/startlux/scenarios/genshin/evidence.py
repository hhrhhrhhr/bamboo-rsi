"""Restore archived observations only after checking every screenshot binding."""
import base64
import hashlib
import re

from .contracts import Observation


def restore_observation(run, value):
    run = run.resolve()
    images = []
    for item in value["images"]:
        identity = item["sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", identity) or item["path"] != "images/" + identity + ".png":
            raise ValueError("invalid archived screenshot path")
        path = run / item["path"]
        if path.is_symlink() or not path.resolve().is_relative_to(run):
            raise ValueError("screenshot escapes the run")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != identity or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("archived screenshot hash mismatch")
        uri = "data:image/png;base64," + base64.b64encode(raw).decode()
        if hashlib.sha256(uri.encode()).hexdigest() != item["data_uri_sha256"]:
            raise ValueError("screenshot URI binding mismatch")
        images.append(uri)
    return Observation.model_validate({**value, "images": images})
