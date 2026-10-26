package dev.bamboo.rsi;

import org.json.JSONArray;
import org.json.JSONObject;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;

/** Frozen evaluator: 100 matched pairs, both sides, no optional stopping. */
public final class PairedEvaluation {
    public static final int PAIRS=100;
    public static final double MIN_GAIN=.05, MAX_COST_RATIO=1.10, ALPHA=.05;
    public static final String VERSION=StrategyVersion.CONTRACT;
    public record Sample(String runId,String policy,String matchId,int pair,boolean swapped,String opponent,
                         Protocol.Outcome outcome,long calls,long durationMillis,boolean verified,boolean assisted) {}
    public record Result(boolean complete,boolean eligible,double parentWinRate,double candidateWinRate,
                         double gain,double pValue,List<String> reasons) {
        public JSONObject json() throws Exception {return new JSONObject().put("complete",complete).put("eligible",eligible).put("parent_win_rate",parentWinRate)
            .put("candidate_win_rate",candidateWinRate).put("gain",gain).put("p_value",pValue).put("reasons",new JSONArray(reasons)).put("evaluator",VERSION);}
    }
    public static Result evaluate(String parent,String candidate,List<Sample> samples){
        return evaluate(parent,candidate,samples,ALPHA);
    }
    public static Result evaluate(String parent,String candidate,List<Sample> samples,double alpha){
        if(!Double.isFinite(alpha)||alpha<=0||alpha>ALPHA)throw new IllegalArgumentException("Invalid preregistered alpha");
        var reasons=new ArrayList<String>();boolean complete=samples.size()==PAIRS*2;
        if(!complete)reasons.add("incomplete_preregistered_cohort");
        if(parent.equals(candidate))reasons.add("identical_versions");
        var runs=new HashSet<String>();var matches=new HashSet<String>();
        Sample[][] pairs=new Sample[PAIRS][2];int pw=0,cw=0,better=0,worse=0;long pc=0,cc=0;
        for(Sample s:samples){
            int arm=parent.equals(s.policy)?0:candidate.equals(s.policy)?1:-1;
            if(arm<0||s.pair<0||s.pair>=PAIRS){reasons.add("unexpected_assignment");continue;}
            if(pairs[s.pair][arm]!=null)reasons.add("duplicate_slot");else pairs[s.pair][arm]=s;
            if(s.runId==null||s.matchId==null||!runs.add(s.runId)||!matches.add(s.matchId))reasons.add("duplicate_or_missing_evidence");
            if(!s.verified||s.assisted||s.outcome==null||s.outcome==Protocol.Outcome.UNKNOWN||s.calls<1||s.calls>300||s.durationMillis<10000)reasons.add("unverified_or_intervened_sample");
            if(arm==0){pc+=s.calls;if(s.outcome==Protocol.Outcome.WIN)pw++;}else{cc+=s.calls;if(s.outcome==Protocol.Outcome.WIN)cw++;}
        }
        for(int i=0;i<PAIRS;i++){
            Sample p=pairs[i][0],c=pairs[i][1];if(p==null||c==null){if(complete)reasons.add("missing_pair");continue;}
            if(p.swapped!=(i%2==1)||p.swapped!=c.swapped||p.opponent==null||p.opponent.isEmpty()||!p.opponent.equals(c.opponent))reasons.add("unmatched_context");
            if(c.outcome==Protocol.Outcome.WIN&&p.outcome==Protocol.Outcome.LOSS)better++;
            if(c.outcome==Protocol.Outcome.LOSS&&p.outcome==Protocol.Outcome.WIN)worse++;
        }
        double gain=(cw-pw)/(double)PAIRS,pValue=binomialTail(better,better+worse);
        if(gain+1e-12<MIN_GAIN)reasons.add("insufficient_win_rate_gain");
        if(pValue>alpha)reasons.add("improvement_not_significant");
        if(pc==0||cc>pc*MAX_COST_RATIO)reasons.add("model_call_cost_regression");
        return new Result(complete,complete&&reasons.isEmpty(),pw/(double)PAIRS,cw/(double)PAIRS,gain,pValue,reasons.stream().distinct().collect(java.util.stream.Collectors.toList()));
    }
    static double binomialTail(int wins,int n){
        if(n==0||wins==0)return 1;
        double probability=Math.scalb(1.0,-n),sum=0;
        for(int i=0;i<=n;i++){if(i>=wins)sum+=probability;if(i<n)probability=probability*(n-i)/(i+1);}
        return Math.min(1,sum);
    }
}
