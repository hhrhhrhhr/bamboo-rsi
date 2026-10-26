package dev.bamboo.rsi;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.widget.*;
import org.json.JSONObject;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.Executors;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;

/** Local operator console. Export is explicit and never includes the provider vault. */
public final class RsiActivity extends Activity {
    private LinearLayout layout;private TextView summary;private EditText authority;private Spinner runs;
    private final java.util.concurrent.ExecutorService worker=Executors.newSingleThreadExecutor();
    private String selectedRun;private interface Job {String run() throws Exception;}
    @Override public void onCreate(Bundle state){super.onCreate(state);getWindow().addFlags(android.view.WindowManager.LayoutParams.FLAG_SECURE);
        var scroll=new ScrollView(this);layout=new LinearLayout(this);layout.setPadding(24,20,24,24);layout.setOrientation(LinearLayout.VERTICAL);scroll.addView(layout);setContentView(scroll);
        summary=label("读取手机本地 RSI 状态…");
        label("固定 100 组配对实机对照、独立评测方签名和晋级门槛全部通过才允许晋级。当前没有内置腾讯对战平台接入或商业授权。");
        CheckBox auto=new CheckBox(this);auto.setText("每局结束后自动提出候选（每个 UTC 日最多 2 次）");auto.setChecked(getSharedPreferences("config",0).getBoolean("auto_optimize",true));
        auto.setOnCheckedChangeListener((v,checked)->getSharedPreferences("config",0).edit().putBoolean("auto_optimize",checked).apply());layout.addView(auto);
        button("基于开发失败记录生成候选",()->job(()->{MobileRsi.optimize(this);return "候选选择已记录；通过对照后才能替换父版本";}));
        label("独立授权评测方 P-256 公钥（Base64 X.509，不是模型 API Key）");authority=new EditText(this);authority.setHint("由独立评测服务提供");layout.addView(authority);
        button("登记完整 100 组配对对照",()->{String key=authority.getText().toString().trim();job(()->{DecisionClient client=MobileRsi.client(this);try{return "已登记："+MobileRsi.open(this).register(MobileRsi.contextHash(this,client,MobileRsi.points(this)),key,System.currentTimeMillis());}finally{client.cancel();}});});
        button("导入独立评测签名结果",()->startActivityForResult(new Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("application/json"),71));
        button("拒绝当前候选并保留所有记录",()->job(()->{MobileRsi.open(this).reject("operator_rejected",System.currentTimeMillis());return "已拒绝，记录保留";}));
        button("回滚到父版本",()->job(()->{MobileRsi.open(this).rollback(System.currentTimeMillis());return "已回滚，记录保留";}));
        label("导出包含游戏画面、账号信息和实验状态。仅保存到你选择的文件，不包含模型密钥。");runs=new Spinner(this);layout.addView(runs);
        button("导出所选运行的录像与证据 ZIP",()->{Object item=runs.getSelectedItem();if(item==null)return;selectedRun=item.toString();startActivityForResult(new Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("application/zip").putExtra(Intent.EXTRA_TITLE,"bamboo-"+selectedRun+".zip"),72);});
        button("刷新状态",()->job(()->"状态已刷新"));job(()->"就绪");
    }
    private TextView label(String text){var view=new TextView(this);view.setText(text);view.setTextSize(16);view.setPadding(0,10,0,10);layout.addView(view);return view;}
    private void button(String text,Runnable action){var button=new Button(this);button.setText(text);button.setOnClickListener(v->action.run());layout.addView(button);}
    private void job(Job action){
        if(SessionController.active()||CaptureService.instance!=null){Toast.makeText(this,"请等当前运行结束",Toast.LENGTH_LONG).show();return;}
        worker.execute(()->{String message;try{message=action.run();JSONObject state=MobileRsi.open(this).snapshot();
            String text="已接受策略："+state.getString("accepted")+"\n晋级次数："+state.getInt("generation")+"\n候选："+state.optString("candidate","无")+"\n";
            text+=state.has("trial")?"对照已预留 "+state.getJSONObject("trial").getInt("reserved")+" / 200 局；未收齐签名不评估胜率":"未登记进行中的对照";
            var root=getExternalFilesDir("runs");String[] names=root==null?new String[0]:root.list((dir,name)->new java.io.File(dir,name).isDirectory());if(names==null)names=new String[0];java.util.Arrays.sort(names,java.util.Collections.reverseOrder());
            String display=text;String[] choices=names;runOnUiThread(()->{summary.setText(display);runs.setAdapter(new ArrayAdapter<>(this,android.R.layout.simple_spinner_dropdown_item,choices));});
        }catch(Exception error){message="未执行："+error.getMessage();}String result=message;runOnUiThread(()->Toast.makeText(this,result,Toast.LENGTH_LONG).show());});
    }
    @Override protected void onActivityResult(int request,int result,Intent data){super.onActivityResult(request,result,data);if(result!=RESULT_OK||data==null||data.getData()==null)return;
        var uri=data.getData();if(request==71)job(()->{
            byte[] bytes;try(var input=getContentResolver().openInputStream(uri)){if(input==null)throw new IllegalStateException("Cannot open proof");var output=new java.io.ByteArrayOutputStream();byte[] buffer=new byte[4096];int n;while((n=input.read(buffer))!=-1){if(output.size()+n>524288)throw new IllegalArgumentException("Proof too large");output.write(buffer,0,n);}bytes=output.toByteArray();}
            JSONObject proof=new JSONObject(new String(bytes,StandardCharsets.UTF_8));String id=proof.getJSONObject("body").getString("run_id");if(!id.matches("[A-Za-z0-9._-]{1,100}"))throw new IllegalArgumentException("Invalid run identity");
            JSONObject local=MobileRsi.open(this).snapshot().getJSONObject("runs").getJSONObject(id).getJSONObject("local");var directory=new java.io.File(getExternalFilesDir("runs"),id);
            if(!local.getString("trace_sha256").equals(Evidence.fileHash(new java.io.File(directory,"trace.jsonl")))||!local.getString("video_sha256").equals(Evidence.fileHash(new java.io.File(directory,"game.mp4"))))throw new IllegalStateException("Raw evidence missing or changed");
            return MobileRsi.open(this).attest(proof,System.currentTimeMillis()).toString();
        });
        if(request==72){String id=selectedRun;job(()->{if(id==null||!id.matches("[A-Za-z0-9._-]{1,100}"))throw new IllegalArgumentException("Select a run again");
            var root=new java.io.File(getExternalFilesDir("runs"),id).getCanonicalFile();java.io.File[] files=root.listFiles();if(files==null)throw new IllegalStateException("Run directory unavailable");
            long bytes=0;for(var file:files){if(!file.isFile()||!file.getCanonicalFile().getParentFile().equals(root)||java.nio.file.Files.isSymbolicLink(file.toPath()))throw new IllegalStateException("Unexpected evidence entry");bytes=Math.addExact(bytes,file.length());}if(bytes>2_000_000_000L)throw new IllegalStateException("Export exceeds 2 GB limit");
            try(var output=getContentResolver().openOutputStream(uri,"w")){if(output==null)throw new IllegalStateException("Cannot open destination");try(var zip=new ZipOutputStream(output)){
                zip.putNextEntry(new ZipEntry("rsi-snapshot.json"));zip.write(MobileRsi.open(this).snapshot().toString(2).getBytes(StandardCharsets.UTF_8));zip.closeEntry();
                for(var file:files){zip.putNextEntry(new ZipEntry("run/"+file.getName()));try(var input=new java.io.FileInputStream(file)){byte[] buffer=new byte[65536];int n;while((n=input.read(buffer))!=-1)zip.write(buffer,0,n);}zip.closeEntry();}
            }}return "录像与证据已导出";});}
    }
    @Override protected void onSaveInstanceState(Bundle out){out.putString("selected_run",selectedRun);super.onSaveInstanceState(out);}
    @Override protected void onRestoreInstanceState(Bundle state){super.onRestoreInstanceState(state);selectedRun=state.getString("selected_run");}
    @Override protected void onDestroy(){worker.shutdown();super.onDestroy();}
}
