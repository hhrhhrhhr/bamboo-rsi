package dev.bamboo.rsi;

import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Platform-independent invariants. Match termination is distinct from victory. */
public final class Protocol {
    public static final String GAME_PACKAGE = "com.tencent.tmgp.sgame";
    public static final Set<String> ACTIONS = actions();
    private static Set<String> actions(){
        var actions=new java.util.HashSet<>(Set.of("wait","stop","attack","skill_1","skill_2","skill_3","move_up_left","move_up_right","move_down_left","move_down_right"));
        for(String direction:List.of("up","down","left","right")){
            actions.add("move_"+direction);actions.add("move_"+direction+"_attack");
            for(int skill=1;skill<=3;skill++)actions.add("skill_"+skill+"_aim_"+direction);
        }return java.util.Collections.unmodifiableSet(actions);
    }
    public enum Outcome { UNKNOWN, WIN, LOSS }
    public record Policy(int moveMillis, int intervalMillis, int maxCalls) {
        public Policy {
            if (moveMillis < 100 || moveMillis > 500 || intervalMillis < 500 || intervalMillis > 5000 || maxCalls < 1 || maxCalls > 300) throw new IllegalArgumentException("Policy out of bounds");
        }
        public String identity() { return sha(moveMillis + ":" + intervalMillis + ":" + maxCalls); }
    }
    public record Point(float x, float y) {
        public Point { if (!Float.isFinite(x) || !Float.isFinite(y) || x < .02f || x > .98f || y < .02f || y > .98f) throw new IllegalArgumentException("Invalid calibration"); }
    }
    public static URI endpoint(String value) {
        URI uri = URI.create(value);
        if (!"https".equals(uri.getScheme()) || uri.getHost() == null || uri.getUserInfo() != null || uri.getQuery() != null || uri.getFragment() != null || !uri.getPath().endsWith("/v1/decisions")) throw new IllegalArgumentException("HTTPS Decisions endpoint required");
        return uri;
    }
    public static boolean applicable(String foreground, long capturedAt, long now, long generation, long currentGeneration, String action) {
        return GAME_PACKAGE.equals(foreground) && generation == currentGeneration && capturedAt <= now && now - capturedAt <= 2500 && ACTIONS.contains(action);
    }
    public static Outcome terminal(String phase, String result, String previousResult, long battleMillis) {
        if (!"result".equals(phase) || battleMillis < 10_000 || result == null || !result.equals(previousResult)) return Outcome.UNKNOWN;
        return "win".equals(result) ? Outcome.WIN : "loss".equals(result) ? Outcome.LOSS : Outcome.UNKNOWN;
    }
    public static String sha(String text) {
        return sha(text.getBytes(StandardCharsets.UTF_8));
    }
    public static String profileIdentity(Policy policy,String endpoint,String model,Map<String,Point> calibration) {
        StringBuilder value=new StringBuilder("bamboo-android-v1\n").append(policy.identity()).append('\n').append(endpoint).append('\n').append(model);
        new java.util.TreeMap<>(calibration).forEach((name,point)->value.append('\n').append(name).append(':').append(Float.toHexString(point.x)).append(',').append(Float.toHexString(point.y)));
        ACTIONS.stream().sorted().forEach(action->value.append('\n').append(action));
        return sha(value.toString());
    }
    public static String sha(byte[] value) {
        try { byte[] hash = MessageDigest.getInstance("SHA-256").digest(value); StringBuilder b = new StringBuilder(); for (byte v : hash) b.append(String.format("%02x", v & 255)); return b.toString(); }
        catch (Exception e) { throw new IllegalStateException(e); }
    }

    /** Diagnostic statistics only: external observations cannot submit a release. */
    public record Match(String policy, String opponent, boolean swapped, Outcome outcome, boolean assisted, boolean verified) {}
    public static Map<String,Object> compare(List<Match> parent, List<Match> candidate) {
        boolean admissible = parent.size() >= 100 && candidate.size() >= 100 &&
            java.util.stream.Stream.concat(parent.stream(), candidate.stream()).allMatch(m -> m.verified && !m.assisted && m.outcome != null && m.outcome != Outcome.UNKNOWN && m.opponent != null && !m.opponent.trim().isEmpty());
        Set<String> parents = parent.stream().map(Match::policy).collect(java.util.stream.Collectors.toSet());
        Set<String> candidates = candidate.stream().map(Match::policy).collect(java.util.stream.Collectors.toSet());
        admissible &= parents.size() == 1 && candidates.size() == 1 && !parents.equals(candidates)
            && java.util.stream.Stream.concat(parents.stream(), candidates.stream()).allMatch(p -> p != null && p.matches("[a-f0-9]{64}"));
        Set<String> opponents = parent.stream().map(Match::opponent).collect(java.util.stream.Collectors.toSet());
        admissible &= !opponents.isEmpty() && opponents.equals(candidate.stream().map(Match::opponent).collect(java.util.stream.Collectors.toSet()));
        for (String opponent : opponents) for (boolean side : new boolean[]{false,true}) {
            long before = parent.stream().filter(m -> java.util.Objects.equals(m.opponent,opponent) && m.swapped == side).count();
            long after = candidate.stream().filter(m -> java.util.Objects.equals(m.opponent,opponent) && m.swapped == side).count();
            admissible &= before > 0 && before == after;
        }
        double p = parent.isEmpty() ? 0 : (double)parent.stream().filter(m -> m.outcome == Outcome.WIN).count()/parent.size();
        double c = candidate.isEmpty() ? 0 : (double)candidate.stream().filter(m -> m.outcome == Outcome.WIN).count()/candidate.size();
        return Map.of("admissible", admissible, "parent_win_rate",p,"candidate_win_rate",c,
                "eligible_for_release",false,"note","Diagnostic only; frozen preregistration and trusted mobile enrollment are required");
    }
}
