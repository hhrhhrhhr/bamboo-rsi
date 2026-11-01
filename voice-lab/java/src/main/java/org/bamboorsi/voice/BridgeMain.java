package org.bamboorsi.voice;

import cn.xinfei.aichat.core.biz.playbook.handler.*;
import cn.xinfei.aichat.core.service.playbook.domain.event.DomainEvents;
import cn.xinfei.aichat.core.service.playbook.domain.model.*;
import cn.xinfei.aichat.core.service.playbook.domain.model.valueobject.*;
import cn.xinfei.aichat.core.service.playbook.domain.service.VariableReplacer;
import cn.xinfei.aichat.core.service.playbook.domain.service.VoiceSliceBuilder;
import cn.xinfei.aichat.core.service.playbook.infrastructure.cache.HookCacheService;
import cn.xinfei.aichat.core.service.playbook.infrastructure.client.algo.AlgoServiceClient;
import cn.xinfei.aichat.core.service.playbook.infrastructure.client.algo.model.*;
import cn.xinfei.aichat.core.service.playbook.infrastructure.client.llm.LLMClient;
import cn.xinfei.aichat.core.service.playbook.infrastructure.config.PlaybookLlmConfig;
import com.alibaba.fastjson.JSON;
import com.alibaba.fastjson.JSONArray;
import com.alibaba.fastjson.JSONObject;
import java.io.*;
import java.net.URI;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.time.*;
import java.util.*;

/** Isolated harness for the actual upstream Playbook chain, not a Spring deployment.
 * No database, Redis, corporate algorithm service, telephony, or account is opened.
 * All requests arrive on stdin, all replies on stdout. SLF4J logs remain on stderr.
 */
public final class BridgeMain {
    private final URI endpoint;
    private Session session;
    private ExecutionGraph graph;
    private PlaybookExecutionChain chain;
    private String version;
    private boolean ended;
    private LocalChatClient client;

    public BridgeMain(String endpoint) {
        this.endpoint=URI.create(endpoint);
        if (!Set.of("127.0.0.1","localhost","::1").contains(this.endpoint.getHost()))
            throw new IllegalArgumentException("This lab only permits a local language server");
    }

    private JSONObject start(JSONObject request) {
        JSONObject policy=request.getJSONObject("policy");
        version=policy.getString("version");
        if(version==null || version.isBlank())throw new IllegalArgumentException("version required");
        String callId=request.getString("callId");
        if(callId==null || !callId.matches("[A-Za-z0-9_-]{1,120}"))throw new IllegalArgumentException("invalid synthetic call id");
        PlaybookLlmConfig config=new PlaybookLlmConfig();
        // Do not use or initialize any upstream embedded credentials or defaults.
        config.setApiKey("");config.setBaseUrl(endpoint.toString());config.setModel("local-voice-lab");
        config.setPrePrompt(policy.getString("system_prompt"));
        config.setGlobalUtter("这次先到这里，您可以通过官方客服继续咨询。");
        List<UtteranceNode> nodes=new ArrayList<>();
        for(Object raw:policy.getJSONArray("utterances")) {
            JSONObject u=(JSONObject)raw;
            nodes.add(UtteranceNode.builder().utterId(u.getString("id")).playbookId("S1")
                .groupName("客户服务").sceneDesc(u.getString("description")).textTemplate(u.getString("text"))
                .nextStep("S1").handoff(u.getString("handoff")).variables(Set.of()).build());
        }
        if(nodes.isEmpty())throw new IllegalArgumentException("utterances required");
        PlaybookNode book=PlaybookNode.builder().playbookId("S1").name("离线语音客服")
            .compiledPrompt(policy.getString("prompt")+"\n仅输出JSON：{\"output\":\"要说的一句话\",\"reference_script_id\":\"所选话术编号\"}。话术编号必须来自下面提供的列表。/no_think")
            .orderNo(1).requiredVariables(Set.of()).voicemailSwitch("OFF")
            .riskQuestionSwitch("ON").riskReplySwitch("ON").eqReplySwitch("ON").build();
        graph=ExecutionGraph.builder().chatbotId("synthetic-voice-lab").playbookVersion(version)
            .utterType("dialogue").compiledAt(LocalDateTime.now()).playbookNodes(Map.of("S1",book))
            .utteranceNodes(Map.of("S1",Map.of("客户服务",nodes)))
            .globalConfig(GlobalConfigNode.builder().riskQReply("这部分需要人工核实。")
                .riskAReply("这部分需要人工核实。").build()).build();
        graph.buildUtterIdIndex();
        session=Session.builder().sessionId(callId).chatbotId(graph.getChatbotId()).currentStep("S1")
            .callStartTime(LocalDateTime.now()).createdAt(LocalDateTime.now()).updatedAt(LocalDateTime.now())
            .history(ConversationHistory.empty()).variables(VariableStore.empty()).build();
        LocalAlgo algo=new LocalAlgo();
        client=new LocalChatClient(config,endpoint);
        chain=new PlaybookExecutionChain(new PreCheckHandler(algo),
            new PromptBuildHandler(new VariableReplacer(),algo,config,new HookCacheService(null,null)),
            new LLMCallHandler(client,config),new PostCheckHandler(algo));
        ended=false;
        return object("status","started","callId",callId,"policyVersion",version,
            "runtime","original_aichatcore_playbook_chain","storage","isolated_memory",
            "corporate_spring_app",false,"telephony",false);
    }

