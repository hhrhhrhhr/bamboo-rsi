package dev.bamboo.rsi;
import org.junit.Test;
import java.util.Map;
import static org.junit.Assert.*;
public class GesturePlanTest {
    private Map<String,Protocol.Point> points(){return Map.of("joystick",new Protocol.Point(.18f,.76f),"attack",new Protocol.Point(.88f,.76f),"skill_1",new Protocol.Point(.72f,.82f),"skill_2",new Protocol.Point(.79f,.64f),"skill_3",new Protocol.Point(.9f,.52f));}
    @Test public void modelCatalogueExactlyMatchesExecutor(){assertEquals(Protocol.ACTIONS,DecisionClient.LABELS.keySet());for(String action:Protocol.ACTIONS){var strokes=GesturePlan.create(action,2400,1080,new Protocol.Policy(500,1000,300),points());assertTrue(strokes.size()<=2);for(var s:strokes){assertTrue(s.millis()<=500);assertTrue(s.toX()>=.02f&&s.toX()<=.98f);assertTrue(s.toY()>=.02f&&s.toY()<=.98f);}}}
    @Test public void moveAndAttackHaveIndependentTouchOrigins(){var strokes=GesturePlan.create("move_left_attack",2400,1080,new Protocol.Policy(250,1000,300),points());assertEquals(2,strokes.size());assertTrue(strokes.get(0).toX()<strokes.get(0).fromX());assertEquals(.88f,strokes.get(1).fromX(),.0001f);}
    @Test public void aimingMovesOnlySelectedSkillAndRejectsArbitraryCommands(){var strokes=GesturePlan.create("skill_2_aim_up",2400,1080,new Protocol.Policy(250,1000,300),points());assertEquals(1,strokes.size());assertEquals(.79f,strokes.get(0).fromX(),.0001f);assertTrue(strokes.get(0).toY()<strokes.get(0).fromY());assertThrows(IllegalArgumentException.class,()->GesturePlan.create("tap_system_settings",2400,1080,new Protocol.Policy(250,1000,300),points()));}
}
