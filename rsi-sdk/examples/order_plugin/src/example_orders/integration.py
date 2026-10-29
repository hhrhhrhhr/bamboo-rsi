"""Reuse the reference business implementation under an explicit installed ID."""
from dataclasses import replace
from bamboo_rsi.storage import identity
from bamboo_rsi_sdk import Integration, Limits
from bamboo_rsi_sdk.adapters.orders import OrdersAdapter, OrdersVerifier
from bamboo_rsi_sdk.contracts import code_hash
from bamboo_rsi_sdk.evaluation import PairedEvaluation


class ExampleInvoices(OrdersAdapter):
    def describe(self):
        return replace(super().describe(), id="example.invoice_reconciliation")

    def fingerprint(self):
        return identity({"base": super().fingerprint(), "plugin": code_hash(__file__)})


def create(config):
    if set(config) - {"dataset", "limits"} or "dataset" not in config:
        raise ValueError("Only dataset and limits are accepted")
    return Integration(ExampleInvoices(config["dataset"], limits=Limits(**config.get("limits", {}))),
                       OrdersVerifier(config["dataset"]), PairedEvaluation())
