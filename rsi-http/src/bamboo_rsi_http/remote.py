"""REST task adapter. Execution and signed independent verification are separate roles."""
import base64
from copy import deepcopy
import hashlib
import hmac
from pathlib import Path
import uuid

from bamboo_rsi.storage import confined, encoded, identity, read, relative_name, valid_id, write
from bamboo_rsi_sdk import Build, Case, Integration, Limits, Measurement, Metric, Plan, TaskSpec
from bamboo_rsi_sdk.contracts import code_hash
from bamboo_rsi_sdk.evaluation import PairedEvaluation
from .transport import JsonClient, secret, strict_json

PROTOCOL = "bamboo-rsi-http/1"


def protocol_code():
    return code_hash(*sorted(Path(__file__).parent.glob("*.py")))


def parse_manifest(value):
    if type(value) is not dict or set(value) != {"protocol", "mode", "task", "revisions", "initial_files", "training", "cases"}:
        raise ValueError("Invalid remote manifest")
    if value["protocol"] != PROTOCOL or value["mode"] not in {"read_only", "shadow"}:
        raise ValueError("Only read-only or isolated shadow evaluation is supported")
    task = deepcopy(value["task"])
    for key in ("editable", "origins", "capabilities"):
        task[key] = tuple(task[key])
    task["metrics"] = tuple(Metric(**m) for m in task["metrics"])
    task["limits"] = Limits(**task["limits"])
    spec = TaskSpec(**task)
    if spec.origins != ("remote_execution",):
        raise ValueError("Remote evidence must have an explicit remote origin")
    if set(value["revisions"]) != {"executor", "verifier", "dataset"}:
        raise ValueError("Pin executor, verifier and dataset revisions")
    for revision in value["revisions"].values():
        valid_id(revision)
    files = value["initial_files"]
    if type(files) is not dict or not 1 <= len(files) <= 32 or not set(spec.editable) <= set(files):
        raise ValueError("Invalid initial remote bundle")
    for filename, content in files.items():
        relative_name(filename)
        if not isinstance(content, str) or len(content.encode()) > 2000000:
            raise ValueError("Invalid version file")
    if type(value["training"]) is not dict or len(encoded(value["training"])) > 1000000:
        raise ValueError("Training feedback must be a bounded, redacted object")
    if type(value["cases"]) is not list or not 1 <= len(value["cases"]) <= 10000:
        raise ValueError("Declare bounded validation cases")
    cases = tuple(Case(**case) for case in value["cases"])
    if len({c.id for c in cases}) != len(cases):
        raise ValueError("Duplicate validation identity")
    return spec, cases


class RemoteTask:
    def __init__(self, config):
        if set(config) - {"task_id", "executor", "verifier", "manifest_sha256", "verification_key_env", "cases_per_round"}:
            raise ValueError("Unknown remote integration option")
        self.config = deepcopy(config)
        self.executor = JsonClient(config["executor"])
        self.verifier = JsonClient(config["verifier"])
        self.pin = valid_id(config["manifest_sha256"])
        self.manifest = self.executor.request("GET", "/rsi/v1/manifest")
        if identity(self.manifest) != self.pin:
            raise ValueError("Remote manifest differs from the configured commitment")
        self.spec, self.cases = parse_manifest(self.manifest)
        if self.spec.id != config["task_id"]:
            raise ValueError("Remote task identity mismatch")
        self.case_count = config.get("cases_per_round", 3)
        if type(self.case_count) is not int or not 3 <= self.case_count <= 1000:
            raise ValueError("At least three independent cases per paired round are required")
        self.key_hash = hashlib.sha256(secret(config["verification_key_env"], 32).encode()).hexdigest()

    def describe(self):
        return self.spec

    def fingerprint(self):
        if hashlib.sha256(secret(self.config["verification_key_env"], 32).encode()).hexdigest() != self.key_hash:
            raise ValueError("Verification trust key changed")
        if identity(self.manifest) != self.pin:
            raise ValueError("Manifest mutated")
        return identity({"code": protocol_code(), "config": self.config, "manifest": self.pin,
                         "verification_key_commitment": self.key_hash})

    def initial_files(self):
        return dict(self.manifest["initial_files"])

    def training_context(self, version):
        return deepcopy(self.manifest["training"])

    def plan(self, used_cases):
        available = [c for c in self.cases if c.id not in used_cases][:self.case_count]
        if len(available) != self.case_count:
            raise ValueError("Fresh validation cases exhausted")
        slots = tuple((arm, c.id) for i, c in enumerate(available)
                      for arm in (("baseline", "candidate") if i % 2 == 0 else ("candidate", "baseline")))
        return Plan(tuple(deepcopy(available)), slots)

    def build(self, version, destination):
        result = {}
        for filename, content in version["files"].items():
            path = confined(destination, filename)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode("utf-8"))
            result[filename] = hashlib.sha256(path.read_bytes()).hexdigest()
        return Build(result)

    def execute(self, version, case, context):
        context.checkpoint()
        execution_id = uuid.uuid4().hex
        request = {"protocol": PROTOCOL, "execution_id": execution_id, "task_id": self.spec.id,
                   "manifest_sha256": self.pin, "revisions": self.manifest["revisions"],
                   "mode": self.manifest["mode"], "version": version, "case": {"id": case.id, "inputs": case.inputs}}
        request_hash = identity(request)
        packet = {"request": request, "request_sha256": request_hash}
        write(context.directory / "request.json", packet, immutable=True)
        # No automatic retries, even when a response is lost. The remote service
        # must retain this key and reject the same key with different bytes.
        execution = self.executor.request("POST", "/rsi/v1/execute", packet,
                    deadline=context.deadline, request_id=execution_id)
        write(context.directory / "execution.json", execution, immutable=True)
        check_execution(self, request, request_hash, execution)
        context.checkpoint()
        evidence_hash = identity(execution)
        verification = {**packet, "execution": execution, "evidence_sha256": evidence_hash}
        proof = self.verifier.request("POST", "/rsi/v1/verify", verification,
                    deadline=context.deadline, request_id=execution_id)
        write(context.directory / "verification.json", proof, immutable=True)
        context.checkpoint()
        return {"origin": "remote_execution", "assisted": False, "execution_id": execution_id,
                "request_sha256": request_hash, "evidence_sha256": evidence_hash, "proof_sha256": identity(proof)}


