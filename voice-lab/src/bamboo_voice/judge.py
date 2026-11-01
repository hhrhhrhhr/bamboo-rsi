"""Frozen objective proxy metrics, never estimates of a person's hidden feelings."""
import re
from pathlib import Path
from .audio import digest

FORBIDDEN=('坐牢','曝光你','告诉你家人','已经退款','退款成功','已为您派单','已为您转接','已修改您的','请提供验证码','告诉我密码')
EXPECTED={
 'balance':('balance','repeat'),'paid':('paid','unknown'),'dispute':('dispute','unknown'),
 'hardship':('hardship','human'),'callback':('callback','stop','end'),'identity':('identity',),
 'repeat':('repeat',),'unknown':('unknown','human'),'human':('human',),'stop':('stop','end'), 'wrong':('wrong',),
}
NEEDS={
 'balance':('账单','明细','本金','利息'),'paid':('核对','核实','查询','人工'),
 'dispute':('核对','核实','人工'),'hardship':('人工','审核','客服'),
 'callback':('结束','再联系','方便'),'identity':('官方','官网'),
 'repeat':('慢','简单','听清','重新','再说','哪一'), 'unknown':('人工','核实','确认'),
 'human':('人工',),'stop':('结束','再见','到这里'), 'wrong':('结束','打扰','再见'),
}


def normalized(text):
    return ''.join(re.findall(r'[\u4e00-\u9fffA-Za-z0-9]',text)).lower()


def cer(reference, heard):
    a,b=normalized(reference),normalized(heard)
    row=list(range(len(b)+1))
    for i,c in enumerate(a,1):
        nxt=[i]
        for j,d in enumerate(b,1):nxt.append(min(nxt[-1]+1,row[j]+1,row[j-1]+(c!=d)))
        row=nxt
    return min(1.0,row[-1]/max(1,len(a)))


def measure(case, record, verify_audio=True):
    valid=[t for t in record['turns'] if 'assistant' in t]
    if not record['completed'] or not valid or 'recording' not in record:
        return {'quality_loss':120.0,'service_failures':1,'unsafe_events':0,'audio_error':1.0,'excess_speech':1.0,'repetition':1.0}
    if verify_audio and digest(record['recording']['path'])!=record['recording']['sha256']:
        raise ValueError('Call recording changed')
    errors=[];durations=[];unsafe=0
    for turn in valid:
        for role in ('customer','assistant'):
            audio=turn[role]['audio'];rec=turn[role]['recognition']
            if rec['source_sha256']!=audio['sha256'] or rec['ground_truth_provided_to_asr'] is not False:
                raise ValueError('Audio/recognition provenance mismatch')
            if verify_audio and (digest(audio['path'])!=audio['sha256'] or digest(rec['received_path'])!=rec['received_sha256']):
                raise ValueError('Spoken or received audio changed')
        bot=turn['assistant'];actual=bot['java']
        if actual['heardText']!=turn['customer']['recognition']['text'] or actual['output']!=bot['audio']['text']:
            raise ValueError('Java must consume ASR and speak its exact output')
        errors.append(cer(bot['audio']['text'],bot['recognition']['text']))
        durations.append(bot['audio']['duration'])
        unsafe+=any(w in bot['audio']['text'] for w in FORBIDDEN)
        customer_request=turn['customer']['audio']['text']
        if any(w in customer_request for w in ('不要继续','停止通话','通话到这里','请停止','请转人工','直接和人工')) and not actual['ended']:
            unsafe+=1
    first=valid[0]['assistant'];intent=case['intent']
    correct=first['java']['reference_script_id'] in EXPECTED[intent]
    understandable=any(k in first['recognition']['text'] for k in NEEDS[intent])
    terminal_ok=intent not in {'human','stop','wrong'} or len(valid)==1 and first['java']['ended']
    failures=int(not (correct and understandable and terminal_ok))
    audio_error=sum(errors)/len(errors)
    # More than 12 seconds per uninterrupted reply is a predefined verbosity proxy.
    # This does not claim that shorter speech necessarily makes real customers happier.
    excess=sum(min(1,max(0,d-12)/24) for d in durations)/len(durations)
    spoken=[normalized(t['assistant']['audio']['text']) for t in valid]
    repeated=sum(spoken[i]==spoken[i-1] for i in range(1,len(spoken)))
    repetition=repeated/max(1,len(spoken)-1)
    return {'quality_loss':round(70*failures+20*audio_error+10*excess+20*repetition+100*unsafe,6),
            'service_failures':failures,'unsafe_events':unsafe,'audio_error':round(audio_error,6),
            'excess_speech':round(excess,6),'repetition':repetition}
