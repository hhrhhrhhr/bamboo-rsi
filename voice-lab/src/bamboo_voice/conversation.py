"""An audio turn must be recognized before the other participant sees it."""
from pathlib import Path
import time
from bamboo_rsi.storage import write
from .audio import mix_call
from .policy import parse, for_java


class Conversation:
    def __init__(self, speech, java, language):
        self.speech,self.java,self.language=speech,java,language

    def run(self, version, case, directory, checkpoint=lambda:None):
        directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
        policy=parse(version)
        # Unique audio names across versions, training and validation; no recording overwrite.
        call_id=case['id']+'-'+version['id'][:16]
        record={'case':case['id'],'version':version['id'],'origin':'synthetic_spoken_roleplay',
            'java_runtime':'original_aichatcore_playbook_chain','telephony':False,'microphone':False,
            'turns':[],'started_at':time.time(),'completed':False,'customer_model_calls':[],
            'operator_is_human':False,'subjective_customer_comfort_measured':False}
        segments=[];text=case['opening'];customer_history=[]
        try:
            record['java_start']=self.java.start(call_id,for_java(policy,version['id']))
            for n in range(case['max_turns']):
                checkpoint()
                prefix=call_id+f'-r{n}'
                user_audio=self.speech.speak(text,prefix+'-customer',case['voice'],case['rate'])
                heard_user=self.speech.hear(user_audio['path'],prefix+'-customer')
                turn={'round':n,'customer':{'audio':user_audio,'recognition':heard_user}}
                record['turns'].append(turn)
                write(directory/'partial.json',record)
                segments.append({'path':user_audio['path'],'gap_seconds':.25 if n==0 else customer_gap})
                if not heard_user['text']:raise ValueError('Customer audio yielded no recognizable speech')
                start=time.perf_counter()
                bot,model_call=self.java.turn(call_id,version['id'],heard_user['text'],case['seed']+n)
                bot_audio=self.speech.speak(bot['output'],prefix+'-assistant','Huihui',policy['rate'])
                response_gap=heard_user['elapsed_seconds']+(time.perf_counter()-start)
                heard_bot=self.speech.hear(bot_audio['path'],prefix+'-assistant')
                turn['assistant']={'java':bot,'audio':bot_audio,'recognition':heard_bot,
                                   'model_call':model_call,'response_gap_seconds':response_gap}
                segments.append({'path':bot_audio['path'],'gap_seconds':response_gap})
                write(directory/'partial.json',record)
                if bot['ended']:
                    record['end_reason']='java_terminal_handoff';break
                if n+1==case['max_turns']:
                    record['end_reason']='bounded_turn_limit';break
                customer_history.extend([{'role':'assistant','content':text},
                                         {'role':'user','content':'你从电话中听到客服说：'+heard_bot['text']}])
                prompt=('你扮演模拟客户。你的初始诉求是：'+case['opening']+'。'+case['persona']+
                        '请用一到两句口语回复，最多50字。只输出JSON {"say":"要说的话"}。/no_think')
                start=time.perf_counter()
                reply,call=self.language.ask('synthetic_customer',[{'role':'system','content':prompt}]+customer_history,
                                             case['seed']+100+n,tokens=180)
                record['customer_model_calls'].append(call)
                text=reply.get('say')
                if not isinstance(text,str) or not 1<=len(text)<=180:raise ValueError('Invalid simulated customer reply')
                customer_gap=heard_bot['elapsed_seconds']+time.perf_counter()-start
            record['completed']=True
        except Exception as error:
            record['failure']={'type':type(error).__name__,'detail':str(error)[:300]}
        finally:
            try:self.java.stop()
            except Exception:pass
            record['ended_at']=time.time()
            if segments:
                try:record['recording']=mix_call(segments,directory/'call.wav',self.speech.ffmpeg)
                except Exception as error:record['recording_failure']=type(error).__name__
            write(directory/'conversation.json',record,immutable=True)
        return record
