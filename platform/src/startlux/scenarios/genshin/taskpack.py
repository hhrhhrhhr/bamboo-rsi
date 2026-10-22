"""Hash-verified v2 development task packs, independent of v1 game exports."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile

from pydantic import Field, field_validator

from .contracts import ActionOption, Contract, Observation, catalog_hash, canonical_hash


def portable_resource_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
    if (not name or path.is_absolute() or "\\" in name or ":" in name
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or any(part.endswith((" ", ".")) or part.split(".", 1)[0].upper() in reserved
                   or re.search(r'[<>"|?*\x00-\x1f]', part) for part in path.parts)):
        raise ValueError("task pack resource path is not Windows-portable")
    return path


class TaskPackManifest(Contract):
    schema_version: int = Field(ge=2, le=2)
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,127}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    game_id: str = Field(pattern=r"^genshin$")
    environment: str = Field(pattern=r"^(fixture|windows)$")
    release_status: str = Field(pattern=r"^development$")
    decision_model: str = Field(min_length=1, max_length=128)
    regions: list[str] = Field(min_length=1)
    files: dict[str, str] = Field(min_length=2, max_length=2000)
    attributions: list[str] = Field(min_length=1)

    @field_validator("files")
    @classmethod
    def validate_resources(cls, files):
        folded = set()
        for name, checksum in files.items():
            portable_resource_path(name)
            if name.casefold() in folded or name.casefold() == "manifest.json":
                raise ValueError("duplicate or reserved resource path")
            folded.add(name.casefold())
            if not re.fullmatch(r"[0-9a-f]{64}", checksum):
                raise ValueError("resource hashes must be SHA-256")
        if not {"actions.json", "instructions.txt"} <= files.keys():
            raise ValueError("actions.json and instructions.txt are required")
        return files


class TaskPack:
    def __init__(self, root: Path, manifest: TaskPackManifest, options: list[ActionOption], instructions: str):
        self.root = root
        self.manifest = manifest
        self.options = options
        self.instructions = instructions

    @property
    def policy_base_hash(self):
        frozen = self.manifest.model_dump()
        frozen["files"].pop("instructions.txt")
        return canonical_hash(frozen)

    @classmethod
    def load(cls, directory: Path | str):
        root = Path(directory).absolute()
        if root.is_symlink() or not root.is_dir():
            raise ValueError("task pack must be a real directory")
        paths = list(root.rglob("*"))
        if any(path.is_symlink() or not (path.is_file() or path.is_dir()) for path in paths):
            raise ValueError("task pack cannot contain symlinks or special files")
        manifest_path = root / "manifest.json"
        if manifest_path.stat().st_size > 1024 * 1024:
            raise ValueError("task pack manifest is too large")
        manifest = TaskPackManifest.model_validate_json(manifest_path.read_bytes())
        actual = {path.relative_to(root).as_posix() for path in paths if path.is_file()}
        if actual != {"manifest.json", *manifest.files}:
            raise ValueError("task pack contains missing or unlisted files")
        resources = {}
        total_bytes = 0
        for name, expected in manifest.files.items():
            path = root.joinpath(*portable_resource_path(name).parts)
            if path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("task pack resource is too large")
            raw = path.read_bytes()
            total_bytes += len(raw)
            if total_bytes > 64 * 1024 * 1024:
                raise ValueError("task pack resources exceed 64 MiB")
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError(f"task pack resource hash mismatch: {name}")
            resources[name] = raw
        raw_options = json.loads(resources["actions.json"])
        if not isinstance(raw_options, list):
            raise ValueError("actions.json must contain an action list")
        options = [ActionOption.model_validate(option) for option in raw_options]
        catalog_hash(options)
        if any(option.preconditions.get("region") not in manifest.regions for option in options):
            raise ValueError("every task action must guard a supported region")
        instructions = resources["instructions.txt"].decode("utf-8").strip()
        if not instructions or len(instructions) > 24000:
            raise ValueError("task instructions must contain 1..24000 characters")
        return cls(root, manifest, options, instructions)

    def candidates(self, observation: Observation) -> list[ActionOption]:
        options = list(self.options)
        if self.manifest.environment == "windows" and observation.facts.get("daily_recovery_v1") is True:
            from .recovery_actions import recovery_options
            options.extend(recovery_options())
        return [option.model_copy(deep=True) for option in options if option.available(observation)]


def export_taskpack(source: Path | str, destination: Path | str) -> TaskPack:
    """Atomically export a verified development bundle; never imply a signed release."""
    pack = TaskPack.load(source)
    destination = Path(destination).absolute()
    if destination.exists():
        raise FileExistsError("task pack export already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".taskpack-", dir=destination.parent))
    try:
        for name in ["manifest.json", *pack.manifest.files]:
            target = staging.joinpath(*portable_resource_path(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(pack.root / name, target)
        TaskPack.load(staging)
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return TaskPack.load(destination)


def build_taskpack(source: Path | str, destination: Path | str) -> TaskPack:
    """Authoring step: recompute resource hashes, then validate the built bundle.

    Runtime loading always verifies hashes. Optimizer candidates use this build step
    so an LLM need not calculate SHA-256 strings when editing policy text.
    """
    source, destination = Path(source).absolute(), Path(destination).absolute()
    if source.is_symlink() or not source.is_dir():
        raise ValueError("task pack source must be a real directory")
    if destination.exists():
        raise FileExistsError("task pack build already exists")
    paths = list(source.rglob("*"))
    if any(path.is_symlink() or not (path.is_file() or path.is_dir()) for path in paths):
        raise ValueError("task pack cannot contain symlinks or special files")
    manifest = TaskPackManifest.model_validate_json((source / "manifest.json").read_bytes())
    actual = {path.relative_to(source).as_posix() for path in paths if path.is_file()}
    if actual != {"manifest.json", *manifest.files}:
        raise ValueError("task pack contains missing or unlisted files")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".taskpack-build-", dir=destination.parent))
    try:
        checksums = {}
        total_bytes = 0
        for name in manifest.files:
            path = source.joinpath(*portable_resource_path(name).parts)
            if path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("task pack resource is too large")
            raw = path.read_bytes()
            total_bytes += len(raw)
            if total_bytes > 64 * 1024 * 1024:
                raise ValueError("task pack resources exceed 64 MiB")
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            checksums[name] = hashlib.sha256(raw).hexdigest()
        value = manifest.model_dump()
        value["files"] = checksums
        (staging / "manifest.json").write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        TaskPack.load(staging)
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return TaskPack.load(destination)
