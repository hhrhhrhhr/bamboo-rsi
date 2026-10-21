"""Frozen BetterGI source/builds; candidates cannot change tests or build projects.

This is a bounded optimization surface, not a sandbox for hostile C# code.
Native artifacts and per-run hosts stay private; upstream GPL notices are retained.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from ...core.contracts import canonical_hash
from ...core.storage import atomic_json
from .episode import ExecutionLease, execution_build, has_stop_resolution, utc_now

PREFIX = "native/"
EDITABLE = frozenset(PREFIX + name for name in (
    "BetterGenshinImpact/Service/Agent/AgentFlightInput.cs",
    "BetterGenshinImpact/GameTask/AutoPathing/Handler/StopFlyingHandler.cs",
    "BetterGenshinImpact/GameTask/AutoPathing/Handler/CombatScriptHandler.cs"))
BINARIES = ("BetterGI.dll", "BetterGenshinImpact.AgentBridge.dll")
PROJECT = "BetterGenshinImpact/BetterGenshinImpact.csproj"
TEST_PROJECT = "Test/BetterGenshinImpact.AgentBridge.Tests/BetterGenshinImpact.AgentBridge.Tests.csproj"


def long_path(path):
    path = Path(path).resolve()
    if os.name == "nt" and not str(path).startswith("\\\\?\\"):
        return Path("\\\\?\\" + str(path))
    return path


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inventory(directory, *, build_tree=False):
    directory = Path(directory)
    result = {}
    for current, dirs, files in os.walk(directory, followlinks=False):
        for name in [*dirs, *files]:
            path = Path(current) / name
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise ValueError("native tree contains a link")
        if build_tree:
            dirs[:] = [d for d in dirs if d not in {".git", "bin", "obj", "TestResults"}]
        for name in sorted(files):
            path = Path(current) / name
            result[path.relative_to(directory).as_posix()] = digest(path)
    return dict(sorted(result.items()))


def control_hash(files):
    return canonical_hash({k: v for k, v in files.items() if PREFIX + k not in EDITABLE})


def fixed_runtime(files):
    return {k: v for k, v in files.items() if k != "User/config.json"
            and not k.startswith(("User/AutoFight/", "log/", "Cache/"))}


def toolchain(dotnet):
    from .rsi_program import clean_env
    result = subprocess.run([str(dotnet), "--version"], capture_output=True, check=True,
                            env=clean_env(), timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    return {"sdk": result.stdout.decode().strip(), "dotnet_sha256": digest(dotnet)}


def compile_native(source, runtime, log, dotnet, *, locked):
    """Fixed evaluator commands. The proposer cannot supply flags or test paths."""
    from .rsi_program import clean_env
    # MSBuild treats the extended-path '?' as a wildcard; Python file I/O still
    # uses extended paths, but .NET receives ordinary absolute paths.
    source = Path(str(source).removeprefix("\\\\?\\"))
    runtime = Path(str(runtime).removeprefix("\\\\?\\"))
    dotnet = Path(dotnet).resolve()
    env = {**clean_env(), "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1"}
    common = ["-p:NuGetAudit=false", "-p:RestorePackagesWithLockFile=true"]
    if locked:
        common.append("-p:RestoreLockedMode=true")
    flags = ["-p:ContinuousIntegrationBuild=true", "-p:Deterministic=true",
             "-p:PathMap=" + str(source) + "=/_/native", "-p:Platform=x64"]
    commands = [
        ["test", str(source / TEST_PROJECT), "-c", "Release", "-r", "win-x64", "-p:SelfContained=true",
         "--verbosity", "quiet", *common, *flags],
        ["publish", str(source / PROJECT), "-c", "Release", "-r", "win-x64", "--self-contained", "true",
         "-o", str(runtime), "--verbosity", "quiet", *common, *flags],
    ]
    with Path(log).open("wb") as output:
        for args in commands:
            result = subprocess.run([str(dotnet), *args], cwd=source, stdout=output, stderr=subprocess.STDOUT,
                                    env=env, timeout=900,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if result.returncode:
                raise ValueError("native independent test/build failed")


class NativeStore:
    def __init__(self, home):
        self.home = Path(home).resolve()
        self.root = long_path(self.home / "rsi/native-programs")

    def directory(self, identity):
        from .rsi_program import valid_id
        path = self.root / valid_id(identity)
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("native artifact escapes storage")
        return path

    def verify(self, identity):
        directory = self.directory(identity)
        value = json.loads((directory / "manifest.json").read_bytes())
        if canonical_hash(value) != identity or value.get("schema_version") != 1:
            raise ValueError("native manifest changed")
        files = inventory(directory / "source")
        if files != value["files"] or control_hash(files) != value["control_sha256"]:
            raise ValueError("frozen native source changed")
        if (value["validation"].get("passed") is not True or value["validation"].get("reproducible") is not True
                or digest(directory / "validation.log") != value["validation"]["log_sha256"]):
            raise ValueError("native validation changed")
        runtime = inventory(directory / "runtime")
        expected = value["binaries"] if value["parent"] else value["runtime"]
        if runtime != expected:
            raise ValueError("frozen native binary changed")
        if value["parent"]:
            # Verify the ancestry as well, including the shared baseline runtime.
            parent = self.verify(value["parent"])
            changed = sorted(PREFIX + k for k in set(files) | set(parent["files"])
                             if files.get(k) != parent["files"].get(k))
            if (not changed or set(changed) - EDITABLE or changed != value["changed_files"]
                    or any(value[k] != parent[k] for k in ("control_sha256", "toolchain", "dependencies"))):
                raise ValueError("native candidate exceeds the frozen surface")
        if set(value["binaries"]) != set(BINARIES) or any(value["runtime"].get(k) != v for k, v in value["binaries"].items()):
            raise ValueError("native runtime binary binding changed")
        if {k: v for k, v in value["runtime"].items() if k not in BINARIES} != value["dependencies"]:
            raise ValueError("native runtime dependencies changed")
        return value

    def publish(self, stage, identity):
        target = self.directory(identity)
        stage = long_path(stage)
        if stage.parent != self.root or not stage.name.startswith(".publishing-"):
            raise ValueError("native publication must stay in its staging directory")
        for attempt in range(50):
            try:
                stage.rename(target)
                break
            except PermissionError:
                if attempt == 49:
                    raise
                time.sleep(0.1)  # Windows scanners may briefly hold a new DLL.
        self.verify(identity)

    def build(self, output, *, dotnet, source=None, parent=None, replacements=None):
        output = long_path(output)
        output.mkdir(parents=True, exist_ok=True)
        work = output / "native-build"
        work.mkdir()  # Retain interrupted and failed work, never overwrite.
        before = self.verify(parent) if parent else None
        source = self.directory(parent) / "source" if parent else long_path(source)
        files = inventory(source, build_tree=not parent)
        if not all(name in files for name in (PROJECT, TEST_PROJECT, *(k[len(PREFIX):] for k in EDITABLE))):
            raise ValueError("native source is incomplete")
        build_source = work / "source"
        for name in files:
            path = build_source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, path)
        changes = []
        for name, text in (replacements or {}).items():
            if name not in EDITABLE or not isinstance(text, str) or not 1 <= len(text) <= 60000:
                raise ValueError("native proposal exceeds the allowed surface")
            path = build_source / name[len(PREFIX):]
            raw = text.encode("utf-8")
            if path.read_bytes() != raw:
                path.write_bytes(raw)
                changes.append(name)
        if parent and not changes:
            raise ValueError("native candidate made no change")
        chain = toolchain(dotnet)
        if before and chain != before["toolchain"]:
            raise ValueError("native compiler changed since the parent")
        initial = inventory(build_source)
        runtime, log = work / "runtime", work / "validation.log"
        try:
            compile_native(build_source, runtime, log, dotnet, locked=bool(parent))
            final = inventory(build_source, build_tree=True)
            # Baseline may add NuGet locks; every candidate must retain them exactly.
            if any(final.get(k) != v for k, v in initial.items()) or any(
                    not k.endswith("packages.lock.json") for k in set(final) - set(initial)):
                raise ValueError("native build changed source or fixed tests")
            if parent and final != initial:
                raise ValueError("native dependency lock changed")
            built = inventory(runtime)
            # Rebuild from a fresh source directory with the same locked inputs.
            # A second publish of an incremental build is not reproducibility proof.
            replica = work / "reproduction-source"
            for name in final:
                path = replica / name
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(build_source / name, path)
            replica_log = work / "reproduction.log"
            compile_native(replica, work / "reproduction-runtime", replica_log, dotnet, locked=True)
            if (inventory(work / "reproduction-runtime") != built
                    or inventory(replica, build_tree=True) != final):
                raise ValueError("native clean rebuild was not reproducible")
            with log.open("ab") as combined:
                combined.write(b"\n--- Independent clean rebuild ---\n" + replica_log.read_bytes())
            binaries = {name: built[name] for name in BINARIES}
            dependencies = {k: v for k, v in built.items() if k not in BINARIES}
            if before and dependencies != before["dependencies"]:
                raise ValueError("native candidate changed frozen runtime dependencies")
            report = {"passed": True, "reproducible": True, "log_sha256": digest(log), "evidence_origin": "offline_native_regression",
                      "eligible_for_client_release": False}
        except Exception as error:
            atomic_json(output / "build-report.json", {"passed": False, "error_type": type(error).__name__,
                "evidence_origin": "offline_native_regression", "eligible_for_client_release": False})
            raise
        manifest = {"schema_version": 1, "parent": parent, "files": final, "control_sha256": control_hash(final),
                    "changed_files": sorted(changes), "toolchain": chain, "runtime": built,
                    "binaries": binaries, "dependencies": dependencies, "validation": report}
        identity = canonical_hash(manifest)
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.directory(identity)
        if not target.exists():
            stage = Path(tempfile.mkdtemp(prefix=".publishing-", dir=self.root))
            for name in final:
                path = stage / "source" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(build_source / name, path)
            for name in (binaries if parent else built):
                path = stage / "runtime" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(runtime / name, path)
            shutil.copyfile(log, stage / "validation.log")
            atomic_json(stage / "manifest.json", manifest)
            self.publish(stage, identity)
        self.verify(identity)
        atomic_json(output / "native-build-report.json", {**report, "native_program": identity})
        return identity

    def prepare_host(self, identity, base_host, destination):
        """Copy a private host before any input; never swap installed DLLs in place."""
        value = self.verify(identity)
        ancestor = identity
        while (parent := json.loads((self.directory(ancestor) / "manifest.json").read_bytes())["parent"]):
            ancestor = parent
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=False)
        for name in fixed_runtime(value["runtime"]):
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            origin = identity if name in BINARIES else ancestor
            shutil.copyfile(self.directory(origin) / "runtime" / name, path)
        if inventory(destination) != fixed_runtime(value["runtime"]):
            raise ValueError("isolated native host differs from the frozen runtime")
        base_host = Path(base_host)
        # Local game configuration remains private and is bound by execution_build.
        (destination / "User").mkdir(exist_ok=True)
        shutil.copyfile(base_host / "User/config.json", destination / "User/config.json")
        fight = base_host / "User/AutoFight"
        inventory(fight)  # Reject linked resources before copying.
        if fight.exists():
            shutil.copytree(fight, destination / "User/AutoFight")
        expected = {**execution_build(base_host), **value["binaries"]}
        if execution_build(destination) != expected:
            raise ValueError("native host settings changed while preparing")
        return destination


def program_build(home, manifest, base):
    identity = manifest.get("native_program")
    return {**base, **NativeStore(home).verify(identity)["binaries"]} if identity else dict(base)


def bind_native(home, bundle, host, audit):
    from .rsi_program import ProgramStore, bundle_program
    identity = bundle_program(home, bundle)
    if not identity:
        return
    manifest = ProgramStore(home).verify(identity)
    native = manifest.get("native_program")
    if not native:
        return
    value = NativeStore(home).verify(native)
    # Runtime files are checked in the actual executable directory, not the store alone.
    actual = fixed_runtime(inventory(host))
    if actual != fixed_runtime(value["runtime"]):
        raise ValueError("running native host differs from its assigned artifact")
    config = json.loads((Path(home) / "rsi/config.json").read_bytes())
    if execution_build(host) != {**config["execution_build"], **value["binaries"]}:
        raise ValueError("native host configuration changed before input")
    audit({"type": "native_program_bound", "native_program": native, "program": identity,
           "binaries": value["binaries"], "runtime_sha256": canonical_hash(value["runtime"])})


def binding_verified(home, manifest, records, metadata):
    native = manifest.get("native_program")
    if not native:
        return True
    value = NativeStore(home).verify(native)
    bindings = [(n, e) for n, e in records if e.get("type") == "native_program_bound"]
    armed = [n for n, e in records if e.get("type") == "execution_arming"]
    return bool(len(bindings) == 1 and armed and bindings[0][0] < min(armed)
                and bindings[0][1].get("native_program") == native
                and bindings[0][1].get("program") == canonical_hash(manifest)
                and bindings[0][1].get("binaries") == value["binaries"]
                and bindings[0][1].get("runtime_sha256") == canonical_hash(value["runtime"])
                and all(metadata.get("execution_build", {}).get(k) == v for k, v in value["binaries"].items()))


def enable_native(home, source, dotnet):
    """One idle, recorded developer migration, before any accepted descendants."""
    from .rsi import RSIStore
    store = RSIStore(home)
    with ExitStack() as stack:
        for path in (store.home, store.root, store.root / "service", store.root / "coordinator"):
            stack.enter_context(ExecutionLease(path))
        pointer = store.read("active.json")
        if pointer.get("campaign_hash") or pointer["generation"]:
            raise ValueError("cannot change native baseline after promotion or during a campaign")
        for path in (store.home / "runs").glob("*/attempt.json"):
            meta = json.loads(path.read_bytes())
            if (meta.get("execution_armed") and not (meta.get("state") == "ended" and
                    meta.get("input_stop_acknowledged") is True) and not has_stop_resolution(path.parent)):
                raise ValueError("prior game input is unresolved")
        if (store.root / "native-baseline.json").exists():
            raise ValueError("native baseline already exists; preserve its lineage")
        output = Path(tempfile.mkdtemp(prefix="native-bootstrap-", dir=store.root))
        identity = NativeStore(home).build(output, source=source, dotnet=dotnet)
        value = {"native_program": identity, "dotnet": str(Path(dotnet).resolve()), "at": utc_now(),
                 "actor": "developer_migration", "autonomous_evidence": False}
        atomic_json(store.root / "native-baseline.json", value)
        atomic_json(store.root / "developer-interventions" / (identity + "-native-baseline.json"), value)
        return {"status": "native_configured", "native_program": identity,
                "next": "refresh_program_baseline_while_idle", "eligible_for_client_release": False}
