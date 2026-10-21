"""Explicit offline fixtures; never used by a real client or enabled implicitly."""
from pathlib import Path

from .core.contracts import DecisionReceipt, canonical_hash, catalog_hash


class FixtureDecision:
    def __init__(self, model):
        if not model.startswith("fixture-"):
            raise ValueError("fixture provider requires a fixture model identity")
        self.model = model

    def decide(self, observation, instructions, options, request_id):
        ids = {o.id for o in options}
        choice = options[0].id
        if {"sum-signed", "sum-positive"} <= ids:
            choice = "sum-signed" if "include negative" in instructions.lower() else "sum-positive"
        abstain = observation.facts.get("fixture_abstain") is True
        return DecisionReceipt(request_id=request_id, observation_hash=observation.binding(),
            catalog_hash=catalog_hash(options), model=self.model,
            outcome="abstained" if abstain else "selected", choice_id=None if abstain else choice,
            elapsed_ms=0.0, payload_hash=canonical_hash({"fixture": True}),
            raw_response={"synthetic_fixture": True, "usage": {"input_tokens": 1, "output_tokens": 1}})

    def close(self):
        pass


class FixtureImprover:
    def __init__(self):
        from harness2.core.spec import ChannelSpec
        self.spec = ChannelSpec("opencode", "fixture-improver")
        self.calls = 0

    def call(self, mission, context_dir, label, *, harvest=None, **kwargs):
        from harness2.core.proposer import Reply
        root = Path(context_dir)
        self.calls += 1
        workspace = None
        ledger = (root / "source/ledger.json").exists()
        if label.startswith("experience.") and not ledger:
            text = "NO_ENTRY"
        elif label.startswith("experience."):
            text = ("<experience>\ncue: A ledger contains signed amounts\n"
                    "lesson: Include negative amounts when summing the complete ledger.\n"
                    "evidence: The source contained negative entries and the signed-sum probe included them.\n"
                    "confidence: synthetic plumbing exercise only\n</experience>")
        elif label.startswith("select."):
            candidates = sorted((root / "candidates").glob("*.md"))
            text = "<why>Synthetic selector fixture.</why>\n```choice\n" + candidates[0].stem + "\n```"
        else:
            workspace = root / "harness"
            if ledger:
                (workspace / "tool_guidance.md").write_text(
                    "For complete ledger totals, include negative amounts as well as positive amounts.\n",
                    encoding="utf-8")
            text = "<notes>Synthetic edit fixture; no live capability claim.</notes>"
        return Reply(label=label, text=text, ok=True, attempts=1, workspace=workspace, channel="fixture")

    def close(self):
        pass
