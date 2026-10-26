package dev.bamboo.rsi;

import android.os.SystemClock;
import org.json.JSONArray;
import org.json.JSONObject;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

/** Recorded, bounded single-match execution. Queuing and permission dialogs are never automated. */
public final class SessionController implements Runnable {
    private static final AtomicLong GENERATION=new AtomicLong();
    private static volatile SessionController current;
    public static volatile String status="尚未运行";
    private final CaptureService service;
    private final Evidence evidence;
    private final DecisionClient provider;
    private final Map<String,Protocol.Point> points;
    private volatile boolean stopped;
    private volatile boolean cancellationRequested;
    private DecisionClient optimizer;
    private String stopReason="budget_exhausted";
    public static long generation(){return GENERATION.get();}
    public static boolean active(){return current!=null;}
    public static void stopCurrent(String reason){var value=current;if(value!=null)value.stop(reason);}
    public synchronized void stop(String reason){cancellationRequested=true;provider.cancel();if(optimizer!=null)optimizer.cancel();if(stopped)return;stopped=true;stopReason=reason;GENERATION.incrementAndGet();status="已请求停止";}
    public SessionController(CaptureService service,Evidence evidence,DecisionClient provider,Map<String,Protocol.Point> points){
        if(current!=null)throw new IllegalStateException("A session is already active");
        this.service=service;this.evidence=evidence;this.provider=provider;this.points=java.util.Collections.unmodifiableMap(new java.util.LinkedHashMap<>(points));current=this;GENERATION.incrementAndGet();
    }
    @Override public void run(){
        Protocol.Outcome outcome=Protocol.Outcome.UNKNOWN;long start=SystemClock.elapsedRealtime();int calls=0,stale=0;boolean battleObserved=false,sealed=false;
        RsiLedger ledger=null;JSONObject assignment=null;String context="";ResultGate gate=new ResultGate();
        try(LocalResultReader reader=new LocalResultReader()){
            ledger=MobileRsi.open(service);context=MobileRsi.contextHash(service,provider,points);
            if(stopped||!service.recording)throw new IllegalStateException("Recording unavailable");
            assignment=ledger.reserve(evidence.directory.getName(),context,System.currentTimeMillis());
            StrategyVersion strategy=ledger.version(assignment.getString("policy"));Protocol.Policy policy=strategy.execution();String profile=provider.profileIdentity(policy,points);
            JSONObject config=provider.configuration().put("profile_sha256",profile).put("strategy",strategy.json()).put("assignment",assignment).put("max_calls",policy.maxCalls());
            JSONObject calibration=new JSONObject();for(var point:points.entrySet())calibration.put(point.getKey(),new JSONArray().put(point.getValue().x()).put(point.getValue().y()));
            evidence.configuration(config.put("calibration",calibration));
            evidence.event("session_started",new JSONObject().put("assignment",assignment).put("game_package",Protocol.GAME_PACKAGE).put("recorder_started",service.recording));
            while(calls<policy.maxCalls()&&!stopped&&SystemClock.elapsedRealtime()-start<1_800_000){
                if(!service.recording)throw new IllegalStateException("Recording stopped");
                GameAccessibility access=GameAccessibility.instance;if(access==null)throw new IllegalStateException("Accessibility unavailable");
                status="观察游戏画面";long generation=GENERATION.get();GameAccessibility.Frame frame=access.frame().get(5,TimeUnit.SECONDS);
                float aspect=service.getSharedPreferences("config",0).getFloat("calibration_aspect",0);
                if(aspect>0&&Math.abs(frame.width()/(float)frame.height()-aspect)>.02f)throw new IllegalStateException("Calibrated display shape changed");
                if(stopped||!service.recording)break;
                String frameHash=evidence.image(frame.png());var words=reader.read(frame.png());JSONArray text=new JSONArray();
                for(var word:words)text.put(new JSONObject().put("text",word.text()).put("x",word.centerX()).put("y",word.centerY()).put("height",word.height()));
                var banner=ResultGate.classify(words);var verified=gate.observe(words,frame.capturedAt(),SystemClock.elapsedRealtime()-start,battleObserved);
                evidence.event("local_result_observed",new JSONObject().put("frame_sha256",frameHash).put("words",text).put("banner",banner.name()).put("confirmed",verified.name()));
                if(stopped||!service.recording)break;
                if(verified!=Protocol.Outcome.UNKNOWN){outcome=verified;stopReason="local_result_confirmed";break;}
                if(banner!=Protocol.Outcome.UNKNOWN){if(!battleObserved){stopReason="result_without_observed_battle";break;}Thread.sleep(600);continue;}
                String request=java.util.UUID.randomUUID().toString();calls++;
                evidence.event("decision_requested",new JSONObject().put("request_id",request).put("frame_sha256",frameHash).put("call_slot",calls).put("policy",assignment.getString("policy")));
                status="Decision 正在选择技能";long requestStart=SystemClock.elapsedRealtime();DecisionClient.Reply reply=provider.decide(frame.png(),profile,context,strategy.directive());
                evidence.event("decision_received",new JSONObject().put("request_id",request).put("phase",reply.phase()).put("action",reply.action()).put("model_reported_result",reply.outcome()).put("confidence",reply.confidence()).put("latency_ms",SystemClock.elapsedRealtime()-requestStart));
                if(stopped)break;
                if(!service.recording||!Protocol.GAME_PACKAGE.equals(access.foreground())){stopReason="game_or_recording_unavailable";break;}
                if(!Protocol.applicable(access.foreground(),frame.capturedAt(),SystemClock.elapsedRealtime(),generation,GENERATION.get(),reply.action())){
                    stale++;evidence.event("decision_discarded",new JSONObject().put("request_id",request).put("reason","stale_or_scope_changed"));
                    if(stale>=5){stopReason="provider_too_slow";break;}
                    Thread.sleep(policy.intervalMillis());continue;
                }
                if(reply.phase().equals("result")){stopReason="model_result_without_local_proof";break;}
                if(!reply.phase().equals("battle")){stopReason="not_in_test_battle";break;}
                battleObserved=true;
                if(reply.action().equals("stop")){stopReason="model_stopped";break;}
                String command=java.util.UUID.randomUUID().toString();
                evidence.event("action_requested",new JSONObject().put("command_id",command).put("request_id",request).put("action",reply.action()));
                boolean done=access.execute(reply.action(),frame,policy,points,generation).get(2,TimeUnit.SECONDS);
                evidence.event("action_result",new JSONObject().put("command_id",command).put("completed",done));
                if(!done){stopReason="gesture_not_completed";break;}
                status="运行中 · 已用调用 "+calls+"/"+policy.maxCalls();Thread.sleep(policy.intervalMillis());
            }
        }catch(Exception error){
            if(!stopped)stopReason="execution_error";
            try{evidence.event("error",new JSONObject().put("error_type",error.getClass().getSimpleName()));}catch(Exception ignored){}
        }finally{
            boolean mayOptimize=!stopped;stopped=true;GENERATION.incrementAndGet();provider.cancel();
            long deadline=SystemClock.elapsedRealtime()+700;GameAccessibility access=GameAccessibility.instance;
            while(access!=null&&!access.inputReleased()&&SystemClock.elapsedRealtime()<deadline)try{Thread.sleep(20);}catch(InterruptedException interrupted){Thread.currentThread().interrupt();break;}
            boolean released=access!=null&&access.inputReleased();service.finishRecording();
            try{
                JSONObject local=evidence.end(stopReason,outcome,new JSONObject().put("assignment",assignment==null?JSONObject.NULL:assignment).put("run_mode",assignment==null?"development":assignment.getString("mode"))
                    .put("calls",calls).put("stale_responses",stale).put("duration_ms",SystemClock.elapsedRealtime()-start).put("input_released",released).put("local_result_verified",outcome!=Protocol.Outcome.UNKNOWN));
                if(ledger!=null&&assignment!=null){ledger.finish(assignment.getString("run_id"),local);sealed=true;}
            }catch(Exception error){status="证据封存失败；保留记录，需检查";}
            if(sealed&&mayOptimize&&released&&!cancellationRequested&&service.getSharedPreferences("config",0).getBoolean("auto_optimize",true)){
                DecisionClient next=null;
                try{next=MobileRsi.client(service);synchronized(this){optimizer=next;if(cancellationRequested)next.cancel();}status="对局已结束，检查优化候选";MobileRsi.optimize(service,next);}
                catch(Exception ignored){}
                finally{if(next!=null)next.cancel();synchronized(this){optimizer=null;}}
            }
            current=null;if(sealed)status="已结束："+stopReason+"；结果 "+outcome.name();service.stopSelf();
        }
    }
}
