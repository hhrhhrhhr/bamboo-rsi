package dev.bamboo.rsi;

import org.json.JSONObject;
import java.nio.charset.StandardCharsets;
import java.security.KeyFactory;
import java.security.Signature;
import java.security.interfaces.ECPublicKey;
import java.security.spec.X509EncodedKeySpec;
import java.util.Base64;

/** The evaluator trusts a frozen, independent arena authority, never the action model. */
public final class ArenaAttestation {
    public static void validateKey(String encoded) throws Exception {
        var key=(ECPublicKey)KeyFactory.getInstance("EC").generatePublic(new X509EncodedKeySpec(Base64.getDecoder().decode(encoded)));
        var parameters=java.security.AlgorithmParameters.getInstance("EC");parameters.init(new java.security.spec.ECGenParameterSpec("secp256r1"));
        var expected=parameters.getParameterSpec(java.security.spec.ECParameterSpec.class);var actual=key.getParams();
        if(!expected.getCurve().equals(actual.getCurve())||!expected.getGenerator().equals(actual.getGenerator())||!expected.getOrder().equals(actual.getOrder())||expected.getCofactor()!=actual.getCofactor())throw new IllegalArgumentException("P-256 authority key required");
    }
    public static PairedEvaluation.Sample verify(JSONObject signed,String publicKey,JSONObject assignment,JSONObject local,long now) throws Exception {
        validateKey(publicKey);JSONObject body=signed.getJSONObject("body");
        Signature verifier=Signature.getInstance("SHA256withECDSA");verifier.initVerify(KeyFactory.getInstance("EC").generatePublic(new X509EncodedKeySpec(Base64.getDecoder().decode(publicKey))));
        verifier.update(CanonicalJson.encode(body).getBytes(StandardCharsets.UTF_8));
        if(!verifier.verify(Base64.getDecoder().decode(signed.getString("signature"))))throw new IllegalArgumentException("Invalid arena signature");
        for(String key:new String[]{"run_id","policy","trial","context_sha256","pair","swapped"})
            if(!CanonicalJson.encode(body.get(key)).equals(CanonicalJson.encode(assignment.get(key))))throw new IllegalArgumentException("Attestation assignment mismatch");
        if(!"authorized_arena_live".equals(body.getString("origin"))||body.getBoolean("assisted"))throw new IllegalArgumentException("Trusted live evidence required");
        if(body.getLong("issued_at_ms")<assignment.getLong("reserved_at_ms")||body.getLong("issued_at_ms")>now+30000)throw new IllegalArgumentException("Invalid attestation clock");
        for(String key:new String[]{"trace_sha256","video_sha256","outcome","calls","duration_ms"})
            if(!CanonicalJson.encode(body.get(key)).equals(CanonicalJson.encode(local.get(key))))throw new IllegalArgumentException("Attestation does not bind local evidence");
        if(!local.getBoolean("video_complete")||!local.getBoolean("input_released")||!local.getBoolean("local_result_verified"))throw new IllegalArgumentException("Incomplete local evidence");
        if(!body.getString("match_id").matches("[A-Za-z0-9._:-]{1,128}")||!body.getString("opponent").matches("[A-Za-z0-9._:-]{1,128}"))throw new IllegalArgumentException("Invalid arena identity");
        Protocol.Outcome outcome=Protocol.Outcome.valueOf(body.getString("outcome"));if(outcome==Protocol.Outcome.UNKNOWN)throw new IllegalArgumentException("Unknown match outcome");
        return new PairedEvaluation.Sample(body.getString("run_id"),body.getString("policy"),body.getString("match_id"),body.getInt("pair"),body.getBoolean("swapped"),body.getString("opponent"),outcome,body.getLong("calls"),body.getLong("duration_ms"),true,false);
    }
}
