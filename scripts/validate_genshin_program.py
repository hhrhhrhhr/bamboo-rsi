"""Trusted offline evaluator. Candidate code cannot select or replace these tests."""
import os
from pathlib import Path
import sys

source, root = map(Path, sys.argv[1:3])
sys.path[:0] = [str(source.resolve()), str(root.resolve() / "platform/tests")]
os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
from startlux.scenarios.genshin.rsi_program import CHECKS
import startlux
if Path(startlux.__file__).resolve().parent != source.resolve() / "startlux":
    raise RuntimeError("candidate import was shadowed by an editable installation")
import pytest
raise SystemExit(pytest.main(["-q", "-p", "no:cacheprovider", "-o", "pythonpath=",
                             "--confcutdir", str(root / "platform/tests"),
                             *[str(root / "platform/tests" / name) for name in CHECKS]]))
