package dev.bamboo.rsi;

import android.app.*;
import android.content.*;
import android.content.pm.ServiceInfo;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.media.MediaMetadataRetriever;
import android.media.MediaRecorder;
import android.media.projection.MediaProjection;
import android.media.projection.MediaProjectionManager;
import android.os.*;
import org.json.JSONObject;
import java.io.File;
import java.io.FileInputStream;
import java.security.MessageDigest;
import java.util.LinkedHashMap;

public final class CaptureService extends Service {
    public static volatile CaptureService instance;
    public volatile boolean recording;
    private boolean closing;
    private Evidence evidence;
    private MediaProjection projection;
    private VirtualDisplay display;
    private MediaRecorder recorder;
    private File video;
    private final Handler handler=new Handler(Looper.getMainLooper());
    @Override public IBinder onBind(Intent intent){return null;}
    @Override public int onStartCommand(Intent intent,int flags,int id){
        if(intent==null){stopSelf();return START_NOT_STICKY;}
        if("stop".equals(intent.getAction())){SessionController.stopCurrent("user_stopped");stopSelf();return START_NOT_STICKY;}
        if(instance!=null&&instance!=this){stopSelf();return START_NOT_STICKY;}
        if(instance==this)return START_NOT_STICKY;
        instance=this;
        NotificationManager notifications=getSystemService(NotificationManager.class);
        notifications.createNotificationChannel(new NotificationChannel("session","游戏实验录制",NotificationManager.IMPORTANCE_LOW));
        PendingIntent stop=PendingIntent.getService(this,0,new Intent(this,CaptureService.class).setAction("stop"),PendingIntent.FLAG_IMMUTABLE);
        Notification notification=new Notification.Builder(this,"session").setSmallIcon(android.R.drawable.ic_media_play)
            .setContentTitle("Bamboo RSI 实验运行中").setContentText("仅操作已进入的测试局；点停止结束")
            .setOngoing(true).addAction(new Notification.Action.Builder(null,"停止",stop).build()).build();
        startForeground(1,notification,ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION);
        try {
            evidence=new Evidence(this);
            int code=intent.getIntExtra("result_code",0);Intent token=intent.getParcelableExtra("result_data");
            projection=getSystemService(MediaProjectionManager.class).getMediaProjection(code,token);
            projection.registerCallback(new MediaProjection.Callback(){@Override public void onStop(){if(!closing){SessionController.stopCurrent("projection_stopped");stopSelf();}}},handler);
            waitForGame(SystemClock.elapsedRealtime()+20_000);
        }catch(Exception error){failed(error,"录制启动失败");}
        return START_NOT_STICKY;
    }
    private void waitForGame(long deadline){
        if(closing)return;
        GameAccessibility access=GameAccessibility.instance;
        if(access!=null&&Protocol.GAME_PACKAGE.equals(access.foreground())){
            try{beginRecording();beginController();}catch(Exception error){failed(error,"启动失败");}
        }else if(SystemClock.elapsedRealtime()<deadline)handler.postDelayed(()->waitForGame(deadline),200);
        else{SessionController.status="未检测到游戏前台，已停止";finishRecording();try{evidence.end("game_not_foreground",Protocol.Outcome.UNKNOWN);}catch(Exception ignored){}stopSelf();}
    }
    private void failed(Exception error,String label){
        SessionController.status=label+"："+error.getClass().getSimpleName();
        finishRecording();
        try{if(evidence!=null){evidence.event("error",new JSONObject().put("error_type",error.getClass().getSimpleName()));evidence.end("startup_failed",Protocol.Outcome.UNKNOWN);}}catch(Exception ignored){}
        stopSelf();
    }
    private void beginRecording() throws Exception {
        if(evidence.directory.getUsableSpace()<1_000_000_000L)throw new IllegalStateException("Recording space unavailable");
        var metrics=getResources().getDisplayMetrics();float scale=Math.min(1f,1280f/Math.max(metrics.widthPixels,metrics.heightPixels));
        int width=(int)(metrics.widthPixels*scale)/2*2,height=(int)(metrics.heightPixels*scale)/2*2;
        if(width<=height)throw new IllegalStateException("Landscape required");
        video=new File(evidence.directory,"game.mp4");recorder=new MediaRecorder();
        recorder.setVideoSource(MediaRecorder.VideoSource.SURFACE);recorder.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4);
        recorder.setVideoEncoder(MediaRecorder.VideoEncoder.H264);recorder.setVideoSize(width,height);recorder.setVideoFrameRate(20);
        recorder.setVideoEncodingBitRate(2_000_000);recorder.setOutputFile(video.getAbsolutePath());
        recorder.setOnErrorListener((r,what,extra)->{SessionController.stopCurrent("recording_error");stopSelf();});
        recorder.prepare();display=projection.createVirtualDisplay("bamboo-game-evidence",width,height,metrics.densityDpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,recorder.getSurface(),null,null);
        recorder.start();recording=true;
        evidence.event("video_started",new JSONObject().put("source","default_display_with_game_foreground_guard").put("microphone",false));
    }
    private void beginController() throws Exception {
        var prefs=getSharedPreferences("config",0);
        DecisionClient client=new DecisionClient(prefs.getString("endpoint","https://api.bamboonode.dev/v1/decisions"),
            prefs.getString("model","startlux-decision-27b-autothink"),ProviderVault.read(this,prefs.getString("endpoint","https://api.bamboonode.dev/v1/decisions")));
        var points=new LinkedHashMap<String,Protocol.Point>();
        for(String name:new String[]{"joystick","attack","skill_1","skill_2","skill_3"}){
            String[] xy=prefs.getString(name,"").split(",");
            if(xy.length!=2)throw new IllegalStateException("Calibration required");
            points.put(name,new Protocol.Point(Float.parseFloat(xy[0]),Float.parseFloat(xy[1])));
        }
        SessionController controller=new SessionController(this,evidence,client,points);
        new Thread(controller,"bamboo-decision-session").start();
    }
    public synchronized void finishRecording(){
        if(closing)return;closing=true;recording=false;handler.removeCallbacksAndMessages(null);
        boolean complete=false;
        try{if(recorder!=null){try{recorder.stop();}finally{recorder.release();recorder=null;}}
            if(video!=null&&video.isFile())try(MediaMetadataRetriever reader=new MediaMetadataRetriever()){
                reader.setDataSource(video.getAbsolutePath());String duration=reader.extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION);
                var bitmap=reader.getFrameAtTime(0);complete=duration!=null&&Long.parseLong(duration)>0&&bitmap!=null;if(bitmap!=null)bitmap.recycle();
            }
        }catch(Exception ignored){}
        finally{if(display!=null){display.release();display=null;}if(projection!=null){projection.stop();projection=null;}}
        try{
            JSONObject body=new JSONObject().put("complete",complete).put("microphone",false).put("source","foreground_guarded_default_display");
            if(video!=null&&video.isFile()){
                MessageDigest hash=MessageDigest.getInstance("SHA-256");try(FileInputStream input=new FileInputStream(video)){byte[] b=new byte[65536];int n;while((n=input.read(b))!=-1)hash.update(b,0,n);}
                StringBuilder hex=new StringBuilder();for(byte b:hash.digest())hex.append(String.format("%02x",b&255));body.put("path",video.getName()).put("bytes",video.length()).put("sha256",hex.toString());
            }
            if(evidence!=null)evidence.video(body);
        }catch(Exception ignored){}
    }
    @Override public void onDestroy(){SessionController.stopCurrent("capture_service_stopped");finishRecording();if(!SessionController.active())try{if(evidence!=null)evidence.end("service_stopped",Protocol.Outcome.UNKNOWN);}catch(Exception ignored){}instance=null;super.onDestroy();}
}
