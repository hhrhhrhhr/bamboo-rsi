package dev.bamboo.rsi;

import android.content.Context;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.view.WindowManager;
import androidx.test.core.app.ActivityScenario;
import androidx.test.platform.app.InstrumentationRegistry;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import org.json.JSONObject;
import org.junit.Test;
import org.junit.runner.RunWith;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyStore;
import java.util.UUID;
import javax.crypto.KeyGenerator;
import javax.crypto.Mac;
import javax.crypto.SecretKey;
import static org.junit.Assert.*;

/** Device-only checks. Never starts capture, calls Decision, or injects game input. */
@RunWith(AndroidJUnit4.class)
public final class DeviceSmokeTest {
    @Test public void ledgerSurvivesReopenAndRejectsTamperingWithAndroidKeystore() throws Exception {
        Context context=InstrumentationRegistry.getInstrumentation().getTargetContext();
        // These names are deliberately unrelated to the production ledger and its key.
        String name="instrumentation-"+UUID.randomUUID();
        Path directory=context.getCacheDir().toPath().resolve(name);
        KeyStore store=KeyStore.getInstance("AndroidKeyStore");store.load(null);
        try {
            KeyGenerator generator=KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_HMAC_SHA256,"AndroidKeyStore");
            generator.init(new KeyGenParameterSpec.Builder(name,KeyProperties.PURPOSE_SIGN|KeyProperties.PURPOSE_VERIFY).build());
            SecretKey key=generator.generateKey();
            RsiLedger.Signer signer=data->{Mac mac=Mac.getInstance("HmacSHA256");mac.init(key);return mac.doFinal(data);};
            RsiLedger first=new RsiLedger(directory,signer);
            String parent=first.snapshot().getString("accepted");
            first.reserve("interrupted-device-smoke","a".repeat(64),System.currentTimeMillis());
            RsiLedger reopened=new RsiLedger(directory,signer);
            reopened.recoverInterrupted(System.currentTimeMillis());
            JSONObject state=reopened.snapshot();
            assertFalse(state.has("active_run"));
            assertEquals(parent,state.getString("accepted"));
            assertEquals("UNKNOWN",state.getJSONObject("runs").getJSONObject("interrupted-device-smoke").getJSONObject("local").getString("outcome"));
            Path file=directory.resolve("ledger.json");
            JSONObject envelope=new JSONObject(new String(Files.readAllBytes(file),StandardCharsets.UTF_8));
            envelope.getJSONObject("state").put("generation",123);
            Files.write(file,envelope.toString().getBytes(StandardCharsets.UTF_8));
            try {new RsiLedger(directory,signer);fail("Tampered ledger accepted");}
            catch(IllegalStateException expected){assertEquals("Ledger integrity failure",expected.getMessage());}
        } finally {
            store.deleteEntry(name);
            // Only these test-owned files can be removed; never production evidence.
            for(String file:new String[]{"ledger.json","ledger.next","ledger.lock"})Files.deleteIfExists(directory.resolve(file));
            Files.deleteIfExists(directory);
        }
    }

    @Test public void openingAndRecreatingHomeDoesNotStartExecutionOrChangeSettings() {
        Context context=InstrumentationRegistry.getInstrumentation().getTargetContext();
        var before=context.getSharedPreferences("config",0).getAll();
        assertFalse(SessionController.active());assertNull(CaptureService.instance);
        try(ActivityScenario<MainActivity> scenario=ActivityScenario.launch(MainActivity.class)){
            scenario.recreate();
            scenario.onActivity(activity->{
                assertNotEquals(0,activity.getWindow().getAttributes().flags&WindowManager.LayoutParams.FLAG_SECURE);
                assertFalse(SessionController.active());assertNull(CaptureService.instance);
                assertTrue("Opening settings must not write them",before.equals(context.getSharedPreferences("config",0).getAll()));
            });
        }
    }
}
