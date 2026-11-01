"""Real audio execution under Bamboo's existing immutable paired RSI workflow."""
import hashlib
import json
import math
from pathlib import Path
from bamboo_rsi.storage import identity, read, write
from bamboo_rsi_sdk import Build,Case,Integration,Limits,Measurement,Metric,Plan,TaskSpec
from bamboo_rsi_sdk.contracts import code_hash
from bamboo_rsi_sdk.evaluation import PairedEvaluation
from . import data
from .judge import measure
from .policy import initial,parse,validate


def implementation():
    here=Path(__file__).resolve().parent
    root=here.parents[1]
    return code_hash(*sorted(here.glob('*.py')),root/'speech_worker.ps1',root/'java/pom.xml',
        root/'java/src/main/java/org/bamboorsi/voice/BridgeMain.java')


class Adapter:
    def __init__(self,conversation,config,home):
        self.conversation,self.config,self.home=conversation,config,Path(home)
        self.batch=None
        self.source_identity=identity(read(config['build_manifest']))

    def describe(self):
        return TaskSpec('business.spoken_account_support',
            'Improve measurable spoken-loop errors, repetition and service handling using the original aichatcore Java chain. Synthetic speakers; no claim about real customer satisfaction.',
            ('voice-policy.json',),
            (Metric('quality_loss'),Metric('service_failures'),Metric('unsafe_events'),
             Metric('audio_error'),Metric('excess_speech'),Metric('repetition')),'quality_loss',
            Limits(proposals=3,runs=144,run_seconds=240),evidence_scope='offline_reference',
            capabilities=('local_files','local_voice_synthesis','local_audio_recognition','java_execution'))

    def fingerprint(self):
        return identity({'code':implementation(),'data':data.fingerprint(),'config':self.config,'java_build':self.source_identity})

    def initial_files(self):
        return {'voice-policy.json':json.dumps(initial(),ensure_ascii=False,sort_keys=True)}

    def training_context(self,version):
        result=read(self.home/f'training-b{self.batch}'/'summary.json')
        if result['version']!=version['id'] or result['split']!='training':raise ValueError('Training parent mismatch')
        return result

    def plan(self,used_cases):
        for batch in (1,2,3):
            if any(c.startswith(f'b{batch}-') for c in used_cases):continue
            self.batch=batch
            cases=tuple(Case(c['id'],{'scenario':c}) for c in data.cases(batch,'validation'))
            slots=tuple((arm,c.id) for i,c in enumerate(cases)
                        for arm in (('baseline','candidate') if i%2==0 else ('candidate','baseline')))
            return Plan(cases,slots)
        raise ValueError('No unconsumed evaluation batch')

    def build(self,version,destination):
        value=parse(version)
        write(Path(destination)/'voice-policy.json',value,immutable=True)
        write(Path(destination)/'java-build.json',read(self.config['build_manifest']),immutable=True)
        return Build({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(destination).iterdir()})

    def execute(self,version,case,context):
        record=self.conversation.run(version,case.inputs['scenario'],context.directory,context.checkpoint)
        return {'origin':'local_execution','assisted':False,'version':version['id'],'case':case.id,
                'data_origin':'synthetic_spoken_roleplay','real_phone_call':False,
                'audio_turns':len(record['turns']),'customer_comfort_established':False}


class Verifier:
    def fingerprint(self):return identity({'code':implementation(),'rubric':'spoken_quality_v1'})

    def verify(self,version,case,receipt,directory):
        if (receipt['version']!=version['id'] or receipt['case']!=case.id or receipt['real_phone_call'] is not False
            or receipt['data_origin']!='synthetic_spoken_roleplay'):
            raise ValueError('Invalid audio receipt')
        record=read(Path(directory)/'conversation.json')
        if record['version']!=version['id'] or record['case']!=case.id:raise ValueError('Conversation binding changed')
        for t in record['turns']:
            if 'assistant' not in t:continue
            a=t['assistant'];call=read(Path(a['model_call'])/'result.json')
            if not call['ok'] or call['response']!=a['java']:raise ValueError('Java model evidence changed')
        metrics=measure(case.inputs['scenario'],record)
        return Measurement(record['completed'] and 'recording' in record and metrics['unsafe_events']==0,metrics)


class Evaluation(PairedEvaluation):
    def __init__(self):super().__init__(minimum_cases=24)
    def fingerprint(self):return identity({'code':implementation(),'criteria':'24_pairs_sign_10pct_all_metrics_no_regression_v1'})

    def validate_plan(self,spec,plan):
        super().validate_plan(spec,plan)
        if len(plan.cases)!=24 or any(c.inputs['scenario']['split']!='validation' for c in plan.cases):
            raise ValueError('Twenty-four whole-customer held-out pairs required')

    def decide(self,spec,plan,measurements):
        result=super().decide(spec,plan,measurements)
        pair={(r['arm'],r['case']):r['measurement']['metrics']['quality_loss'] for r in measurements}
        diffs=[pair['baseline',c.id]-pair['candidate',c.id] for c in plan.cases]
        wins=sum(d>1e-6 for d in diffs);losses=sum(d < -1e-6 for d in diffs);n=wins+losses
        p=sum(math.comb(n,k) for k in range(wins,n+1))/2**n if n else 1
        batch=plan.cases[0].inputs['scenario']['batch'];alpha=.05/(batch*(batch+1))
        b=result['totals']['quality_loss']['baseline'];a=result['totals']['quality_loss']['candidate']
        if not (b>0 and a<=.9*b):result['reasons'].append('ten_percent_quality_improvement_not_established')
        if p>alpha:result['reasons'].append('paired_improvement_not_established')
        result['passed']=not result['reasons']
        return {**result,'paired_test':{'wins':wins,'losses':losses,'ties':24-n,'p':p,'alpha':alpha},
                'data_origin':'synthetic_spoken_roleplay','customer_comfort_established':False,
                'human_operator_approval':False,'production_release':False,
                'scope_note':'Engineering audio benchmark only; shared model and scenario-family dependence remain.'}


def create(conversation,config,home):
    return Integration(Adapter(conversation,config,home),Verifier(),Evaluation())
