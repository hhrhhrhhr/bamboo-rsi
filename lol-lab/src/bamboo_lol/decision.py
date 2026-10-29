"""Decision provider chooses a goal, bounded skill and visible screen cell."""
import base64
import hashlib
import ipaddress
import json
import ssl
import time
from urllib.parse import urlsplit
import httpx

from bamboo_rsi.storage import encoded
from bamboo_delta.protocol import ProbeError, choice, endpoint
from .protocol import GOALS, GRID, SKILLS, neighbours, permitted, own_player
from .recording import target_frame
from .vision import candidates, bind_target

JEV_MODEL = 'typesafe/jev-1.14'
ABSTAIN = '__abstain__'


def provider_endpoint(config):
    """Explicit local SystemOne transport; cloud endpoint policy stays intact."""
    protocol = config.get('decision_protocol', 'auto')
    if protocol == 'auto':
        return endpoint(config['decision_endpoint'])
    if protocol != 'systemone':
        raise ProbeError('invalid_decision_protocol')
    address = config['decision_endpoint']
    try:
        url = urlsplit(address)
        port = url.port
        if (url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.path != '/v1/systemone'
                or any(c.isspace() for c in address) or port == 0):
            raise ValueError()
        if url.scheme == 'http':
            host = ipaddress.ip_address(url.hostname)
            networks = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8', '::1/128')
            if not any(host in ipaddress.ip_network(n) for n in networks):
                raise ValueError()
        if config.get('decision_transport') != 'direct':
            raise ProbeError('systemone_requires_direct_transport')
    except (TypeError, ValueError):
        raise ProbeError('invalid_systemone_endpoint') from None
    return address


def validate_provider(config):
    address = provider_endpoint(config)
    if config.get('decision_protocol') == 'systemone':
        if config['model'].startswith('typesafe/jev'):
            raise ProbeError('systemone_requires_reported_model_identity')
        return 'systemone'
    path = urlsplit(address).path
    model = config['model']
    if model.startswith('typesafe/jev'):
        if model != JEV_MODEL or path != '/jev/v1/decisions':
            raise ProbeError('jev_model_or_endpoint_mismatch')
        return 'jev'
    if path == '/jev/v1/decisions':
        raise ProbeError('jev_model_or_endpoint_mismatch')
    return 'startlux'


def wire_catalogue(catalogue, model):
    if model != JEV_MODEL:
        return catalogue
    if ABSTAIN in catalogue:
        raise ProbeError('reserved_abstention_choice')
    return {**catalogue, ABSTAIN: '信息不充分、画面不明确或所有其他选项都不适用；弃权，不执行动作。'}


def parse_response(raw, model, catalogues):
    if len(raw) > 1_000_000:
        raise ProbeError("decision_response_too_large")
    try:
        data = json.loads(raw)
        if data["model"] != model or set(data["answers"]) != set(catalogues):
            raise ProbeError("decision_identity_or_questions_mismatch")
        selected = {key: choice(data["answers"][key], wire_catalogue(catalogue, model))
                    for key, catalogue in catalogues.items()}
        if model == JEV_MODEL:
            selected = {k: None if value == ABSTAIN else value for k, value in selected.items()}
        return selected, hashlib.sha256(raw).hexdigest()
    except ProbeError:
        raise
    except (KeyError, TypeError, ValueError):
        raise ProbeError("invalid_decision_response") from None