    private JSONObject turn(JSONObject request) {
        if(session==null || ended)throw new IllegalStateException("No open session");
        if(!Objects.equals(request.getString("callId"),session.getSessionId()) || !Objects.equals(request.getString("policyVersion"),version))
            throw new IllegalArgumentException("Session/version mismatch");
        String query=request.getString("asrText");
        if(query==null || query.isBlank() || query.length()>1600)throw new IllegalArgumentException("Non-empty ASR transcript required");
        if(session.getRoundCount()>=8)throw new IllegalStateException("Session turn budget exhausted");
        client.seed=request.getIntValue("seed");
        long start=System.nanoTime();
        ExecutionContext context=ExecutionContext.builder().chatbotId(graph.getChatbotId())
            .currentStep(session.getCurrentStep()).query(query).variables(Map.of()).metadata(Map.of())
            .session(session).executionGraph(graph).startTime(System.currentTimeMillis()).build();
        PlaybookExecutionChain.ExecutionResult result=chain.execute(context);
        if(!result.isSuccess())throw new IllegalStateException("Original chain stopped: "+result.getStopResult().getStopReason());
        StructuredOutput out=context.getLlmOutput();
        if(out==null || out.getOutput()==null || out.getOutput().isBlank())throw new IllegalStateException("Empty original chain output");
        List<VoiceSlice> slices=new VoiceSliceBuilder().build(out.getOutput());
        session.addMessage(Message.user(query));session.addMessage(Message.assistant(out.getOutput()));
        session.progressTo(out.getNextStep());
        ended=Set.of("end","human").contains(out.getHandoff());
        return object("status","ok","callId",session.getSessionId(),"policyVersion",version,
            "round",session.getRoundCount(),"heardText",query,"output",out.getOutput(),
            "reference_script_id",out.getUtterId(),"handoff",out.getHandoff(),"ended",ended,
            "voiceSlices",slices,"latencyMs",(System.nanoTime()-start)/1_000_000.0,
            "modelRequest",JSON.parseObject(context.getLlmInputJson()),
            "modelResponse",JSON.parseObject(context.getLlmOutputJson()),
            "preCheck",context.getPreCheckResult(),"postCheck",context.getPostCheckResult(),
            "executionChain","PreCheckHandler > PromptBuildHandler > LLMCallHandler > PostCheckHandler",
            "historySize",session.getHistory().getAll().size(),"algorithm_service","local_rules_not_corporate_service");
    }

    public JSONObject handle(JSONObject request) {
        return switch(request.getString("action")) {
            case "start" -> start(request);
            case "turn" -> turn(request);
            case "stop" -> {ended=true;yield object("status","stopped");}
            default -> throw new IllegalArgumentException("Unknown action");
        };
    }

    private static JSONObject object(Object... pairs) {
        JSONObject value=new JSONObject(true);
        for(int i=0;i<pairs.length;i+=2)value.put((String)pairs[i],pairs[i+1]);
        return value;
    }

