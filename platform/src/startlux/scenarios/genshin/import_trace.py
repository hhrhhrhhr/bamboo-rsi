"""Import verified captures without inventing labels or independent holdouts."""
import json
from pathlib import Path

from ...core.contracts import canonical_hash, catalog_hash
from ...core.storage import atomic_json
from .evidence import restore_observation
from .snapshot import SnapshotCase, SnapshotSuite
from .taskpack import TaskPack


def captured_cases(run):
    run = run.resolve()
    pack = TaskPack.load(run / "pack")
    if pack.manifest.environment != "windows":
        raise ValueError("only Windows capture runs can be imported")
    cases, seen = [], set()
    with (run / "trace.jsonl").open(encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            if record.get("type") != "decision_requested":
                continue
            if record.get("requested_model") != pack.manifest.decision_model:
                raise ValueError("trace model does not match the frozen pack")
            state = restore_observation(run, record["observation"])
            if state.binding() != record["observation_hash"]:
                raise ValueError("observation binding mismatch")
            options = pack.candidates(state)
            if catalog_hash(options) != record["catalog_hash"]:
                raise ValueError("trace action catalog differs from the frozen pack")
            group = canonical_hash({"facts": state.facts, "images": state.images, "scope": state.scope.model_dump()})
            if group in seen:
                continue
            seen.add(group)
            cases.append(SnapshotCase(id="capture-" + group[:24], split="evolve", observation=state,
                                      acceptable_actions=[], label_available=False))
    return pack, cases


def import_runs(runs, output, *, heldout_days=()):
    if output.exists():
        raise FileExistsError("replay dataset already exists")
    heldout_days = set(heldout_days)
    cases, sources, bases = {}, [], set()
    for run in runs:
        pack, captured = captured_cases(run)
        bases.add(pack.policy_base_hash)
        sources.append(run.name)
        for case in captured:
            cases.setdefault(case.id, case.model_copy(update={
                "split": "heldout" if case.observation.scope.game_day in heldout_days else "evolve"}))
    if len(bases) != 1:
        raise ValueError("replay runs must share frozen model, actions and execution resources")
    days = {case.observation.scope.game_day for case in cases.values()}
    if heldout_days and (not heldout_days < days):
        raise ValueError("heldout days must exist and leave at least one whole evolve day")
    suite = SnapshotSuite(evidence_origin="captured_game", cases=list(cases.values()),
                          partition_method="game_day" if heldout_days else "unpartitioned")
    atomic_json(output, suite.model_dump())
    return {"cases": len(cases), "labels": "unlabelled", "evidence_origin": "captured_game",
            "source_runs": sources, "game_days": sorted(days), "heldout_days": sorted(heldout_days),
            "partition_method": suite.partition_method, "policy_base_hash": next(iter(bases)),
            "optimization_ready": False, "eligible_for_client_release": False}


def import_run(run: Path, output: Path):
    # A single game day remains a collection corpus, never an 80/20 frame split.
    return import_runs([run], output)


def collect_home(home, output):
    """Keep failure assessments for every run, group usable captures by base."""
    from .assessment import assess_home
    if output.exists():
        raise FileExistsError("collection output already exists")
    assessment = assess_home(home, output / "assessment")
    groups, rejected = {}, []
    for run in sorted((home / "runs").iterdir()):
        if not run.is_dir():
            continue
        try:
            pack, cases = captured_cases(run)
            if not cases:
                raise ValueError("no decisions")
            group = groups.setdefault(pack.policy_base_hash, {"cases": {}, "runs": []})
            group["runs"].append(run.name)
            for case in cases:
                group["cases"].setdefault(case.id, case)
        except (OSError, ValueError, KeyError, TypeError) as error:
            rejected.append({"run_id": run.name, "reason": type(error).__name__,
                             "assessment_preserved": True})
    collections = []
    for base, group in groups.items():
        suite = SnapshotSuite(evidence_origin="captured_game", partition_method="unpartitioned",
                              cases=list(group["cases"].values()))
        atomic_json(output / "corpora" / (base + ".json"), suite.model_dump())
        collections.append({"base_hash": base, "source_runs": group["runs"], "cases": len(suite.cases),
                            "game_days": sorted({c.observation.scope.game_day for c in suite.cases}),
                            "suite": "corpora/" + base + ".json"})
    result = {"schema_version": 1, "attempt_count": assessment["attempt_count"],
              "collections": collections, "rejected_captures": rejected,
              "labels": "unlabelled", "optimization_ready": False, "eligible_for_client_release": False}
    atomic_json(output / "collection.json", result)
    return result
