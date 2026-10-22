"""Receipt-bound real-time execution with semantic freshness and local stop."""
from __future__ import annotations

from copy import deepcopy
import base64
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid

from .contracts import (ActionCommand, ActionResult, DecisionReceipt,
                        RealtimeDecisionProvider, RealtimeSession, catalog_hash)
from .settings import TransientProviderError


class AuditLog:
    """Exclusive append-only JSONL; HTTP credentials never enter this contract."""
    def __init__(self, path: Path | str):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("x", encoding="utf-8")
        self.image_directory = path.parent / "images"
        self.lock = threading.Lock()

    def __call__(self, event: dict):
        line = json.dumps(event, ensure_ascii=False, allow_nan=False)
        with self.lock:
            self.file.write(line + "\n")
            self.file.flush()

    def close(self):
        self.file.close()

    def archive_observation(self, observation):
        value = observation.audit_value()
        for index, image in enumerate(observation.images):
            raw = base64.b64decode(image.split(",", 1)[1], validate=True)
            if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("screenshot is not PNG")
            identity = hashlib.sha256(raw).hexdigest()
            self.image_directory.mkdir(parents=True, exist_ok=True)
            target = self.image_directory / (identity + ".png")
            try:
                with target.open("xb") as output:
                    output.write(raw)
            except FileExistsError:
                if hashlib.sha256(target.read_bytes()).hexdigest() != identity:
                    raise ValueError("archived screenshot is corrupt") from None
            value["images"][index].update(sha256=identity, path="images/" + target.name, mime="image/png")
        return value


