"""Run isolated audible training, frozen evaluation, promotion, and parent inheritance."""
import argparse
import json
from pathlib import Path
import time
from bamboo_rsi.storage import read,write
from bamboo_rsi_sdk.workflow import Workflow
from .runtime import Budget,Language,Java,Decision
from .audio import Speech
from .conversation import Conversation
from .integration import create
from .optimizer import Proposer
from .data import cases
from .judge import measure


def train(home,batch,version,conversation,decision,status):
    dest=home/f'training-b{batch}';observations=[];reviews=[]
    for case in cases(batch,'training'):
        record=conversation.run(version,case,dest/case['id'])
        metrics=measure(case,record)
        turns=[{'customer_spoken':t['customer']['audio']['text'],'java_heard':t['customer']['recognition']['text'],
                'assistant_spoken':t['assistant']['audio']['text'],'customer_heard':t['assistant']['recognition']['text']}
               for t in record['turns'] if 'assistant' in t]
        observations.append({'case':case['id'],'intent':case['intent'],'metrics':metrics,'turns':turns})
        # A review timeout must never strand the already-recorded observations in memory.
        write(dest/'progress.json',{'version':version['id'],'batch':batch,'split':'training',
            'observations':observations,'reviews':reviews,'complete':False})
        for key in ('job','job_status','completed_slots'):status.pop(key,None)
        status.update(stage='training',batch=batch,training_completed=len(observations),parent=version['id'])
        write(home/'status.json',status)
        print(json.dumps({'stage':'training','batch':batch,'completed':len(observations),'loss':metrics['quality_loss']},ensure_ascii=False),flush=True)
        if len(observations)%12==0:
            recent=observations[-12:]
            totals={k:sum(o['metrics'][k] for o in recent) for k in recent[0]['metrics']}
            try:
                review,call=decision.review({'review_type':'training_issue_triage',
                    'candidate_policy':'No deployment requested; identify issues for a later bounded candidate.',
                    'training_totals':totals,'observations':sorted(recent,key=lambda o:o['metrics']['quality_loss'],reverse=True)[:2]})
            except Exception as error:
                write(dest/f'operator-review-pending-{len(observations):03d}.json',
                    {'status':'pending','observed_calls':len(observations),'error':type(error).__name__,
                     'operator_is_human':False,'approval_granted':False},immutable=True)
                raise
            reviews.append({'decision':review,'model_call':call,'operator_is_human':False})
            write(dest/'progress.json',{'version':version['id'],'batch':batch,'split':'training',
                'observations':observations,'reviews':reviews,'complete':len(observations)==24})
    totals={k:sum(o['metrics'][k] for o in observations) for k in observations[0]['metrics']}
    summary={'version':version['id'],'batch':batch,'split':'training','observations':observations,'totals':totals,
             'reviews':reviews,'customer_data':'synthetic_spoken_roleplay','evaluation_data_exposed':False}
    write(dest/'summary.json',summary,immutable=True)
    return summary


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--output',required=True)
    p.add_argument('--batches',type=int,default=2,choices=(1,2));args=p.parse_args()
    config=read(args.config);home=Path(args.output).resolve()
    home.mkdir(parents=True,exist_ok=False)
    budget=Budget(config['budget'],inherited=config.get('inherited_requests',0),limit=config.get('request_limit',700))
    language=Language(config['endpoint'],budget)
    decision=Decision(config['decision_endpoint'],budget,timeout=config.get('decision_timeout_seconds',60))
    java=Java(config['java'],config['classes'],config['dependencies'],config['endpoint'],budget,home/'java.stderr.log')
    speech=Speech(config['speech_script'],home/'audio',config['ffmpeg'],config['asr_model'])
    conversation=Conversation(speech,java,language)
    workflow=Workflow(home/'workflow',create(conversation,config,home));states=[]
    status={'started_at':time.time(),'stage':'initializing','scope':'synthetic_spoken_roleplay','real_phone_calls':0}
    try:
        active=workflow.initialize()
        for batch in range(1,args.batches+1):
            parent=workflow.store.version(active['version'])
            train(home,batch,parent,conversation,decision,status)
            state=workflow.prepare(Proposer(language,decision));states.append(state)
            while state['status']=='collecting' and state['next_slot']<48:
                state=workflow.run_next(state['job']);states[-1]=state
                status.update(stage='validation',batch=batch,job=state['job'],completed_slots=state['next_slot'],job_status=state['status'])
                write(home/'status.json',status)
                print(json.dumps({k:status[k] for k in ('stage','batch','completed_slots','job_status')}),flush=True)
            if state['status']=='collecting':state=workflow.finish(state['job']);states[-1]=state
            write(home/f'batch-{batch}-result.json',state,immutable=True)
            if state['status']=='accepted':
                active=workflow.store.active()
                # The next batch actually runs on this accepted version before proposing again.
            if state['status'] in {'failed','interrupted'}:break
        status.update(stage='finished',jobs=states,active=workflow.store.active(),ended_at=time.time())
        write(home/'report.json',status,immutable=True)
    except Exception as error:
        status.update(stage='failed',error=type(error).__name__,detail=str(error)[:350],jobs=states,ended_at=time.time())
        write(home/'failure.json',status,immutable=True)
        raise
    finally:
        write(home/'status.json',status)
        speech.close();java.close()


if __name__=='__main__':main()
