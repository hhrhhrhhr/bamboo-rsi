package dev.bamboo.rsi;

import org.junit.Test;
import org.junit.Rule;
import org.junit.rules.TemporaryFolder;
import org.json.JSONObject;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.charset.StandardCharsets;
import java.security.*;
import java.security.spec.ECGenParameterSpec;
import java.util.Base64;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import static org.junit.Assert.*;

/** Synthetic unit fixtures only. Never exported into application storage or counted as live matches. */
public class RsiLedgerTest {
    @Rule public TemporaryFolder temp=new TemporaryFolder();
    private final String context=Protocol.sha("unit-fixture-context");
    private RsiLedger ledger(Path path) throws Exception {return new RsiLedger(path,data->{Mac mac=Mac.getInstance("HmacSHA256");mac.init(new SecretKeySpec(new byte[32],"HmacSHA256"));return mac.doFinal(data);});}
    private JSONObject local(String id,String result) throws Exception {return new JSONObject().put("outcome",result).put("reason","unit_fixture").put("calls",20).put("duration_ms",20000).put("trace_sha256",Protocol.sha("trace"+id)).put("video_sha256",Protocol.sha("video"+id)).put("video_complete",true).put("input_released",true).put("local_result_verified",true);}
    private void training(RsiLedger ledger,String prefix) throws Exception {for(int i=0;i<3;i++){String id=prefix+i;ledger.reserve(id,context,1000+i);ledger.finish(id,local(id,"LOSS"));}}
    private String propose(RsiLedger ledger,long now) throws Exception {JSONObject ticket=ledger.reserveProposal(now);var parent=ledger.version(ticket.getString("parent"));String child=parent.neighbours().get(0).identity();ledger.completeProposal(ticket.getString("id"),child,new JSONObject().put("origin","unit_fixture"));return child;}
    private KeyPair authority() throws Exception {var generator=KeyPairGenerator.getInstance("EC");generator.initialize(new ECGenParameterSpec("secp256r1"));return generator.generateKeyPair();}
    private JSONObject proof(JSONObject assignment,JSONObject local,KeyPair pair,String match) throws Exception {
        JSONObject body=new JSONObject(assignment.toString());for(String field:new String[]{"outcome","calls","duration_ms","trace_sha256","video_sha256"})body.put(field,local.get(field));
        body.put("origin","authorized_arena_live").put("assisted",false).put("issued_at_ms",200000).put("match_id",match).put("opponent","fixture-opponent");
        var signature=Signature.getInstance("SHA256withECDSA");signature.initSign(pair.getPrivate());signature.update(CanonicalJson.encode(body).getBytes(StandardCharsets.UTF_8));return new JSONObject().put("body",body).put("signature",Base64.getEncoder().encodeToString(signature.sign()));
    }
    @Test public void interruptedReservationConsumesSlotAndCannotReuseEvidence() throws Exception {
        Path path=temp.newFolder().toPath();var ledger=ledger(path);ledger.reserve("crashed",context,100);ledger=ledger(path);ledger.recoverInterrupted(200);
        assertEquals("UNKNOWN",ledger.snapshot().getJSONObject("runs").getJSONObject("crashed").getJSONObject("local").getString("outcome"));
        RsiLedger reopened=ledger;assertThrows(IllegalStateException.class,()->reopened.reserve("crashed",context,300));
    }
    @Test public void tamperedLedgerIsRejectedWithoutReset() throws Exception {
        Path path=temp.newFolder().toPath();ledger(path);Path file=path.resolve("ledger.json");JSONObject value=new JSONObject(new String(Files.readAllBytes(file),StandardCharsets.UTF_8));value.getJSONObject("state").put("generation",99);Files.write(file,value.toString().getBytes(StandardCharsets.UTF_8));
        assertThrows(IllegalStateException.class,()->ledger(path));assertTrue(new String(Files.readAllBytes(file),StandardCharsets.UTF_8).contains("99"));
    }
    @Test public void optimizerBudgetSurvivesCrashAndNoCandidate() throws Exception {
        var ledger=ledger(temp.newFolder().toPath());training(ledger,"dev");ledger.reserveProposal(10000);ledger.recoverInterrupted(11000);
        var ticket=ledger.reserveProposal(12000);ledger.completeProposal(ticket.getString("id"),null,new JSONObject());assertThrows(IllegalStateException.class,()->ledger.reserveProposal(13000));
        assertNotNull(ledger.reserveProposal(86_400_000L));
    }
    @Test public void changedContextAndWrongAuthorityCannotConsumeProof() throws Exception {
        var ledger=ledger(temp.newFolder().toPath());training(ledger,"dev");propose(ledger,10000);var key=authority();ledger.register(context,Base64.getEncoder().encodeToString(key.getPublic().getEncoded()),15000);
        assertThrows(IllegalStateException.class,()->ledger.reserve("wrong",Protocol.sha("changed"),16000));assertEquals(0,ledger.snapshot().getJSONObject("trial").getInt("reserved"));
        JSONObject a=ledger.reserve("real-slot",context,16000),local=local("real-slot","WIN");ledger.finish("real-slot",local);
        assertThrows(IllegalArgumentException.class,()->ledger.attest(proof(a,local,authority(),"match"),210000));assertFalse(ledger.snapshot().getJSONObject("runs").getJSONObject("real-slot").has("attestation"));
    }
    @Test public void unsignedSelfReportedWinsNeverPromote() throws Exception {
        var ledger=ledger(temp.newFolder().toPath());training(ledger,"dev");String child=propose(ledger,10000);var key=authority();ledger.register(context,Base64.getEncoder().encodeToString(key.getPublic().getEncoded()),15000);
        for(int i=0;i<200;i++){String id="unsigned"+i;ledger.reserve(id,context,16000+i);ledger.finish(id,local(id,"WIN"));}
        assertEquals(0,ledger.snapshot().getInt("generation"));assertNotEquals(child,ledger.snapshot().getString("accepted"));assertThrows(IllegalStateException.class,()->ledger.reserve("extra",context,19000));
    }
    @Test public void completeIndependentFixturePromotesAndNextCandidateInherits() throws Exception {
        var ledger=ledger(temp.newFolder().toPath());training(ledger,"dev");String parent=ledger.snapshot().getString("accepted"),child=propose(ledger,10000);var key=authority();ledger.register(context,Base64.getEncoder().encodeToString(key.getPublic().getEncoded()),15000);
        JSONObject last=null;
        for(int i=0;i<200;i++){String id="paired"+i;JSONObject a=ledger.reserve(id,context,16000+i);boolean candidate=child.equals(a.getString("policy"));String outcome=a.getInt("pair")<(candidate?70:40)?"WIN":"LOSS";
            JSONObject local=local(id,outcome);ledger.finish(id,local);last=ledger.attest(proof(a,local,key,"match"+i),210000);if(i<199){assertEquals(parent,ledger.snapshot().getString("accepted"));assertFalse(last.has("candidate_win_rate"));}}
        assertTrue(last.getBoolean("eligible"));assertEquals(child,ledger.snapshot().getString("accepted"));assertEquals(1,ledger.snapshot().getInt("generation"));
        assertThrows(IllegalStateException.class,()->ledger.reserveProposal(300000));training(ledger,"next");String grandchild=propose(ledger,300000);assertEquals(child,ledger.version(grandchild).parent());
        ledger.reject("fixture_cleanup",310000);ledger.rollback(320000);assertEquals(parent,ledger.snapshot().getString("accepted"));assertEquals(1,ledger.snapshot().getInt("generation"));
    }
}
