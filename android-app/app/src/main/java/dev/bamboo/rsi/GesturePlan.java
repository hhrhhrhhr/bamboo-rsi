package dev.bamboo.rsi;

import java.util.List;
import java.util.Map;

/** Pure bounded gesture geometry. No coordinates can come directly from model text. */
public final class GesturePlan {
    public record Stroke(float fromX,float fromY,float toX,float toY,int millis) {}
    public static List<Stroke> create(String action,int width,int height,Protocol.Policy policy,Map<String,Protocol.Point> points) {
        if(!Protocol.ACTIONS.contains(action)||width<=height||height<=0)throw new IllegalArgumentException("Unsupported action or display");
        if(action.equals("wait")||action.equals("stop"))return List.of();
        if(action.startsWith("move_")){
            boolean attack=action.endsWith("_attack");String direction=action.substring(5).replace("_attack","");
            float[] delta=direction(direction);var point=required(points,"joystick");
            Stroke move=stroke(point,delta[0]*.065f*height/width,delta[1]*.065f,policy.moveMillis());
            if(!attack)return List.of(move);
            var button=required(points,"attack");return List.of(move,stroke(button,0,0,80));
        }
        if(action.contains("_aim_")){
            String[] pieces=action.split("_aim_");var point=required(points,pieces[0]);float[] delta=direction(pieces[1]);
            return List.of(stroke(point,delta[0]*.12f*height/width,delta[1]*.12f,Math.min(350,policy.moveMillis())));
        }
        return List.of(stroke(required(points,action),0,0,80));
    }
    private static Protocol.Point required(Map<String,Protocol.Point> points,String name){var point=points.get(name);if(point==null)throw new IllegalArgumentException("Missing control calibration");return point;}
    private static Stroke stroke(Protocol.Point point,float dx,float dy,int millis){return new Stroke(point.x(),point.y(),Math.max(.02f,Math.min(.98f,point.x()+dx)),Math.max(.02f,Math.min(.98f,point.y()+dy)),millis);}
    private static float[] direction(String direction){return switch(direction){
        case "up"->new float[]{0,-1};case "down"->new float[]{0,1};case "left"->new float[]{-1,0};case "right"->new float[]{1,0};
        case "up_left"->new float[]{-.7071f,-.7071f};case "up_right"->new float[]{.7071f,-.7071f};case "down_left"->new float[]{-.7071f,.7071f};case "down_right"->new float[]{.7071f,.7071f};
        default->throw new IllegalArgumentException("Invalid direction");};}
}
