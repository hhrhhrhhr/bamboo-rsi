"""Content-addressed Python/JS programs, separate from the trusted RSI controller.

Candidates may change the Genshin Decision adapter, checkpoint runner and recovery
policy plus the JS execute branch. Evidence, gates, credentials, native binaries
and the controller are sealed.
This boundary prevents changing the acceptance criteria; it is not an OS sandbox
for executing arbitrary hostile Python. Native-code optimization is not enabled.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from ...core.contracts import canonical_hash
from ...core.storage import atomic_json
from .episode import ExecutionLease, utc_now


ADAPTER = "integrations/auto_commission/agent-main.js"
EDITABLE = frozenset({"scenarios/genshin/decision.py", "scenarios/genshin/runner.py",
                      "scenarios/genshin/supervisor.py", ADAPTER})
CHECKS = ("test_genshin_autonomy.py", "test_genshin_checkpoint.py",
          "test_genshin_thinking.py", "test_genshin_recording.py", "test_genshin_rsi.py")
BUILD_VERSION = 2


def execution_block(source):
    start = '    } else if (settings.operation === "execute") {\n'
    end = '      // Execution return values never mark a commission complete.'
    text = source.replace("\r\n", "\n")
    if text.count(start) != 1 or text.count(end) != 1:
        raise ValueError("adapter execution boundary is missing or ambiguous")
    prefix, remainder = text.split(start)
    body, suffix = remainder.split(end)
    return prefix + start, body, end + suffix


def validate_adapter(before, after):
    import re
    original, candidate = execution_block(before), execution_block(after)
    if original[0] != candidate[0] or original[2] != candidate[2]:
        raise ValueError("adapter changed frozen recognition or journal evidence code")
    if re.search(r"\b(agentJournal|CommissionBasic|RecognitionObject|eval|require|import|constructor|prototype|globalThis)\b", candidate[1]):
        raise ValueError("adapter execution patch touches evidence or dynamic code")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def valid_id(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("invalid program identity")
    return value


def source_files(source, *, sealed=False):
    source = Path(source).resolve()
    files = {}
    for path in sorted(source.rglob("*")):
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError("program source cannot contain links")
        if path.is_file() and path.suffix == ".py":
            files[path.relative_to(source).as_posix()] = path.read_bytes()
        elif path.is_file() and sealed:
            raise ValueError("sealed program contains unexpected executable/data files")
    if not files or "scenarios/genshin/desktop.py" not in files:
        raise ValueError("program source is incomplete")
    return files


def runtime_hash(files):
    digest = hashlib.sha256()
    for name, raw in sorted(files.items()):
        digest.update(name.encode() + b"\0" + raw)
    return digest.hexdigest()


def control_hash(files):
    return canonical_hash({name: sha(raw) for name, raw in files.items() if name not in EDITABLE})


def dependencies():
    return {"python": list(sys.version_info[:3]), "packages": {
        name: importlib.metadata.version(name)
        for name in ("httpx", "httpcore", "pydantic", "pydantic-core", "anyio", "certifi", "h11", "idna", "socksio")}}


def evaluator_hash(root):
    root = Path(root)
    # Freeze all tests, not a candidate-supplied test selection or result.
    files = {p.relative_to(root).as_posix(): sha(p.read_bytes())
             for p in sorted((root / "platform/tests").glob("*.py"))}
    for name in ("scripts/genshin_program_entry.py", "scripts/validate_genshin_program.py",
                 "scripts/run_genshin_dev.py", "scripts/genshin_desktop_entry.py", "requirements-genshin.lock",
                 "integrations/auto_commission/agent-main.test.cjs"):
        files[name] = sha((root / name).read_bytes())
    return canonical_hash(files)


def clean_env():
    # Neither tests nor the proposer receive the game's API key or a Python path
    # capable of silently selecting the editable working copy.
    return {k: v for k, v in os.environ.items()
            if not any(word in k.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
            and k.upper() not in {"PYTHONPATH", "PYTHONHOME", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"}}


class ProgramStore:
    def __init__(self, home):
        self.home = Path(home).resolve()
        self.root = self.home / "rsi/programs"

    def directory(self, identity):
        path = self.root / valid_id(identity)
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("program escapes storage")
        return path

    def verify(self, identity, *, expected_control=None, check_dependencies=True):
        directory = self.directory(identity)
        manifest = json.loads((directory / "manifest.json").read_bytes())
        if canonical_hash(manifest) != identity or manifest.get("schema_version") != BUILD_VERSION:
            raise ValueError("program manifest changed")
        files = source_files(directory / "src/startlux", sealed=True)
        if ({k: sha(v) for k, v in files.items()} != manifest["files"]
                or runtime_hash(files) != manifest["runtime_sha256"]
                or control_hash(files) != manifest["control_sha256"]):
            raise ValueError("frozen program bytes changed")
        if expected_control is not None and manifest["control_sha256"] != expected_control:
            raise ValueError("program changed trusted controller/evaluator code")
        validation = manifest["validation"]
        if validation.get("passed") is not True or validation.get("exit_code") != 0:
            raise ValueError("program did not pass independent regression checks")
        if sha((directory / "validation.log").read_bytes()) != validation["log_sha256"]:
            raise ValueError("program validation evidence changed")
        if check_dependencies and manifest["dependencies"] != dependencies():
            raise ValueError("program Python dependencies changed")
        resources = directory / "resources"
        entries = list(resources.iterdir())
        if (len(entries) != 1 or entries[0].name != "agent-main.js" or entries[0].is_symlink()
                or sha(entries[0].read_bytes()) != manifest["resources"].get("agent-main.js")):
            raise ValueError("program execution resources changed")
        if manifest["parent"]:
            parent_dir = self.directory(manifest["parent"])
            parent = json.loads((parent_dir / "manifest.json").read_bytes())
            if canonical_hash(parent) != manifest["parent"]:
                raise ValueError("program parent manifest changed")
            changed = {k for k in set(parent["files"]) | set(manifest["files"])
                       if parent["files"].get(k) != manifest["files"].get(k)}
            if parent["resources"] != manifest["resources"]:
                changed.add(ADAPTER)
                validate_adapter((parent_dir / "resources/agent-main.js").read_text(encoding="utf-8"),
                                 entries[0].read_text(encoding="utf-8"))
            if (not changed or not changed <= EDITABLE or sorted(changed) != manifest["changed_files"]
                    or parent["control_sha256"] != manifest["control_sha256"]
                    or parent["dependencies"] != manifest["dependencies"]
                    or parent["evaluator_sha256"] != manifest["evaluator_sha256"]):
                raise ValueError("program exceeds the frozen optimization surface")
        return manifest

    def build(self, root, output, *, parent=None, replacements=None):
        """Assemble once, independently test, then publish an immutable artifact."""
        root, output = Path(root).resolve(), Path(output).resolve()
        output.mkdir(parents=True, exist_ok=True)
        destination = output / "program-build"
        destination.mkdir()  # Never overwrite a failed or interrupted build.
        if parent:
            before = self.verify(parent)
            files = source_files(self.directory(parent) / "src/startlux")
            if evaluator_hash(root) != before["evaluator_sha256"]:
                raise ValueError("independent evaluator changed since the parent build")
            if control_hash(source_files(root / "platform/src/startlux")) != before["control_sha256"]:
                raise ValueError("trusted runtime changed since the parent build")
            adapter = (self.directory(parent) / "resources/agent-main.js").read_bytes()
        else:
            files = source_files(root / "platform/src/startlux")
            adapter = (root / ADAPTER).read_bytes()
        changes = []
        for name, content in (replacements or {}).items():
            if name not in EDITABLE or not isinstance(content, str) or not 1 <= len(content) <= 60000:
                raise ValueError("code proposal is outside the allowed surface")
            raw = content.encode("utf-8")
            if name == ADAPTER:
                validate_adapter(adapter.decode("utf-8"), content)
                if raw != adapter:
                    adapter = raw
                    changes.append(name)
                continue
            if raw != files[name]:
                files[name] = raw
                changes.append(name)
        if parent and not changes:
            raise ValueError("program candidate made no code change")
        for name, raw in files.items():
            target = destination / "src/startlux" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            compile(raw, name, "exec")
        frozen = runtime_hash(files)
        (destination / "resources").mkdir()
        (destination / "resources/agent-main.js").write_bytes(adapter)
        evaluation = evaluator_hash(root)
        command = [sys.executable, "-I", "-B", str(root / "scripts/validate_genshin_program.py"),
                   str(destination / "src"), str(root)]
        env = {**clean_env(), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        with (destination / "validation.log").open("wb") as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, cwd=destination,
                                    env=env, timeout=240,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            # Fixed native-boundary tests run against the exact candidate JS in
            # a separate test directory. Candidate code cannot replace the test.
            js_test = output / "native-boundary"
            js_test.mkdir()
            (js_test / "agent-main.js").write_bytes(adapter)
            shutil.copyfile(root / "integrations/auto_commission/agent-main.test.cjs", js_test / "agent-main.test.cjs")
            node = shutil.which("node")
            if node is None:
                raise ValueError("Node.js is required to validate program execution resources")
            js_result = subprocess.run([node, str(js_test / "agent-main.test.cjs")], stdout=log,
                stderr=subprocess.STDOUT, cwd=js_test, env=env, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        report = {"passed": result.returncode == js_result.returncode == 0,
                  "exit_code": result.returncode or js_result.returncode,
                  "log_sha256": sha((destination / "validation.log").read_bytes()),
                  "checks": [*CHECKS, "agent-main.test.cjs"], "evidence_origin": "offline_regression",
                  "eligible_for_client_release": False}
        atomic_json(output / "build-report.json", report)
        if (not report["passed"] or runtime_hash(source_files(destination / "src/startlux", sealed=True)) != frozen
                or (destination / "resources/agent-main.js").read_bytes() != adapter
                or evaluator_hash(root) != evaluation):
            raise ValueError("independent program build/checks failed or files changed during validation")
        manifest = {"schema_version": BUILD_VERSION, "parent": parent, "runtime_sha256": frozen,
                    "control_sha256": control_hash(files), "evaluator_sha256": evaluation,
                    "dependencies": dependencies(), "files": {k: sha(v) for k, v in files.items()},
                    "resources": {"agent-main.js": sha(adapter)},
                    "changed_files": sorted(changes), "validation": report}
        identity = canonical_hash(manifest)
        atomic_json(destination / "manifest.json", manifest)
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.directory(identity)
        if target.exists():
            self.verify(identity)
        else:
            # Both directories are under the same private home in normal use.
            stage = Path(tempfile.mkdtemp(prefix=".publishing-", dir=self.root))
            shutil.copytree(destination, stage, dirs_exist_ok=True)
            stage.rename(target)
        self.verify(identity)
        return identity


def bundle_program(home, bundle):
    if bundle.program:
        return bundle.program
    path = Path(home) / "rsi/program-baseline.json"
    return json.loads(path.read_bytes())["program"] if path.exists() else None


def program_runtime(home, bundle, fallback):
    identity = bundle_program(home, bundle)
    return ProgramStore(home).verify(identity)["runtime_sha256"] if identity else fallback


def verify_running_program(home, bundle, audit):
    identity = bundle_program(home, bundle)
    if identity is None:
        return
    store = ProgramStore(home)
    manifest = store.verify(identity)
    executing_source = Path(__file__).resolve().parents[2]
    if (executing_source != store.directory(identity) / "src/startlux"
            or audit.value["runtime_sha256"] != manifest["runtime_sha256"]):
        raise ValueError("worker did not load the assigned frozen program")
    audit({"type": "program_bound", "program": identity, "parent": manifest["parent"],
           "runtime_sha256": manifest["runtime_sha256"], "control_sha256": manifest["control_sha256"]})


def bind_resources(pack, home, bundle, audit):
    """Bind a separately frozen adapter after resolving the fixed base policy."""
    from .taskpack import TaskPack
    identity = bundle_program(home, bundle)
    if not identity:
        return pack
    programs = ProgramStore(home)
    manifest = programs.verify(identity)
    raw = (programs.directory(identity) / "resources/agent-main.js").read_bytes()
    (pack.root / "agent-main.js").write_bytes(raw)
    value = pack.manifest.model_dump()
    value["files"]["agent-main.js"] = sha(raw)
    atomic_json(pack.root / "manifest.json", value)
    pack = TaskPack.load(pack.root)
    normalized_pack_hash(pack, home, bundle)
    audit({"type": "program_resources_bound", "program": identity, "resources": manifest["resources"],
           "manifest_sha256": sha((pack.root / "manifest.json").read_bytes())})
    return pack


def normalized_pack_hash(pack, home, bundle):
    identity = bundle_program(home, bundle)
    if not identity:
        return pack.policy_base_hash
    from .taskpack import TaskPack
    manifest = ProgramStore(home).verify(identity)
    base = TaskPack.load(Path(home) / "rsi/base-pack")
    value = pack.manifest.model_dump()
    if value["files"].get("agent-main.js") != manifest["resources"]["agent-main.js"]:
        raise ValueError("executed adapter differs from the assigned program")
    value["files"]["agent-main.js"] = base.manifest.files["agent-main.js"]
    value["files"].pop("instructions.txt")
    result = canonical_hash(value)
    if result != base.policy_base_hash:
        raise ValueError("program changed frozen task resources")
    return result


def enable_codex(home, root, *, executable="codex", max_calls=8, refresh=False):
    """Explicit idle migration; preserves every historical attempt and budget."""
    home, root = Path(home).resolve(), Path(root).resolve()
    if type(max_calls) is not int or not 1 <= max_calls <= 32:
        raise ValueError("Codex call budget must be 1..32")
    from .rsi import RSIStore
    store = RSIStore(home)
    with ExitStack() as stack:
        for path in (home, store.root, store.root / "service", store.root / "coordinator"):
            stack.enter_context(ExecutionLease(path))
        from .episode import has_stop_resolution
        for path in (home / "runs").glob("*/attempt.json"):
            meta = json.loads(path.read_bytes())
            if (meta.get("execution_armed") and not (meta.get("state") == "ended" and
                    meta.get("input_stop_acknowledged") is True) and not has_stop_resolution(path.parent)):
                raise ValueError("prior game input is unresolved")
        if store.read("active.json").get("campaign_hash"):
            raise ValueError("cannot migrate the optimizer during a frozen campaign")
        config = store.read("config.json")
        baseline = store.root / "program-baseline.json"
        if refresh:
            pointer = store.read("active.json")
            if pointer["generation"] or store.bundle(pointer["bundle_hash"]).program:
                raise ValueError("cannot replace a baseline with accepted code descendants")
            if baseline.exists():
                previous = store.read("program-baseline.json")
                archive = store.root / "developer-interventions" / (previous["program"] + "-baseline-migration.json")
                if archive.exists():
                    raise ValueError("baseline migration was already recorded")
                # Copy first; the previous program and all failed jobs stay intact.
                atomic_json(archive, {"at": utc_now(), "previous_baseline": previous,
                    "previous_config": config, "actor": "developer_migration", "autonomous_evidence": False})
                baseline.unlink()
        if baseline.exists():
            current = store.read("program-baseline.json")
            manifest = ProgramStore(home).verify(current["program"])
            if (manifest["runtime_sha256"] != runtime_hash(source_files(root / "platform/src/startlux"))
                    or manifest["evaluator_sha256"] != evaluator_hash(root)):
                raise ValueError("existing code baseline is frozen; use a recorded migration")
        else:
            directory = Path(tempfile.mkdtemp(prefix="bootstrap-", dir=store.root))
            identity = ProgramStore(home).build(root, directory)
            manifest = ProgramStore(home).verify(identity)
            atomic_json(directory / "previous-config.json", config)
            atomic_json(baseline, {"program": identity, "at": utc_now(), "previous_runtime": config["runtime_sha256"],
                                  "actor": "developer_migration", "autonomous_evidence": False})
        pending_path = store.root / "pending-candidate.json"
        if pending_path.exists():
            pending = json.loads(pending_path.read_bytes())
            if pending.get("status") == "pending":
                atomic_json(store.root / "developer-interventions" / (pending["candidate_hash"] + "-before-migration.json"), pending)
                atomic_json(pending_path, {**pending, "status": "superseded_by_program_migration"})
        config.update(optimizer_backend="codex_exec", codex_executable=executable,
                      runtime_sha256=manifest["runtime_sha256"])
        config.setdefault("max_codex_calls", max_calls)
        atomic_json(store.root / "config.json", config)
        atomic_json(store.root / "state.json", {"status": "paused", "reason": "codex_backend_configured", "at": utc_now()})
        return {"status": "configured", "program": store.read("program-baseline.json")["program"],
                "backend": "codex_exec", "eligible_for_client_release": False}
