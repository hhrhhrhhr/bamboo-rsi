package dev.bamboo.rsi;

import android.app.*;
import android.content.*;
import android.media.projection.*;
import android.os.*;
import android.provider.Settings;
import android.text.InputType;
import android.view.ViewGroup;
import android.widget.*;
import java.util.LinkedHashMap;

public final class MainActivity extends Activity {
    private LinearLayout layout;
    private EditText endpoint,model,key;
    private CheckBox consent;
    private TextView status;
    private final LinkedHashMap<String,EditText> points=new LinkedHashMap<>();
    private final Handler handler=new Handler(Looper.getMainLooper());
    @Override public void onCreate(Bundle state){
        super.onCreate(state);getWindow().addFlags(android.view.WindowManager.LayoutParams.FLAG_SECURE);var scroll=new ScrollView(this);layout=new LinearLayout(this);layout.setOrientation(LinearLayout.VERTICAL);layout.setPadding(28,24,28,32);scroll.addView(layout);setContentView(scroll);
        text("Bamboo RSI · 安卓实验版",24);
        text("手机独立连接 Decision，提供有版本血缘的策略 RSI。用于训练场或获准测试局；尚未通过真机对战和商业发布验收。",16);
        var prefs=getSharedPreferences("config",0);
        endpoint=field("Decision 接口",prefs.getString("endpoint","https://api.bamboonode.dev/v1/decisions"));
        model=field("精确模型名称",prefs.getString("model","startlux-decision-27b-autothink"));
        key=field("API Key（仅本机加密保存；留空保留已有值）","");key.setInputType(InputType.TYPE_CLASS_TEXT|InputType.TYPE_TEXT_VARIATION_PASSWORD);
        text("横屏按键校准：填写按键中心的 x,y 比例，取值 0.02–0.98。需要按你的手机实际布局确认。",16);
        String[][] defaults={{"joystick","0.18,0.76"},{"attack","0.88,0.76"},{"skill_1","0.72,0.82"},{"skill_2","0.79,0.64"},{"skill_3","0.90,0.52"}};
        for(String[] row:defaults)points.put(row[0],field(row[0],prefs.getString(row[0],row[1])));
        button("保存配置",()->save());
        button("从游戏截图点选按键位置",()->{if(!SessionController.active())startActivityForResult(new Intent(this,CalibrationActivity.class),44);});
        button("RSI 候选、对照与证据导出",()->startActivity(new Intent(this,RsiActivity.class)));
        button("开启无障碍服务",()->new AlertDialog.Builder(this).setTitle("无障碍服务用途").setMessage("Bamboo RSI 将读取王者荣耀前台画面并按模型选择派发有限触摸手势。游戏截图会发送到你配置的模型接口。可随时在系统设置关闭服务。是否继续前往系统设置？").setNegativeButton("取消",null).setPositiveButton("继续",(d,w)->startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))).show());
        if(Build.VERSION.SDK_INT>=33)button("允许显示停止通知",()->requestPermissions(new String[]{android.Manifest.permission.POST_NOTIFICATIONS},43));
        text("运行时会把游戏截图发送到所填模型接口。录制整个屏幕，不录麦克风；仅在王者荣耀前台执行，离开游戏或停止后结束。证据留在应用本机目录。结算识别使用 Google ML Kit，在本机处理图像，SDK 会向 Google 发送性能和使用指标。",16);
        consent=new CheckBox(this);consent.setText("我已校准布局，并将进入训练场或获准测试对局；同意以上截图传输与录制");layout.addView(consent);
        button("启动录制与 Decision 演练",()->startExperiment());
        button("停止",()->{SessionController.stopCurrent("user_stopped");stopService(new Intent(this,CaptureService.class));});
        status=text("尚未运行",16);
        handler.post(new Runnable(){@Override public void run(){status.setText(SessionController.status);handler.postDelayed(this,1000);}});
    }
    private TextView text(String value,int size){TextView view=new TextView(this);view.setText(value);view.setTextSize(size);view.setPadding(0,10,0,8);layout.addView(view);return view;}
    private EditText field(String label,String value){text(label,14);EditText view=new EditText(this);view.setSingleLine(true);view.setText(value);layout.addView(view,new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,ViewGroup.LayoutParams.WRAP_CONTENT));return view;}
    private void button(String value,Runnable run){Button button=new Button(this);button.setText(value);button.setOnClickListener(v->run.run());layout.addView(button);}
    private boolean save(){
        if(SessionController.active()||CaptureService.instance!=null){Toast.makeText(this,"当前运行配置已冻结，请先停止",Toast.LENGTH_LONG).show();return false;}
        try{
            Protocol.endpoint(endpoint.getText().toString().trim());if(model.getText().toString().trim().isEmpty())throw new IllegalArgumentException();
            String oldEndpoint=getSharedPreferences("config",0).getString("endpoint","https://api.bamboonode.dev/v1/decisions");
            if(!oldEndpoint.equals(endpoint.getText().toString().trim())&&key.getText().toString().trim().isEmpty()){
                Toast.makeText(this,"接口变更后请重新填写 API Key",Toast.LENGTH_LONG).show();return false;
            }
            var edit=getSharedPreferences("config",0).edit().putString("endpoint",endpoint.getText().toString().trim()).putString("model",model.getText().toString().trim());
            for(var row:points.entrySet()){
                String value=row.getValue().getText().toString().trim();String[] xy=value.split(",");if(xy.length!=2)throw new IllegalArgumentException();
                new Protocol.Point(Float.parseFloat(xy[0]),Float.parseFloat(xy[1]));edit.putString(row.getKey(),value);
            }
            if(!key.getText().toString().trim().isEmpty()){ProviderVault.save(this,key.getText().toString().trim(),endpoint.getText().toString().trim());key.setText("");}
            if(!edit.commit())throw new IllegalStateException();Toast.makeText(this,"配置已保存在本机",Toast.LENGTH_SHORT).show();return true;
        }catch(Exception e){Toast.makeText(this,"请检查接口、模型名称和坐标格式",Toast.LENGTH_LONG).show();return false;}
    }
    private void startExperiment(){
        if(CaptureService.instance!=null){Toast.makeText(this,"实验已在运行",Toast.LENGTH_SHORT).show();return;}
        if(!consent.isChecked()||GameAccessibility.instance==null){Toast.makeText(this,"请先校准、确认用途并开启无障碍服务",Toast.LENGTH_LONG).show();return;}
        if(getPackageManager().getLaunchIntentForPackage(Protocol.GAME_PACKAGE)==null){Toast.makeText(this,"未发现国服王者荣耀客户端",Toast.LENGTH_LONG).show();return;}
        if(!save())return;
        try{ProviderVault.read(this,endpoint.getText().toString().trim());}catch(Exception e){Toast.makeText(this,"请先填写模型 API Key",Toast.LENGTH_LONG).show();return;}
        MediaProjectionManager manager=getSystemService(MediaProjectionManager.class);
        Intent capture=Build.VERSION.SDK_INT>=34?manager.createScreenCaptureIntent(MediaProjectionConfig.createConfigForDefaultDisplay()):manager.createScreenCaptureIntent();
        startActivityForResult(capture,42);
    }
    @Override protected void onActivityResult(int request,int result,Intent data){
        super.onActivityResult(request,result,data);
        if(request==44&&result==RESULT_OK){var prefs=getSharedPreferences("config",0);for(var point:points.entrySet())point.getValue().setText(prefs.getString(point.getKey(),""));}
        if(request==42&&result==RESULT_OK&&data!=null){
            Intent service=new Intent(this,CaptureService.class).putExtra("result_code",result).putExtra("result_data",data);startForegroundService(service);
            Intent game=getPackageManager().getLaunchIntentForPackage(Protocol.GAME_PACKAGE);if(game!=null)startActivity(game);
        }
    }
    @Override protected void onDestroy(){handler.removeCallbacksAndMessages(null);super.onDestroy();}
}
