package dev.bamboo.rsi;

import android.content.Context;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;
import java.security.KeyStore;

/** Android Keystore protects the provider key; logs never contain it. */
public final class ProviderVault {
    private static final String ALIAS="bamboo-rsi-provider";
    private static SecretKey key() throws Exception {
        KeyStore store=KeyStore.getInstance("AndroidKeyStore");store.load(null);
        if(!store.containsAlias(ALIAS)){
            KeyGenerator generator=KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES,"AndroidKeyStore");
            generator.init(new KeyGenParameterSpec.Builder(ALIAS,KeyProperties.PURPOSE_ENCRYPT|KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build());generator.generateKey();
        }
        return (SecretKey)store.getKey(ALIAS,null);
    }
    public static void save(Context context,String secret,String endpoint) throws Exception {
        if(secret.trim().isEmpty()||secret.chars().anyMatch(Character::isWhitespace))throw new IllegalArgumentException("Invalid provider credential");
        Cipher cipher=Cipher.getInstance("AES/GCM/NoPadding");cipher.init(Cipher.ENCRYPT_MODE,key());
        cipher.updateAAD(Protocol.endpoint(endpoint).toString().getBytes(java.nio.charset.StandardCharsets.UTF_8));
        boolean saved=context.getSharedPreferences("private",0).edit().putString("iv",Base64.encodeToString(cipher.getIV(),Base64.NO_WRAP))
            .putString("key",Base64.encodeToString(cipher.doFinal(secret.getBytes(java.nio.charset.StandardCharsets.UTF_8)),Base64.NO_WRAP)).commit();
        if(!saved)throw new IllegalStateException("Credential could not be persisted");
    }
    public static String read(Context context,String endpoint) throws Exception {
        var prefs=context.getSharedPreferences("private",0);String encrypted=prefs.getString("key",null),iv=prefs.getString("iv",null);
        if(encrypted==null||iv==null)throw new IllegalStateException("Provider credential required");
        Cipher cipher=Cipher.getInstance("AES/GCM/NoPadding");cipher.init(Cipher.DECRYPT_MODE,key(),new GCMParameterSpec(128,Base64.decode(iv,Base64.NO_WRAP)));
        cipher.updateAAD(Protocol.endpoint(endpoint).toString().getBytes(java.nio.charset.StandardCharsets.UTF_8));
        return new String(cipher.doFinal(Base64.decode(encrypted,Base64.NO_WRAP)),java.nio.charset.StandardCharsets.UTF_8);
    }
}
