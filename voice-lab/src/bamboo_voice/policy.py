"""Bounded editable speech policy; the evaluator and session machinery are not editable."""
import json

SYSTEM = ('你是离线模拟的信贷账单客服，不是真实催收员。尊重客户，不能威胁、施压或冒充法律机关。'
          '没有真实账户和工具权限，不能编造金额、已退款、已到账、已修改账户或已派单。'
          '不要索取密码、验证码或证件号码，不联系家属。这里只能解释、记录待核实问题、提供人工入口。'
          '客户明确要求停止通话就选择 stop；要求人工就选择 human；明确打错人选择 wrong。'
          '根据语音识别文本判断，听不清关键事实先澄清，不能猜测。')
CATALOGUE = [
    ('balance','询问本金利息或账单明细','您可以通过官方账单查看明细，有不清楚的项目可以请人工核对。','continue'),
    ('paid','声称已经还款但仍被提醒','到账情况需要核对，您可以通过官方客服查询付款记录。','continue'),
    ('dispute','对费用有疑问或不认可账单','我理解您的疑问，具体费用需要人工核对后解释。','continue'),
    ('hardship','失业或当前困难无法付款','我理解您的困难，可以请人工客服了解您的情况，是否调整需要审核。','continue'),
    ('callback','当前不方便或要求稍后联系','现在不方便的话，我们可以先结束，您方便时再联系官方客服。','continue'),
    ('identity','担心诈骗或不知道来电身份','先通过官方应用或官网核实客服身份，不要提供密码或验证码。','continue'),
    ('repeat','没听清要求复述或慢一点','好的，我换个简单的说法，请告诉我哪一部分没听清。','continue'),
    ('unknown','无法确定的政策或系统问题','这个问题我目前不能确认，需要人工核实后答复。','continue'),
    ('human','明确要求人工客服','可以，这个问题需要由人工客服协助，模拟通话在这里结束。','human'),
    ('stop','明确拒绝继续通话或继续联系','明白，尊重您的意愿，这次通话就到这里。','end'),
    ('wrong','打错人或不是本人','抱歉打扰，无法确认是本人，我们就先结束。','end'),
    ('end','客户已经理解并明确结束','好的，感谢您的沟通，再见。','end'),
]


def initial():
    return {'schema_version':1,'rate':0,
            'prompt':'请耐心回答客户的实际问题，说明处理途径，并询问是否还有其他问题。保持自然、清楚和准确。'}


def validate(value):
    if (type(value) is not dict or set(value)!={'schema_version','rate','prompt'} or value['schema_version']!=1
        or type(value['rate']) is not int or not -1<=value['rate']<=2
        or not isinstance(value['prompt'],str) or not 8<=len(value['prompt'])<=1600):
        raise ValueError('Invalid bounded voice policy')
    return value


def parse(version):
    return validate(json.loads(version['files']['voice-policy.json']))


def for_java(value, version):
    validate(value)
    return {'version':version,'system_prompt':SYSTEM,'prompt':value['prompt'],
            'utterances':[{'id':i,'description':d,'text':t,'handoff':h} for i,d,t,h in CATALOGUE]}
