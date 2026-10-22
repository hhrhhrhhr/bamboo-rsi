"""Trusted bounded recovery catalog, outside the optimizer's editable surface.

Recovery is the second daily attempt, not an extra unlimited combat loop.
The native executor separately reserves the attempt and checks current ground state.
"""
from .contracts import ActionOption


def recovery_options():
    modes = (
        ("ranged", "在当前委托位置切夜兰，最多15秒普通远程攻击高处目标", 45000),
        ("reposition_left", "向左侧移动350毫秒避开柱子，然后夜兰普通远程攻击最多15秒", 45000),
        ("reposition_right", "向右侧移动350毫秒避开柱子，然后夜兰普通远程攻击最多15秒", 45000),
        ("heal_retry", "到七天神像恢复生命后重新执行该委托路线", 300000),
    )
    return [ActionOption(
        id=f"recover.{slot}.{mode}", label=f"委托{slot}改变策略重试：{label}；占用今天最后一次尝试，结束后核验日志",
        skill="daily.recover", skill_version="1", arguments={"slot": slot, "mode": mode},
        preconditions={"account_verified": True, "foreground": True, "region": "Mondstadt",
            "scene": "world", "daily_recovery_v1": True, "daily_recovery_grounded": True,
            "daily_needs_scan": False,
            f"daily_slot_{slot}_retry" if mode == "heal_retry" else f"daily_slot_{slot}_local_recovery": True},
        timeout_ms=timeout, ttl_ms=15000)
        for slot in range(1, 5) for mode, label, timeout in modes]
