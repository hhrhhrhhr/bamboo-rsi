"""Create a private runtime config from explicitly supplied local dependencies.

Build java/pom.xml first. This tool hashes source and binaries, does not copy upstream
source or credentials, and never starts the enterprise application's main class.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def main():
    p=argparse.ArgumentParser()
    for field in ('aichatcore','java','ffmpeg','vosk-model','decision-endpoint','output'):
        p.add_argument('--'+field,required=True)
    p.add_argument('--endpoint',default='http://127.0.0.1:8794/v1/chat/completions')
    p.add_argument('--inherited-requests',type=int,default=0)
    p.add_argument('--request-limit',type=int,default=700)
    p.add_argument('--decision-timeout-seconds',type=int,default=60,choices=range(1,181),metavar='1..180')
    args=p.parse_args()
    if not 0<=args.inherited_requests<args.request_limit:raise ValueError('Invalid cumulative budget')
    lab=Path(__file__).resolve().parents[1]
    source=Path(args.aichatcore).resolve();out=Path(args.output).resolve()
    if out.exists():raise FileExistsError('Use a new private configuration directory')
    target=lab/'java/target'
    if not (target/'classes/org/bamboorsi/voice/BridgeMain.class').exists():
        raise FileNotFoundError('Build the Java harness with the supplied aichatcore checkout first')
    for file in (args.java,args.ffmpeg):
        if not Path(file).is_file():raise FileNotFoundError(file)
    if not Path(args.vosk_model).is_dir():raise FileNotFoundError('Vosk model directory missing')
    hashes=lambda base,pattern:{p.relative_to(base).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in sorted(base.rglob(pattern)) if p.is_file()}
    original_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=source,text=True).strip()
    manifest={'original_repository':str(source),'original_commit':original_commit,
              'sources':hashes(source,'*.java'),'compiled':hashes(target/'classes','*.class'),
              'dependencies':hashes(target/'dependency','*.jar')}
    # Retain the same compiled path convention as experiment artifacts.
    manifest['compiled']={'classes/'+k:v for k,v in manifest['compiled'].items()}
    out.mkdir(parents=True)
    (out/'java-build-manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    config={'java':str(Path(args.java).resolve()),'classes':str(target/'classes'),
            'dependencies':str(target/'dependency'),'endpoint':args.endpoint,'decision_endpoint':args.decision_endpoint,
            'speech_script':str(lab/'speech_worker.ps1'),'ffmpeg':str(Path(args.ffmpeg).resolve()),
            'asr_model':str(Path(args.vosk_model).resolve()),'budget':str(out/'model-budget'),
            'inherited_requests':args.inherited_requests,'request_limit':args.request_limit,
            'decision_timeout_seconds':args.decision_timeout_seconds,
            'build_manifest':str(out/'java-build-manifest.json')}
    (out/'config.json').write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'configuration':str(out/'config.json'),'source_files_hashed':len(manifest['sources']),
                      'credentials_copied':False}))


if __name__=='__main__':main()
