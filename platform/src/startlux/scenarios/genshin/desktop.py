"""Lean Windows runtime; optimization runs in a separate environment."""
from __future__ import annotations

import argparse
import base64
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import sys
import threading
import time
import uuid

from ...core.settings import DecisionSettings as Settings
from ...core.releases import PolicyStore
from ...core.scenario import PolicyBundle, GUARDRAILS
from ...core.contracts import canonical_hash
from ...core.storage import atomic_json
from .contracts import AccountScope
from .daily import CommissionEvidence, RewardEvidence, verify_daily
from .decision import StartLuxRealtimeProvider, configured_decision_url
from .runner import AuditLog, RealtimeRunner
from .taskpack import TaskPack
from .windows_pipe import WindowsPipeSession
from .episode import EpisodeAudit, ExecutionLease, reconcile_attempt, record_acknowledged_stop, execution_build
from .supervisor import DecisionRecovery
from .recording import RecordingError


class DesktopSetupError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class GameReadyTimeout(TimeoutError):
    code = "game_not_ready"


def error_fields(error: Exception) -> dict:
    fields = {"error_type": type(error).__name__}
    if isinstance(error, (DesktopSetupError, GameReadyTimeout, RecordingError)):
        fields["error_code"] = error.code
    armed = getattr(error, "_startlux_execution_armed", None)
    if type(armed) is bool:
        fields["execution_armed"] = armed
    return fields


def configured_model(config: dict) -> str:
    confirmed = config.get("decision_model_confirmed", config.get("top_ranked_deployment_confirmed"))
    model = config.get("model_id")
    if confirmed is not True:
        raise DesktopSetupError("model_unconfirmed", "confirm the selected StartLux model deployment")
    if not isinstance(model, str) or not model.strip() or model.strip().startswith(("fixture-", "configure-")):
        raise DesktopSetupError("model_invalid", "an explicit StartLux deployment is required")
    return model.strip()


