package dev.bamboo.rsi;

import java.util.List;
import java.util.Locale;

/** Conservative result-banner detector. Never uses Decision's answer as ground truth. */
public final class ResultGate {
    public record Word(String text,float centerX,float centerY,float height) {}
    private Protocol.Outcome previous=Protocol.Outcome.UNKNOWN;
    private long previousAt=-1;
    public static Protocol.Outcome classify(List<Word> words) {
        boolean win=false,loss=false;
        for(Word word:words){
            if(!Float.isFinite(word.centerX)||!Float.isFinite(word.centerY)||!Float.isFinite(word.height)||word.centerX<.25f||word.centerX>.75f||word.centerY<.1f||word.centerY>.65f||word.height<.045f)continue;
            String text=word.text.replaceAll("\\s+","").toUpperCase(Locale.ROOT);
            win|=text.equals("胜利")||text.equals("VICTORY");loss|=text.equals("失败")||text.equals("DEFEAT");
        }
        return win==loss?Protocol.Outcome.UNKNOWN:win?Protocol.Outcome.WIN:Protocol.Outcome.LOSS;
    }
    public Protocol.Outcome observe(List<Word> words,long capturedAt,long elapsed,boolean battleObserved) {
        Protocol.Outcome result=classify(words);boolean confirmed=battleObserved&&elapsed>=10000&&result!=Protocol.Outcome.UNKNOWN&&result==previous&&capturedAt-previousAt>=500&&capturedAt-previousAt<=5000;
        previous=result;previousAt=capturedAt;return confirmed?result:Protocol.Outcome.UNKNOWN;
    }
}
