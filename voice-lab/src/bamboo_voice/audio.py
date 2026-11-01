from pathlib import Path
import hashlib
import json
import subprocess
import time
import wave
import re
from .runtime import JsonProcess

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

class Speech:
    def __init__(self, script, root, ffmpeg, model_path):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.ffmpeg=str(Path(ffmpeg).resolve())
        self.worker=JsonProcess(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
            '-File',str(Path(script).resolve()),'-OutputRoot',str(self.root)],
            self.root/'speech.stderr.log')
        from vosk import Model,SetLogLevel
        SetLogLevel(-1)
        self.model=Model(str(Path(model_path).resolve()))

    def speak(self,text,name,voice='Huihui',rate=0):
        request={'text':text,'name':name,'voice':voice,'rate':rate}
        result=self.worker.request(request,timeout=20)
        if result['status']!='ok':raise RuntimeError('Speech synthesis failed: '+result.get('detail',''))
        wav=Path(result['path'])
        with wave.open(str(wav),'rb') as f:
            result.update(duration=f.getnframes()/f.getframerate(),sample_rate=f.getframerate(),channels=f.getnchannels())
        result.update(text=text,sha256=digest(wav),role_voice=voice,rate=rate)
        return result

    def hear(self,wav_path,name,telephone=True):
        """Only audio enters recognition. No ground-truth text/hotword grammar is accepted."""
        from vosk import KaldiRecognizer
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,180}',name):raise ValueError('Invalid recording name')
        path=self.root/(name+'-received.wav')
        if path.exists():raise FileExistsError('Received audio is immutable')
        filters='highpass=f=180,lowpass=f=3400,aresample=8000,aresample=16000' if telephone else 'aresample=16000'
        subprocess.run([self.ffmpeg,'-hide_banner','-loglevel','error','-i',str(wav_path),'-af',filters,
            '-ac','1','-ar','16000','-c:a','pcm_s16le',str(path)],check=True,timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW)
        start=time.perf_counter();chunks=[]
        with wave.open(str(path),'rb') as f:
            if f.getnchannels()!=1 or f.getsampwidth()!=2 or f.getframerate()!=16000:raise ValueError('Invalid receiver audio')
            rec=KaldiRecognizer(self.model,16000);rec.SetWords(True)
            while data:=f.readframes(4000):
                if rec.AcceptWaveform(data):chunks.append(json.loads(rec.Result()))
            chunks.append(json.loads(rec.FinalResult()))
        heard=''.join(c.get('text','').replace(' ','') for c in chunks)
        return {'text':heard,'source_sha256':digest(wav_path),'received_path':str(path),'received_sha256':digest(path),
            'elapsed_seconds':time.perf_counter()-start,'segments':chunks,'telephone_bandlimit':telephone,
            'ground_truth_provided_to_asr':False,'engine':'vosk-model-small-cn-0.22'}

    def close(self):
        self.worker.close()

def mix_call(segments,output,ffmpeg, *, maximum_gap=None):
    """Concatenate actual utterance waves with measured response gaps; never synthesize narration."""
    output=Path(output)
    if output.exists():raise FileExistsError(output)
    pieces=[]
    for index,item in enumerate(segments):
        gap=max(0,float(item.get('gap_seconds',.25)))
        if maximum_gap is not None:gap=min(maximum_gap,gap)
        normalized=output.parent/f'{output.stem}-part-{index:03d}.wav'
        delay=round(gap*1000)
        subprocess.run([str(ffmpeg),'-hide_banner','-loglevel','error','-i',str(item['path']),'-af',f'adelay={delay}',
            '-ar','16000','-ac','1','-c:a','pcm_s16le',str(normalized)],check=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
        pieces.append(normalized)
    listing=output.with_suffix('.concat.txt')
    listing.write_text('\n'.join("file '"+str(p.resolve()).replace('\\','/')+"'" for p in pieces),encoding='utf-8')
    subprocess.run([str(ffmpeg),'-hide_banner','-loglevel','error','-f','concat','-safe','0','-i',str(listing),
        '-c:a','pcm_s16le',str(output)],check=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
    return {'path':str(output),'sha256':digest(output),'gap_cap_seconds':maximum_gap,
            'gap_capped':maximum_gap is not None and any(s.get('gap_seconds',0)>maximum_gap for s in segments)}
