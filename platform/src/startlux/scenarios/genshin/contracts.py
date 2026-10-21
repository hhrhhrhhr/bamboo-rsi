"""BetterGI v1 scope; generic action semantics remain in the platform core."""
from pydantic import Field
from ...core.contracts import (Contract, ActionOption, ActionResult, DecisionReceipt,
                               canonical_hash, catalog_hash, RealtimeSession, RealtimeDecisionProvider)
from ...core.contracts import Observation as CoreObservation, ActionCommand as CoreActionCommand

class AccountScope(Contract):
    account_id: str = Field(min_length=1, max_length=128)
    server_id: str = Field(min_length=1, max_length=64)
    game_day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

class Observation(CoreObservation):
    scope: AccountScope

class ActionCommand(CoreActionCommand):
    scope: AccountScope
