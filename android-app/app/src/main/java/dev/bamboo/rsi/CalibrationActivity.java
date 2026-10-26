package dev.bamboo.rsi;

import android.app.Activity;
import android.content.Intent;
import android.graphics.*;
import android.os.Bundle;
import android.view.MotionEvent;
import android.view.View;
import android.widget.Toast;

/** Tap five control centers on an imported full-display game screenshot. */
public final class CalibrationActivity extends Activity {
    private Bitmap bitmap;
    @Override public void onCreate(Bundle state){super.onCreate(state);getWindow().addFlags(android.view.WindowManager.LayoutParams.FLAG_SECURE);
        startActivityForResult(new Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("image/*"),81);
    }
    @Override protected void onActivityResult(int request,int result,Intent data){super.onActivityResult(request,result,data);
        if(request!=81||result!=RESULT_OK||data==null){finish();return;}
        try{
            BitmapFactory.Options options=new BitmapFactory.Options();options.inJustDecodeBounds=true;
            try(var input=getContentResolver().openInputStream(data.getData())){BitmapFactory.decodeStream(input,null,options);}
            if(options.outWidth<=options.outHeight||options.outWidth>8192||options.outHeight<200)throw new IllegalArgumentException("Full landscape screenshot required");
            options.inJustDecodeBounds=false;options.inSampleSize=Math.max(1,options.outWidth/2048);
            try(var input=getContentResolver().openInputStream(data.getData())){bitmap=BitmapFactory.decodeStream(input,null,options);}if(bitmap==null)throw new IllegalArgumentException();
            setContentView(new CanvasView());
        }catch(Exception error){Toast.makeText(this,"请选择完整游戏横屏截图，不要裁剪",Toast.LENGTH_LONG).show();finish();}
    }
    private final class CanvasView extends View {
        private final String[] controls={"joystick","attack","skill_1","skill_2","skill_3"};
        private final String[] labels={"左侧摇杆中心","普通攻击","第一技能","第二技能","第三技能"};
        private final Protocol.Point[] points=new Protocol.Point[5];private int index;
        private final Paint paint=new Paint(Paint.ANTI_ALIAS_FLAG);private final RectF area=new RectF();
        CanvasView(){super(CalibrationActivity.this);setContentDescription("点选五个游戏按键中心，按返回取消");}
        @Override protected void onDraw(Canvas canvas){super.onDraw(canvas);canvas.drawColor(Color.BLACK);
            float scale=Math.min(getWidth()/(float)bitmap.getWidth(),(getHeight()-80f)/bitmap.getHeight());float w=bitmap.getWidth()*scale,h=bitmap.getHeight()*scale;
            area.set((getWidth()-w)/2,80+(getHeight()-80-h)/2,(getWidth()+w)/2,80+(getHeight()-80+h)/2);canvas.drawBitmap(bitmap,null,area,paint);
            paint.setColor(Color.WHITE);paint.setTextSize(30);canvas.drawText("点选 "+labels[Math.min(index,4)]+"（"+(index+1)+"/5）；返回可重来",16,48,paint);
            paint.setColor(Color.CYAN);for(var point:points)if(point!=null)canvas.drawCircle(area.left+point.x()*area.width(),area.top+point.y()*area.height(),12,paint);
        }
        @Override public boolean onTouchEvent(MotionEvent event){if(event.getAction()!=MotionEvent.ACTION_UP)return true;performClick();if(index>=5||!area.contains(event.getX(),event.getY()))return true;
            try{points[index]=new Protocol.Point((event.getX()-area.left)/area.width(),(event.getY()-area.top)/area.height());index++;invalidate();
                if(index==5){var edit=getSharedPreferences("config",0).edit();for(int i=0;i<5;i++)edit.putString(controls[i],points[i].x()+","+points[i].y());
                    edit.putFloat("calibration_aspect",bitmap.getWidth()/(float)bitmap.getHeight());if(!edit.commit())throw new IllegalStateException();setResult(RESULT_OK);finish();}
            }catch(Exception error){Toast.makeText(CalibrationActivity.this,"坐标未保存，请重新校准",Toast.LENGTH_LONG).show();finish();}return true;
        }
        @Override public boolean performClick(){super.performClick();return true;}
    }
    @Override protected void onDestroy(){super.onDestroy();if(bitmap!=null)bitmap.recycle();}
}
