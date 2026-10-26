package dev.bamboo.rsi;
import org.junit.Test;
import java.util.List;
import static org.junit.Assert.*;
public class ResultGateTest {
    private List<ResultGate.Word> win(){return List.of(new ResultGate.Word("胜 利",.5f,.3f,.09f));}
    @Test public void needsBattleAndTwoSeparateFrames(){var gate=new ResultGate();assertEquals(Protocol.Outcome.UNKNOWN,gate.observe(win(),10000,10000,false));assertEquals(Protocol.Outcome.UNKNOWN,gate.observe(win(),10000,10000,true));assertEquals(Protocol.Outcome.WIN,gate.observe(win(),10600,10600,true));}
    @Test public void ignoresChatSmallTextAndAmbiguousBanners(){assertEquals(Protocol.Outcome.UNKNOWN,ResultGate.classify(List.of(new ResultGate.Word("胜利",.1f,.8f,.03f))));assertEquals(Protocol.Outcome.UNKNOWN,ResultGate.classify(List.of(new ResultGate.Word("胜利",.5f,.3f,.08f),new ResultGate.Word("失败",.5f,.3f,.08f))));}
    @Test public void staleFrameAndMissingObservationResetConfirmation(){var gate=new ResultGate();gate.observe(win(),10000,10000,true);assertEquals(Protocol.Outcome.UNKNOWN,gate.observe(win(),20000,20000,true));gate.observe(List.of(),20600,20600,true);assertEquals(Protocol.Outcome.UNKNOWN,gate.observe(win(),21200,21200,true));}
    @Test public void lossMustBeExplicit(){assertEquals(Protocol.Outcome.LOSS,ResultGate.classify(List.of(new ResultGate.Word("DEFEAT",.5f,.3f,.08f))));assertEquals(Protocol.Outcome.UNKNOWN,ResultGate.classify(List.of(new ResultGate.Word("即将胜利",.5f,.3f,.08f))));}
}
