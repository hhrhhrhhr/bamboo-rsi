package dev.bamboo.rsi;

import org.json.JSONArray;
import org.json.JSONObject;
import java.util.ArrayList;
import java.util.Collections;

/** Stable encoding for hashes and signatures. No credentials belong in these documents. */
public final class CanonicalJson {
    private CanonicalJson() {}
    public static String encode(Object value) throws Exception {
        if(value==null||value==JSONObject.NULL)return "null";
        if(value instanceof JSONObject object){
            var keys=new ArrayList<String>();object.keys().forEachRemaining(keys::add);Collections.sort(keys);
            var parts=new ArrayList<String>();for(String key:keys)parts.add(JSONObject.quote(key)+":"+encode(object.get(key)));
            return "{"+String.join(",",parts)+"}";
        }
        if(value instanceof JSONArray array){var parts=new ArrayList<String>();for(int i=0;i<array.length();i++)parts.add(encode(array.get(i)));return "["+String.join(",",parts)+"]";}
        if(value instanceof String text)return JSONObject.quote(text);
        if(value instanceof Boolean)return value.toString();
        if(value instanceof Number number){double d=number.doubleValue();if(!Double.isFinite(d))throw new IllegalArgumentException("Non-finite JSON number");return JSONObject.numberToString(number);}
        throw new IllegalArgumentException("Unsupported canonical value");
    }
    public static String hash(Object value) throws Exception {return Protocol.sha(encode(value));}
}
