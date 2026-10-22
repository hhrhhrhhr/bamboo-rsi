"""Bind the shared engine to BetterGI's wire-compatible account scope."""
from ...core.engine import AuditLog, ExecutionEngine
import uuid
from .contracts import ActionCommand, ActionOption, DecisionReceipt, catalog_hash

class RealtimeRunner(ExecutionEngine):
    def __init__(self, *args, checkpoint_enabled=False, supervision_enabled=False, **kwargs):
        super().__init__(*args, command_type=ActionCommand, **kwargs)
        self.checkpoint_enabled = checkpoint_enabled
        self.checkpoint_count = 0
        self.checkpoint_pending = None
        self.checkpoint_started = 0.0
        self.checkpoint_verification = None
        self.supervision_enabled = supervision_enabled and checkpoint_enabled
        self.checkpoint_limit = 8 if self.supervision_enabled else 2
        self.review_command = None
        self.review_after = 0.0
        self.review_count = 0
        self.review_previous = None
        self.review_checkpoint_frame = None
        self.review_checkpoint_sent = False
        self.review_checkpoint_phase = None

    def _same_control(self, observation, command):
        return (observation.scope == command.scope and observation.session_id == command.session_id
                and observation.control_generation == command.control_generation)

    def _review_active(self):
        """Decision may end the current skill, never issue concurrent game input."""
        command = self.active
        current = self.session.observe()
        if not self._same_control(current, command):
            self.stop()
            return {"status": "control_changed"}
        self.review_after = self.clock() + 20
        if (current.facts.get("daily_review_interruptible") is not True
                or current.facts.get("foreground") is not True
                or current.facts.get("account_verified") is not True):
            return {"status": "running", "command_id": command.command_id}
        observation = current.model_copy(deep=True)
        observation.facts["execution_review"] = {
            "command_id": command.command_id, "slot": command.option.arguments["slot"],
            "elapsed_seconds": max(0, self.clock() - self.active_since),
            "previous_review": self.review_previous[0] if self.review_previous else None,
            "image_order": "current_then_previous_review"}
        observation = observation.model_copy(update={"images": list(current.images[:1])
            + (self.review_previous[1] if self.review_previous else [])})
        options = [ActionOption(id=identity, label=label, skill="supervision." + identity,
            skill_version="1", preconditions={"daily_review_interruptible": True}, ttl_ms=15000)
            for identity, label in (
                ("continue", "目标尚未完成，当前移动、战斗或交互有进展，继续现有有时限的技能"),
                ("rescan", "目标似已完成，或明显卡住/反复无进展：停止当前技能，重新核验日志，再选择任务"))]
        request = uuid.uuid4().hex
        started = self.clock()
        instructions = ("检查正在执行的委托是否仍在接近目标。第一张是当前画面，第二张若有则是上次复判画面。"
            "结合任务名称、阶段耗时、execution_progress、player_low_hp和历史失败判断。"
            "similar_world_seconds只表示画面变化小，不能证明没有伤害；enemy_hp_fraction和target_distance为空时不要编造。"
            "若前后画面目标、遮挡和人物位置基本不变，已经反复攻击但看不出目标血量或阶段变化，应选rescan。"
            "原石到账或委托完成提示出现就选rescan；"
            "敌人不可达、被障碍反复卡住且无进展也选rescan。正常赶路、战斗、等待动画不要误判完成。"
            "仅有敌人消失或脚本结束不能证明完成；rescan之后必须独立核验。"
            "核验未完成后可由下一次Decision选择远程、侧移避障、恢复生命重试，或选择其他尚未失败的委托。"
            "不要因缺少当前可选的恢复动作而一直选择continue；本次只决定是否安全停止。不要发新键鼠操作。")
        self.review_count += 1
        self._record("supervision_requested", request_id=request, command_id=command.command_id,
            observation_hash=observation.binding(),
            observation=self.audit.archive_observation(observation) if isinstance(self.audit, AuditLog)
                else observation.audit_value(), instructions=instructions,
            catalog=[o.model_dump() for o in options], catalog_hash=catalog_hash(options), requested_model=self.model)
        try:
            receipt = DecisionReceipt.model_validate(self.provider.decide(observation.model_copy(deep=True),
                instructions, options, request).model_dump())
            self._record("supervision_received", command_id=command.command_id, receipt=receipt.model_dump())
            if (receipt.request_id != request or receipt.observation_hash != observation.binding()
                    or receipt.catalog_hash != catalog_hash(options) or receipt.model != self.model
                    or receipt.choice_id not in {None, "continue", "rescan"}):
                raise ValueError("supervision receipt does not match its observation and catalog")
            if self.stopped.is_set():
                return {"status": "stopped"}
            fresh = self.session.observe()
            if not self._same_control(fresh, command):
                self.stop()
                return {"status": "control_changed"}
            result = self.session.reconcile(command.command_id)
            if result.terminal:
                self._record("supervision_discarded", request_id=request, reason="skill_already_ended")
                return self._consume_result(result)
            self.review_previous = ({"elapsed_seconds": observation.facts["execution_review"]["elapsed_seconds"],
                                     "goal": current.facts.get("daily_goal"), "choice": receipt.choice_id,
                                     "progress": current.facts.get("execution_progress"),
                                     "player_low_hp": current.facts.get("player_low_hp")},
                                    list(current.images[:1]))
            self.review_after = self.clock() + 20
            if (self.clock() - started >= 15 or fresh.facts.get("daily_review_interruptible") is not True
                    or fresh.facts.get("daily_goal", {}).get("phase_revision") !=
                       current.facts.get("daily_goal", {}).get("phase_revision")):
                self._record("supervision_discarded", request_id=request, reason="expired_or_phase_changed")
                return {"status": "running", "command_id": command.command_id}
            if receipt.outcome == "abstained" or receipt.choice_id == "continue":
                self._record("supervision_resolved", request_id=request, effect="continue_existing_skill")
                return {"status": "running", "command_id": command.command_id}
            self.checkpoint_pending = command
            self.checkpoint_started = self.clock()
            self.review_checkpoint_frame = fresh.frame_id
            self.review_checkpoint_phase = fresh.facts.get("daily_goal", {}).get("phase_revision")
            self.review_checkpoint_sent = False
            self.checkpoint_count += 1
            self._record("recovery_scheduled", reason="decision_journal_checkpoint",
                         request_id=request, command_id=command.command_id,
                         reason_attempt=self.checkpoint_count, operation="reconcile_then_model_scan", input_executed=False)
            return self._poll_active()
        except Exception:
            self._stop_after_error()
            raise

    def _poll_active(self):
        command = self.active
        expired = (self.clock() - self.active_since) * 1000 >= command.option.timeout_ms
        if self.checkpoint_pending is None and not (expired and self.checkpoint_enabled
                and command.option.skill in {"daily.execute", "daily.recover"} and self.checkpoint_count < self.checkpoint_limit):
            value = super()._poll_active()
            if (self.supervision_enabled and self.active is not None and command.option.skill in {"daily.execute", "daily.recover"}
                    and not self.stopped.is_set() and self.checkpoint_count < self.checkpoint_limit
                    and self.review_count < 64 and self.clock() >= self.review_after):
                try:
                    return self._review_active()
                except Exception:
                    if not self.stopped.is_set():
                        self._stop_after_error()
                    raise
            return value
        if self.checkpoint_pending is None:
            self.checkpoint_count += 1
            self.checkpoint_pending = command
            self.checkpoint_started = self.clock()
            self._record("recovery_scheduled", reason="timeout_journal_checkpoint",
                         reason_attempt=self.checkpoint_count, operation="reconcile_then_model_scan",
                         input_executed=False)
            self._record("action_timeout", command_id=command.command_id)
        try:
            current = self.session.observe()
            if (current.scope != command.scope or current.session_id != command.session_id
                    or current.control_generation != command.control_generation):
                self.stop()
                return {"status": "control_changed", "command_id": command.command_id}
            if self.review_checkpoint_frame is None:
                result = self.session.checkpoint(command.command_id)
            elif self.review_checkpoint_sent:
                result = self.session.reconcile(command.command_id)
            else:
                if current.facts.get("daily_goal", {}).get("phase_revision") != self.review_checkpoint_phase:
                    self.checkpoint_pending = None
                    self.review_checkpoint_frame = None
                    self.checkpoint_count -= 1
                    self._record("supervision_discarded", reason="phase_changed_before_checkpoint")
                    return {"status": "running", "command_id": command.command_id}
                result = self.session.checkpoint(command.command_id, review_frame_id=current.frame_id)
                if result.reason == "review_checkpoint_not_safe":
                    self.checkpoint_pending = None
                    self.review_checkpoint_frame = None
                    self.checkpoint_count -= 1
                    self._record("supervision_discarded", reason="native_checkpoint_not_safe")
                    return {"status": "running", "command_id": command.command_id}
                self.review_checkpoint_sent = True
            if result.command_id != command.command_id:
                raise ValueError("checkpoint response belongs to another action")
            if result.status in {"running", "accepted"}:
                if self.clock() - self.checkpoint_started >= 10:
                    self.stop()
                    return {"status": "timed_out", "command_id": command.command_id}
                return {"status": "reconciling", "command_id": command.command_id}
            current = self.session.observe()
            if (current.scope != command.scope or current.session_id != command.session_id
                    or current.control_generation != command.control_generation):
                self.stop()
                return {"status": "control_changed", "command_id": command.command_id}
            options = self.session.candidates(current)
            if (result.status not in {"succeeded", "cancelled", "failed"} or
                    current.facts.get("daily_needs_scan") is not True or not options
                    or any(option.skill != "daily.scan" for option in options)):
                self.stop()
                return {"status": "needs_reconciliation", "command_id": command.command_id}
            super()._consume_result(result)
            was_review = self.review_checkpoint_frame is not None
            self.checkpoint_pending = None
            self.review_checkpoint_frame = None
            self.review_checkpoint_sent = False
            self.checkpoint_verification = command
            self._record("execution_checkpoint_ready" if was_review else "timeout_checkpoint_ready",
                         command_id=command.command_id)
            return {"status": "checkpointed", "command_id": command.command_id}
        except Exception:
            self._stop_after_error()
            raise

    def _consume_result(self, result):
        if self.active is not None and self.review_command != self.active.command_id:
            self.review_command = self.active.command_id
            self.review_after = self.clock() + 20
            self.review_previous = None
        if (self.checkpoint_enabled and self.active is not None
                and self.active.option.skill in {"daily.execute", "daily.recover"}
                and result.status in {"failed", "cancelled"} and self.checkpoint_count < self.checkpoint_limit):
            command = self.active
            current = self.session.observe()
            options = self.session.candidates(current)
            if (current.scope == command.scope and current.session_id == command.session_id
                    and current.control_generation == command.control_generation
                    and current.facts.get("daily_needs_scan") is True and options
                    and all(option.skill == "daily.scan" for option in options)):
                super()._consume_result(result)
                self.checkpoint_count += 1
                self.checkpoint_verification = command
                self._record("recovery_scheduled", reason="failure_journal_checkpoint",
                             reason_attempt=self.checkpoint_count, input_executed=False,
                             operation="model_scan_after_terminal_failure")
                self._record("execution_checkpoint_ready", command_id=command.command_id)
                return {"status": "checkpointed", "command_id": command.command_id}
        if (self.active is not None and self.active.option.skill == "resin.domain" and result.status == "failed"):
            command = self.active
            current = self.session.observe()
            options = self.session.candidates(current)
            if (current.scope == command.scope and current.session_id == command.session_id
                    and current.control_generation == command.control_generation
                    and current.facts.get("resin_needs_scan") is True
                    and current.facts.get("resin", {}).get("blocked") is False
                    and options and all(o.skill == "resin.scan" for o in options)):
                super()._consume_result(result)
                self._record("resin_failure_replan", command_id=command.command_id, input_executed=False)
                return {"status": "checkpointed", "command_id": command.command_id}
        scanning = self.active is not None and self.active.option.skill == "daily.scan"
        value = super()._consume_result(result)
        if scanning and result.status == "succeeded" and self.checkpoint_verification is not None:
            command = self.checkpoint_verification
            current = self.session.observe()
            if (current.scope != command.scope or current.session_id != command.session_id
                    or current.control_generation != command.control_generation):
                self.stop()
                return {"status": "control_changed", "command_id": command.command_id}
            from .desktop import report_from_observation
            report = report_from_observation(current, expected_scope=command.scope)
            slot = str(command.option.arguments["slot"]) + ":"
            completed = any(identity.startswith(slot) for identity in report.get("completed_quest_ids", []))
            if not completed and not (self.supervision_enabled and report.get("status") == "incomplete"):
                self.stop()
                return {"status": "failed", "reason": "checkpoint_completion_unverified"}
            self._record("execution_checkpoint_observation" if completed else "commission_incomplete_replan",
                         command_id=command.command_id, completed=completed,
                         observation=self.audit.archive_observation(current)
                         if isinstance(self.audit, AuditLog) else current.audit_value())
            self.checkpoint_verification = None
        return value
