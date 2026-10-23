"""Actual file-search benchmark. Restricted policy execution, independent exhaustive oracle.

This measures local I/O and comparisons on real public source files. It is NOT
gameplay, arbitrary-code sandboxing, or proof of transfer to an unseen game.
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time

from .storage import confined, identity, read, write


def snapshot(repository, destination):
    repository, destination = Path(repository).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ValueError("Use a new immutable dataset directory")
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=repository).split(b"\0")
    entries = {}
    for raw in names:
        if not raw:
            continue
        name = raw.decode("utf-8")
        source = confined(repository, name)
        if source.suffix not in {".py", ".js", ".cjs", ".md", ".json", ".cs", ".toml"}:
            continue
        data = source.read_bytes()
        if len(data) > 1_000_000:
            continue
        try:
            data.decode("utf-8-sig")
        except UnicodeError:
            continue
        # Public snapshot excludes credentials even if a caller forgot its repo scan.
        if re.search(rb"(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})", data):
            raise ValueError("Source snapshot contains a credential-shaped value")
        target = confined(destination / "files", name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        entries[name] = hashlib.sha256(data).hexdigest()
    if len(entries) < 10:
        raise ValueError("A real repository snapshot needs at least ten text files")
    manifest = {"origin": "local_repository_snapshot", "files": entries,
                "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository).decode().strip()}
    write(destination / "manifest.json", manifest, immutable=True)
    return manifest


class RepositorySearch:
    domain = "repository.literal_search"
    editable = frozenset({"search-policy.json"})

    def __init__(self, dataset):
        self.dataset = Path(dataset).resolve()
        self.manifest = read(self.dataset / "manifest.json")
        self._bank = None

    def fingerprint(self):
        current = read(self.dataset / "manifest.json")
        if current != self.manifest:
            raise ValueError("Dataset manifest changed")
        for name, expected in current["files"].items():
            if hashlib.sha256(confined(self.dataset / "files", name).read_bytes()).hexdigest() != expected:
                raise ValueError("Dataset content changed")
        return identity({"domain": self.domain, "dataset": current,
                         "adapter": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})

    def initial_files(self):
        return {"search-policy.json": json.dumps({"cache": "none", "prefilter": False})}

    def policy(self, version):
        value = json.loads(version["files"]["search-policy.json"])
        if (set(value) != {"cache", "prefilter"} or value["cache"] not in {"none", "batch"}
                or type(value["prefilter"]) is not bool):
            raise ValueError("Only bounded cache and literal prefilter settings are editable")
        return value

    def bank(self):
        if self._bank is None:
            words = set()
            for name in self.manifest["files"]:
                text = confined(self.dataset / "files", name).read_text(encoding="utf-8-sig")
                words.update(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{5,39}\b", text))
            self._bank = sorted(words, key=lambda w: hashlib.sha256(w.encode()).hexdigest())
        return self._bank

    def preregister(self, used_cases):
        words = self.bank()[3:]
        batches = [words[i:i+3] for i in range(0, len(words)-2, 3)]
        selected = [q for q in batches if identity(q) not in used_cases][:3]
        if len(selected) != 3:
            raise ValueError("Fresh held-out search batches exhausted")
        return {"domain": self.domain, "adapter": self.fingerprint(),
                "case_ids": [identity(q) for q in selected], "queries": selected,
                "criteria": {"origins": ["local_execution"], "minimum_cases": 3,
                             "work_ratio": 0.9, "maximum_work_regression": 1.1},
                "design": "paired_fresh_query_batches", "measurement": "bytes_read_plus_literal_comparison_characters"}

    def execute(self, policy, queries):
        cache = {}
        result, bytes_read, compared = {}, 0, 0
        for query in queries:
            matches = []
            for name in sorted(self.manifest["files"]):
                if name not in cache:
                    raw = confined(self.dataset / "files", name).read_bytes()
                    bytes_read += len(raw)
                    text = raw.decode("utf-8-sig")
                    if policy["cache"] == "batch":
                        cache[name] = text
                else:
                    text = cache[name]
                if policy["prefilter"]:
                    compared += len(text)
                    if query not in text:
                        continue
                for line_number, line in enumerate(text.splitlines(), 1):
                    compared += len(line)
                    if query in line:
                        matches.append([name, line_number])
            result[query] = matches
        return result, {"bytes_read": bytes_read, "comparison_characters": compared,
                        "work_units": bytes_read + compared}

    def oracle(self, queries):
        # Separate exhaustive verification, with no candidate policy or counters.
        outputs = {q: [] for q in queries}
        for name in sorted(self.manifest["files"]):
            lines = confined(self.dataset / "files", name).read_text(encoding="utf-8-sig").splitlines()
            for n, line in enumerate(lines, 1):
                for query in queries:
                    if query in line:
                        outputs[query].append([name, n])
        return outputs

    def training_context(self, version):
        queries = self.bank()[:3]
        result, counters = self.execute(self.policy(version), queries)
        return {"task": "Find every literal query occurrence as (relative file, one-based line), preserving sorted results.",
                "policy_schema": {"cache": ["none", "batch"], "prefilter": [False, True]},
                "semantics": "batch caches decoded file contents within one batch; prefilter searches the whole file before per-line comparisons. Memory cache is discarded between batches.",
                "objective": "Reduce actual bytes read plus literal comparison characters; preserve all matches.",
                "file_count": len(self.manifest["files"]), "training_queries": queries,
                "training_work": counters, "training_correct": result == self.oracle(queries),
                "scope": "Real local file-search benchmark, not a game or a general capability claim."}

    def build(self, version, destination):
        policy = self.policy(version)
        write(Path(destination) / "policy.compiled.json", policy, immutable=True)
        return {"passed": True, "artifact": identity(policy)}

    def evaluate(self, version, plan, destination):
        destination = Path(destination)
        rows, artifacts = [], {}
        for case, queries in zip(plan["case_ids"], plan["queries"]):
            started = time.perf_counter()
            result, counters = self.execute(self.policy(version), queries)
            duration = time.perf_counter() - started
            expected = self.oracle(queries)
            artifact = {"queries": queries, "actual": result, "expected": expected, "counters": counters}
            name = case + ".json"
            write(destination / name, artifact, immutable=True)
            artifacts[name] = hashlib.sha256((destination / name).read_bytes()).hexdigest()
            rows.append({"case": case, "success": result == expected, "verified": True,
                         "work_units": counters["work_units"], "duration_seconds": duration})
        return {"origin": "local_execution", "assisted": False, "adapter": self.fingerprint(),
                "version": version["id"], "plan": identity(plan), "rows": rows, "artifacts": artifacts}
