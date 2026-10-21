"""Installed Python entry points register trusted scenario implementations."""
from importlib.metadata import entry_points
import json
from pathlib import Path


def load_scenario(name, config):
    if name == "genshin.daily":
        from .scenarios.genshin.scenario import GenshinScenario
        from .scenarios.genshin.snapshot import SnapshotSuite
        from .scenarios.genshin.taskpack import TaskPack
        return GenshinScenario(TaskPack.load(config["pack"]),
            SnapshotSuite.model_validate_json(Path(config["suite"]).read_bytes()))
    if name == "workspace.ledger":
        from .scenarios.workspace.scenario import WorkspaceScenario
        return WorkspaceScenario(json.loads(Path(config["dataset"]).read_text()), config["model"])
    plugins = [ep for ep in entry_points(group="startlux.scenarios") if ep.name == name]
    if len(plugins) != 1:
        raise ValueError("scenario must resolve to one installed plugin")
    scenario = plugins[0].load()(config)
    if scenario.id != name:
        raise ValueError("scenario plugin identity mismatch")
    return scenario
