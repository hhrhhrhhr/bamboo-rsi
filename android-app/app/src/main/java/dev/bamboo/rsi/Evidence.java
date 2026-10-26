package dev.bamboo.rsi;

import android.content.Context;
import org.json.JSONObject;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.UUID;

public final class Evidence {
    public final File directory;
    private JSONObject video=new JSONObject();
    private JSONObject configuration=new JSONObject();
    private boolean ended;
    private JSONObject finalReport;
    public Evidence(Context context) throws Exception {
        directory = new File(context.getExternalFilesDir("runs"), System.currentTimeMillis()+"-"+UUID.randomUUID().toString().substring(0,8));
        if (!directory.mkdirs()) throw new IllegalStateException("Cannot create evidence directory");
    }
    public synchronized void event(String type, JSONObject body) throws Exception {
        if(ended)throw new IllegalStateException("Evidence already sealed");
        JSONObject value = new JSONObject().put("type",type).put("at_ms",System.currentTimeMillis()).put("data",body);
        try(FileOutputStream stream=new FileOutputStream(new File(directory,"trace.jsonl"),true)) {
            stream.write((value+"\n").getBytes(StandardCharsets.UTF_8)); stream.getFD().sync();
        }
    }
    public synchronized String image(byte[] bytes) throws Exception {
        String hash=Protocol.sha(bytes);File path=new File(directory,hash+".png");
        if(!path.exists())try(FileOutputStream stream=new FileOutputStream(path)){stream.write(bytes);stream.getFD().sync();}
        return hash;
    }
    public synchronized void video(JSONObject value) throws Exception { video=value;event("video_finalized",value); }
    public synchronized void configuration(JSONObject value) throws Exception { configuration=value;event("configuration_frozen",value); }
    public synchronized void end(String reason, Protocol.Outcome outcome) throws Exception {
        end(reason,outcome,new JSONObject());
    }
    public synchronized JSONObject end(String reason, Protocol.Outcome outcome,JSONObject details) throws Exception {
        if(ended)return new JSONObject(finalReport.toString());
        JSONObject report=new JSONObject().put("reason",reason).put("outcome",outcome.name()).put("run_mode","development")
            .put("autonomous_promotion_eligible",false).put("mobile_profile_validated",false).put("video",video).put("configuration",configuration);
        for(var keys=details.keys();keys.hasNext();){String key=keys.next();report.put(key,details.get(key));}
        event("session_ended",report);
        report.put("trace_sha256",fileHash(new File(directory,"trace.jsonl"))).put("video_complete",video.optBoolean("complete"))
            .put("video_sha256",video.optString("sha256",""));
        try(FileOutputStream stream=new FileOutputStream(new File(directory,"report.json"))){stream.write(report.toString(2).getBytes(StandardCharsets.UTF_8));stream.getFD().sync();}
        finalReport=report;ended=true;return new JSONObject(report.toString());
    }
    public static String fileHash(File file) throws Exception {
        var digest=java.security.MessageDigest.getInstance("SHA-256");
        try(var input=new java.io.FileInputStream(file)){byte[] bytes=new byte[65536];int n;while((n=input.read(bytes))!=-1)digest.update(bytes,0,n);}
        StringBuilder hex=new StringBuilder();for(byte b:digest.digest())hex.append(String.format("%02x",b&255));return hex.toString();
    }
}
