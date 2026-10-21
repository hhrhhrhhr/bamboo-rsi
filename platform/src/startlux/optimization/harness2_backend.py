"""Actual Google Harness² orchestration over the platform's scenario interface.

This optional module runs in the Linux optimizer environment. The Windows
runtime imports neither it nor the upstream research package.
"""
from dataclasses import dataclass
import json
from pathlib import Path
import re

from harness2.core import schedule, tree, prompts
from harness2.core.spec import BenchPrompts, BenchSpec, ChannelSpec
from harness2.core.tree import SurfaceSpec

from ..core.contracts import canonical_hash
from ..core.scenario import POLICY_FILES, candidate_policy, measure_pair, Trial
from ..core.storage import atomic_json

UPSTREAM_COMMIT = "1cfe254e8d25b15f37048c3730134686abe60335"
SURFACE = SurfaceSpec(name="startlux", always_on=POLICY_FILES, always_on_max=24000,
    always_on_cap_reason="the Decision request has a bounded instruction budget",
    skill_dir="skills", skill_name_rule="must_match", trigger_hint="",
    autotrigger_file="guardrails.md", trigger_line="", subagent_requires_mode=False,
    plugin_dir=None, hooks_file=None, reject_empty_always_on=True)

# The pinned upstream exposes adapter protocols but its prompt registry only
# lists Codex/OpenCode. Register our constrained surface in this one shim;
# recursion, probing, composition, selection and libraries stay upstream code.
_SURFACE_DESCRIPTION = (
    "# WHAT YOU CAN CHANGE\nEdit context/harness/instructions.md, memory.md and tool_guidance.md. "
    "Keep guardrails.md byte-identical. These text components enter the Decision request. "
    "The total text budget is 24000 characters. Tools, scripts, action arguments, model identity, "
    "verification and execution permissions are fixed. Explain each change using observed behavior. "
    "Do not embed task IDs or expected answers. Do not write outside these files."
)
prompts._SURFACE_TEXTS["startlux"] = prompts._SurfaceText(
    surface_md=_SURFACE_DESCRIPTION,
    inv="Improve transferable observation and tool-selection instructions using the execution evidence.",
    dom="Improve domain guidance using the supplied source materials and behavioral contrasts.",
    tts_surface=_SURFACE_DESCRIPTION,
)


class StartLuxSubstrate:
    name = "startlux"

    def surface(self):
        return SURFACE


