using Xunit;
namespace StartLux.Desktop.Tests;
public class VoiceIntentTests
{
    [Theory]
    [InlineData("开始 每日 任务", "start")]
    [InlineData("做每日任务并领取原石", "start")]
    [InlineData("完成每日委托并领取奖励。", "start")]
    [InlineData("停止！", "stop")]
    [InlineData("停止 接管", "stop")]
    [InlineData("不要开始每日任务", "unknown")]
    [InlineData("别停止", "unknown")]
    [InlineData("帮我充值原石", "unknown")]
    [InlineData("开始任务然后购买体力", "unknown")]
    [InlineData("", "unknown")]
    public void OnlyExplicitSupportedIntentsExecute(string text, string expected) => Assert.Equal(expected, VoiceIntent.Parse(text));
}
