"""Read-only CSV reconciliation. Candidates select a bounded join algorithm only."""
import csv
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import re

from bamboo_rsi.storage import confined, identity, read, write
from ..contracts import (Build, Case, Integration, Limits, Measurement, Metric,
                         Plan, TaskSpec, code_hash)
from ..evaluation import PairedEvaluation

INITIAL = {"join": "nested_scan"}


def policy(version):
    value = json.loads(version["files"]["reconciliation-policy.json"])
    if type(value) is not dict or set(value) != {"join"} or value["join"] not in {"nested_scan", "indexed"}:
        raise ValueError("Only the bounded join strategy is editable")
    return value["join"]


def cents(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,11})\.[0-9]{2}", value):
        raise ValueError("Amounts require nonnegative exact two-decimal values")
    return int(Decimal(value) * 100)


class Dataset:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.manifest = read(self.root / "manifest.json")
        m = self.manifest
        if set(m) != {"schema_version", "scope", "files", "training", "validation"} or m["schema_version"] != 1:
            raise ValueError("Unsupported reconciliation dataset")
        if m["scope"] not in {"offline_reference", "business_validation"}:
            raise ValueError("Dataset must declare its evidence scope")
        if not m["training"] or len(m["validation"]) < 6:
            raise ValueError("Separate training and at least six validation cases required")
        cases = m["training"] + m["validation"]
        ids, used_files = [], set()
        for case in cases:
            if set(case) != {"id", "orders", "payments"}:
                raise ValueError("Invalid dataset case")
            Case(case["id"], {})
            ids.append(case["id"])
            for key in ("orders", "payments"):
                if case[key] in used_files:
                    raise ValueError("Training and validation inputs must be distinct")
                used_files.add(case[key])
                confined(self.root, case[key])
        if len(ids) != len(set(ids)) or set(m["files"]) != used_files:
            raise ValueError("Duplicate cases or incomplete dataset inventory")
        self.fingerprint()

    def fingerprint(self):
        if read(self.root / "manifest.json") != self.manifest:
            raise ValueError("Dataset manifest changed")
        for file, expected in self.manifest["files"].items():
            path = confined(self.root, file)
            if path.stat().st_size > 20_000_000 or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("Dataset bytes changed or oversized")
        return identity(self.manifest)

    def load(self, case):
        self.fingerprint()
        if case not in self.manifest["training"] + self.manifest["validation"]:
            raise ValueError("Unregistered dataset case")
        values = []
        for kind, columns in (("orders", ["order_id", "currency", "amount"]),
                              ("payments", ["payment_id", "order_id", "currency", "amount", "status"])):
            reader = csv.DictReader(io.StringIO(confined(self.root, case[kind]).read_text(encoding="utf-8-sig")))
            if reader.fieldnames != columns:
                raise ValueError("CSV schema mismatch")
            rows = list(reader)
            if not rows or len(rows) > 100000:
                raise ValueError("Empty or oversized input")
            ids = set()
            for row in rows:
                if set(row) != set(columns) or any(not isinstance(row[c], str) or len(row[c]) > 128 for c in columns):
                    raise ValueError("Malformed CSV record")
                if not re.fullmatch(r"[A-Z]{3}", row["currency"]):
                    raise ValueError("Invalid currency")
                row["cents"] = cents(row["amount"])
                record_id = row["order_id"] if kind == "orders" else row["payment_id"]
                if not record_id or record_id in ids:
                    raise ValueError("Duplicate or empty source record identity")
                ids.add(record_id)
                if kind == "payments" and row["status"] not in {"captured", "pending", "failed"}:
                    raise ValueError("Unknown payment status")
            values.append(rows)
        return values


def calculate(strategy, orders, payments, checkpoint):
    """Count actual traversals; no generated code or external payment API runs."""
    work = 0
    totals = {}
    if strategy == "indexed":
        for payment in payments:
            checkpoint(); work += 1
            if payment["status"] == "captured":
                key = (payment["order_id"], payment["currency"])
                totals[key] = totals.get(key, 0) + payment["cents"]
    result = []
    for order in orders:
        checkpoint()
        if strategy == "nested_scan":
            paid = 0
            for payment in payments:
                checkpoint(); work += 1
                if payment["status"] == "captured" and (payment["order_id"], payment["currency"]) == (order["order_id"], order["currency"]):
                    paid += payment["cents"]
        else:
            work += 1
            paid = totals.get((order["order_id"], order["currency"]), 0)
        result.append({"order_id": order["order_id"], "currency": order["currency"],
                       "expected_cents": order["cents"], "paid_cents": paid,
                       "difference_cents": paid - order["cents"]})
    known = {(o["order_id"], o["currency"]) for o in orders}
    unmatched = []
    for payment in payments:
        checkpoint(); work += 1
        if payment["status"] == "captured" and (payment["order_id"], payment["currency"]) not in known:
            unmatched.append(payment["payment_id"])
    return {"orders": result, "unmatched_captured_payments": unmatched}, work