class ExecutionEngine:
    def __init__(self, session: RealtimeSession, provider: RealtimeDecisionProvider,
                 instructions: str, *, model: str, audit=None, clock=time.monotonic, command_type=ActionCommand):
        if not model.strip() or not instructions.strip():
            raise ValueError("a pinned model and instructions are required")
        self.command_type = command_type
        self.session = session
        self.provider = provider
        self.instructions = instructions
        self.model = model
        self.audit = audit or (lambda event: None)
        self.clock = clock
        self.stopped = threading.Event()
        self.tick_lock = threading.Lock()
        self.active: ActionCommand | None = None
        self.active_since = 0.0
        self.needs_reconciliation = False
        self.scope = None
        self.session_id = None
        self.control_generation = None

    def _record(self, kind: str, **fields):
        self.audit(deepcopy({"type": kind, **fields}))

    def stop(self):
        # Never wait for the HTTP request or tick_lock before releasing input.
        self.stopped.set()
        self.session.stop()

    def _stop_after_error(self):
        try:
            self.stop()
        except Exception as error:
            # A broken control pipe must not replace the original failure.
            # The local stopped flag is already set; native lease expiry still
            # releases input when its acknowledgement cannot be obtained.
            self._record("stop_unacknowledged", error_type=type(error).__name__)

    def tick(self) -> dict:
        if not self.tick_lock.acquire(blocking=False):
            raise RuntimeError("only one tick may run at a time")
        try:
            return self._tick()
        finally:
            self.tick_lock.release()

    def _tick(self) -> dict:
        if self.stopped.is_set():
            return {"status": "stopped"}
        if self.active is not None:
            return self._poll_active()
        if self.needs_reconciliation:
            return {"status": "needs_reconciliation"}

        observation = self.session.observe().model_copy(deep=True)
        if self.scope is None:
            self.scope, self.session_id = observation.scope, observation.session_id
            self.control_generation = observation.control_generation
        elif observation.scope != self.scope or observation.session_id != self.session_id:
            self.stop()
            return {"status": "scope_changed"}
        elif observation.control_generation != self.control_generation:
            self.stop()
            return {"status": "control_changed"}
        observation_audit = (self.audit.archive_observation(observation)
                             if isinstance(self.audit, AuditLog) else observation.audit_value())
        options = deepcopy(self.session.candidates(observation.model_copy(deep=True)))
        if not options:
            self._record("no_candidates", observation=observation_audit)
            return {"status": "no_candidates", "facts": deepcopy(observation.facts)}
        if any(not option.available(observation) for option in options):
            raise ValueError("host offered an action whose preconditions are not satisfied")
        expected_catalog = catalog_hash(options)
        expected_observation = observation.binding()
        request_id = uuid.uuid4().hex
        started = self.clock()
        self._record("decision_requested", request_id=request_id,
                     observation=observation_audit, observation_hash=expected_observation,
                     catalog=[option.model_dump() for option in options],
                     catalog_hash=expected_catalog, requested_model=self.model,
                     instructions=self.instructions, action_granularity="skill")
        try:
            receipt = self.provider.decide(observation.model_copy(deep=True), self.instructions,
                                           deepcopy(options), request_id)
            receipt = DecisionReceipt.model_validate(receipt.model_dump())
            self._record("decision_received", receipt=receipt.model_dump())
            if (receipt.request_id != request_id or receipt.observation_hash != expected_observation
                    or receipt.catalog_hash != expected_catalog or receipt.model != self.model):
                raise ValueError("decision receipt does not match the frozen request")
            if self.stopped.is_set():
                self._record("decision_discarded", request_id=request_id, reason="stopped")
                return {"status": "stopped"}
            if receipt.outcome == "abstained":
                return {"status": "abstained", "request_id": request_id}
            selected = next((option for option in options if option.id == receipt.choice_id), None)
            if selected is None:
                raise ValueError("choice is outside the complete action catalog")
            remaining = selected.ttl_ms - int((self.clock() - started) * 1000)
            current = self.session.observe()
            current_options = self.session.candidates(current.model_copy(deep=True))
            current_selected = next((option for option in current_options if option.id == selected.id), None)
            command = self.command_type(command_id=uuid.uuid4().hex, request_id=request_id,
                                    session_id=observation.session_id, scope=observation.scope,
                                    control_generation=observation.control_generation,
                                    source_frame_id=observation.frame_id,
                                    basis_hash=selected.basis(observation), catalog_hash=expected_catalog,
                                    remaining_ttl_ms=max(1, remaining), option=selected)
            if remaining <= 0 or current_selected != selected or not command.applicable(current):
                self._record("decision_discarded", request_id=request_id,
                             reason="expired_or_preconditions_changed", observation=current.audit_value())
                return {"status": "stale", "request_id": request_id}
            if self.stopped.is_set():
                return {"status": "stopped"}
            # Once sent, an ambiguous transport failure must be reconciled by ID.
            # Never infer non-execution from a lost response and submit a new ID.
            self.active = command
            self.active_since = self.clock()
            self._record("action_requested", command=command.model_dump())
            try:
                result = self.session.start(command.model_copy(deep=True))
            except (ConnectionError, TimeoutError, OSError):
                self._record("action_transport_ambiguous", command_id=command.command_id)
                return {"status": "reconciling", "command_id": command.command_id}
            return self._consume_result(result)
        except Exception as error:
            # Error messages from third-party HTTP clients may include secrets.
            self._record("decision_or_execution_error", request_id=request_id,
                         error_type=type(error).__name__,
                         **({"error_code": error.code,
                             "elapsed_ms": max(0, (self.clock() - started) * 1000)}
                            if isinstance(error, TransientProviderError) else {}))
            self._stop_after_error()
            raise

    def _poll_active(self) -> dict:
        command = self.active
        try:
            if (self.clock() - self.active_since) * 1000 >= command.option.timeout_ms:
                try:
                    self.session.cancel(command.command_id)
                finally:
                    self.stop()
                self._record("action_timeout", command_id=command.command_id)
                self.needs_reconciliation = True
                self.active = None
                return {"status": "timed_out", "command_id": command.command_id}
            current = self.session.observe()
            if (current.session_id != command.session_id or current.scope != command.scope
                    or current.control_generation != command.control_generation):
                self.stop()
                self._record("active_control_changed", command_id=command.command_id)
                return {"status": "control_changed", "command_id": command.command_id}
            return self._consume_result(self.session.reconcile(command.command_id))
        except (ConnectionError, TimeoutError, OSError):
            return {"status": "reconciling", "command_id": command.command_id}
        except Exception:
            self._stop_after_error()
            raise

    def _consume_result(self, result: ActionResult) -> dict:
        result = ActionResult.model_validate(result.model_dump())
        if result.command_id != self.active.command_id:
            raise ValueError("execution result belongs to another command")
        self._record("action_result", result=result.model_dump())
        if result.terminal:
            self.active = None
            if result.status == "unknown":
                self.needs_reconciliation = True
                self.stop()
        return result.model_dump()
