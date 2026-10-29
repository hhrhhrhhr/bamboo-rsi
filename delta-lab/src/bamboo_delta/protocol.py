"""A fixed choice catalogue for observation; choices are never executed here."""
import base64
import hashlib
import json
import math
import urllib.parse
import urllib.request

from bamboo_rsi.storage import encoded


PHASES = {"active": "可见游戏内角色视角", "menu": "大厅、登录、匹配或菜单",
          "result": "明确的任务或对局结算画面", "unknown": "无法判断"}
SKILLS = {"observe": "继续观察", "stop": "停止并等待检查", "move_forward": "短距离向前移动",
          "move_back": "短距离后退", "strafe_left": "短距离左移", "strafe_right": "短距离右移",
          "look_left": "小幅向左转动视角", "look_right": "小幅向右转动视角",
          "interact": "交互", "reload": "换弹", "heal": "恢复生命", "seek_cover": "寻找掩体"}
PROGRESS = {"progressing": "目标有进展", "stuck": "疑似受阻", "unknown": "缺少连续证据"}
CATALOGUES = {"phase": PHASES, "skill": SKILLS, "progress": PROGRESS}


class ProbeError(RuntimeError):
    """Only fixed, non-secret error codes cross the report boundary."""


def endpoint(value):
    url = urllib.parse.urlsplit(value)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or not url.path.endswith("/v1/decisions")
            or any(c.isspace() for c in value)):
        raise ProbeError("invalid_decision_endpoint")
    return value


def choice(answer, catalogue):
    if not isinstance(answer, dict) or not isinstance(answer.get("probabilities"), dict):
        raise ProbeError("invalid_choice_shape")
    probabilities = answer["probabilities"]
    if set(probabilities) != set(catalogue):
        raise ProbeError("choice_catalogue_mismatch")
    values = [*probabilities.values(), answer.get("confidence"), answer.get("none_probability", 0)]
    if any(type(v) not in (float, int) or not math.isfinite(v) or not 0 <= v <= 1 for v in values):
        raise ProbeError("invalid_choice_probability")
    if abs(sum(probabilities.values()) - 1) > .02:
        raise ProbeError("invalid_probability_total")
    selected = answer.get("choice")
    if selected is not None and (not isinstance(selected, str) or selected not in catalogue):
        raise ProbeError("unknown_choice")
    if (selected is None or answer["confidence"] < .9 or answer.get("none_probability", 0) >= .2
            or probabilities[selected] < .9):
        return None
    return selected


def parse_reply(raw, model):
    if len(raw) > 1_000_000:
        raise ProbeError("provider_response_too_large")
    try:
        data = json.loads(raw)
        if data["model"] != model:
            raise ProbeError("model_identity_mismatch")
        answers = data["answers"]
        result = {name: choice(answers[name], options) for name, options in CATALOGUES.items()}
        result["response_sha256"] = hashlib.sha256(raw).hexdigest()
        # An active scene is not proof that a match is authorized; no execution is exposed.
        result["suggested_skill"] = result.pop("skill") or "observe"
        if result["phase"] != "active":
            result["suggested_skill"] = "stop"
        result["executed"] = False
        result["verified_outcome"] = "unknown"
        return result
    except ProbeError:
        raise
    except (ValueError, KeyError, TypeError):
        raise ProbeError("invalid_provider_response") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ProbeError("provider_redirect_rejected")


class Decision:
    def __init__(self, config, secret):
        self.endpoint, self.model = endpoint(config["decision_endpoint"]), config["model"]
        if not secret or any(c.isspace() for c in secret):
            raise ProbeError("missing_or_invalid_model_key")
        self.secret = secret

    def decide(self, png, objective, prior):
        payload = {"model": self.model, "state": encoded({"objective": objective,
                   "mode": "observation_only", "previous_observations": prior[-2:]}).decode(),
                   "images": ["data:image/png;base64," + base64.b64encode(png).decode()],
                   "allow_none": True, "none_check": True, "think_mode": "never", "return_details": True,
                   "questions": {name: {"type": "choice", "allow_none": True, "criteria": choices,
                       "instructions": "只依据可见画面及历史观察选择；不确定时弃权。仅提出建议，不执行。不要将模型判断当作成功证明。"}
                       for name, choices in CATALOGUES.items()}}
        request = urllib.request.Request(self.endpoint, encoded(payload),
            {"Content-Type": "application/json", "Authorization": "Bearer " + self.secret})
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
                raw = response.read(1_000_001)
            return parse_reply(raw, self.model)
        except ProbeError:
            raise
        except Exception:
            # HTTP error bodies, URLs and transport exception text can contain secrets.
            raise ProbeError("decision_request_failed") from None
