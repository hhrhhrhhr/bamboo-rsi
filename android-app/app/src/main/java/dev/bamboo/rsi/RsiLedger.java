package dev.bamboo.rsi;

import org.json.JSONArray;
import org.json.JSONObject;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.nio.channels.FileChannel;
import java.security.MessageDigest;
import java.util.*;

/** Transactional phone-local lineage. Failed reservations are never returned to the budget. */
public final class RsiLedger {
    public interface Signer { byte[] sign(byte[] data) throws Exception; }
    private interface Operation<T> { T apply(JSONObject state) throws Exception; }
    private final Path directory; private final Signer signer;
    private static final Object PROCESS_LOCK=new Object();
    public RsiLedger(Path directory,Signer signer) throws Exception {this.directory=directory;this.signer=signer;Files.createDirectories(directory);transaction(s->null);}
    private <T> T transaction(Operation<T> operation) throws Exception {
        synchronized(PROCESS_LOCK){
            try(var channel=FileChannel.open(directory.resolve("ledger.lock"),StandardOpenOption.CREATE,StandardOpenOption.WRITE);var lock=channel.lock()){
                Path file=directory.resolve("ledger.json");JSONObject state;
                if(Files.exists(file)){
                    if(Files.size(file)>16_000_000)throw new IllegalStateException("Ledger too large");
                    JSONObject envelope=new JSONObject(new String(Files.readAllBytes(file),StandardCharsets.UTF_8));state=envelope.getJSONObject("state");
                    if(!MessageDigest.isEqual(Base64.getDecoder().decode(envelope.getString("mac")),signer.sign(CanonicalJson.encode(state).getBytes(StandardCharsets.UTF_8))))throw new IllegalStateException("Ledger integrity failure");
                }else{
                    var base=new StrategyVersion("",250,1000,"balanced");String id=base.identity();
                    state=new JSONObject().put("schema",1).put("accepted",id).put("generation",0).put("versions",new JSONObject().put(id,base.json()))
                        .put("runs",new JSONObject()).put("history",new JSONArray()).put("optimizer",new JSONArray());
                }
                if(state.getInt("schema")!=1)throw new IllegalStateException("Unknown ledger schema");
                T result=operation.apply(state);
                JSONObject envelope=new JSONObject().put("state",state).put("mac",Base64.getEncoder().encodeToString(signer.sign(CanonicalJson.encode(state).getBytes(StandardCharsets.UTF_8))));
                byte[] bytes=envelope.toString().getBytes(StandardCharsets.UTF_8);Path temporary=directory.resolve("ledger.next");
                try(var output=FileChannel.open(temporary,StandardOpenOption.CREATE,StandardOpenOption.TRUNCATE_EXISTING,StandardOpenOption.WRITE)){var buffer=java.nio.ByteBuffer.wrap(bytes);while(buffer.hasRemaining())output.write(buffer);output.force(true);}
                Files.move(temporary,file,StandardCopyOption.ATOMIC_MOVE,StandardCopyOption.REPLACE_EXISTING);
                return result;
            }
        }
    }
    public JSONObject snapshot() throws Exception {return transaction(s->new JSONObject(s.toString()));}
    public StrategyVersion version(String id) throws Exception {return transaction(s->{var value=StrategyVersion.from(s.getJSONObject("versions").getJSONObject(id));if(!value.identity().equals(id))throw new IllegalStateException("Version hash mismatch");return value;});}
    private static void idle(JSONObject s) throws Exception {if(s.has("active_run"))throw new IllegalStateException("A run is reserved");}
    public JSONObject reserve(String runId,String context,long now) throws Exception {
        if(!runId.matches("[A-Za-z0-9._-]{1,100}")||!context.matches("[a-f0-9]{64}"))throw new IllegalArgumentException("Invalid reservation");
        return transaction(s->{idle(s);JSONObject runs=s.getJSONObject("runs");if(runs.has(runId))throw new IllegalStateException("Duplicate run");
            JSONObject a=new JSONObject().put("run_id",runId).put("context_sha256",context).put("reserved_at_ms",now).put("policy",s.getString("accepted")).put("mode","development");
            if(s.has("trial")){
                JSONObject trial=s.getJSONObject("trial");if(!context.equals(trial.getString("context_sha256")))throw new IllegalStateException("Frozen trial context changed");
                int slot=trial.getInt("reserved");if(slot>=PairedEvaluation.PAIRS*2)throw new IllegalStateException("All trial slots consumed; import independent attestations");
                JSONObject plan=trial.getJSONArray("plan").getJSONObject(slot);
                a.put("trial",trial.getString("id")).put("pair",plan.getInt("pair")).put("swapped",plan.getBoolean("swapped")).put("policy",plan.getString("policy")).put("mode","prospective");
                trial.put("reserved",slot+1);
            }
            runs.put(runId,new JSONObject().put("assignment",a));s.put("active_run",runId);return new JSONObject(a.toString());});
    }
    public void finish(String runId,JSONObject report) throws Exception {transaction(s->{
        if(!runId.equals(s.optString("active_run")))throw new IllegalStateException("Run reservation mismatch");
        s.getJSONObject("runs").getJSONObject(runId).put("local",new JSONObject(report.toString()));s.remove("active_run");return null;
    });}
    public void recoverInterrupted(long now) throws Exception {transaction(s->{
        if(s.has("active_run")){String id=s.getString("active_run");s.getJSONObject("runs").getJSONObject(id).put("local",new JSONObject().put("outcome","UNKNOWN").put("reason","process_interrupted").put("recovered_at_ms",now).put("video_complete",false).put("input_released",false).put("local_result_verified",false));s.remove("active_run");}
        JSONArray calls=s.getJSONArray("optimizer");for(int i=0;i<calls.length();i++){JSONObject call=calls.getJSONObject(i);if("pending".equals(call.getString("status")))call.put("status","interrupted");}return null;
    });}
    /** Optimizer sees development evidence only, never the held-out cohort. */
    public JSONObject reserveProposal(long now) throws Exception {return transaction(s->{idle(s);
        if(s.has("candidate")||s.has("trial"))throw new IllegalStateException("Candidate already pending");
        String parent=s.getString("accepted");JSONArray training=new JSONArray();JSONObject runs=s.getJSONObject("runs");
        List<String> ids=new ArrayList<>();runs.keys().forEachRemaining(ids::add);Collections.sort(ids);
        for(String id:ids){JSONObject run=runs.getJSONObject(id),a=run.getJSONObject("assignment");if("development".equals(a.getString("mode"))&&parent.equals(a.getString("policy"))&&run.has("local")){
            JSONObject local=run.getJSONObject("local");if(local.optBoolean("video_complete")&&local.optBoolean("input_released"))training.put(new JSONObject().put("run_id",id).put("outcome",local.getString("outcome")).put("reason",local.optString("reason")).put("calls",local.optLong("calls")).put("stale_responses",local.optLong("stale_responses")));}}
        if(training.length()<3)throw new IllegalStateException("At least three recorded development runs from this parent required");
        JSONArray calls=s.getJSONArray("optimizer");int today=0;for(int i=0;i<calls.length();i++){JSONObject c=calls.getJSONObject(i);if(c.getLong("at_ms")/86_400_000L==now/86_400_000L)today++;if("pending".equals(c.getString("status")))throw new IllegalStateException("Optimizer already running");}
        if(today>=2)throw new IllegalStateException("Daily optimizer budget exhausted");
        // Bound request size without selecting by outcome.
        JSONArray recent=new JSONArray();for(int i=Math.max(0,training.length()-20);i<training.length();i++)recent.put(training.get(i));
        JSONObject ticket=new JSONObject().put("id",UUID.randomUUID().toString()).put("parent",parent).put("training",recent).put("at_ms",now).put("status","pending");calls.put(ticket);return new JSONObject(ticket.toString());});}
    public void completeProposal(String ticketId,String chosen,JSONObject receipt) throws Exception {transaction(s->{
        JSONArray calls=s.getJSONArray("optimizer");JSONObject ticket=null;for(int i=0;i<calls.length();i++)if(ticketId.equals(calls.getJSONObject(i).getString("id")))ticket=calls.getJSONObject(i);
        if(ticket==null||!"pending".equals(ticket.getString("status")))throw new IllegalStateException("Optimizer ticket is not pending");
        if(!ticket.getString("parent").equals(s.getString("accepted"))||s.has("candidate")||s.has("trial"))throw new IllegalStateException("Parent changed");
        ticket.put("receipt",receipt).put("status",chosen==null?"no_candidate":"proposed");
        if(chosen!=null){StrategyVersion child=null;for(var candidate:StrategyVersion.from(s.getJSONObject("versions").getJSONObject(s.getString("accepted"))).neighbours())if(candidate.identity().equals(chosen))child=candidate;
            if(child==null)throw new IllegalArgumentException("Candidate outside bounded search space");s.getJSONObject("versions").put(chosen,child.json());s.put("candidate",chosen);}
        return null;
    });}
    public String register(String context,String authority,long now) throws Exception {
        if(!context.matches("[a-f0-9]{64}"))throw new IllegalArgumentException("Invalid context");ArenaAttestation.validateKey(authority);
        return transaction(s->{idle(s);if(s.has("trial")||!s.has("candidate"))throw new IllegalStateException("No candidate ready");
            String parent=s.getString("accepted"),candidate=s.getString("candidate"),id=UUID.randomUUID().toString();
            if(!parent.equals(StrategyVersion.from(s.getJSONObject("versions").getJSONObject(candidate)).parent()))throw new IllegalStateException("Candidate parent mismatch");
            JSONArray plan=new JSONArray();for(int pair=0;pair<PairedEvaluation.PAIRS;pair++){boolean reverse=(Protocol.sha(id+":"+pair).charAt(0)%2)==1;for(int j=0;j<2;j++)plan.put(new JSONObject().put("pair",pair).put("swapped",pair%2==1).put("policy",(j==0)==reverse?candidate:parent));}
            int ordinal=s.optInt("trial_count",0)+1;if(ordinal>10000)throw new IllegalStateException("Trial limit reached");s.put("trial_count",ordinal);
            double alpha=PairedEvaluation.ALPHA/((double)ordinal*(ordinal+1));
            s.put("trial",new JSONObject().put("id",id).put("parent",parent).put("candidate",candidate).put("context_sha256",context).put("authority",authority).put("registered_at_ms",now).put("plan",plan).put("reserved",0).put("ordinal",ordinal).put("alpha",alpha).put("evaluator",PairedEvaluation.VERSION));return id;
        });
    }
    public JSONObject attest(JSONObject proof,long now) throws Exception {return transaction(s->{idle(s);
        JSONObject t=s.getJSONObject("trial"),body=proof.getJSONObject("body");JSONObject run=s.getJSONObject("runs").getJSONObject(body.getString("run_id"));
        if(!t.getString("id").equals(run.getJSONObject("assignment").optString("trial")))throw new IllegalArgumentException("Wrong trial");
        if(run.has("attestation"))throw new IllegalArgumentException("Proof already imported");
        ArenaAttestation.verify(proof,t.getString("authority"),run.getJSONObject("assignment"),run.getJSONObject("local"),now);
        JSONObject runs=s.getJSONObject("runs");var keys=runs.keys();while(keys.hasNext()){JSONObject other=runs.getJSONObject(keys.next());if(other.has("attestation")&&body.getString("match_id").equals(other.getJSONObject("attestation").getJSONObject("body").getString("match_id")))throw new IllegalArgumentException("Reused match evidence");}
        run.put("attestation",proof);List<PairedEvaluation.Sample> samples=new ArrayList<>();keys=runs.keys();
        while(keys.hasNext()){JSONObject r=runs.getJSONObject(keys.next());if(t.getString("id").equals(r.getJSONObject("assignment").optString("trial"))&&r.has("attestation"))samples.add(ArenaAttestation.verify(r.getJSONObject("attestation"),t.getString("authority"),r.getJSONObject("assignment"),r.getJSONObject("local"),now));}
        if(samples.size()!=PairedEvaluation.PAIRS*2)return new JSONObject().put("status","collecting").put("attested",samples.size()).put("required",PairedEvaluation.PAIRS*2);
        var evaluation=PairedEvaluation.evaluate(t.getString("parent"),t.getString("candidate"),samples,t.getDouble("alpha"));JSONObject result=evaluation.json().put("alpha",t.getDouble("alpha"));
        if(evaluation.eligible()){
            if(!s.getString("accepted").equals(t.getString("parent")))throw new IllegalStateException("Promotion parent changed");
            s.put("accepted",t.getString("candidate")).put("generation",s.getInt("generation")+1);
        }
        t.put("result",result).put("closed_at_ms",now).put("status",evaluation.eligible()?"promoted":"rejected");s.getJSONArray("history").put(t);s.remove("trial");s.remove("candidate");return result;
    });}
    public void reject(String reason,long now) throws Exception {transaction(s->{idle(s);if(reason.trim().isEmpty())throw new IllegalArgumentException("Reason required");
        if(!s.has("candidate"))throw new IllegalStateException("No pending candidate");JSONObject item=s.has("trial")?s.getJSONObject("trial"):new JSONObject().put("candidate",s.getString("candidate")).put("parent",s.getString("accepted"));
        s.getJSONArray("history").put(item.put("status","rejected").put("reason",reason).put("closed_at_ms",now));s.remove("trial");s.remove("candidate");return null;});}
    public void rollback(long now) throws Exception {transaction(s->{idle(s);if(s.has("trial")||s.has("candidate"))throw new IllegalStateException("Close the pending trial first");String current=s.getString("accepted"),parent=StrategyVersion.from(s.getJSONObject("versions").getJSONObject(current)).parent();if(parent.isEmpty())throw new IllegalStateException("Already at root");
        s.put("accepted",parent);s.getJSONArray("history").put(new JSONObject().put("status","rolled_back").put("from",current).put("to",parent).put("at_ms",now));return null;});}
}
