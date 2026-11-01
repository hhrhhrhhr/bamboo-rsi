"""Invented adults and issues. No phone numbers, accounts, or real customer records."""
from bamboo_rsi.storage import identity

SCENARIOS = [
 ('balance', ['本金和利息分别怎么算的？能告诉我查明细的地方吗？','账单看着有点复杂，我想先核对里面每一项。'],
             ['我只想看账单里本金和利息的明细，不要先让我付款。','麻烦说说在哪查看每笔费用，我现在还没核实。']),
 ('paid', ['我昨天已经付过了，为什么今天还提醒？','钱已经转了，但账户上还没有显示。'],
          ['我刚刚还过款，系统还是显示未付，需要怎么查询？','我昨天付款了，能先查下有没有到账吗？']),
 ('dispute', ['这笔费用我不认可，请帮我核实。','账单里有一项我从来没见过。'],
             ['利息这一项有问题，我需要先确认，不能直接付款。','我觉得费用重复计算了，该找谁核对？']),
 ('hardship', ['我最近失业了，暂时没有收入。','这个月家里有困难，没办法按时付款。'],
              ['我现在手头确实紧张，需要和人工谈谈可行的办法。','我生病请假了，收入变少，可以先了解处理办法吗？']),
 ('callback', ['我在开会，现在不方便继续。','我正在接孩子，晚点再说可以吗？'],
              ['我现在正在工作，能不能等我有空再联系？','我在地铁里，听不清，先结束这通电话吧。']),
 ('identity', ['怎么确认你是官方客服，不是骗子？','我不认识这个来电，怎么核实身份？'],
              ['我怕遇到诈骗，你能告诉我自己怎么核实吗？','你是哪个平台的？我想通过官方渠道核对。']),
 ('repeat', ['你说得有点快，可以慢一点吗？','刚才那一句我没听清，重新说一遍好吗？'],
            ['我听力不太好，一次只说一件事好吗？','刚才有杂音，你能简单点再说一遍吗？']),
 ('unknown', ['特殊假期的处理规则是什么？','系统显示的特殊标记是什么意思？'],
             ['我这里有一个你没有说明的政策代码，具体是什么意思？','我想知道特殊补贴能不能抵扣，这个需要谁确认？']),
 ('human', ['我想找人工客服，别继续自动回答了。','请转人工，我要详细说明情况。'],
           ['麻烦让人工客服来处理，不用机器人继续解释。','我希望直接和人工沟通，这里先停吧。']),
 ('stop', ['不要再跟我说了，这次通话到此结束。','我不想继续接听了，请停止。'],
          ['请不要继续这次通话，我要挂了。','这通电话到这里就结束，请不要再追问。']),
 ('wrong', ['你打错了，我不是你要找的人。','这不是我的账单，请不要跟我说别人的事情。'],
           ['不好意思，你们找错人了，这个账户不是我的。','这个号码的主人已经换了，别向我说他的账单。']),
 ('balance', ['你先别催，我想知道账单怎么查。','可以把查询明细的方法简单说一下吗？'],
             ['我只需要一个简单的账单查询方法，能说清楚吗？','你先说本金和利息去哪里核对，我自己查看。']),
]


def cases(batch, split):
    if split not in {'training','validation'} or batch not in {1,2,3}:
        raise ValueError('Unknown sealed batch')
    result=[]
    for index,(intent,training,validation) in enumerate(SCENARIOS):
        for variant,text in enumerate(training if split=='training' else validation):
            case_id=f'b{batch}-{split}-{index:02d}-{variant}'
            # Fresh wording/voice/seed after an arrival; same preregistered task families.
            prefix=['','您好，','我再说明一下，'][batch-1]
            result.append({'id':case_id,'intent':intent,'opening':prefix+text,'split':split,'batch':batch,
                'voice':'Kangkang' if variant==0 else 'Yaoyao','rate':(-1,0,1)[(index+variant+batch)%3],
                'seed':batch*10000+index*100+variant,'max_turns':3,
                'persona':'成年人，认真表达自己的需求。只根据实际听到的客服内容回应，不假装看过完整文本。'
                          '如果听不清就要求澄清；问题仍未解决要追问，解决或希望结束就说明。'
                          '不提供个人敏感信息，不虚构付款完成。不因为客服态度客气就假装问题已解决。'})
    return result


def fingerprint():
    return identity([cases(b,s) for b in (1,2,3) for s in ('training','validation')])
