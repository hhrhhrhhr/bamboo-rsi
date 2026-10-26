package dev.bamboo.rsi;

import android.content.Context;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import org.json.JSONObject;
import javax.crypto.KeyGenerator;
import javax.crypto.Mac;
import javax.crypto.SecretKey;
import java.security.KeyStore;

/** Android storage adapter; the ledger and evaluator have no Android dependency. */
public final class MobileRsi {
    private static RsiLedger ledger;
    public static synchronized RsiLedger open(Context context) throws Exception {
        if(ledger!=null)return ledger;
        var directory=new java.io.File(context.getFilesDir(),"rsi").toPath();
        KeyStore store=KeyStore.getInstance("AndroidKeyStore");store.load(null);String alias="bamboo-rsi-ledger-v1";
        if(!store.containsAlias(alias)){
            if(java.nio.file.Files.exists(directory.resolve("ledger.json")))throw new IllegalStateException("Ledger key unavailable; preserve evidence before recovery");
            KeyGenerator generator=KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_HMAC_SHA256,"AndroidKeyStore");
            generator.init(new KeyGenParameterSpec.Builder(alias,KeyProperties.PURPOSE_SIGN|KeyProperties.PURPOSE_VERIFY).build());generator.generateKey();
        }
        SecretKey secret=(SecretKey)store.getKey(alias,null);
        RsiLedger opened=new RsiLedger(directory,data->{Mac mac=Mac.getInstance("HmacSHA256");mac.init(secret);return mac.doFinal(data);});
        opened.recoverInterrupted(System.currentTimeMillis());ledger=opened;return ledger;
    }
    public static DecisionClient client(Context context) throws Exception {
        var prefs=context.getSharedPreferences("config",0);String endpoint=prefs.getString("endpoint","https://api.bamboonode.dev/v1/decisions");
        return new DecisionClient(endpoint,prefs.getString("model","startlux-decision-27b-autothink"),ProviderVault.read(context,endpoint));
    }
    public static java.util.Map<String,Protocol.Point> points(Context context) {
        var result=new java.util.LinkedHashMap<String,Protocol.Point>();var prefs=context.getSharedPreferences("config",0);
        for(String name:new String[]{"joystick","attack","skill_1","skill_2","skill_3"}){
            String[] xy=prefs.getString(name,"").split(",");if(xy.length!=2)throw new IllegalStateException("Calibration required");
            result.put(name,new Protocol.Point(Float.parseFloat(xy[0]),Float.parseFloat(xy[1])));
        }return result;
    }
    public static String contextHash(Context context,DecisionClient client,java.util.Map<String,Protocol.Point> points) throws Exception {
        // Freeze the actual installed executor/evaluator, game build, device, model and controls.
        JSONObject value=client.configuration().put("app_sha256",Evidence.fileHash(new java.io.File(context.getApplicationInfo().sourceDir)))
            .put("device",android.os.Build.FINGERPRINT).put("game_version",context.getPackageManager().getPackageInfo(Protocol.GAME_PACKAGE,0).getLongVersionCode())
            .put("controls",Protocol.profileIdentity(new Protocol.Policy(250,1000,300),"", "",points)).put("calibration_aspect",context.getSharedPreferences("config",0).getFloat("calibration_aspect",0)).put("evaluator",PairedEvaluation.VERSION);
        return CanonicalJson.hash(value);
    }
    public static JSONObject optimize(Context context) throws Exception {
        DecisionClient client=client(context);try{return optimize(context,client);}finally{client.cancel();}
    }
    public static JSONObject optimize(Context context,DecisionClient client) throws Exception {
        RsiLedger ledger=open(context);JSONObject ticket=ledger.reserveProposal(System.currentTimeMillis());
        try{
            StrategyVersion parent=ledger.version(ticket.getString("parent"));DecisionClient.Proposal reply=client.propose(parent,ticket.getJSONArray("training"));
            JSONObject receipt=client.configuration().put("selection",reply.choice()==null?JSONObject.NULL:reply.choice()).put("confidence",reply.confidence()).put("response_sha256",reply.responseHash());
            ledger.completeProposal(ticket.getString("id"),reply.choice(),receipt);return receipt;
        }catch(Exception error){ledger.completeProposal(ticket.getString("id"),null,new JSONObject().put("error_type",error.getClass().getSimpleName()));throw error;}
    }
}