def prepare_daily_pack(source: Path, destination: Path, model: str) -> TaskPack:
    model = configured_model({"model_id": model, "decision_model_confirmed": True})
    if not source.is_dir() or not (source / "agent-main.js").is_file():
        raise DesktopSetupError("assets_missing", "daily execution assets are missing; build them or supply --assets")
    destination.mkdir(parents=True, exist_ok=False)
    files = {}
    for item in sorted(source.rglob("*")):
        if not item.is_file():
            continue
        name = item.relative_to(source).as_posix()
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = item.read_bytes()
        target.write_bytes(raw)
        files[name] = hashlib.sha256(raw).hexdigest()
    common = {"region": "Mondstadt", "account_verified": True, "foreground": True}
    actions = [{"id": "scan", "label": "返回游戏主界面，打开冒险之证并逐项识别四个真实每日委托及完成标记",
                "skill": "daily.scan", "skill_version": "1", "arguments": {},
                "preconditions": {**common, "daily_needs_scan": True}, "timeout_ms": 180000, "ttl_ms": 30000}]
    for slot in range(1, 5):
        actions.append({"id": f"quest-{slot}", "label": f"执行已经识别并支持的第 {slot} 项蒙德每日委托，结束后重新读取委托页",
                        "skill": "daily.execute", "skill_version": "1", "arguments": {"slot": slot},
                        "preconditions": {**common, f"daily_slot_{slot}_pending": True},
                        "timeout_ms": 300000, "ttl_ms": 30000})
    actions.append({"id": "claim", "label": "四项真实委托均已完成，前往蒙德凯瑟琳只领取每日委托奖励并检查冒险之证",
                    "skill": "daily.claim", "skill_version": "1", "arguments": {},
                    "preconditions": {**common, "daily_needs_scan": False, "daily_all_complete": True,
                                      "daily_reward_claimed": False}, "timeout_ms": 180000, "ttl_ms": 30000})
    content = {"actions.json": json.dumps(actions, ensure_ascii=False, indent=2).encode(),
               "instructions.txt": ("用户要求完成今天四个真实每日委托并领取凯瑟琳奖励。只选择可用目录内的动作。"
                   "第一张图是当前游戏画面，其余图是保存的手册证据，可能早于刚结束的委托。"
                   "daily_needs_scan为true且scan可用时，优先选择scan获取新的手册状态；扫描不消费游戏资源。"
                   "每次执行委托后必须先扫描冒险之证；脚本返回不等于委托完成，历练点4/4不等于真实委托完成。"
                   "观察 daily_evidence 中的任务名称、状态和截图。无法判断时弃权。禁止花原石、体力或购买物品。"
                   f"本次决策模型固定为用户确认的部署 {model}；不得替换为其他模型。").encode()}
    for name, raw in content.items():
        (destination / name).write_bytes(raw)
        files[name] = hashlib.sha256(raw).hexdigest()
    manifest = {"schema_version": 2, "id": "startlux.mondstadt.daily", "version": "0.3.0", "game_id": "genshin",
                "environment": "windows", "release_status": "development", "decision_model": model,
                "regions": ["Mondstadt"], "files": files,
                "attributions": ["AutoCommission LX666-666 and contributors, GPL-3.0, f9b5a49942b613c545e9bea3bed4d015424844d1",
                                 "BetterGI babalae and contributors, GPL-3.0; StartLux daily adapter"]}
    (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return TaskPack.load(destination)


def load_run_policy(pack: TaskPack, home: Path, *, scope=None, rehearsal=None, run_mode="autonomous"):
    """Freeze an explicitly installed trial pack or a previously validated release."""
    base = PolicyBundle(scenario="genshin.daily", model=pack.manifest.decision_model,
        base_hash=pack.policy_base_hash, files={"instructions.md": pack.instructions,
            "memory.md": "", "tool_guidance.md": "", "guardrails.md": GUARDRAILS})
    trial_path = home / "trial-pack"
    rsi = None
    if rehearsal and (run_mode != "development" or not (home / "rsi/active.json").exists()):
        raise ValueError("RSI rehearsal requires a development run and an initialized RSI store")
    if (home / "rsi/active.json").exists():
        from .rsi import RSIStore
        if trial_path.exists() or scope is None:
            raise ValueError("RSI requires an explicit scope and no external trial override")
        store = RSIStore(home)
        active = store.active(base)
        if rehearsal:
            if run_mode != "development":
                raise ValueError("RSI rehearsal must be marked development")
            bundle = store.bundle(rehearsal)
            if bundle.parent != active.identity:
                raise ValueError("rehearsal must inherit the current accepted parent")
            if (bundle.policy.scenario, bundle.policy.base_hash, bundle.policy.model) != (
                    base.scenario, base.base_hash, base.model):
                raise ValueError("rehearsal changed fixed resources or model")
            enrollment = None
        else:
            bundle, enrollment = store.assignment(scope)
        instructions, identity, source = bundle.policy.instructions, bundle.policy.identity, "rsi"
        rsi = {"bundle_hash": bundle.identity, "enrollment": enrollment, **({"rehearsal": True} if rehearsal else {})}
    elif trial_path.exists():
        trial = TaskPack.load(trial_path)
        if trial.policy_base_hash != pack.policy_base_hash:
            raise ValueError("trial pack changed frozen model, actions or execution resources")
        instructions, identity, source = trial.instructions, canonical_hash(trial.manifest.model_dump()), "development_trial"
    else:
        if rehearsal:
            raise ValueError("RSI rehearsal requires an initialized RSI store")
        active = PolicyStore(home / "policies").active(base)
        instructions = active.instructions if active else pack.instructions
        identity = active.identity if active else base.identity
        source = "validated_release" if active else "bundled_base"
    if instructions != pack.instructions:
        raw = instructions.encode("utf-8")
        (pack.root / "instructions.txt").write_bytes(raw)
        manifest = pack.manifest.model_dump()
        manifest["files"]["instructions.txt"] = hashlib.sha256(raw).hexdigest()
        (pack.root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        pack = TaskPack.load(pack.root)
    return pack, {"type": "policy_loaded", "policy_hash": identity, "source": source,
                  **({"rsi": rsi} if rsi else {})}


def report_from_observation(observation, *, expected_scope=None):
    """Require archived PNGs for every accepted piece of journal evidence."""
    if ((expected_scope is not None and observation.scope != expected_scope)
            or observation.facts.get("daily_needs_scan") is True):
        return {"status": "unverified", "commission_completed_count": 0, "daily_reward_claimed": None}
    data = observation.facts.get("daily_evidence", {})
    ids = data.get("expected_quest_ids", [])
    if len(ids) != 4:
        return {"status": "unverified", "commission_completed_count": 0, "daily_reward_claimed": None}
    images = {hashlib.sha256(base64.b64decode(image.split(",", 1)[1], validate=True)).hexdigest()
              for image in observation.images}
    commissions = [CommissionEvidence.model_validate(row) for row in data.get("commissions", [])]
    reward = RewardEvidence.model_validate(data["reward"]) if data.get("reward") else None
    for evidence in [*(row.evidence for row in commissions), *([reward.evidence] if reward else [])]:
        if evidence.screenshot_sha256 not in images:
            raise ValueError("journal evidence is missing its screenshot")
    return verify_daily(observation.scope, ids, commissions, reward).model_dump(mode="json")


def emit(event):
    # ASCII JSON survives pythonw/console GBK defaults and UTF-8 readers alike.
    print(json.dumps(event, ensure_ascii=True), flush=True)


def wait_for_game_ready(session, uid, *, seconds=180, audit=None):
    """Wait for a verified foreground account and a known recoverable game UI."""
    deadline = time.monotonic() + seconds
    previous_readiness = None
    while time.monotonic() < deadline:
        try:
            state = session.observe()
            scene = state.facts.get("scene")
            text = state.facts.get("ocr_text")
            # BetterGI can label the Paimon menu unknown or map (shared back
            # controls). Its three OCR anchors take precedence over that broad
            # template result. A verified foreground map is also recoverable:
            # daily.scan closes it with returnMainUi before opening the journal.
            if scene in {"unknown", "map"} and isinstance(text, str):
                compact_text = re.sub(r"\s+", "", text)
                if all(anchor in compact_text for anchor in ("冒险等阶", "世界等级", "冒险之证")):
                    scene = "paimon_menu"
                elif all(anchor in compact_text for anchor in
                         ("选择委托任务倾向地域", "每日委托奖励", "历练点阶段奖励")):
                    scene = "commission_journal"
            readiness = (state.scope.account_id, state.facts.get("account_verified"),
                         state.facts.get("foreground"), scene)
            if audit is not None and readiness != previous_readiness:
                audit({"type": "startup_observation", "observation": audit.archive_observation(state)})
                previous_readiness = readiness
            if state.scope.account_id != uid:
                raise DesktopSetupError("account_mismatch", "game UID differs from the requested account")
            if (state.facts.get("account_verified") is True and state.facts.get("foreground") is True
                    and scene in {"world", "map", "paimon_menu", "commission_journal", "cold_defeat"}):
                if audit is not None and scene != "world":
                    audit({"type": "startup_menu_ready", "scene": scene,
                           "recovery_skill": "daily.scan", "input_executed": False})
                return state
        except (OSError, TimeoutError, ConnectionError, RuntimeError):
            pass  # Read-only startup/capture failures can be retried before arming.
        time.sleep(1)
    raise GameReadyTimeout("game did not reach a verified, recoverable game view")


class ExecutionHeartbeat:
    """Keep the native lease alive independently of OCR and screenshot transport."""
    def __init__(self, controller):
        self.controller = controller
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stopped.wait(0.5):
            active = self.controller.active
            if active is not None and not self.controller.stopped.is_set():
                try:
                    self.controller.session.reconcile(active.command_id)
                except (ConnectionError, TimeoutError, OSError):
                    pass  # Native watchdog releases inputs if this connection is lost.

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["run", "stop", "reconcile", "self-test"])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--pipe", default="startlux-genshin")
    parser.add_argument("--assets", type=Path, help="daily execution resources for source development runs")
    parser.add_argument("--run-mode", choices=["development", "autonomous"], default="development",
                        help="development runs never qualify as unattended daily evidence")
    parser.add_argument("--rsi-rehearsal", help="explicit candidate hash for a development-only rehearsal")
    parser.add_argument("--run-id", help="interrupted attempt to reconcile using its original input pipe")
    parser.add_argument("--host-directory", type=Path, help="native host directory for execution provenance")
    args = parser.parse_args()
    if args.operation == "self-test":
        # Imports and DTO validation in the actual frozen executable.
        AccountScope(account_id="123456789", server_id="cn", game_day="2026-01-01")
        from ... import __version__
        emit({"status": "runtime_ready", "platform": sys.platform, "platform_version": __version__})
        return
    if args.operation == "stop":
        for attempt in range(3):
            try:
                WindowsPipeSession(args.pipe).stop()
                if args.config is not None:
                    record_acknowledged_stop(args.config.resolve().parent, args.pipe)
                break
            except (OSError, TimeoutError, ConnectionError):
                if attempt == 2:
                    raise
                time.sleep(0.05)
        emit({"status": "stop_sent"})
        return
    if args.config is None:
        raise ValueError("desktop configuration is required")
    if args.operation == "reconcile":
        if not args.run_id:
            raise ValueError("reconciliation requires an explicit run ID")
        result = reconcile_attempt(args.config.resolve().parent, args.run_id,
                                   lambda pipe: WindowsPipeSession(pipe).stop())
        emit({"status": "reconciled", **result})
        return
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    config["model_id"] = configured_model(config)
    with ExecutionLease(args.config.resolve().parent):
        execute_run(args, config)


def execute_run(args, config):
    # Credential comes through an anonymous inherited stdin pipe; never CLI/env/file.
    key = sys.stdin.readline().strip()
    if not key or any(char.isspace() for char in key):
        raise ValueError("invalid Decision credential")
    home = args.config.resolve().parent
    run = home / "runs" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    run.mkdir(parents=True)
    audit = EpisodeAudit(run, config, args.run_mode)
    provider = controller = heartbeat = None
    combat_lease = None
    recording = None
    from .rsi_route import TimedRoutePolicy
    route_policy = TimedRoutePolicy()
    route_bundle_hash = None
    stop_ack = False
    execution_armed = False
    termination = "error"
    try:
        unresolved = audit.unresolved_attempts()
        if unresolved:
            audit({"type": "prior_execution_unresolved", "run_ids": unresolved})
            raise DesktopSetupError("prior_execution_unresolved", "a previous input lease needs reconciliation")
        host_directory = args.host_directory or (Path(sys.executable).parent.parent / "bettergi"
                                                 if getattr(sys, "frozen", False) else None)
        assets = args.assets or (Path(sys.executable).parent.parent / "task-source" if getattr(sys, "frozen", False)
                                 else Path(__file__).resolve().parents[5] / "dist" / "genshin-task-source")
        pack = prepare_daily_pack(assets, run / "pack", config["model_id"])
        from .episode import configured_scope
        launch_scope = configured_scope(config)
        pack, policy_event = load_run_policy(pack, home, scope=launch_scope,
            rehearsal=getattr(args, "rsi_rehearsal", None), run_mode=args.run_mode)
        audit(policy_event)
        if "rsi" in policy_event:
            from .rsi import RSIStore
            from .rsi_combat import CombatLease
            store = RSIStore(home)
            bundle = store.bundle(policy_event["rsi"]["bundle_hash"])
            from .rsi_program import verify_running_program, bind_resources
            verify_running_program(home, bundle, audit)
            pack = bind_resources(pack, home, bundle, audit)
            route_policy = bundle.route or TimedRoutePolicy()
            route_bundle_hash = bundle.identity
            try:
                route_policy.require_combat()
            except ValueError as error:
                raise DesktopSetupError("required_combat", str(error)) from error
            enrollment = policy_event["rsi"]["enrollment"]
            if enrollment:
                plan = store.plan(enrollment["campaign_hash"])
                if (audit.value["runtime_sha256"] != plan.expected_runtime(bundle.identity)
                        or execution_build(host_directory) != plan.execution_build or args.run_mode != "autonomous"):
                    raise ValueError("campaign runtime or autonomous mode changed")
                store.consume(enrollment, run.name)
            audit({"type": "rsi_assignment", "bundle_hash": bundle.identity,
                   "campaign_hash": enrollment["campaign_hash"] if enrollment else None})
            if bundle.combat:
                if host_directory is None:
                    raise ValueError("combat candidate requires an owned native host")
                combat_lease = CombatLease(host_directory, bundle.combat, run / "combat-install.json")
                combat_lease.apply()
        audit({"type": "execution_build", "files": execution_build(host_directory)})
        host_config = {"binding": {"account_id": config["uid"], "server_id": config["server_id"],
                        "server_utc_offset_minutes": config["server_utc_offset_minutes"], "region": "Mondstadt"},
                       "pack_directory": str(pack.root), "timed_route_policy": route_policy.model_dump(),
                       "record_video": config.get("record_video", False)}
        host_path = home / "bridge.json"
        host_path.write_text(json.dumps(host_config), encoding="utf-8")
        emit({"status": "pack_ready", "bridge_config": str(host_path), "pipe": args.pipe,
              "run_directory": str(run)})
        # The UI starts BetterGI only after pack/config exist and acknowledges via stdin.
        if sys.stdin.readline().strip() != "host_started":
            raise DesktopSetupError("host_not_acknowledged", "execution host was not acknowledged")
        session = WindowsPipeSession(args.pipe)
        descriptor = None
        connect_deadline = time.monotonic() + 90
        while time.monotonic() < connect_deadline:
            try:
                descriptor = session.describe()
                break
            except (OSError, TimeoutError, ConnectionError):
                time.sleep(0.5)
        if descriptor is None:
            raise DesktopSetupError("bridge_unavailable", "execution host did not connect")
        if config.get("record_video", False):
            if descriptor.get("game_video_v1") is not True:
                raise DesktopSetupError("bridge_mismatch", "native game recording is unavailable")
            from .recording import GameRecording
            recording = GameRecording(run)
        if descriptor.get("timed_route_policy") != route_policy.model_dump():
            raise DesktopSetupError("bridge_mismatch", "native route policy differs from the selected bundle")
        audit({"type": "timed_route_policy_bound", "bundle_hash": route_bundle_hash,
               "expected": route_policy.model_dump(), "native": descriptor["timed_route_policy"]})
        if (descriptor.get("decision_model") != pack.manifest.decision_model
                or descriptor.get("manifest_file_sha256") != hashlib.sha256((pack.root / "manifest.json").read_bytes()).hexdigest()):
            raise DesktopSetupError("bridge_mismatch", "execution host task pack/model mismatch")
        settings = replace(Settings.load(), decision_api_key_file="", decision_model=config["model_id"],
                           decision_url=configured_decision_url(config))
        audit({"type": "provider_endpoint_bound", "endpoint": settings.decision_url,
               "model": settings.decision_model})
        provider = StartLuxRealtimeProvider(settings)
        provider._headers["Authorization"] = "Bearer " + key
        key = ""
        controller = RealtimeRunner(session, provider, pack.instructions, model=pack.manifest.decision_model, audit=audit,
            checkpoint_enabled="command_checkpoint_v1" in descriptor.get("capabilities", []))
        heartbeat = ExecutionHeartbeat(controller)
        heartbeat.thread.start()
        emit({"status": "waiting_for_game"})
        ready = wait_for_game_ready(session, config["uid"], audit=audit)
        if recording is not None:
            audit({"type": "video_started", "recording": recording.wait_started()})
        if "rsi" in policy_event and ready.scope != launch_scope:
            raise DesktopSetupError("scope_changed", "account or game day changed before input")
        recovery = DecisionRecovery(audit.recovery_history(ready.scope))
        controller.checkpoint_count = sum(r["reason"] in {"timeout_journal_checkpoint", "failure_journal_checkpoint"}
                                         for r in audit.recovery_history(ready.scope))
        # Treat an ambiguous arm response conservatively during host cleanup.
        execution_armed = True
        audit({"type": "execution_arming", "pipe": args.pipe, "scope": ready.scope.model_dump()})
        session.arm()
        emit({"status": "running", "trace": str(run / "trace.jsonl")})
        deadline = time.monotonic() + 3600
        decisions = 0
        while time.monotonic() < deadline and decisions < 100:
            if recording is not None:
                recording.check()
            deciding = controller.active is None
            result = controller.tick()
            decisions += int(deciding)
            emit({"status": "progress", "execution": result})
            retry = recovery.consider(result, stopped=controller.stopped.is_set(), active=controller.active is not None)
            if retry is not None:
                audit(retry)  # Persist budget before attempting the recovery.
                if retry["type"] == "recovery_exhausted":
                    termination = "recovery_exhausted"
                    break
                if retry["operation"] == "wait_for_readiness":
                    emit({"status": "waiting_for_game"})
                    wait_for_game_ready(session, config["uid"], seconds=30, audit=audit)
                else:
                    # A new tick takes new pixels and requests a new receipt;
                    # no old decision or action command is resubmitted.
                    controller.stopped.wait(1)
                continue
            if result["status"] not in {"accepted", "running", "succeeded", "reconciling", "checkpointed"}:
                termination = result["status"]
                break
            time.sleep(0.2)
        else:
            termination = "budget_exhausted"
        observation = session.observe()
        audit({"type": "final_observation", "observation": audit.archive_observation(observation)})
        report = report_from_observation(observation, expected_scope=ready.scope)
        report.update(termination_reason=termination, trace=str(run / "trace.jsonl"),
                      evidence_origin="windows_live", model=config["model_id"])
        (run / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        audit({"type": "daily_report", "report": report})
        emit({"status": "finished", "report": report})
    except Exception as error:
        termination = "error"
        error._startlux_execution_armed = execution_armed
        report = {"status": "unverified", "termination_reason": termination,
                  **error_fields(error), "trace": str(run / "trace.jsonl"),
                  "evidence_origin": "windows_live" if execution_armed else "windows_startup",
                  "model": config["model_id"]}
        (run / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        audit({"type": "daily_report", "report": report})
        raise
    finally:
        key = ""
        try:
            if controller is not None:
                try:
                    controller.stop()
                    stop_ack = True
                    audit({"type": "input_stop", "acknowledged": True})
                except Exception as stop_error:
                    # Preserve the original run error and the host watchdog.
                    audit({"type": "input_stop", "acknowledged": False,
                           "error_type": type(stop_error).__name__})
        finally:
            if heartbeat is not None:
                heartbeat.close()
            if recording is not None:
                try:
                    video = recording.stop()
                    atomic_json(run / "video-report.json", video)
                    audit({"type": "video_finalized", "recording": video})
                    emit({"status": "video_finalized", "recording": video})
                except Exception as video_error:
                    audit({"type": "video_finalize_failed", "error_type": type(video_error).__name__})
            if combat_lease is not None:
                try:
                    combat_lease.restore(stopped=stop_ack or not execution_armed)
                except (OSError, RuntimeError, ValueError) as error:
                    audit({"type": "combat_restore_failed", "error_type": type(error).__name__})
            audit({"type": "run_ended", "termination_reason": termination})
            if provider is not None:
                provider.close()
            audit.close()
            # Offline assessment never changes input or substitutes for a model.
            # Preserve the original runtime exception if evidence packaging fails.
            try:
                from .assessment import assess_run, assess_home
                atomic_json(run / "feedback.json", assess_run(run))
                assess_home(home, home / "episodes")
            except Exception as assessment_error:
                try:
                    atomic_json(run / "assessment-error.json", {"error_type": type(assessment_error).__name__})
                except OSError:
                    emit({"status": "assessment_failed", "error_type": type(assessment_error).__name__})


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Never surface credential-bearing HTTP or native exception strings.
        emit({"status": "error", **error_fields(error)})
        raise SystemExit(1)
