package dev.bamboo.rsi;

import org.json.JSONObject;
import org.junit.Test;
import java.util.Set;
import static org.junit.Assert.*;

public class DecisionClientTest {
    @Test public void cancelledOptimizerCannotStartAProposal() {
        DecisionClient client=new DecisionClient("https://api.example.test/v1/decisions","test-model","test-only");client.cancel();
        assertThrows(IllegalStateException.class,()->client.propose(new StrategyVersion("",250,1000,"balanced"),new org.json.JSONArray()));
    }
    @Test public void cancelledClientCannotStartAnotherRequest() {
        DecisionClient client=new DecisionClient("https://api.example.test/v1/decisions","test-model","test-only");
        client.cancel();
        assertThrows(IllegalStateException.class,()->client.decide(new byte[0],"profile","scope"));
    }
    private JSONObject answer() throws Exception {
        return new JSONObject().put("choice","attack").put("probabilities",new JSONObject().put("attack",.96).put("wait",.04)).put("confidence",.96).put("none_probability",.01);
    }
    @Test public void validatedChoiceIsAccepted() throws Exception {
        assertEquals("attack",DecisionClient.choice(answer(),Set.of("attack","wait")));
    }
    @Test public void uncertaintyAndNoneMustAbstain() throws Exception {
        assertNull(DecisionClient.choice(answer().put("confidence",.89),Set.of("attack","wait")));
        assertNull(DecisionClient.choice(answer().put("none_probability",.21),Set.of("attack","wait")));
        assertNull(DecisionClient.choice(answer().put("choice",JSONObject.NULL),Set.of("attack","wait")));
        JSONObject contradictory=answer();contradictory.getJSONObject("probabilities").put("attack",.04).put("wait",.96);
        assertNull(DecisionClient.choice(contradictory,Set.of("attack","wait")));
    }
    @Test public void extraChoicesAndInvalidProbabilitiesAreRejected() throws Exception {
        JSONObject extra=answer();extra.getJSONObject("probabilities").put("purchase",0);
        assertThrows(IllegalStateException.class,()->DecisionClient.choice(extra,Set.of("attack","wait")));
        JSONObject invalid=answer();invalid.getJSONObject("probabilities").put("attack",.1);
        assertThrows(IllegalStateException.class,()->DecisionClient.choice(invalid,Set.of("attack","wait")));
        assertThrows(IllegalStateException.class,()->DecisionClient.choice(answer().put("confidence","0.96"),Set.of("attack","wait")));
        assertThrows(IllegalStateException.class,()->DecisionClient.choice(answer().put("none_probability",true),Set.of("attack","wait")));
    }
}
