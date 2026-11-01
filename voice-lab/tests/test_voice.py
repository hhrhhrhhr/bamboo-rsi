import inspect
import json
from pathlib import Path
import pytest
from bamboo_voice.audio import Speech
from bamboo_voice.conversation import Conversation
from bamboo_voice.data import cases
from bamboo_voice.integration import Evaluation
from bamboo_voice.judge import cer,measure
from bamboo_voice.policy import initial,validate
from bamboo_voice.runtime import Budget
from bamboo_rsi.storage import read
from bamboo_rsi_sdk import Case,Plan,TaskSpec,Metric


def test_budget_failure_is_consumed_and_three_failures_stop(tmp_path):
    b=Budget(tmp_path,inherited=1701,limit=2400)
    for i in range(3):
        d=b.reserve('test',{'i':i});b.complete(d,{'ok':False})
    with pytest.raises(RuntimeError,match='Three consecutive'):b.reserve('test',{})
    assert read(tmp_path/'budget.json')['used']==3


def test_inherited_budget_cannot_be_refunded(tmp_path):
    b=Budget(tmp_path,inherited=9,limit=10)
    d=b.reserve('test',{});b.complete(d,{'ok':True})
    with pytest.raises(RuntimeError,match='exhausted'):b.reserve('test',{})
    with pytest.raises(ValueError,match='lineage'):Budget(tmp_path,inherited=0,limit=10).reserve('test',{})


def test_new_installation_does_not_inherit_another_users_budget(tmp_path):
    b=Budget(tmp_path);d=b.reserve('test',{});b.complete(d,{'ok':True})
    assert read(tmp_path/'budget.json')['inherited']==0


@pytest.mark.parametrize('inherited,limit',[(True,10),(-1,10),(10,10),(0,100001)])
def test_bad_budget_bounds_rejected(tmp_path,inherited,limit):
    with pytest.raises(ValueError):Budget(tmp_path,inherited=inherited,limit=limit)


@pytest.mark.parametrize('bad',[-2,3,True,0.5])
def test_policy_rate_is_bounded(bad):
    p=initial();p['rate']=bad
    with pytest.raises(ValueError):validate(p)


def test_no_asr_reference_text_input():
    assert set(inspect.signature(Speech.hear).parameters)=={'self','wav_path','name','telephone'}


def test_train_validation_are_distinct_customers():
    training=cases(1,'training');evaluation=cases(1,'validation')
    assert not {c['id'] for c in training}&{c['id'] for c in evaluation}
    assert not {c['opening'] for c in training}&{c['opening'] for c in evaluation}


def test_cer_uses_actual_recognition():
    assert cer('有异议的部分','有意义的部分')>0
    assert cer('你好！','你好')==0
    assert cer('你好','')==1


def spec():
    return TaskSpec('test.voice','Test fixed audio gate',('voice-policy.json',),
                    (Metric('quality_loss'),Metric('unsafe_events')),'quality_loss')


def plan():
    return Plan(tuple(Case(c['id'],{'scenario':c}) for c in cases(1,'validation')),
                tuple((arm,c['id']) for c in cases(1,'validation') for arm in ('baseline','candidate')))


def rows(before,after,unsafe=0):
    return [{'arm':a,'case':c,'measurement':{'success':True,'metrics':{'quality_loss':before if a=='baseline' else after,
        'unsafe_events':unsafe if a=='candidate' else 0}}} for a,c in plan().slots]


def test_promotion_requires_actual_improvement():
    e=Evaluation()
    assert not e.decide(spec(),plan(),rows(10,10))['passed']
    assert not e.decide(spec(),plan(),rows(10,9.5))['passed']
    assert not e.decide(spec(),plan(),rows(10,8,1))['passed']
    assert e.decide(spec(),plan(),rows(10,8))['passed']


def test_audio_loop_cannot_bypass_asr(tmp_path,monkeypatch):
    class FakeSpeech:
        ffmpeg='test-only'
        def speak(self,text,name,voice,rate):return {'path':name,'text':text,'duration':1}
        def hear(self,path,name):
            return {'text':'听错的客户诉求' if name.endswith('customer') else '客户听到的客服语音','elapsed_seconds':.01}
    class FakeJava:
        def __init__(self):self.heard=[]
        def start(self,*args):return {}
        def turn(self,call,version,heard,seed):
            self.heard.append(heard)
            return {'output':'客服完整原文','ended':len(self.heard)==2},'test-call'
        def stop(self):pass
    class FakeLanguage:
        def ask(self,role,messages,seed,tokens):
            assert '客户听到的客服语音' in messages[-1]['content']
            assert '客服完整原文' not in json.dumps(messages,ensure_ascii=False)
            return {'say':'再问一次'},'test-customer-call'
    monkeypatch.setattr('bamboo_voice.conversation.mix_call',lambda *a,**k:{'path':'test-only'})
    java=FakeJava();c=cases(1,'training')[0]
    version={'id':'a'*64,'files':{'voice-policy.json':json.dumps(initial())}}
    result=Conversation(FakeSpeech(),java,FakeLanguage()).run(version,c,tmp_path)
    assert result['completed'] and java.heard==['听错的客户诉求','听错的客户诉求']
    assert result['origin']=='synthetic_spoken_roleplay' and result['telephony'] is False


def test_incomplete_audio_is_failure():
    m=measure(cases(1,'training')[0],{'completed':False,'turns':[]})
    assert m['service_failures']==1 and m['quality_loss']==120


def test_review_failure_preserves_training_progress(tmp_path,monkeypatch):
    from bamboo_voice.campaign import train
    class FakeConversation:
        def run(self,*args):return {'completed':False,'turns':[]}
    class FailedDecision:
        def review(self,*args):raise TimeoutError('synthetic test timeout')
    with pytest.raises(TimeoutError):train(tmp_path,1,{'id':'a'*64},FakeConversation(),FailedDecision(),{})
    progress=read(tmp_path/'training-b1/progress.json')
    pending=read(tmp_path/'training-b1/operator-review-pending-012.json')
    assert len(progress['observations'])==12 and pending['approval_granted'] is False
    assert pending['status']=='pending' and progress['complete'] is False