    private static final class LocalChatClient extends LLMClient {
        private final URI endpoint;
        private final HttpClient http=HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).followRedirects(HttpClient.Redirect.NEVER).build();
        int seed;
        LocalChatClient(PlaybookLlmConfig c,URI endpoint){super(c);this.endpoint=endpoint;}
        @Override public void init(){throw new IllegalStateException("Corporate client initialization is disabled in this harness");}
        @Override public LLMResponse chatCompletion(List<Message> messages)throws Exception {
            List<Map<String,String>> rows=messages.stream().map(m->Map.of("role",m.getRole(),"content",m.getContent())).toList();
            JSONObject payload=object("model","local-voice-lab","messages",rows,"temperature",0.2,"seed",seed,
                "max_tokens",240,"response_format",Map.of("type","json_object"),
                "chat_template_kwargs",Map.of("enable_thinking",false));
            String raw=payload.toJSONString();long start=System.nanoTime();
            HttpRequest req=HttpRequest.newBuilder(endpoint).timeout(Duration.ofSeconds(45))
                .header("Content-Type","application/json").POST(HttpRequest.BodyPublishers.ofString(raw,StandardCharsets.UTF_8)).build();
            HttpResponse<String> reply=http.send(req,HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
            if(reply.statusCode()!=200)throw new IOException("Local language HTTP "+reply.statusCode());
            JSONObject parsed=JSON.parseObject(reply.body());
            JSONObject choice=parsed.getJSONArray("choices").getJSONObject(0);
            if(!"stop".equals(choice.getString("finish_reason")))throw new IOException("Incomplete model response");
            return LLMResponse.builder().content(choice.getJSONObject("message").getString("content"))
                .requestJson(raw).responseJson(reply.body()).latencyMs((int)((System.nanoTime()-start)/1_000_000)).build();
        }
    }

    /** Concrete local algorithm adapter; intentionally separate from enterprise checks. */
    private static final class LocalAlgo extends AlgoServiceClient {
        LocalAlgo(){super(null,null);}
        @Override public PreCheckResponse preCheck(PreCheckRequest request) {
            return JSON.parseObject("{\"code\":\"0\",\"data\":{\"output\":{\"mail\":{\"action\":\"pass\"},\"riskPrev\":{\"action\":\"pass\"}}}}",PreCheckResponse.class);
        }
        @Override public PromptResponse buildPrompt(PromptRequest request) {
            var c=request.getContext();
            return PromptResponse.builder().code("0").data(PromptResponse.ResponseData.builder()
                .modelName("local-passthrough").output(PromptResponse.Output.builder().systemPrompt(c.getBasePrompt())
                    .query(c.getQuery()).history(c.getHistory()).build()).build()).build();
        }
        @Override public PostCheckResponse postCheck(PostCheckRequest request) {
            String output=request.getContext().getQuery().getText();
            String action=output.matches("(?s).*(坐牢|曝光你|威胁|告诉你家人|提供验证码|告诉我密码).*")?"stop":"pass";
            return JSON.parseObject("{\"code\":\"0\",\"data\":{\"output\":{\"riskPost\":{\"action\":\""+action+"\"},\"deviationPost\":{\"action\":\"pass\"}}}}",PostCheckResponse.class);
        }
    }

    public static void main(String[] args)throws Exception {
        System.setProperty("org.slf4j.simpleLogger.defaultLogLevel","error");
        if(args.length!=1)throw new IllegalArgumentException("Local completion endpoint required");
        BridgeMain app=new BridgeMain(args[0]);
        try(BufferedReader reader=new BufferedReader(new InputStreamReader(System.in,StandardCharsets.UTF_8));
            PrintWriter writer=new PrintWriter(new OutputStreamWriter(System.out,StandardCharsets.UTF_8),true)) {
            String line;
            while((line=reader.readLine())!=null) {
                try{writer.println(app.handle(JSON.parseObject(line)).toJSONString());}
                catch(Exception e){writer.println(object("status","failed","errorType",e.getClass().getSimpleName(),"detail",e.getMessage()).toJSONString());}
            }
        }
    }
}
