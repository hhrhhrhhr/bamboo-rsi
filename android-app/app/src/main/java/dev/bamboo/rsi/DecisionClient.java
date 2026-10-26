package dev.bamboo.rsi;

import android.util.Base64;
import org.json.JSONArray;
import org.json.JSONObject;
import javax.net.ssl.HttpsURLConnection;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import java.io.ByteArrayOutputStream;

/** Choice-only provider adapter. Credentials never enter evidence or model state. */
public final class DecisionClient {
    public record Reply(String phase, String outcome, String action, double confidence) {}
    public record Proposal(String choice,double confidence,String responseHash) {}
    private final String endpoint, model, secret;
    private volatile HttpsURLConnection connection;
    private boolean cancelled;
    public static final Map<String,String> LABELS=new LinkedHashMap<>();
    static {
        LABELS.put("wait","短暂观察，不触控");LABELS.put("stop","停止：不是已进入的授权测试对局，或无法可靠判断");
        LABELS.put("move_up","向上短距离移动");LABELS.put("move_down","向下短距离移动");
        LABELS.put("move_left","向左短距离移动");LABELS.put("move_right","向右短距离移动");
        LABELS.put("attack","普通攻击一次");LABELS.put("skill_1","释放第一技能");LABELS.put("skill_2","释放第二技能");LABELS.put("skill_3","释放第三技能");
        LABELS.put("move_up_left","向左上短移动");LABELS.put("move_up_right","向右上短移动");LABELS.put("move_down_left","向左下短移动");LABELS.put("move_down_right","向右下短移动");
        for(var direction:Map.of("up","上","down","下","left","左","right","右").entrySet()){
            LABELS.put("move_"+direction.getKey()+"_attack","向"+direction.getValue()+"短移动，同时点普攻");
            for(int skill=1;skill<=3;skill++)LABELS.put("skill_"+skill+"_aim_"+direction.getKey(),"将第"+skill+"技能向屏幕"+direction.getValue()+"拖动后释放；只有该英雄支持方向技能时使用");
        }
    }
    public DecisionClient(String endpoint,String model,String secret) {
        this.endpoint=Protocol.endpoint(endpoint).toString();this.model=model;this.secret=secret;
        if(model.trim().isEmpty())throw new IllegalArgumentException("Exact model required");
    }
    private JSONObject question(String text,Map<String,String> choices) throws Exception {
        return new JSONObject().put("type","choice").put("instructions",text).put("criteria",new JSONObject(choices)).put("allow_none",true);
    }
    public String profileIdentity(Protocol.Policy policy,Map<String,Protocol.Point> calibration){return Protocol.profileIdentity(policy,endpoint,model,calibration);}
    public JSONObject configuration(){return new JSONObject(Map.of("endpoint",endpoint,"model",model));}
    public Reply decide(byte[] png, String profile, String scope) throws Exception {
        return decide(png,profile,scope,"");
    }
    public Reply decide(byte[] png, String profile, String scope,String directive) throws Exception {
        synchronized(this){if(cancelled)throw new IllegalStateException("Session cancelled");}
        JSONObject questions=new JSONObject()
            .put("phase",question("判断当前画面。仅正在进行的对局属于battle；大厅、登录、匹配、选英雄、授权页均属于other。结算画面属于result。",Map.of("battle","正在进行的对局","result","明确的对局结算","other","其他或不确定")))
            .put("result",question("只有明确结算胜利或失败文字时才能选择win或loss；其他情况必须unknown。",Map.of("win","明确胜利结算","loss","明确失败结算","unknown","无法核验")))
            .put("primary_action",question("你正在操作用户事先进入的训练或获准测试局。选择一个有限技能。不得登录、排队、购买、充值或操作系统授权。非对局画面选stop；不确定选wait或弃权。"+directive,LABELS));
        JSONObject body=new JSONObject().put("model",model).put("state",new JSONObject().put("scope_hash",scope)
            .put("policy_sha256",profile).put("action_granularity","skill").toString())
            .put("questions",questions).put("images",new JSONArray().put("data:image/png;base64,"+Base64.encodeToString(png,Base64.NO_WRAP)))
            .put("allow_none",true).put("none_check",true).put("think_mode","never").put("return_details",true);
        JSONObject value=post(body);JSONObject answers=value.getJSONObject("answers");
        String phase=choice(answers.getJSONObject("phase"),Set.of("battle","result","other"));
        String result=choice(answers.getJSONObject("result"),Set.of("win","loss","unknown"));
        String action=choice(answers.getJSONObject("primary_action"),Protocol.ACTIONS);
        return new Reply(phase==null?"other":phase,result==null?"unknown":result,action==null?"wait":action,
            answers.getJSONObject("primary_action").getDouble("confidence"));
    }
    public Proposal propose(StrategyVersion parent,JSONArray training) throws Exception {
        Map<String,String> choices=new LinkedHashMap<>();choices.put("no_change","证据不足，不产生候选");
        for(var candidate:parent.neighbours())choices.put(candidate.identity(),candidate.json().toString());
        JSONObject body=new JSONObject().put("model",model).put("state",new JSONObject().put("parent",parent.json()).put("development_runs",training).toString())
            .put("questions",new JSONObject().put("candidate",question("依据开发运行失败与延迟证据选择一个待检验的策略变化；这是候选，不代表已经改善。不得更改评测标准。",choices)))
            .put("allow_none",true).put("none_check",true).put("think_mode","never").put("return_details",true);
        JSONObject response=post(body),answer=response.getJSONObject("answers").getJSONObject("candidate");String choice=choice(answer,choices.keySet());
        return new Proposal("no_change".equals(choice)?null:choice,answer.getDouble("confidence"),CanonicalJson.hash(response));
    }
    private JSONObject post(JSONObject body) throws Exception {
        synchronized(this){if(cancelled)throw new IllegalStateException("Session cancelled");}
        HttpsURLConnection current=(HttpsURLConnection)Protocol.endpoint(endpoint).toURL().openConnection();
        synchronized(this){if(cancelled){current.disconnect();throw new IllegalStateException("Session cancelled");}connection=current;}
        try {
            current.setConnectTimeout(10_000);current.setReadTimeout(20_000);current.setInstanceFollowRedirects(false);
            current.setRequestMethod("POST");current.setDoOutput(true);current.setRequestProperty("Content-Type","application/json");
            current.setRequestProperty("Authorization","Bearer "+secret);
            byte[] encoded=body.toString().getBytes(StandardCharsets.UTF_8);current.setFixedLengthStreamingMode(encoded.length);
            try(var stream=current.getOutputStream()){stream.write(encoded);}
            if(current.getResponseCode()!=200)throw new IllegalStateException("Decision request did not succeed");
            ByteArrayOutputStream output=new ByteArrayOutputStream();
            try(var input=current.getInputStream()){
                byte[] buffer=new byte[4096];int n;
                while((n=input.read(buffer))!=-1){if(output.size()+n>1_000_000)throw new IllegalStateException("Oversized response");output.write(buffer,0,n);}
            }
            JSONObject value=new JSONObject(new String(output.toByteArray(),StandardCharsets.UTF_8));
            if(!model.equals(value.getString("model")))throw new IllegalStateException("Model identity mismatch");
            return value;
        } finally { current.disconnect();if(connection==current)connection=null; }
    }
    static String choice(JSONObject answer,Set<String> allowed) throws Exception {
        JSONObject probabilities=answer.getJSONObject("probabilities");
        Set<String> keys=new java.util.HashSet<>();probabilities.keys().forEachRemaining(keys::add);
        if(!keys.equals(allowed))throw new IllegalStateException("Choice catalogue mismatch");
        double sum=0;
        for(String key:keys){Object raw=probabilities.get(key);if(!(raw instanceof Number))throw new IllegalStateException("Invalid probability type");double p=((Number)raw).doubleValue();if(!Double.isFinite(p)||p<0||p>1)throw new IllegalStateException("Invalid probability");sum+=p;}
        Object confidenceValue=answer.get("confidence"),noneValue=answer.opt("none_probability");
        if(!(confidenceValue instanceof Number)||(noneValue!=null&&noneValue!=JSONObject.NULL&&!(noneValue instanceof Number)))throw new IllegalStateException("Invalid confidence type");
        double confidence=((Number)confidenceValue).doubleValue(),none=noneValue instanceof Number?((Number)noneValue).doubleValue():0;
        if(!Double.isFinite(confidence)||confidence<0||confidence>1||!Double.isFinite(none)||none<0||none>1||Math.abs(sum-1)>.02)throw new IllegalStateException("Invalid choice confidence");
        if(answer.isNull("choice")||none>=.2||confidence<.90)return null;
        String choice=answer.getString("choice");if(!allowed.contains(choice))throw new IllegalStateException("Unknown choice");
        return probabilities.getDouble(choice)>=.90?choice:null;
    }
    public synchronized void cancel(){cancelled=true;HttpsURLConnection current=connection;if(current!=null)current.disconnect();}
}
