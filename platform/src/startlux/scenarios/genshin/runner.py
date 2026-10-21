"""Bind the shared engine to BetterGI's wire-compatible account scope."""
from ...core.engine import AuditLog, ExecutionEngine
from .contracts import ActionCommand

class RealtimeRunner(ExecutionEngine):
    def __init__(self, *args, checkpoint_enabled=False, **kwargs):
        super().__init__(*args, command_type=ActionCommand, **kwargs)
        self.checkpoint_enabled = checkpoint_enabled
        self.checkpoint_count = 0
        self.checkpoint_pending = None
        self.checkpoint_started = 0.0
        self.checkpoint_verification = None

    def _poll_active(self):
        command = self.active
        expired = (self.clock() - self.active_since) * 1000 >= command.option.timeout_ms
        if self.checkpoint_pending is None and not (expired and self.checkpoint_enabled
                and command.option.skill == "daily.execute" and self.checkpoint_count < 2):
            return super()._poll_active()
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
            result = self.session.checkpoint(command.command_id)
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
            self.checkpoint_pending = None
            self.checkpoint_verification = command
            self._record("timeout_checkpoint_ready", command_id=command.command_id)
            return {"status": "checkpointed", "command_id": command.command_id}
        except Exception:
            self._stop_after_error()
            raise

    def _consume_result(self, result):
        if (self.checkpoint_enabled and self.active is not None
                and self.active.option.skill == "daily.execute"
                and result.status in {"failed", "cancelled"} and self.checkpoint_count < 2):
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
            if not any(identity.startswith(slot) for identity in report.get("completed_quest_ids", [])):
                self.stop()
                return {"status": "failed", "reason": "checkpoint_completion_unverified"}
            self._record("execution_checkpoint_observation", command_id=command.command_id,
                         observation=self.audit.archive_observation(current)
                         if isinstance(self.audit, AuditLog) else current.audit_value())
            self.checkpoint_verification = None
        return value
