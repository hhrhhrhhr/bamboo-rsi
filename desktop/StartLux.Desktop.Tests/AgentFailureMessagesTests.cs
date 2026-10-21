using Xunit;
namespace StartLux.Desktop.Tests;

public class AgentFailureMessagesTests
{
    [Fact]
    public void AccountMismatchHasActionableInstructions() =>
        Assert.Contains("UID", AgentFailureMessages.Describe("account_mismatch"));

    [Fact]
    public void TimeoutExplainsThatTheGameMustBeReady() =>
        Assert.Contains("游戏主界面", AgentFailureMessages.Describe("game_not_ready"));

    [Fact]
    public void MissingBridgeDoesNotClaimThereWasAModelMismatch()
    {
        var message = AgentFailureMessages.Describe("bridge_unavailable");
        Assert.Contains("管理员权限", message);
        Assert.DoesNotContain("模型不匹配", message);
    }

    [Theory]
    [InlineData(null)]
    [InlineData("untrusted-provider-secret")]
    public void UnknownErrorsNeverEchoTheirInput(string? code) =>
        Assert.Equal("任务发生异常，请查看本次运行记录", AgentFailureMessages.Describe(code));
}