class Decision:
    def __init__(self, config, secret):
        self.endpoint, self.model = provider_endpoint(config), config["model"]
        self.protocol = validate_provider(config)
        if self.protocol != 'systemone' and (not secret or any(c.isspace() for c in secret)):
            raise ProbeError("model_key_missing")
        # This unauthenticated endpoint must never receive a cloud account key,
        # even when an enclosing process happens to have one in its environment.
        self.secret = '' if self.protocol == 'systemone' else secret
        self.last_response = None
        self.last_images = []
        transport = config.get('decision_transport', 'system')
        if transport not in ('system', 'direct'):
            raise ProbeError('invalid_decision_transport')
        self.client = httpx.Client(verify=ssl.create_default_context(), follow_redirects=False, trust_env=transport == 'system',
            timeout=6, limits=httpx.Limits(max_connections=1, max_keepalive_connections=1, keepalive_expiry=60))

    def close(self):
        self.client.close()

    def request(self, state, questions, png=None, *, timeout=6):
        self.last_response = None
        self.last_images = [] if png is None else [{'role': 'full', 'data': png}]
        if self.protocol == 'jev':
            native_questions = {key: {'type': 'choice', 'instructions': q['instructions'],
                'criteria': wire_catalogue(q['criteria'], self.model)} for key, q in questions.items()}
            payload = {'model': self.model, 'state': encoded(state).decode(), 'questions': native_questions}
            if png is not None:
                media = 'image/jpeg' if png.startswith(b'\xff\xd8\xff') else 'image/png'
                payload['state'] = [encoded(state).decode(), {'type': 'image_url', 'image_url': {
                    'url': f'data:{media};base64,' + base64.b64encode(png).decode()}}]
        else:
            payload = {"model": self.model, "state": encoded(state).decode(), "questions": questions,
                       "allow_none": True, "none_check": True, "think_mode": "never", "return_details": True}
            if self.protocol == 'systemone':
                payload.pop('model')  # The local service selects and reports its pinned model.
                payload['think_mode'] = 'auto'
        if png is not None and self.protocol in ('startlux', 'systemone'):
            # The provider gates thinking on first-pass top probability, while
            # our parser also checks confidence. A small trigger margin permits
            # reconsideration when top probability clears .9 but confidence
            # does not. Parser confidence and freshness gates stay unchanged.
            payload.update(think_mode='auto', gate=.92, think_budget=64)
            # The provider binds image inputs to explicit markers in shared state.
            payload["state"] = encoded({"screenshot": "<image>", "observation": state}).decode()
            media = 'image/jpeg' if png.startswith(b'\xff\xd8\xff') else 'image/png'
            payload["images"] = [f"data:{media};base64," + base64.b64encode(png).decode()]
            if 'farm_wave' in questions.get('skill', {}).get('criteria', {}):
                points = candidates(png)
                visible = {k: v for k, v in {**GRID, **points}.items()
                           if k in questions.get('target', {}).get('criteria', {})}
                detail, bounds, annotations = target_frame(png, visible or None)
                self.last_images.append({'role': 'world_targets', 'data': detail,
                                         'source_bounds': bounds, 'annotations': annotations})
                payload['state'] = encoded({'observation': state, 'views': [
                    {'role': 'full', 'image': '<image>'},
                    {'role': 'world_targets', 'image': '<image>', 'source_bounds': bounds,
                     'annotation_kind': annotations['kind']}],
                    'view_guidance': '两张图来自同一帧：第一张是未加标注的完整画面，第二张是裁剪后叠加候选位置编号的辅助视图。'
                        '青色十字及文字是程序标注。p编号仅是格点；c编号是程序从暗色短条与红色像素生成的疑似小兵位置，可能误检。'
                        '标注不是游戏单位、血条、奖励或推荐动作，必须在原图确认确实是敌方小兵；误检或看不清就选none。'
                        'target选项与对应编号十字的位置一一对应；坐标始终指完整画面，不是裁剪图坐标。'
                        '以第一张原图确认实际游戏状态，标注遮挡时回看原图。局部图没有敌人不能证明全图没有敌人；无法确认就弃权。'}).decode()
                payload['images'].append('data:image/jpeg;base64,' + base64.b64encode(detail).decode())
        started = time.monotonic()
        headers = {'Content-Type': 'application/json'}
        if self.protocol != 'systemone':
            headers['Authorization'] = 'Bearer ' + self.secret
        try:
            with self.client.stream("POST", self.endpoint, content=encoded(payload), timeout=timeout,
                    headers=headers) as response:
                if 300 <= response.status_code < 400:
                    raise ProbeError("provider_redirect_rejected")
                if response.status_code != 200:
                    if response.status_code == 429:
                        raise ProbeError('decision_rate_limited')
                    if response.status_code == 403:
                        # Read a bounded diagnostic, never include its text in an error/log.
                        body = bytearray()
                        for chunk in response.iter_bytes(chunk_size=1024):
                            body.extend(chunk)
                            if len(body) >= 4096:
                                break
                        if b'insufficient user quota' in bytes(body).lower():
                            raise ProbeError('decision_quota_exhausted')
                    raise ProbeError(f"decision_http_{response.status_code}")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() - started > timeout:
                        raise ProbeError("decision_request_timeout")
                    if len(raw) + len(chunk) > 1_000_000:
                        raise ProbeError("decision_response_too_large")
                    raw.extend(chunk)
            raw = bytes(raw)
            self.last_response = raw
            return parse_response(raw, self.model, {k: q["criteria"] for k, q in questions.items()})
        except ProbeError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise ProbeError("decision_request_timeout") from None
        except httpx.RequestError:
            raise ProbeError("decision_transport_failed") from None
        except Exception:
            raise ProbeError("decision_request_failed") from None

    @staticmethod
    def questions(state, parameters, image=None):
        abilities = state['abilities']
        # A frozen preset removes equivalent upgrade choices, while Decision still
        # decides whether the present image permits learning or requires a stop.
        opening = [key for key in 'qew' if abilities[key] == 0]
        priority = ['r', *opening, 'e', 'q', 'w']
        upgrade = next((f'level_{key}' for key in priority if
            permitted({'goal': 'farm', 'skill': f'level_{key}'}, state, parameters) == f'level_{key}'), None)
        if upgrade is not None:
            questions = {
                'goal': {'type': 'choice', 'allow_none': True,
                    'instructions': '当前有未分配技能点。依据截图，先处理弹窗或安全地完成加点准备；不要仅因敌人不可见而判断未知。',
                    'criteria': {'farm': '画面可正常操作，可以先升级技能准备发育',
                                 'resume': '画面被暂离警告及OK按钮遮挡，先关闭', 'unknown': '画面无法判断或其他情况'}},
                'skill': {'type': 'choice', 'allow_none': True,
                    'instructions': '当前有未分配技能点，预设加点是一级Q、二级E、三级W，优先R再主E副Q。'
                                    '现在只选一个实际动作；没有暂离警告不得选择关闭。',
                    'criteria': {upgrade: f'正常游戏画面，升级本级预设技能{upgrade[-1].upper()}',
                                 'dismiss_idle': '当前明确显示暂离警告及OK，关闭这个提示',
                                 'wait': '暂时不能执行任何操作', 'stop': '异常，终止本轮'}}}
            return questions
        skills = {
            'join_top_lane': '当前画面可确认自己仍在己方泉水或基地内，需要出基地前往上路；已经走在上路道路上不选此项',
            'advance_lane': '当前画面是上路道路、自己已离开基地，主画面未见敌方单位且前方有可见安全道路：沿路前进，不猜测敌人位置',
            'farm_wave': '主画面可见敌方小兵实体及红色血条：向该兵线附近攻击移动，不要求小兵已在近战距离',
            'move': '危险中短距离躲避或后撤到主画面可见的安全位置',
            'return_to_base': '低血危险：当前生命不足且身处危险；向己方基地后撤',
            'recall': '低血安全：当前生命不足且处于安全位置；回城恢复',
            'dismiss_idle': '暂离弹窗：画面明确显示暂离警告和OK按钮；关闭提示',
            'wait': '暂不可操作：当前必须等待，其他动作均不适用',
            'stop': '异常：当前无法继续本轮，停止执行'}
        if abilities['q']:
            skills['melee'] = '主画面有近身敌方英雄且Q可用：Q增强普攻并向该英雄附近攻击移动'
        if abilities['w']:
            skills['w'] = '当前正在承受攻击且W可用：开启W减伤；这不是攻击'
        if abilities['e']:
            skills['e'] = '敌方小兵或英雄紧邻自己且E可用、尚未旋转：开启E近身伤害；旋转中不要再按E取消'
        questions = {
            'goal': {'type': 'choice', 'allow_none': True,
                'instructions': '选择当前唯一最紧迫目标。满血在出生区时前往上路发育；敌方小兵属于farm补兵，只有敌方英雄才属于trade。',
                'criteria': {'farm': '前往上路，接近并攻击敌方小兵补兵发育', 'trade': '附近有敌方英雄，需要交战',
                    'recover': '生命不足，需要回城治疗', 'retreat': '已经处于危险，需要后撤',
                    'resume': '暂离警告弹窗挡住画面，先关闭它', 'unknown': '看不清或不属于这些情况'}},
            'skill': {'type': 'choice', 'allow_none': True,
                'instructions': '根据当前截图的可见事实选择一个有界动作。主画面指游戏世界，排除右下角小地图和底部技能栏。'
                    '绿色血条的盖伦是自己，蓝色单位是友军；小地图红点不等于主画面有敌人。'
                    '区分空路、敌方小兵和近身敌方英雄；发育目标或历史动作不能作为当前看见敌人的证据。'
                    '在基地选join_top_lane；已在上路且画面没有敌人选advance_lane；看到敌方小兵选farm_wave。'
                    '不需要猜测是否已经到过外塔，不得仅根据时间或历史导航次数推定到达。'
                    '连续相同导航动作无进展时重新看画面选位置，不要再次点已到达的集结点。技能是否冷却以当前画面为准。'
                    '看不清就弃权，不要猜测。',
                'criteria': skills},
            'target': {'type': 'choice', 'allow_none': True,
                'instructions': '独立选择可见位置，不依赖skill答案。方位与坐标均对应第一张完整截图；辅助图如有p00至p14标注，可用对应编号十字定位。'
                    '这些编号只是候选格，没有推荐含义；局部图和小地图不是点击坐标。'
                    '危险或低血时选通向己方安全区的道路；否则优先选可见敌方单位脚下最近的格。'
                    '上路空路时沿己方蓝色建筑至敌方红色建筑的道路方向前进，可参考小地图辨认方向，落点必须是主画面可见的安全道路。'
                    '避开自己的当前位置、墙、建筑和敌塔危险区；不猜测视野外敌人。基地内、看不清或无安全格时选none。'
                    '最终动作不需要位置时，执行器忽略该位置。',
                'criteria': {'none': '基地内无需画面位置，或者当前没有可确定的安全目标格',
                    **{key: f"{('上排', '中排', '下排')[index // 5]}·{('最左', '偏左', '正中', '偏右', '最右')[index % 5]}"
                            f'（全图横向{round(x * 100)}%、纵向{round(y * 100)}%）'
                       for index, (key, (x, y)) in enumerate(GRID.items())}}}}
        points = candidates(image) if image is not None else {}
        if points and state['health_ratio'] > parameters['retreat_hp']:
            questions['target'] = {'type': 'choice', 'allow_none': True,
                'instructions': 'c编号只是视觉算法疑似红方小兵的位置，必须看原图确认。'
                    '选择离自己最近、可安全接近的可见敌方小兵；多个同距离时优先残血。'
                    '不得把火焰、装饰、友军或编号当成敌人。没有明确敌人、危险或看不清选none。',
                'criteria': {'none': '没有可确认且可安全接近的敌方小兵',
                    **{k: f'确认原图中c编号对应敌方小兵（全图{x:.1%},{y:.1%}），向它接近并攻击'
                       for k, (x, y) in points.items()}}}
        elif not points:
            # Four distinct local directions avoid fifteen near-equivalent cells.
            # This never chooses a direction or supplies a target after abstention.
            choices = questions['target']['criteria']
            questions['target']['criteria'] = {k: choices[k] for k in ('none', 'p02', 'p06', 'p08', 'p12')}
        return questions

    def decide(self, png, state, parameters, history):
        questions = self.questions(state, parameters, png)
        # Retain observable progress without resending full rosters and response
        # hashes twice. Recorded live requests became stale with that repetition.
        recent = [{"game_time": item["state"]["game_time"],
                   "health_ratio": item["state"]["health_ratio"],
                   "scores": own_player(item["state"])["scores"],
                   "goal": item["decision"].get("goal"),
                   "requested_skill": item["decision"].get("skill"),
                   "executed_skill": item["executed_skill"],
                   "target": item["decision"].get("target"),
                   "feedback": item.get("feedback")} for item in history[-2:]]
        repeated = 0
        if history:
            for item in reversed(history):
                if item['executed_skill'] != history[-1]['executed_skill']:
                    break
                repeated += 1
        result, digest = self.request({"objective": "固定五分钟对线，提高补刀且不增加死亡",
            "state": state, "policy": parameters, "history": recent,
            "repeated_skill_count": repeated,
            "feedback_guidance": 'feedback是动作前后遥测变化。补刀或击杀增加才有得分证据；没有增加不等于没移动或没造成伤害。'
                '血量变化仅是自己的血量，不是敌人受伤。重复动作次数提示重新判断当前画面，不能证明卡住。'}, questions, png)
        return bind_target(result, digest, png)

    def propose(self, context):
        options = neighbours(context["policy"])
        result, digest = self.request(context, {"candidate": {"type": "choice", "allow_none": True,
            "criteria": {key: encoded(value).decode() for key, value in options.items()},
            "instructions": "仅根据父版本开发记录选一次有依据的策略改进；没有证据则 keep。不得修改评测或伪造结果。"}}, timeout=15)
        key = result["candidate"] or "keep"
        return {"choice": key, "policy": options[key], "response_sha256": digest}
