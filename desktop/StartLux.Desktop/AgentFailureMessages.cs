namespace StartLux.Desktop;

public static class AgentFailureMessages
{
    public static string Describe(string? code) => code switch
    {
        "account_mismatch" => "游戏 UID 与设置不一致，请核对当前账号和 UID",
        "game_not_ready" => "等待游戏主界面超时，请切回游戏并完成登录",
        "bridge_mismatch" => "BetterGI 任务包或模型不匹配，请重新启动助手",
        "bridge_unavailable" => "无法连接 BetterGI，请检查启动状态和管理员权限",
        "prior_execution_unresolved" => "上次执行未确认释放输入，已暂停并保留记录；需要先核对执行器停止状态",
        "host_not_acknowledged" => "执行器未确认启动，请重新启动助手",
        "assets_missing" => "每日任务执行资源缺失，请重新安装或构建客户端",
        "model_unconfirmed" => "请确认要使用的 StartLux 模型部署 ID",
        "model_invalid" => "StartLux 模型部署 ID 无效，请填写真实 API 模型名称",
        _ => "任务发生异常，请查看本次运行记录"
    };
}
