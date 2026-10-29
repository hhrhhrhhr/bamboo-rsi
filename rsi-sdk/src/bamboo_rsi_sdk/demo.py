"""Explicit offline fixtures and scripted proposals, never a real-model acceptance claim."""
import csv
import hashlib
import json
from pathlib import Path
from bamboo_rsi.storage import write


def example_dataset(root):
    root = Path(root)
    if root.exists():
        raise ValueError("Use a new fixture directory")
    root.mkdir(parents=True)
    manifest = {"schema_version": 1, "scope": "offline_reference", "files": {}, "training": [], "validation": []}
    for n in range(7):
        case = {"id": f"batch-{n}", "orders": f"batch-{n}-orders.csv", "payments": f"batch-{n}-payments.csv"}
        orders = [[f"order-{n}-{i}", "CNY" if i % 2 == 0 else "USD", f"{i+1}.00"] for i in range(20+n)]
        payments = []
        for i, (order, currency, amount) in enumerate(orders):
            payments.append([f"pay-{n}-{i}", order, currency, amount, "pending" if i % 5 == 0 else "captured"])
        payments += [[f"extra-{n}", orders[1][0], "USD", "1.25", "captured"],
                     [f"orphan-{n}", "unknown-order", "CNY", "3.20", "captured"],
                     [f"other-currency-{n}", orders[0][0], "EUR", "4.00", "captured"]]
        for key, header, rows in (("orders", ["order_id", "currency", "amount"], orders),
                                   ("payments", ["payment_id", "order_id", "currency", "amount", "status"], payments)):
            target = root / case[key]
            with target.open("w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f); writer.writerow(header); writer.writerows(rows)
            manifest["files"][case[key]] = hashlib.sha256(target.read_bytes()).hexdigest()
        manifest["training" if n == 0 else "validation"].append(case)
    write(root / "manifest.json", manifest, immutable=True)
    return root


class ReferenceProposer:
    """For integration conformance demonstrations only. Makes zero model calls."""
    def propose(self, context, destination):
        file = "reconciliation-policy.json"
        before = context["files"][file]
        changes = [] if json.loads(before["content"])["join"] == "indexed" else [
            {"path": file, "before_sha256": before["sha256"], "content": '{"join":"indexed"}'}]
        write(Path(destination) / "provenance.json", {"backend": "scripted_reference", "real_model": False}, immutable=True)
        return {"summary": "Offline integration reference", "hypothesis": "Index captured payments once per batch", "changes": changes}
