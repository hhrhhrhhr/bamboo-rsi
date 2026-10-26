package dev.bamboo.rsi;
import org.junit.Test;
import java.util.*;
import static org.junit.Assert.*;
public class PairedEvaluationTest {
    private List<PairedEvaluation.Sample> cohort(){var samples=new ArrayList<PairedEvaluation.Sample>();for(int i=0;i<100;i++)for(String policy:List.of("parent","candidate"))samples.add(new PairedEvaluation.Sample(policy+i,policy,policy+"match"+i,i,i%2==1,"fixture",i<(policy.equals("parent")?40:70)?Protocol.Outcome.WIN:Protocol.Outcome.LOSS,20,30000,true,false));return samples;}
    @Test public void pairedImprovementCanPassOnlyCompleteCohort(){var cohort=cohort();assertTrue(PairedEvaluation.evaluate("parent","candidate",cohort).eligible());cohort.remove(0);assertFalse(PairedEvaluation.evaluate("parent","candidate",cohort).eligible());}
    @Test public void duplicatesAndCostRegressionFail(){var cohort=cohort();cohort.set(1,cohort.get(0));assertFalse(PairedEvaluation.evaluate("parent","candidate",cohort).eligible());cohort=cohort();var c=cohort.get(1);cohort.set(1,new PairedEvaluation.Sample(c.runId(),c.policy(),c.matchId(),c.pair(),c.swapped(),c.opponent(),c.outcome(),300,30000,true,false));assertFalse(PairedEvaluation.evaluate("parent","candidate",cohort).eligible());}
    @Test public void noDifferenceCannotPassAndAlphaCannotLoosen(){var samples=cohort();for(int i=0;i<samples.size();i++){var c=samples.get(i);samples.set(i,new PairedEvaluation.Sample(c.runId(),c.policy(),c.matchId(),c.pair(),c.swapped(),c.opponent(),Protocol.Outcome.WIN,20,30000,true,false));}assertFalse(PairedEvaluation.evaluate("parent","candidate",samples).eligible());assertThrows(IllegalArgumentException.class,()->PairedEvaluation.evaluate("parent","candidate",samples,.5));}
}
