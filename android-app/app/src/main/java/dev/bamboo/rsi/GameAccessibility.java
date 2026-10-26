package dev.bamboo.rsi;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.graphics.Bitmap;
import android.graphics.Path;
import android.os.Handler;
import android.os.Looper;
import android.view.Display;
import android.view.accessibility.AccessibilityEvent;
import java.io.ByteArrayOutputStream;
import java.util.Map;
import java.util.concurrent.CompletableFuture;

/** Only the target game in the foreground can be observed or acted upon. */
public final class GameAccessibility extends AccessibilityService {
    public static volatile GameAccessibility instance;
    private final java.util.concurrent.atomic.AtomicInteger gestures=new java.util.concurrent.atomic.AtomicInteger();
    public boolean inputReleased(){return gestures.get()==0;}
    public record Frame(byte[] png,int width,int height,long capturedAt) {}
    @Override protected void onServiceConnected(){instance=this;}
    @Override public void onAccessibilityEvent(AccessibilityEvent event){
        CaptureService capture=CaptureService.instance;
        if(capture!=null&&capture.recording&&!Protocol.GAME_PACKAGE.equals(foreground())){
            SessionController.stopCurrent("game_left_foreground");capture.stopSelf();
        }
    }
    @Override public void onInterrupt(){SessionController.stopCurrent("accessibility_interrupted");}
    @Override public void onDestroy(){instance=null;SessionController.stopCurrent("accessibility_disconnected");super.onDestroy();}
    public String foreground(){var root=getRootInActiveWindow();return root==null||root.getPackageName()==null?"":root.getPackageName().toString();}
    public CompletableFuture<Frame> frame(){
        CompletableFuture<Frame> future=new CompletableFuture<>();
        if(!Protocol.GAME_PACKAGE.equals(foreground())){future.completeExceptionally(new IllegalStateException("Game not foreground"));return future;}
        takeScreenshot(Display.DEFAULT_DISPLAY,getMainExecutor(),new TakeScreenshotCallback(){
            @Override public void onSuccess(ScreenshotResult result){
                long capturedAt=android.os.SystemClock.elapsedRealtime();
                try(var buffer=result.getHardwareBuffer()){
                    Bitmap hardware=Bitmap.wrapHardwareBuffer(buffer,result.getColorSpace());
                    if(hardware==null)throw new IllegalStateException("No screenshot");
                    int width=hardware.getWidth(),height=hardware.getHeight();
                    if(width<=height||!Protocol.GAME_PACKAGE.equals(foreground()))throw new IllegalStateException("Landscape game required");
                    Bitmap bitmap=hardware.copy(Bitmap.Config.ARGB_8888,false);hardware.recycle();
                    float scale=Math.min(1f,960f/width);Bitmap small=Bitmap.createScaledBitmap(bitmap,Math.round(width*scale),Math.round(height*scale),true);
                    ByteArrayOutputStream bytes=new ByteArrayOutputStream();small.compress(Bitmap.CompressFormat.PNG,100,bytes);
                    if(small!=bitmap)small.recycle();bitmap.recycle();
                    future.complete(new Frame(bytes.toByteArray(),width,height,capturedAt));
                }catch(Exception e){future.completeExceptionally(e);}
            }
            @Override public void onFailure(int code){future.completeExceptionally(new IllegalStateException("Screenshot unavailable"));}
        });return future;
    }
    public CompletableFuture<Boolean> execute(String action,Frame frame,Protocol.Policy policy,Map<String,Protocol.Point> points,long generation){
        CompletableFuture<Boolean> future=new CompletableFuture<>();
        new Handler(Looper.getMainLooper()).post(()->{
            if(!Protocol.applicable(foreground(),frame.capturedAt(),android.os.SystemClock.elapsedRealtime(),generation,SessionController.generation(),action)){
                future.complete(false);return;
            }
            if(action.equals("wait")||action.equals("stop")){future.complete(true);return;}
            GestureDescription gesture;
            try{var builder=new GestureDescription.Builder();for(var stroke:GesturePlan.create(action,frame.width(),frame.height(),policy,points)){
                Path path=new Path();path.moveTo(stroke.fromX()*frame.width(),stroke.fromY()*frame.height());
                if(stroke.fromX()!=stroke.toX()||stroke.fromY()!=stroke.toY())path.lineTo(stroke.toX()*frame.width(),stroke.toY()*frame.height());
                builder.addStroke(new GestureDescription.StrokeDescription(path,0,stroke.millis()));
            }gesture=builder.build();}catch(RuntimeException error){future.complete(false);return;}
            gestures.incrementAndGet();var settled=new java.util.concurrent.atomic.AtomicBoolean();
            java.util.function.Consumer<Boolean> finish=ok->{if(settled.compareAndSet(false,true)){gestures.decrementAndGet();future.complete(ok);}};
            boolean accepted;
            try{accepted=dispatchGesture(gesture,new GestureResultCallback(){
                @Override public void onCompleted(GestureDescription g){finish.accept(true);}
                @Override public void onCancelled(GestureDescription g){finish.accept(false);}
            },null);
            }catch(RuntimeException error){finish.accept(false);return;}
            if(!accepted)finish.accept(false);
        });return future;
    }
}
