package dev.bamboo.rsi;

import org.json.JSONObject;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;

/** Candidates are data, never executable downloaded code or editable evaluation rules. */
public record StrategyVersion(String parent,int moveMillis,int intervalMillis,String style) {
    public static final Set<String> STYLES=Set.of("balanced","cautious","ranged");
    public static final String CONTRACT="android-paired-arena-v1";
    public StrategyVersion {
        if(parent==null||(!parent.isEmpty()&&!parent.matches("[a-f0-9]{64}")))throw new IllegalArgumentException("Invalid parent");
        new Protocol.Policy(moveMillis,intervalMillis,300);
        if(!STYLES.contains(style))throw new IllegalArgumentException("Invalid strategy style");
    }
    public JSONObject json() throws Exception {return new JSONObject().put("parent",parent).put("move_ms",moveMillis).put("interval_ms",intervalMillis).put("style",style).put("contract",CONTRACT);}
    public String identity() throws Exception{return CanonicalJson.hash(json());}
    public static StrategyVersion from(JSONObject value) throws Exception {
        if(!CONTRACT.equals(value.getString("contract"))||value.length()!=5)throw new IllegalArgumentException("Strategy schema mismatch");
        return new StrategyVersion(value.getString("parent"),value.getInt("move_ms"),value.getInt("interval_ms"),value.getString("style"));
    }
    public Protocol.Policy execution(){return new Protocol.Policy(moveMillis,intervalMillis,300);}
    public String directive(){return switch(style){
        case "cautious" -> "优先保命、离开明显危险区域；没有可靠进攻条件时观察或撤退。";
        case "ranged" -> "若当前英雄适合远程输出，优先保持距离并攻击；不要把近战英雄当远程英雄。";
        default -> "依据当前画面平衡推进、输出与生存；不能确认状态时观察。";
    };}
    public List<StrategyVersion> neighbours() throws Exception {
        var result=new ArrayList<StrategyVersion>();String id=identity();
        for(int move:new int[]{moveMillis-50,moveMillis+50})if(move>=100&&move<=500)result.add(new StrategyVersion(id,move,intervalMillis,style));
        for(int interval:new int[]{intervalMillis-250,intervalMillis+250})if(interval>=500&&interval<=5000)result.add(new StrategyVersion(id,moveMillis,interval,style));
        for(String value:List.of("balanced","cautious","ranged"))if(!value.equals(style))result.add(new StrategyVersion(id,moveMillis,intervalMillis,value));
        return result;
    }
}
