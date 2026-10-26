package dev.bamboo.rsi;

import org.junit.Test;
import java.util.ArrayList;
import java.util.List;
import static org.junit.Assert.*;

public class ProtocolTest {
    @Test public void endpointsAreHttpsAndCannotContainCredentials() {
        assertEquals("api.example.test",Protocol.endpoint("https://api.example.test/v1/decisions").getHost());
        for(String endpoint:List.of("http://api.example.test/v1/decisions","https://user:secret@api.example.test/v1/decisions","https://api.example.test/v1/decisions?key=secret","https://api.example.test/v1/decisions#fragment","https://api.example.test/v1/chat/completions"))
            assertThrows(IllegalArgumentException.class,()->Protocol.endpoint(endpoint));
    }
    @Test public void staleFutureAndCancelledRepliesCannotAct() {
        assertTrue(Protocol.applicable(Protocol.GAME_PACKAGE,1000,3500,2,2,"attack"));
        assertFalse(Protocol.applicable(Protocol.GAME_PACKAGE,1000,3501,2,2,"attack"));
        assertFalse(Protocol.applicable(Protocol.GAME_PACKAGE,1001,1000,2,2,"attack"));
        assertFalse(Protocol.applicable(Protocol.GAME_PACKAGE,1000,1001,2,3,"attack"));
        assertFalse(Protocol.applicable("com.android.settings",1000,1001,2,2,"attack"));
        assertFalse(Protocol.applicable(Protocol.GAME_PACKAGE,1000,1001,2,2,"buy"));
    }
    @Test public void gesturesAndCallBudgetsRemainBounded() {
        for(int milliseconds:new int[]{0,99,501,10000})assertThrows(IllegalArgumentException.class,()->new Protocol.Policy(milliseconds,1000,60));
        assertThrows(IllegalArgumentException.class,()->new Protocol.Policy(250,100,60));
        assertThrows(IllegalArgumentException.class,()->new Protocol.Policy(250,1000,301));
        assertThrows(IllegalArgumentException.class,()->new Protocol.Point(Float.NaN,.5f));
        assertThrows(IllegalArgumentException.class,()->new Protocol.Point(.5f,1));
        assertNotEquals(new Protocol.Policy(250,1000,60).identity(),new Protocol.Policy(300,1000,60).identity());
    }
    @Test public void timeoutAndBattleExitAreNeverWins() {
        assertEquals(Protocol.Outcome.UNKNOWN,Protocol.terminal("battle","win","win",60000));
        assertEquals(Protocol.Outcome.UNKNOWN,Protocol.terminal("other","win","win",60000));
        assertEquals(Protocol.Outcome.UNKNOWN,Protocol.terminal("result","unknown","unknown",60000));
        assertEquals(Protocol.Outcome.UNKNOWN,Protocol.terminal("result","win","loss",60000));
        assertEquals(Protocol.Outcome.UNKNOWN,Protocol.terminal("result","win","win",9999));
        assertEquals(Protocol.Outcome.UNKNOWN,Protocol.terminal("result",null,null,60000));
    }
    @Test public void twoConsistentResultObservationsAreRequired() {
        assertEquals(Protocol.Outcome.WIN,Protocol.terminal("result","win","win",10000));
        assertEquals(Protocol.Outcome.LOSS,Protocol.terminal("result","loss","loss",10000));
        assertEquals(Protocol.Outcome.UNKNOWN,Protocol.terminal("result","win","unknown",10000));
    }
    @Test public void calibrationAndProviderAreBoundToProfile() {
        var policy=new Protocol.Policy(250,1000,60);
        var points=java.util.Map.of("attack",new Protocol.Point(.8f,.8f));
        String identity=Protocol.profileIdentity(policy,"https://api.example.test/v1/decisions","decision-v1",points);
        assertEquals(identity,Protocol.profileIdentity(policy,"https://api.example.test/v1/decisions","decision-v1",points));
        assertNotEquals(identity,Protocol.profileIdentity(policy,"https://api.example.test/v1/decisions","decision-v1",java.util.Map.of("attack",new Protocol.Point(.7f,.8f))));
        assertNotEquals(identity,Protocol.profileIdentity(policy,"https://other.example.test/v1/decisions","decision-v1",points));
        assertNotEquals(identity,Protocol.profileIdentity(policy,"https://api.example.test/v1/decisions","decision-v2",points));
    }
    private List<Protocol.Match> matches(String policy,int wins) {
        var rows=new ArrayList<Protocol.Match>();
        for(int i=0;i<100;i++)rows.add(new Protocol.Match(Protocol.sha(policy),"frozen-opponent",i%2==0,i<wins?Protocol.Outcome.WIN:Protocol.Outcome.LOSS,false,true));
        return rows;
    }
    @Test public void diagnosticImprovementCannotPromoteAnything() {
        var result=Protocol.compare(matches("parent",40),matches("candidate",65));
        assertEquals(true,result.get("admissible"));
        assertEquals(.40,(double)result.get("parent_win_rate"),.0001);
        assertEquals(.65,(double)result.get("candidate_win_rate"),.0001);
        assertEquals(false,result.get("eligible_for_release"));
    }
    @Test public void assistedUnknownOrMixedPoliciesAreRejected() {
        for(Protocol.Match replacement:List.of(
            new Protocol.Match(Protocol.sha("candidate"),"frozen-opponent",true,Protocol.Outcome.WIN,true,true),
            new Protocol.Match(Protocol.sha("candidate"),"frozen-opponent",true,Protocol.Outcome.UNKNOWN,false,true),
            new Protocol.Match(Protocol.sha("candidate"),"frozen-opponent",true,Protocol.Outcome.WIN,false,false),
            new Protocol.Match(Protocol.sha("other-version"),"frozen-opponent",true,Protocol.Outcome.WIN,false,true))) {
            var candidate=matches("candidate",65);candidate.set(0,replacement);
            assertEquals(false,Protocol.compare(matches("parent",40),candidate).get("admissible"));
        }
    }
    @Test public void opponentSidesAndSampleCountsMustMatch() {
        var candidate=matches("candidate",65);candidate.set(0,new Protocol.Match(Protocol.sha("candidate"),"different-opponent",true,Protocol.Outcome.WIN,false,true));
        assertEquals(false,Protocol.compare(matches("parent",40),candidate).get("admissible"));
        candidate=matches("candidate",65);candidate.remove(0);
        assertEquals(false,Protocol.compare(matches("parent",40),candidate).get("admissible"));
        candidate=matches("candidate",65);candidate.set(0,new Protocol.Match(Protocol.sha("candidate"),"frozen-opponent",false,Protocol.Outcome.WIN,false,true));
        assertEquals(false,Protocol.compare(matches("parent",40),candidate).get("admissible"));
        assertEquals(false,Protocol.compare(matches("parent",40),matches("parent",65)).get("admissible"));
    }
}
