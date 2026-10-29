"""Bridge the existing real-file benchmark into the same SDK coordinator."""
import hashlib
from pathlib import Path

from bamboo_rsi.repository import RepositorySearch
from bamboo_rsi.storage import confined, identity, read, write
from ..contracts import Build, Case, Integration, Measurement, Metric, Plan, TaskSpec, code_hash
from ..evaluation import PairedEvaluation


class RepositoryAdapter:
    def __init__(self, dataset):
        self.search = RepositorySearch(dataset)

    def describe(self):
        return TaskSpec("repository.literal_search", "Find all literal query matches with exact file and line numbers; reduce actual reads and comparisons.",
                        ("search-policy.json",), (Metric("work_units", regression=0.1),), "work_units",
                        evidence_scope="local_benchmark")

    def fingerprint(self):
        return identity({"bridge": code_hash(__file__), "search": self.search.fingerprint()})

    def initial_files(self): return self.search.initial_files()

    def training_context(self, version): return self.search.training_context(version)

    def plan(self, used_cases):
        # Prefix IDs to satisfy the public task identifier grammar.
        used = {s.removeprefix("query-") for s in used_cases}
        old = self.search.preregister(used)
        cases = tuple(Case("query-" + key, {"queries": queries}) for key, queries in zip(old["case_ids"], old["queries"]))
        return Plan(cases, tuple((arm, c.id) for i, c in enumerate(cases)
                    for arm in (("baseline", "candidate") if i % 2 == 0 else ("candidate", "baseline"))))

    def build(self, version, destination):
        self.search.build(version, destination)
        path = Path(destination) / "policy.compiled.json"
        return Build({path.name: hashlib.sha256(path.read_bytes()).hexdigest()})

    def execute(self, version, case, context):
        context.checkpoint()
        output, counters = self.search.execute(self.search.policy(version), case.inputs["queries"])
        context.checkpoint()
        write(context.directory / "matches.json", output, immutable=True)
        return {"origin": "local_execution", "assisted": False, "output": "matches.json",
                "counters": counters, "input": identity(case.inputs)}


class RepositoryVerifier:
    def __init__(self, dataset):
        self.search = RepositorySearch(dataset)

    def fingerprint(self):
        return identity({"bridge": code_hash(__file__), "search": self.search.fingerprint(), "role": "verifier"})

    def verify(self, version, case, receipt, directory):
        if (set(receipt) != {"origin", "assisted", "output", "counters", "input"}
                or receipt["output"] != "matches.json" or receipt["input"] != identity(case.inputs)):
            raise ValueError("Repository input binding changed")
        queries = case.inputs["queries"]
        actual = read(confined(directory, receipt["output"]))
        # Exhaustive oracle is separate from policy execution.
        expected = self.search.oracle(queries)
        selected = self.search.policy(version)
        size, compare = 0, 0
        for name in sorted(self.search.manifest["files"]):
            raw = confined(self.search.dataset / "files", name).read_bytes()
            text = raw.decode("utf-8-sig")
            size += len(raw) * (1 if selected["cache"] == "batch" else len(queries))
            for q in queries:
                if selected["prefilter"]:
                    compare += len(text)
                    if q not in text: continue
                compare += sum(map(len, text.splitlines()))
        counters = {"bytes_read": size, "comparison_characters": compare, "work_units": size + compare}
        if receipt["counters"] != counters:
            raise ValueError("Repository counters do not match frozen algorithm")
        return Measurement(actual == expected, {"work_units": size + compare})


def integration(config):
    if set(config) != {"dataset"}:
        raise ValueError("Repository integration expects one dataset path")
    return Integration(RepositoryAdapter(config["dataset"]), RepositoryVerifier(config["dataset"]), PairedEvaluation())