def check_execution(task, request, request_hash, execution):
    expected = {"protocol": PROTOCOL, "execution_id": request["execution_id"], "request_sha256": request_hash,
                "manifest_sha256": task.pin, "executor_revision": task.manifest["revisions"]["executor"],
                "origin": "remote_execution", "assisted": False}
    if (type(execution) is not dict or set(execution) != {*expected, "result"}
            or any(execution[k] != v for k, v in expected.items()) or execution["assisted"] is not False
            or type(execution["result"]) is not dict):
        raise ValueError("Execution is not bound to the registered request")


def binding(task, request, request_hash, evidence_hash):
    return {"protocol": PROTOCOL, "task_id": task.spec.id, "manifest_sha256": task.pin,
            "verifier_revision": task.manifest["revisions"]["verifier"],
            "execution_id": request["execution_id"], "version_id": request["version"]["id"],
            "case_id": request["case"]["id"], "request_sha256": request_hash, "evidence_sha256": evidence_hash}


def sign_measurement(bindings, measurement, key):
    """Reference for bridge implementations. Sign exact bytes, not reserialized JSON.

    Only an independently controlled verifier should hold the signing key.
    Java/Go implementations can use their normal JSON encoding for these bytes.
    """
    payload = encoded({**bindings, "measurement": measurement})
    return {"payload_base64": base64.b64encode(payload).decode("ascii"),
            "signature_hex": hmac.new(key.encode("utf-8"), payload, hashlib.sha256).hexdigest()}


class AttestedVerifier:
    def __init__(self, task):
        self.task = task

    def fingerprint(self):
        return identity({"task": self.task.fingerprint(), "role": "independent_hmac_verifier_v1"})

    def verify(self, version, case, receipt, directory):
        packet, execution, proof = (read(Path(directory) / f) for f in ("request.json", "execution.json", "verification.json"))
        task = self.task
        request = packet["request"]
        expected_request = {"protocol": PROTOCOL, "execution_id": receipt["execution_id"], "task_id": task.spec.id,
                            "manifest_sha256": task.pin, "revisions": task.manifest["revisions"],
                            "mode": task.manifest["mode"], "version": version, "case": {"id": case.id, "inputs": case.inputs}}
        if request != expected_request or packet != {"request": expected_request, "request_sha256": identity(expected_request)}:
            raise ValueError("Stored remote request changed")
        check_execution(task, request, packet["request_sha256"], execution)
        evidence_hash = identity(execution)
        expected_receipt = {"origin": "remote_execution", "assisted": False, "execution_id": request["execution_id"],
                            "request_sha256": identity(request), "evidence_sha256": evidence_hash, "proof_sha256": identity(proof)}
        if receipt != expected_receipt or type(proof) is not dict or set(proof) != {"payload_base64", "signature_hex"}:
            raise ValueError("Stored remote evidence changed")
        raw = base64.b64decode(proof["payload_base64"], validate=True)
        actual = hmac.new(secret(task.config["verification_key_env"], 32).encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(actual, valid_id(proof["signature_hex"])):
            raise ValueError("Independent verification signature mismatch")
        payload = strict_json(raw)
        expected = binding(task, request, identity(request), evidence_hash)
        if type(payload) is not dict or set(payload) != {*expected, "measurement"} or any(payload[k] != v for k, v in expected.items()):
            raise ValueError("Verification belongs to another execution, version or case")
        measurement = Measurement(**payload["measurement"])
        measurement.validate(task.spec)
        return measurement


def integration(config):
    task = RemoteTask(config)
    return Integration(task, AttestedVerifier(task), PairedEvaluation(task.case_count))
