"""Durable daily coordinator for Windows execution and isolated WSL optimization."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from ...core.contracts import canonical_hash
from ...core.scenario import PolicyBundle, GUARDRAILS
from ...core.storage import atomic_json
from .episode import ExecutionLease, configured_scope, execution_build, runtime_identity, utc_now
from .rsi import Bundle, RSIStore
from .snapshot import SnapshotSuite
from .taskpack import TaskPack
from .decision import configured_decision_url


def base_policy(pack):
    return PolicyBundle(scenario="genshin.daily", model=pack.manifest.decision_model,
        base_hash=pack.policy_base_hash, files={"instructions.md": pack.instructions, "memory.md": "",
                                              "tool_guidance.md": "", "guardrails.md": GUARDRAILS})


def wsl_path(path):
    path = Path(path).resolve()
    if not path.drive or len(path.drive) != 2:
        raise ValueError("WSL transfer requires a local drive path")
    return "/mnt/" + path.drive[0].lower() + "/" + "/".join(path.parts[1:])


def initialize(home, root, host, *, blocked_quota=False, wsl_python=None, distribution="Ubuntu-24.04"):
    from .desktop import prepare_daily_pack, configured_model
    import shutil
    import tempfile
    home, root, host = Path(home).resolve(), Path(root).resolve(), Path(host).resolve()
    store = RSIStore(home)
    config_path = store.root / "config.json"
    if config_path.exists():
        raise ValueError("RSI is already configured; preserve its frozen baseline")
    if wsl_python is None:
        linux_home = subprocess.check_output(["wsl.exe", "-d", distribution, "--", "printenv", "HOME"],
                                            text=True, encoding="utf-8", timeout=20).strip()
        wsl_python = linux_home + "/.cache/startlux-rsi-venv/bin/python"
    settings = json.loads((home / "settings.json").read_text(encoding="utf-8-sig"))
    if (home / "trial-pack").exists():
        raise ValueError("remove the external trial override before configuring RSI")
    with tempfile.TemporaryDirectory() as temporary:
        assets = Path(temporary)
        shutil.copytree(root / "integrations/auto_commission/upstream", assets / "autocommission")
        shutil.copyfile(root / "integrations/auto_commission/agent-main.js", assets / "agent-main.js")
        pack = prepare_daily_pack(assets, store.root / "base-pack", configured_model(settings))
    store.initialize(base_policy(pack))
    config = {"schema_version": 1, "root": str(root), "host": str(host), "home": str(home),
              "wsl_distribution": distribution, "wsl_python": wsl_python,
              "decision_url": configured_decision_url(settings),
              "optimizer_backend": "decision_search",
              "improver_url": "https://api.bamboonode.dev/v1/chat/completions", "improver_model": "gpt-6.1-sol",
              "recursion": 1, "max_improver_calls": 32, "max_decision_calls": 80,
              "max_evolve_cases": 4, "max_generations": 3, "poll_seconds": 30,
              "max_rehearsals_per_day": 2,
              "runtime_sha256": runtime_identity(), "execution_build": execution_build(host),
              "account": {k: settings[k] for k in ("uid", "server_id", "server_utc_offset_minutes")}}
    atomic_json(config_path, config)
    atomic_json(store.root / "state.json", {"status": "blocked_quota" if blocked_quota else "ready",
                                            "updated_at": utc_now()})
    return config


class Coordinator:
    def __init__(self, home, *, optimizer=None, launcher=None, native_validator=None):
        self.store = RSIStore(home)
        self.config = self.store.read("config.json")
        self.base = base_policy(TaskPack.load(self.store.root / "base-pack"))
        self.optimizer = optimizer or self.optimize
        self.launcher = launcher or self.launch
        self.native_validator = native_validator or self.validate_native

    def status(self, status, **fields):
        state = {"status": status, "updated_at": utc_now(), "providers": self.providers(), **fields}
        atomic_json(self.store.root / "state.json", state)
        return state

    def providers(self):
        path = self.store.root / "providers.json"
        return json.loads(path.read_bytes()) if path.exists() else {}

    def record_quota(self, services, *, job=None):
        providers = self.providers()
        for service in services:
            if service not in {"decision", "improver"}:
                continue
            providers[service] = {"status": "blocked_quota", "checked_at": utc_now(),
                "endpoint": self.config.get("decision_url" if service == "decision" else "improver_url"),
                "error_code": "insufficient_user_quota", **({"job": job} if job else {})}
        atomic_json(self.store.root / "providers.json", providers)

    def resume(self):
        # Explicit operator command after funding or correcting infrastructure.
        # Old jobs retain their costs and errors; only new evidence permits a new job.
        state = self.store.read("state.json")
        providers = self.providers()
        job_id = state.get("job") or providers.get("improver", {}).get("job")
        uses_chat = self.config.get("optimizer_backend", "harness2_chat") == "harness2_chat"
        if uses_chat and (state.get("status") == "blocked_quota" or providers.get("improver", {}).get("status") == "blocked_quota") and job_id:
            import uuid
            directory = self.store.root / "jobs" / job_id
            job = json.loads((directory / "job.json").read_bytes())
            output = directory / ("retry-" + uuid.uuid4().hex[:12])
            job.update(windows_output=str(output), output=wsl_path(output))
            atomic_json(self.store.root / "retry-job.json", {"status": "pending", "job": job})
        # Explicit retry resets only availability, never accumulated call budgets.
        for service, value in providers.items():
            if value.get("status") == "blocked_quota" and (service != "improver" or uses_chat):
                providers[service] = {**value, "status": "unknown", "retry_requested_at": utc_now()}
        if providers:
            atomic_json(self.store.root / "providers.json", providers)
        return self.status("ready", resumed_at=utc_now())

    def _attempts(self, scope):
        return self.store.attempts(scope)

    def cycle(self, *, now=None):
        now = now or datetime.now(timezone.utc)
        with ExecutionLease(self.store.root / "coordinator"):
            state = self.store.read("state.json")
            if (self.store.root / "stop.json").exists():
                return self.status("paused")
            providers = self.providers()
            if state["status"] in {"paused", "attention_required"}:
                return state
            if providers.get("decision", {}).get("status") == "blocked_quota":
                return self.status("blocked_quota", blocked_services=["decision"])
            if state["status"] == "blocked_quota" and providers.get("decision", {}).get("status") != "available":
                return state  # Legacy unscoped failures require an explicit probe or retry.
            if self.config["runtime_sha256"] != runtime_identity():
                return self.status("attention_required", reason="runtime_changed_after_configuration")
            if (self.store.root / "program-baseline.json").exists():
                from .rsi_program import ProgramStore, evaluator_hash
                baseline = ProgramStore(self.store.home).verify(self.store.read("program-baseline.json")["program"])
                if baseline["evaluator_sha256"] != evaluator_hash(self.config["root"]):
                    return self.status("attention_required", reason="independent_evaluator_changed")
            from .rsi_combat import recover_combat
            recover_combat(self.store.home, self.config["host"])
            active = self.store.active(self.base)
            pointer = self.store.read("active.json")
            scope = configured_scope(self.config["account"], now)
            if not pointer.get("campaign_hash"):
                from .rsi_rehearsal import pending_rehearsal
                rehearsal = pending_rehearsal(self, scope, now)
                if rehearsal is not None:
                    return rehearsal
            if pointer.get("campaign_hash"):
                report = self.store.finish(expected_active=active.identity, now=now)
                if not self.store.read("active.json").get("campaign_hash"):
                    return self.status("promoted" if report["gate"]["eligible_for_client_release"] else "rejected",
                                       gate=report["gate"])
            # A post-release failed day rolls back to the last accepted parent.
            if not pointer.get("campaign_hash") and pointer["generation"]:
                from .assessment import assess_run
                from .episode import has_stop_resolution
                for metadata_path in sorted((self.store.home / "runs").glob("*/attempt.json")):
                    path = metadata_path.parent
                    metadata = json.loads(metadata_path.read_bytes())
                    if metadata.get("rsi_assignment", {}).get("bundle_hash") == active.identity:
                        if (metadata.get("state") != "ended" and metadata.get("execution_armed")
                                and not has_stop_resolution(path)):
                            return self.status("attention_required", reason="prior_input_unresolved", run_id=path.name)
                        # The successful development rehearsal is intentionally
                        # ineligible as an autonomous day. It must not trigger
                        # rollback immediately after this candidate is promoted.
                        if (metadata.get("run_mode") == "development"
                                or metadata.get("rsi_assignment", {}).get("campaign_hash")):
                            continue
                        assessment = assess_run(path)
                        if metadata.get("state") == "ended" and not assessment["autonomy_eligible"]:
                            previous = self.store.rollback(expected_active=active.identity, reason="live_regression")
                            return self.status("rolled_back", bundle_hash=previous["bundle_hash"], run_id=path.name)
            if (not pointer.get("campaign_hash") and pointer["generation"] < self.config["max_generations"]
                    and (self.config.get("optimizer_backend", "harness2_chat") in {"decision_search", "codex_exec"}
                         or providers.get("improver", {}).get("status") != "blocked_quota")):
                proposal = self.prepare_job(active)
                if proposal:
                    self.status("optimizing", job=proposal["id"])
                    result = self.optimizer(proposal)
                    atomic_json(Path(proposal["windows_output"]) / "result.json", result)
                    if (self.store.root / "stop.json").exists():
                        return self.status("paused", job=proposal["id"])
                    if result["status"] == "blocked_quota":
                        services = result.get("blocked_services", ["unknown"])
                        self.record_quota(services, job=proposal["id"])
                        if set(services) != {"improver"}:
                            return self.status("blocked_quota", job=proposal["id"], blocked_services=services)
                        # A paid text improver can be unavailable while the free
                        # Decision endpoint still runs the accepted daily policy.
                    if result["status"] == "candidate_ready":
                        output = Path(proposal["windows_output"])
                        candidate = Bundle.model_validate_json((output / "candidate.json").read_bytes())
                        if candidate.parent != active.identity or candidate.identity != result["candidate_hash"]:
                            raise ValueError("optimizer returned an unbound candidate")
                        if self.config.get("max_rehearsals_per_day", 0):
                            from .rsi_rehearsal import queue_candidate, pending_rehearsal
                            queue_candidate(self, candidate, result, proposal)
                            return pending_rehearsal(self, scope, now)
                        if candidate.combat and candidate.combat != active.combat:
                            self.native_validator(candidate.combat, output)
                        first_day = (datetime.fromisoformat(scope.game_day) + timedelta(days=1)).date().isoformat()
                        plan = self.store.preregister(candidate, first_day=first_day,
                            training_days=result["training_days"], account_id=scope.account_id, server_id=scope.server_id,
                            server_utc_offset_minutes=self.config["account"]["server_utc_offset_minutes"], now=now,
                            runtime_sha256=self.config["runtime_sha256"], execution_build=self.config["execution_build"])
                        return self.status("campaign_registered", campaign_hash=plan.identity, first_day=first_day)
            # Enroll and launch once per day. Persist reservation before invoking
            # any child process, so crashes cannot repeat irreversible dailies.
            day_path = self.store.root / "execution-days" / (canonical_hash(scope.model_dump()) + ".json")
            if self._attempts(scope) or day_path.exists():
                return self.status("waiting_next_game_day", scope=scope.model_dump())
            if self.store.read("active.json").get("campaign_hash"):
                plan = self.store.plan(self.store.read("active.json")["campaign_hash"])
                if scope.game_day not in plan.assignments:
                    return self.status("waiting_campaign_start", first_day=min(plan.assignments))
            if not self.has_input_privilege():
                return self.status("admin_required")
            if execution_build(self.config["host"]) != self.config["execution_build"]:
                return self.status("attention_required", reason="native_build_changed")
            enrollment = self.store.enroll(scope, now=now)
            atomic_json(day_path, {"scope": scope.model_dump(), "status": "reserved", "at": utc_now(),
                                   "enrollment": enrollment})
            self.status("executing", scope=scope.model_dump())
            result = self.launcher()
            atomic_json(day_path, {"scope": scope.model_dump(), "status": "returned", "result": result,
                                   "enrollment": enrollment, "at": utc_now()})
            for path, _ in self._attempts(scope):
                if (path / "report.json").exists() and json.loads((path / "report.json").read_bytes()).get("error_type") == "QuotaExceeded":
                    self.record_quota(["decision"])
                    return self.status("blocked_quota", run_id=path.name, blocked_services=["decision"])
            return self.status("day_finished", scope=scope.model_dump(), result=result)

    def prepare_job(self, active):
        from .import_trace import captured_cases, collect_home
        retry_path = self.store.root / "retry-job.json"
        if retry_path.exists():
            retry = json.loads(retry_path.read_bytes())
            if (retry["status"] == "pending" and retry["job"].get("optimizer_backend", "harness2_chat")
                    == self.config.get("optimizer_backend", "harness2_chat")):
                parent = Bundle.model_validate_json((self.store.root / "jobs" / retry["job"]["id"] / "parent.json").read_bytes())
                if parent.identity != active.identity:
                    raise ValueError("cannot resume optimization against a changed parent")
                atomic_json(retry_path, {**retry, "status": "reserved"})
                return retry["job"]
        groups, runs = {}, []
        for run in sorted((self.store.home / "runs").iterdir()):
            if not run.is_dir():
                continue
            try:
                pack, cases = captured_cases(run)
                if pack.policy_base_hash != self.base.base_hash:
                    from .rsi_program import normalized_pack_hash
                    metadata = json.loads((run / "attempt.json").read_bytes())
                    bundle = self.store.bundle(metadata["rsi_assignment"]["bundle_hash"])
                    if normalized_pack_hash(pack, self.store.home, bundle) != self.base.base_hash:
                        continue
                for case in cases:
                    groups.setdefault(case.observation.scope.game_day, {}).setdefault(case.id, case)
                runs.append(run)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        if not groups:
            return None
        backend = self.config.get("optimizer_backend", "harness2_chat")
        if backend not in {"harness2_chat", "decision_search", "codex_exec"}:
            raise ValueError("unknown RSI optimizer backend")
        from .rsi_decision_search import SEARCH_VERSION
        from .rsi_rehearsal import environment, rejection_records
        rejections = list(rejection_records(self.store, self.config))
        code_failures = []
        if backend == "codex_exec":
            from .rsi_codex import build_rejections
            code_failures = build_rejections(self.store)
        problem = {"parent": active.identity, "captures": {k: sorted(v) for k,v in groups.items()},
            "backend": backend, "search_version": SEARCH_VERSION if backend == "decision_search" else None,
            "environment": environment(self.config),
            "development_training_days": self.config.get("development_training_days", []),
            "rejected_hypotheses": sorted({record["candidate_hash"] for record in rejections})}
        if backend == "codex_exec":
            problem["code_build_rejections"] = sorted({r["proposal_hash"] for r in code_failures})
            from .rsi_program import bundle_program
            problem["parent_program"] = bundle_program(self.store.home, active)
        problem_hash = canonical_hash(problem)
        days = sorted(groups)
        exposed = set()
        exposed.update(self.config.get("development_training_days", []))
        for prior in (self.store.root / "jobs").glob("*/job.json"):
            previous = json.loads(prior.read_bytes())
            if previous.get("problem_hash") == problem_hash:
                result_path = Path(previous["windows_output"]) / "result.json"
                result = json.loads(result_path.read_bytes()) if result_path.exists() else {}
                if result.get("status") not in {"candidate_ready", "replay_rejected", "no_change", "build_rejected"}:
                    return None  # No new budget for interrupted/unknown optimization.
            exposed.update(previous.get("training_days", []))
            exposed.update(previous.get("heldout_days", []))
        for gate in (self.store.root / "campaigns").glob("*/gate.json"):
            exposed.update(json.loads(gate.read_bytes()).get("rows", {}))
        # An earlier generation's validation is historical regression data,
        # never a fresh independent heldout set for its descendants.
        fresh = [day for day in days if day not in exposed]
        heldout = fresh[-1] if len(days) >= 2 and fresh else None
        training_days = [day for day in days if day != heldout]
        heldout_days = [heldout] if heldout else []
        # Exposure and live rejection evidence change the optimization problem
        # even when the captured image IDs are unchanged. Compute the cache key
        # after partitioning so a previous validation failure can become history
        # in the next generation, without reusing it as fresh validation.
        identity = canonical_hash({**problem, "training_days": training_days, "heldout_days": heldout_days})
        directory = self.store.root / "jobs" / identity
        if directory.exists():
            return None  # An unchanged failed/ambiguous problem keeps its original budget.
        evolve = [case for day in days if day != heldout for case in groups[day].values()]
        # Fixed deterministic cap for development cost; preserve the full corpus
        # and all failure packets alongside the explicitly selected training IDs.
        cap = self.config["max_evolve_cases"]
        # Keep early regressions and recent failures in the fixed-size budget.
        # Selection depends only on collection order, never probe outcomes.
        if len(evolve) > cap:
            early = cap // 2
            evolve = evolve[:early] + evolve[-(cap - early):]
        cases = evolve + ([c.model_copy(update={"split": "heldout"}) for c in groups[heldout].values()] if heldout else [])
        suite = SnapshotSuite(evidence_origin="captured_game", partition_method="game_day" if heldout else "unpartitioned", cases=cases)
        directory.mkdir(parents=True)
        collection = collect_home(self.store.home, directory / "collection")
        atomic_json(directory / "suite.json", suite.model_dump())
        atomic_json(directory / "parent.json", active.model_dump())
        failures, native_diagnostics = [], []
        for run in runs:
            assessment = json.loads((directory / "collection/assessment/attempts" / (run.name + ".json")).read_bytes())
            if (assessment.get("scope") or {}).get("game_day") == heldout:
                continue  # Combat repair must not see heldout failures either.
            metadata = json.loads((run / "attempt.json").read_bytes()) if (run / "attempt.json").exists() else {}
            executed_bundle = metadata.get("rsi_assignment", {}).get("bundle_hash")
            from .assessment import read_records
            records = dict(read_records(run)[0])
            for failure in assessment.get("failures", []):
                request = records.get(failure.get("request_line"), {})
                evidence = request.get("observation", {}).get("facts", {}).get("daily_evidence", {})
                action = failure.get("action") or {}
                prefix = str(action.get("arguments", {}).get("slot")) + ":"
                quest_id = next((q for q in evidence.get("expected_quest_ids", []) if q.startswith(prefix)), None)
                failures.append({**{k: failure.get(k) for k in ("code", "category", "decision", "action")},
                    "executed_bundle": executed_bundle, "quest_id": quest_id,
                    "game_day": (assessment.get("scope") or {}).get("game_day")})
            native = run / "native-diagnostics.txt"
            if native.exists():
                text = native.read_text(encoding="utf-8")
                blocks = [block for block in text.split("\n\n") if any(
                    word in block for word in ("[WRN]", "[ERR]", "卡死", "超时", "失败", "timeout"))]
                excerpt = "\n\n".join("\n".join(block.splitlines()[:5])[:800] for block in blocks[-8:])
                native_diagnostics.append({"run_id": run.name, "sha256": hashlib.sha256(native.read_bytes()).hexdigest(),
                                           "excerpt": excerpt, "selection": "last_eight_warning_or_failure_blocks"})
        script = Path(self.config["host"]) / "User/AutoFight/StartLux/bennett-ayaka-yelan-zhongli.txt"
        job = {**{k: self.config[k] for k in ("improver_url", "improver_model", "decision_url", "recursion",
                                             "max_improver_calls", "max_decision_calls")},
            "id": identity, "problem_hash": problem_hash,
            "windows_output": str(directory / "output"), "failures": failures,
            "optimizer_backend": backend, "native_diagnostics": native_diagnostics[-3:],
            "rejected_hypotheses": [self.store.bundle(record["candidate_hash"]).model_dump()
                for record in rejections][-8:],
            "repair_combat": any(f["category"] == "execution" and (f.get("action") or {}).get("skill") == "daily.execute" for f in failures),
            "repair_timed_route": any(f["category"] == "execution"
                and (f.get("quest_id") or "").split(":")[-1] == "极速前进" for f in failures),
            "combat_baseline": {"script": script.read_text(encoding="utf-8-sig"), "sha256": hashlib.sha256(script.read_bytes()).hexdigest()},
            "pack": wsl_path(self.store.root / "base-pack"), "suite": wsl_path(directory / "suite.json"),
            "parent": wsl_path(directory / "parent.json"), "output": wsl_path(directory / "output"),
            "budget_root": wsl_path(directory / "budgets"),
            "training_days": training_days, "heldout_days": heldout_days,
            "selected_evolve_ids": [c.id for c in evolve], "collection_attempts": collection["attempt_count"]}
        atomic_json(directory / "job.json", job)
        if backend == "codex_exec":
            from .rsi_program import bundle_program
            job["parent_program"] = bundle_program(self.store.home, active)
            job["code_rejections"] = code_failures[-3:] + [{"candidate_hash": r["candidate_hash"], "status": r.get("status"),
                "issues": (r.get("assessment") or {}).get("issues", [])} for r in rejections][-4:]
            atomic_json(directory / "job.json", job)
        return job

    def optimize(self, job):
        if job.get("optimizer_backend") == "codex_exec":
            from .rsi_codex import optimize
            return optimize(self, job)
        # Import the existing Windows DPAPI reader without exposing the key in
        # command arguments, environment variables, logs, or a WSL key file.
        import importlib.util
        root = Path(self.config["root"])
        spec = importlib.util.spec_from_file_location("credential_reader", root / "scripts/run_genshin_dev.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        key = module.read_credential(self.store.home / "credential.dpapi")
        output = Path(job["windows_output"])
        output.mkdir(parents=True, exist_ok=True)
        atomic_json(output / "worker-job.json", job)
        with (output / "worker.log").open("w", encoding="utf-8") as log:
            worker = None
            try:
                worker = subprocess.Popen(["wsl.exe", "-d", self.config["wsl_distribution"], "--cd", wsl_path(root), "--",
                    self.config["wsl_python"], "-m", "startlux.scenarios.genshin.rsi_worker",
                    wsl_path(output / "worker-job.json")], stdin=subprocess.PIPE, text=True, encoding="utf-8",
                    stdout=log, stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                worker.stdin.write(key + "\n")
                worker.stdin.flush()
                key = ""
                deadline = time.monotonic() + 7200
                while worker.poll() is None:
                    if (self.store.root / "stop.json").exists() or time.monotonic() >= deadline:
                        worker.stdin.write("stop\n")
                        worker.stdin.flush()
                        worker.wait(timeout=15)
                        return {"status": "optimizer_interrupted", "eligible_for_client_release": False}
                    time.sleep(0.5)
            finally:
                key = ""
                if worker is not None:
                    worker.stdin.close()  # EOF also stops the worker after coordinator failure.
        if worker.returncode or not (output / "result.json").exists():
            return {"status": "optimizer_failed", "exit_code": worker.returncode, "eligible_for_client_release": False}
        return json.loads((output / "result.json").read_bytes())

    def validate_native(self, patch, output):
        root = Path(self.config["root"])
        script, report = output / "candidate-combat.txt", output / "native-combat-validation.json"
        script.write_bytes(patch.script.encode("utf-8"))
        subprocess.run([str(root / ".runtime/dotnet/dotnet.exe"),
            str(root / ".runtime/ocr-probe/bin/Debug/net8.0-windows/OcrProbe.dll"), self.config["host"],
            "--validate-combat", str(script), str(report)], capture_output=True, check=True, timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        result = json.loads(report.read_bytes())
        if result.get("syntax_valid") is not True or result.get("game_execution") is not False:
            raise ValueError("native combat parser did not validate the candidate")

    def launch(self, *, rehearsal=None):
        root = Path(self.config["root"])
        worker = subprocess.Popen([sys.executable, str(root / "scripts/run_genshin_dev.py"),
                                 "--run-mode", "development" if rehearsal else "autonomous",
                                 *(["--rsi-rehearsal", rehearsal] if rehearsal else []),
                                 "--home", str(self.store.home), "--install", str(Path(self.config["host"]).parent)],
            cwd=root, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            worker.wait(timeout=4200)
        except subprocess.TimeoutExpired:
            atomic_json(root / ".runtime/live-command.json", {"id": time.time_ns(), "action": "stop"})
            worker.wait(timeout=20)  # Never kill an unacknowledged input watchdog.
        return {"exit_code": worker.returncode}

    def has_input_privilege(self):
        if os.name != "nt":
            return False
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())

    def serve(self):
        with ExecutionLease(self.store.root / "service"):
            atomic_json(self.store.root / "service.json", {"pid": os.getpid(), "started_at": utc_now()})
            try:
                while not (self.store.root / "stop.json").exists():
                    try:
                        self.cycle()
                    except Exception as error:
                        self.status("attention_required", error_type=type(error).__name__)
                    time.sleep(self.config["poll_seconds"])
            finally:
                atomic_json(self.store.root / "service.json", {"status": "stopped", "at": utc_now()})