class OrdersAdapter:
    def __init__(self, dataset, *, limits=Limits()):
        self.data, self.limits = Dataset(dataset), limits

    def describe(self):
        return TaskSpec("business.order_reconciliation", "Reconcile captured payments against every order without losing rows or mixing currencies; reduce record visits while preserving exact cent amounts.",
                        ("reconciliation-policy.json",), (Metric("record_visits"),), "record_visits",
                        limits=self.limits, capabilities=("local_files",), evidence_scope=self.data.manifest["scope"])

    def fingerprint(self):
        return identity({"code": code_hash(__file__), "dataset": self.data.fingerprint()})

    def initial_files(self):
        return {"reconciliation-policy.json": json.dumps(INITIAL)}

    def training_context(self, version):
        rows = []
        for case in self.data.manifest["training"]:
            orders, payments = self.data.load(case)
            _, work = calculate(policy(version), orders, payments, lambda: None)
            rows.append({"orders": len(orders), "payments": len(payments), "record_visits": work})
        return {"policy_schema": {"join": ["nested_scan", "indexed"]},
                "semantics": "nested_scan traverses payments for each order; indexed builds a lookup once per case. Both preserve original order order, exact cents, status, currencies and unmatched payments.",
                "training_measurements": rows,
                "privacy": "Only counts and strategy are supplied; no order identifiers, payment identifiers or validation records."}

    def plan(self, used_cases):
        selected = [c for c in self.data.manifest["validation"] if c["id"] not in used_cases][:3]
        if len(selected) != 3:
            raise ValueError("Fresh held-out reconciliation cases exhausted")
        cases = tuple(Case(c["id"], dict(c)) for c in selected)
        # Alternate order to avoid always running the candidate second.
        slots = tuple((arm, c.id) for i, c in enumerate(cases)
                      for arm in (("baseline", "candidate") if i % 2 == 0 else ("candidate", "baseline")))
        return Plan(cases, slots)

    def build(self, version, destination):
        compiled = {"join": policy(version)}
        write(Path(destination) / "policy.json", compiled, immutable=True)
        return Build({"policy.json": hashlib.sha256((Path(destination) / "policy.json").read_bytes()).hexdigest()})

    def execute(self, version, case, context):
        orders, payments = self.data.load(case.inputs)
        output, work = calculate(policy(version), orders, payments, context.checkpoint)
        write(context.directory / "reconciliation.json", output, immutable=True)
        return {"origin": "local_execution", "assisted": False, "output": "reconciliation.json",
                "record_visits": work, "input": identity(case.inputs), "dataset": self.data.fingerprint()}


class OrdersVerifier:
    def __init__(self, dataset):
        self.data = Dataset(dataset)

    def fingerprint(self):
        return identity({"code": code_hash(__file__), "dataset": self.data.fingerprint(), "role": "verifier"})

    def verify(self, version, case, receipt, directory):
        if set(receipt) != {"origin", "assisted", "output", "record_visits", "input", "dataset"}:
            raise ValueError("Invalid reconciliation receipt")
        if receipt["dataset"] != self.data.fingerprint() or receipt["input"] != identity(case.inputs) or receipt["output"] != "reconciliation.json":
            raise ValueError("Reconciliation input binding changed")
        orders, payments = self.data.load(case.inputs)
        # Independent exhaustive oracle, never calls the candidate calculation.
        expected = {"orders": [], "unmatched_captured_payments": []}
        for order in orders:
            paid = sum(int(Decimal(p["amount"]) * 100) for p in payments
                       if p["status"] == "captured" and p["order_id"] == order["order_id"] and p["currency"] == order["currency"])
            wanted = int(Decimal(order["amount"]) * 100)
            expected["orders"].append({"order_id": order["order_id"], "currency": order["currency"],
                "expected_cents": wanted, "paid_cents": paid, "difference_cents": paid - wanted})
        for p in payments:
            if p["status"] == "captured" and not any(o["order_id"] == p["order_id"] and o["currency"] == p["currency"] for o in orders):
                expected["unmatched_captured_payments"].append(p["payment_id"])
        # Counters are independently derivable because candidates only select these two fixed algorithms.
        work = (len(orders) * len(payments) if policy(version) == "nested_scan" else len(orders) + len(payments)) + len(payments)
        if type(receipt["record_visits"]) is not int or receipt["record_visits"] != work:
            raise ValueError("Execution counter does not match the frozen algorithm")
        actual = read(confined(directory, receipt["output"]))
        return Measurement(actual == expected, {"record_visits": work})


def integration(config):
    if set(config) - {"dataset", "limits"} or "dataset" not in config:
        raise ValueError("Only dataset and limits are accepted; credentials do not belong in configuration")
    return Integration(OrdersAdapter(config["dataset"], limits=Limits(**config.get("limits", {}))),
                       OrdersVerifier(config["dataset"]), PairedEvaluation())
