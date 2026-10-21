using System.Text.RegularExpressions;
namespace StartLux.Desktop;
public static class VoiceIntent
{
    public static string Parse(string text)
    {
        text = Regex.Replace(text, @"[\s，。！？,.!?]", "");
        if (text is "停止" or "停止接管" or "停止任务" or "暂停" or "取消任务") return "stop";
        if (text is "开始每日任务" or "开始每日委托" or "做今天的每日任务" or "做每日任务并领取原石"
            or "做今天每日任务并领取原石" or "完成每日委托并领取奖励" or "开始任务") return "start";
        return "unknown";
    }
}