@dataclass
class Harness2Adapter:
    scenario: object
    root: Path
    provider_factory: object
    channel: ChannelSpec

    def __post_init__(self):
        self.substrate = StartLuxSubstrate()
        self.spec = BenchSpec(name=self.scenario.id, runs_root=self.root / "runs",
            install_mode="workspace_config", channels={"proposer": self.channel, "selector": self.channel},
            prompts=BenchPrompts(work=self.scenario.work,
                inv_levers="Improve instructions.md, memory.md and tool_guidance.md. Keep guardrails.md identical.",
                dom_levers="Explain how to use the available domain tools and interpret visible evidence.",
                selection_checks="Check the actual task and source materials; do not invent execution outcomes."),
            allow_subagents=False)
        tree.write_tree(SURFACE, self.scenario.base_policy.files, self.base_harness(), stubs=False)

    def tasks(self, split="evolve"):
        if split != "evolve":
            raise ValueError("the optimizer may only access evolve tasks")
        ids = self.scenario.tasks(split)
        if (not ids or len(ids) != len(set(ids)) or any(not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", task) for task in ids)):
            raise ValueError("scenario task IDs must be unique, nonempty and path-safe")
        return ids

    def domain_of(self, task_id):
        if task_id not in self.tasks():
            raise ValueError("task is outside the optimization split")
        return self.scenario.id

    def task_prompt(self, task_id):
        self.domain_of(task_id)
        return self.scenario.task_prompt(task_id)

    def task_files(self, task_id):
        self.domain_of(task_id)
        directory = self.root / "sources" / canonical_hash(task_id)
        return self.scenario.source_files(task_id, directory)

    def base_harness(self):
        return self.root / "base"

    def validate_tree(self, files):
        candidate_policy(self.scenario.base_policy, files)

    def execute(self, task_id, harness_dir, run_id, *, timeout=None):
        self.domain_of(task_id)
        files = tree.read_tree(SURFACE, harness_dir or self.base_harness())
        policy = candidate_policy(self.scenario.base_policy, files)
        # Hash arbitrary upstream run IDs instead of trusting them as paths.
        directory = self.root / "executions" / canonical_hash(run_id)
        seal = self.root / "sealed" / (canonical_hash(run_id) + ".json")
        if seal.exists():
            previous = json.loads(seal.read_text(encoding="utf-8"))
            if (previous["trial"]["policy_hash"] != policy.identity or
                    previous["trial"]["task_id"] != task_id):
                raise ValueError("cannot resume changed task or policy")
            if not self.artifacts_intact(directory):
                raise ValueError("cached execution artifacts changed; use a fresh output directory")
            return directory
        directory.mkdir(parents=True, exist_ok=False)
        trial = self.scenario.run(task_id, policy, directory, self.provider_factory)
        (directory / "trajectory.md").write_text(trial.trajectory, encoding="utf-8")
        (directory / "answer.txt").write_text(trial.answer, encoding="utf-8")
        atomic_json(directory / "completion.json", {"delivered": trial.delivered,
            "public_hash": canonical_hash([trial.trajectory, trial.answer])})
        atomic_json(seal, {"trial": trial.model_dump()})
        return directory

    def artifacts_intact(self, directory):
        try:
            completed = json.loads((directory / "completion.json").read_text())
            actual = canonical_hash([self.trajectory(directory), self.deliverable_text(directory)])
            return completed["public_hash"] == actual
        except (OSError, ValueError, KeyError):
            return False

    def delivered(self, directory):
        return self.artifacts_intact(directory) and json.loads(
            (directory / "completion.json").read_text())["delivered"] is True

    def trajectory(self, directory, *, obs_trunc=50000):
        return (directory / "trajectory.md").read_text(encoding="utf-8")

    def deliverable_text(self, directory):
        return (directory / "answer.txt").read_text(encoding="utf-8")

    def deliverable_files(self, directory):
        return {"answer.txt": self.deliverable_text(directory)}

    def score(self, directory, task_id):
        # Only the separate gate reads sealed outcomes. Harness² selection gets
        # execution artifacts, not correctness labels.
        raise RuntimeError("use the independent post-selection regression gate")


def optimize(scenario, output, provider_factory, proposer, *, k=1, mode="sequential", fixture=False,
             proposal_only=False):
    import os
    if os.name == "nt":
        raise RuntimeError("run the Harness² optimizer on Linux or WSL; Windows runs the client")
    if type(k) is not int or not 1 <= k <= 4:
        raise ValueError("recursion budget must be 1..4")
    validator = getattr(scenario, "validate_optimization_data", None)
    if validator is not None and not proposal_only:
        validator()
    output = Path(output).resolve()
    mode_token = schedule.mode_token(mode)
    provenance = {"backend": "google-research/harness2", "upstream_commit": UPSTREAM_COMMIT,
        "scenario": scenario.id, "scenario_identity": scenario.identity,
        "base_policy": scenario.base_policy.identity, "improver": proposer.spec.model,
        "provider_mode": "synthetic_fixture" if fixture else "startlux",
        "k": k, "mode": mode_token, "adapter_version": 2, "proposal_only": proposal_only}
    output.mkdir(parents=True, exist_ok=True)
    # One writer owns all upstream libraries and resume artifacts in this root.
    import fcntl
    with (output / ".optimizer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        identity_path = output / "provenance.json"
        if identity_path.exists():
            if json.loads(identity_path.read_text()) != provenance:
                raise ValueError("optimizer configuration changed; choose a new output directory")
        else:
            if any(p.name != ".optimizer.lock" for p in output.iterdir()):
                raise ValueError("output directory contains unbound results")
            atomic_json(identity_path, provenance)
        adapter = Harness2Adapter(scenario, output, provider_factory, proposer.spec)
        result = schedule.run_domain(scenario.id, adapter.tasks(), k, adapter=adapter,
            prop=proposer, runs_root=adapter.spec.runs_root, library_root=output / "library",
            modes=(mode_token,))
        report = {**provenance, "workflow": result, "eligible_for_client_release": False,
                  "improver_cost_usd": None, "live_game_verified": False}
        if result["failed"]:
            atomic_json(output / "report.json", report)
            return report
        # Adopt the final task's selected harness BEFORE seeing heldout outcomes.
        last = adapter.tasks()[-1]
        summary = schedule.Namespace(scenario.id, "startlux", "agg", k).task_dir(adapter.spec.runs_root, last)
        selection = json.loads((summary / "summary.json").read_text())["selections"][mode_token]
        chosen = schedule.Namespace(scenario.id, "startlux", mode_token, k).task_dir(adapter.spec.runs_root, last)
        files = tree.read_tree(SURFACE, chosen / "harnesses" / selection["chosen_label"])
        policy = candidate_policy(scenario.base_policy, files)
        atomic_json(output / "candidate.json", policy.model_dump())
        if proposal_only:
            report.update(candidate_hash=policy.identity, status="awaiting_independent_validation")
            atomic_json(output / "report.json", report)
            return report
        gate_path = output / "gate.json"
        if gate_path.exists():
            gate = json.loads(gate_path.read_text())
            if gate["candidate_hash"] != policy.identity:
                raise ValueError("selected policy differs from the recorded gate")
        else:
            rows = {"baseline": [], "candidate": []}
            for kind, current in (("baseline", scenario.base_policy), ("candidate", policy)):
                for task_id in scenario.tasks("heldout"):
                    dest = output / "validation" / kind / canonical_hash(task_id)
                    dest.mkdir(parents=True, exist_ok=True)
                    trial_file = dest / "trial.json"
                    if trial_file.exists():
                        trial = Trial.model_validate_json(trial_file.read_bytes())
                        if trial.task_id != task_id or trial.policy_hash != current.identity:
                            raise ValueError("validation trial identity changed")
                    else:
                        trial = scenario.run(task_id, current, dest, provider_factory)
                        atomic_json(trial_file, trial.model_dump())
                    rows[kind].append(trial)
            gate = {**measure_pair(rows["baseline"], rows["candidate"]),
                    "candidate_hash": policy.identity, "baseline_hash": scenario.base_policy.identity}
            if fixture:
                gate["eligible_for_client_release"] = False
            atomic_json(gate_path, gate)
        report.update(gate=gate, candidate_hash=policy.identity,
                      eligible_for_client_release=gate["eligible_for_client_release"])
        atomic_json(output / "report.json", report)
        return report
