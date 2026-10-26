package dev.bamboo.rsi;

import android.graphics.BitmapFactory;
import com.google.android.gms.tasks.Tasks;
import com.google.mlkit.vision.common.InputImage;
import com.google.mlkit.vision.text.TextRecognition;
import com.google.mlkit.vision.text.TextRecognizer;
import com.google.mlkit.vision.text.chinese.ChineseTextRecognizerOptions;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeUnit;

/** Bundled Chinese OCR model; no remote model can supply the match result. */
public final class LocalResultReader implements AutoCloseable {
    private final TextRecognizer reader=TextRecognition.getClient(new ChineseTextRecognizerOptions.Builder().build());
    public List<ResultGate.Word> read(byte[] png) throws Exception {
        var bitmap=BitmapFactory.decodeByteArray(png,0,png.length);if(bitmap==null)throw new IllegalStateException("Invalid frame");
        // Wait for ownership to return before recycling the bitmap, including on timeout.
        var task=reader.process(InputImage.fromBitmap(bitmap,0));
        try{
            var result=Tasks.await(task,5,TimeUnit.SECONDS);List<ResultGate.Word> words=new ArrayList<>();
            for(var block:result.getTextBlocks())for(var line:block.getLines()){
                var box=line.getBoundingBox();if(box!=null)words.add(new ResultGate.Word(line.getText(),box.exactCenterX()/bitmap.getWidth(),box.exactCenterY()/bitmap.getHeight(),box.height()/(float)bitmap.getHeight()));
            }return words;
        }finally{if(task.isComplete())bitmap.recycle();else task.addOnCompleteListener(t->bitmap.recycle());}
    }
    @Override public void close(){reader.close();}
}
