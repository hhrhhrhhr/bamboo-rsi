"""Genshin decisions use bounded reasoning; legacy experiments retain defaults."""
from ...core.decision import StartLuxDecision
from .contracts import AccountScope


def configured_decision_url(config):
    """Explicitly authorized Bamboo gateways; never forward a key to an arbitrary URL."""
    url = config.get("decision_url", "https://api.bamboonode.dev/v1/decisions")
    if url not in {"https://api.bamboonode.dev/v1/decisions",
                   "https://api.bamboonode.cn/v1/decisions"}:
        raise ValueError("unsupported Genshin Decision endpoint")
    return url


class StartLuxRealtimeProvider(StartLuxDecision):
    def __init__(self, settings, client=None, *, none_threshold=0.2):
        super().__init__(settings, client, none_threshold=none_threshold,
                         think_mode="always", think_budget=128)

    def model_images(self, observation):
        # The first image is the current frame. Four full archived handbook
        # frames otherwise dominate upload latency on every decision. Their
        # verified quest identities/statuses remain in facts, and all original
        # pixels remain bound to the observation and independently audited.
        if isinstance(observation.scope, AccountScope):
            if observation.facts.get("execution_review"):
                return list(observation.images[:2])
            return list(observation.images[:1])
        # Optimizer observations can hold distinct training examples rather
        # than a current frame plus archived handbook pages.
        return super().model_images(observation)

    def criteria(self, observation, options):
        result = super().criteria(observation, options)
        if observation.facts.get("daily_needs_scan") is not False:
            return result
        ids = observation.facts.get("daily_evidence", {}).get("expected_quest_ids", [])
        for option in options:
            if option.skill not in {"daily.execute", "daily.recover"}:
                continue
            prefix = str(option.arguments.get("slot")) + ":"
            names = [identity[len(prefix):] for identity in ids
                     if isinstance(identity, str) and identity.startswith(prefix)]
            if len(names) == 1:
                result[option.id] += "；当前日志核验的委托名称：" + names[0]
            failures = observation.facts.get("daily_goal", {}).get("failed_attempts_by_slot", {})
            count = failures.get(str(option.arguments.get("slot")), 0)
            persistent = observation.facts.get("daily_goal", {}).get("failure_budget_scope", "")
            period = "本游戏日" if persistent.startswith("account_server_game_day") else "本轮"
            result[option.id] += (f"；{period}已失败或中断{count}次。优先选择有希望完成且尚未失败的其他委托；"
                                  "同一游戏日最多尝试两次，重启或换版本不会清空额度。")
            if option.skill == "daily.recover":
                result[option.id] += ("这是改变策略后的最后一次尝试。高处目标选ranged，柱子挡住视线选reposition；"
                    "低血量选heal_retry。原地恢复必须看到目标和安全落脚点，悬崖旁不要侧移；"
                    "恢复动作返回不证明清怪成功，下一步仍须核验日志。若不适合恢复，选择其他委托。")
        return result
