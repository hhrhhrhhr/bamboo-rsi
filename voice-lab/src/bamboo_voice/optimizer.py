import json
from bamboo_rsi.storage import write
from .policy import validate


class Proposer:
    def __init__(self,language,decision):self.language,self.decision=language,decision

    def propose(self,context,directory):
        parent=json.loads(context['files']['voice-policy.json']['content'])
        rows=context['training']['observations']
        examples=sorted(rows,key=lambda r:r['metrics']['quality_loss'],reverse=True)[:8]
        request={'accepted_parent':context['accepted_parent'],'generation':context['accepted_generation'],
                 'parent_policy':parent,'training_totals':context['training']['totals'],'training_examples':examples}
        system=('你是语音客服的策略优化器。只看训练通话证据，优化下一轮中文语音回复的可理解性和问题处理。'
            '你只能修改动态提示词和语速。不要改评测、角色边界、日志或客户需求。'
            '不要声称知道任何真实账户事实或已执行业务操作；不要施压催款。'
            '目标是回应客户当前问题、避免机械复读、听不清先澄清，必要时明确人工核实。'
            '固定话术列表仅是意图和示例，润色应针对当前上下文。'
            'rate为整数-1到2，0是正常语速。返回JSON {"prompt":"完整的新版动态提示词",'
            '"rate":0,"hypothesis":"为什么训练证据支持该改动"}。无需改善时保留原策略。/no_think')
        proposal,call=self.language.ask('voice_optimizer',[{'role':'system','content':system},
            {'role':'user','content':json.dumps(request,ensure_ascii=False)}],seed=800+context['accepted_generation'],tokens=700)
        value=validate({'schema_version':1,'rate':proposal.get('rate'),'prompt':proposal.get('prompt')})
        review,review_call=self.decision.review({'parent_policy':parent,'candidate_policy':value,
            'hypothesis':proposal.get('hypothesis'),'training_totals':context['training']['totals'],
            'training_examples':examples[:3]})
        write(directory/'evidence.json',{'request':request,'proposal':proposal,'model_call':call,
            'operator_review':review,'operator_model_call':review_call,'operator_is_human':False},immutable=True)
        approved=review['choice']=='approve'
        content=json.dumps(value,ensure_ascii=False,sort_keys=True)
        return {'summary':'AI-generated speech policy; simulated Jev operator review: '+review['choice'],
            'hypothesis':str(proposal.get('hypothesis','')),
            'changes':[] if not approved or value==parent else [{'path':'voice-policy.json',
                'before_sha256':context['files']['voice-policy.json']['sha256'],'content':content}]}
