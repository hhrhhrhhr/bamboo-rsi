"""Bounded timer-aware route policy, selected by Decision and bound to a run."""
from pydantic import model_validator

from ...core.contracts import Contract


class TimedRoutePolicy(Contract):
    skip_optional_fights_below_seconds: int = 0

    @model_validator(mode="after")
    def bounded_threshold(self):
        if self.skip_optional_fights_below_seconds not in (0, 30, 45, 60):
            raise ValueError("timed route threshold must be 0, 30, 45 or 60 seconds")
        return self


    def require_combat(self):
        # Retain parsing of historical bundles without permitting their unsafe
        # hypothesis to execute again. User-confirmed route enemies gate passage.
        if self.skip_optional_fights_below_seconds:
            raise ValueError("Full Speed Ahead requires defeating every route encounter")


def required_combat_route(parent):
    if parent.route and parent.route.skip_optional_fights_below_seconds:
        return TimedRoutePolicy()
    return parent.route


def route_binding_verified(run, bundle):
    from .assessment import read_records
    records, issues = read_records(run)
    if issues:
        return False
    events = [(line, event) for line, event in records if event.get("type") == "timed_route_policy_bound"]
    # Historical bundles had no route policy or binding event. Their identities
    # and recorded outcomes remain unchanged.
    if not events:
        return bundle.route is None
    if len(events) != 1:
        return False
    line, event = events[0]
    armed = [n for n, e in records if e.get("type") == "execution_arming"]
    expected = (bundle.route or TimedRoutePolicy()).model_dump()
    return (bool(armed) and line < min(armed) and event.get("bundle_hash") == bundle.identity
            and event.get("expected") == expected and event.get("native") == expected)
