"""Explicit installed entry points; never import executable paths from task data."""
from importlib.metadata import entry_points
from .contracts import Integration


def available():
    return sorted({"business.order_reconciliation", "repository.literal_search",
                   *(e.name for e in entry_points(group="bamboo_rsi.integrations"))})


def load(identifier, config):
    matches = [e for e in entry_points(group="bamboo_rsi.integrations") if e.name == identifier]
    if identifier in {"business.order_reconciliation", "repository.literal_search"}:
        if matches:
            raise ValueError("Installed integration shadows a built-in identity")
        if identifier == "business.order_reconciliation":
            from .adapters.orders import integration
        else:
            from .adapters.repository import integration
        result = integration(config)
    else:
        if len(matches) != 1:
            raise ValueError("Integration must resolve to exactly one installed plugin")
        result = matches[0].load()(config)
    if not isinstance(result, Integration) or result.adapter.describe().id != identifier:
        raise ValueError("Plugin task identity mismatch")
    result.fingerprint()
    return result
