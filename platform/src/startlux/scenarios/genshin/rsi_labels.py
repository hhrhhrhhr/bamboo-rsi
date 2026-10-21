"""Independent invariant labels from archived journal pixels and fixed tools.

These labels test tool selection only. They never grade combat or assert that
OCR is ground truth, and historical model choices are not consulted.
"""
from .desktop import report_from_observation
from .snapshot import SnapshotSuite, ExpectedAction


def label_suite(pack, suite):
    cases, labelled = [], 0
    for case in suite.cases:
        state = case.observation
        available = pack.candidates(state)
        accepted = []
        if state.facts.get("daily_needs_scan") is True:
            accepted = [a for a in available if a.skill == "daily.scan"]
        else:
            try:
                report = report_from_observation(state)
            except (ValueError, KeyError, TypeError):
                report = {"status": "unverified"}
            if report["status"] != "unverified":
                if (report["commission_completed_count"] == 4 and state.facts.get("daily_reward_claimed") is False
                        and report.get("daily_reward_claimed") is not True):
                    accepted = [a for a in available if a.skill == "daily.claim"]
                elif report["commission_completed_count"] < 4:
                    evidence = state.facts["daily_evidence"]
                    pending = {r["quest_id"].split(":", 1)[0] for r in evidence["commissions"]
                               if r["completed"] is False}
                    accepted = [a for a in available if a.skill == "daily.execute"
                                and str(a.arguments.get("slot")) in pending]
        labels = [ExpectedAction(id=a.id, skill=a.skill, skill_version=a.skill_version, arguments=a.arguments)
                  for a in accepted]
        labelled += bool(labels)
        cases.append(case.model_copy(update={"acceptable_actions": labels, "label_available": bool(labels),
                                             "expect_abstention": False}))
    result = SnapshotSuite.model_validate({**suite.model_dump(), "cases": [c.model_dump() for c in cases]})
    return result, {"method": "journal_and_catalog_invariants_v1", "labelled": labelled,
                    "unknown": len(cases)-labelled, "scope": "tool_selection_only", "human_labelled": False}
