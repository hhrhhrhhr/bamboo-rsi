using System.Diagnostics;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Windows;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace StartLux.Desktop;

public partial class MainViewModel : ObservableObject
{
    private readonly string _home = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "StartLux", "Genshin");
    private readonly string _root = AppContext.BaseDirectory;
    private readonly string _pipe = "startlux-genshin-" + Environment.ProcessId;
    private Process? _worker, _host;
    private LocalVoice? _voice;
    private bool _closing;
    private bool _stopping;
    private bool _hostNeverArmed;
    public bool Closed { get; private set; }
    public string[] Servers { get; } = ["中国大陆", "亚洲 / 港澳台", "美洲", "欧洲"];
    [ObservableProperty] private string _gamePath = "";
    [ObservableProperty] private string _uid = "";
    [ObservableProperty] private string _server = "中国大陆";
    [ObservableProperty] private string _modelId = "startlux-decision-27b-autothink";
    [ObservableProperty] private bool _modelConfirmed;
    [ObservableProperty] private string _commandText = "做每日任务并领取原石";
    [ObservableProperty] private string _status = "就绪 · 尚未接管游戏";
    [ObservableProperty] private string _progress = "";
    [ObservableProperty] private string _voiceStatus = "麦克风未开启";
    [ObservableProperty] private string _voiceButtonText = "开启语音";

    public MainViewModel()
    {
        Directory.CreateDirectory(_home);
        Directory.CreateDirectory(Path.Combine(_home, "runs"));
        var file = Path.Combine(_home, "settings.json");
        if (!File.Exists(file)) return;
        try
        {
            var config = JObject.Parse(File.ReadAllText(file));
            GamePath = config.Value<string>("game_path") ?? ""; Uid = config.Value<string>("uid") ?? "";
            ModelId = config.Value<string>("model_id") ?? ModelId; Server = config.Value<string>("server_label") ?? Servers[0];
            ModelConfirmed = config.Value<bool?>("decision_model_confirmed")
                ?? config.Value<bool?>("top_ranked_deployment_confirmed") ?? false;
        }
        catch (Exception e) when (e is IOException or JsonException) { Status = "设置读取失败，请重新填写"; }
    }

    public void StoreCredential(string value)
    {
        if (string.IsNullOrWhiteSpace(value)) return; // Empty box preserves a previously saved credential.
        try
        {
            var encrypted = ProtectedData.Protect(Encoding.UTF8.GetBytes(value), null, DataProtectionScope.CurrentUser);
            File.WriteAllBytes(Path.Combine(_home, "credential.dpapi.tmp"), encrypted);
            File.Move(Path.Combine(_home, "credential.dpapi.tmp"), Path.Combine(_home, "credential.dpapi"), true);
        }
        catch (Exception e) when (e is IOException or CryptographicException or UnauthorizedAccessException)
        { Status = "密钥保存失败，请检查当前用户目录权限"; }
    }

    [RelayCommand]
    private async Task StartAsync()
    {
        if (_closing || _worker is { HasExited: false }) { Status = "任务已经在运行"; return; }
        if (VoiceIntent.Parse(CommandText) != "start") { Status = "当前支持：开始每日任务、停止"; return; }
        if (!File.Exists(GamePath) || Path.GetFileName(GamePath) is not ("YuanShen.exe" or "GenshinImpact.exe")
            || !Regex.IsMatch(Uid, "^[0-9]{8,10}$") || string.IsNullOrWhiteSpace(ModelId) || !ModelConfirmed)
        { Status = "请填写游戏路径、UID，并确认要使用的 StartLux 模型部署 ID"; return; }
        var credential = Path.Combine(_home, "credential.dpapi");
        if (!File.Exists(credential)) { Status = "请填写 Bamboo API Key"; return; }
        if (Process.GetProcessesByName("BetterGI").Any())
        { Status = "请先关闭已打开的 BetterGI，再启动接管，以免两个执行器同时操作游戏"; return; }
        var reportedError = false;
        try
        {
            _stopping = false;
            _hostNeverArmed = false;
            var index = Array.IndexOf(Servers, Server);
            var config = new JObject
            {
                ["uid"] = Uid, ["game_path"] = GamePath, ["model_id"] = ModelId.Trim(), ["server_label"] = Server,
                ["server_id"] = index switch { 2 => "america", 3 => "europe", 1 => "asia", _ => "cn" },
                ["server_utc_offset_minutes"] = index switch { 2 => -300, 3 => 60, _ => 480 },
                ["decision_model_confirmed"] = ModelConfirmed
            };
            var configPath = Path.Combine(_home, "settings.json");
            File.WriteAllText(configPath, config.ToString(), new UTF8Encoding(false));
            if (_host is { HasExited: false })
            {
                Status = "上一个执行器仍在退出，请稍后重试";
                return;
            }
            var executable = Path.Combine(_root, "agent", "genshin-agent.exe");
            var start = new ProcessStartInfo(executable) { UseShellExecute = false, CreateNoWindow = true,
                RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8, WorkingDirectory = _root };
            foreach (var arg in new[] { "run", "--config", configPath, "--pipe", _pipe }) start.ArgumentList.Add(arg);
            _worker = Process.Start(start) ?? throw new IOException();
            // Drain stderr without presenting unsanitized third-party messages.
            var stderr = _worker.StandardError.ReadToEndAsync();
            var secret = ProtectedData.Unprotect(File.ReadAllBytes(credential), null, DataProtectionScope.CurrentUser);
            await _worker.StandardInput.WriteLineAsync(Encoding.UTF8.GetString(secret));
            CryptographicOperations.ZeroMemory(secret);
            Status = "正在准备任务包和执行器"; Progress = "";
            while (await _worker.StandardOutput.ReadLineAsync() is { } line)
            {
                var message = JObject.Parse(line);
                var state = message.Value<string>("status");
                if (state == "pack_ready")
                {
                    // Launch only after the worker has frozen and hashed this run's pack.
                    var host = new ProcessStartInfo(Path.Combine(_root, "bettergi", "BetterGI.exe"))
                        { UseShellExecute = false, WorkingDirectory = Path.Combine(_root, "bettergi") };
                    host.Environment["STARTLUX_AGENT_CONFIG"] = message.Value<string>("bridge_config");
                    host.Environment["STARTLUX_AGENT_PIPE"] = _pipe;
                    _host = Process.Start(host) ?? throw new IOException();
                    if (!Process.GetProcessesByName(Path.GetFileNameWithoutExtension(GamePath)).Any())
                        Process.Start(new ProcessStartInfo(GamePath) { UseShellExecute = true, WorkingDirectory = Path.GetDirectoryName(GamePath) });
                    Status = "正在连接 BetterGI，核对已登录的游戏账号";
                    await _worker.StandardInput.WriteLineAsync("host_started");
                    Application.Current.MainWindow.WindowState = WindowState.Minimized;
                }
                else if (state == "running") Status = "正在执行每日委托 · 可语音说“停止”";
                else if (state == "waiting_for_game") Status = "等待已核对账号的可操作游戏画面（最多三分钟）";
                else if (state == "progress") Progress = message["execution"]?.ToString(Formatting.None) ?? "";
                else if (state == "finished")
                {
                    var report = message["report"]!;
                    var done = report.Value<int>("commission_completed_count");
                    Status = report.Value<string>("status") == "complete"
                        ? "已验证：四项每日委托完成，凯瑟琳奖励已领取"
                        : $"任务结束：已验证 {done}/4 项委托，仍需检查";
                    Progress = $"结束原因：{report["termination_reason"]}。原石到账数量仅在识别到独立证据后记录。\n记录：{report["trace"]}";
                }
                else if (state == "error")
                {
                    reportedError = true;
                    _hostNeverArmed = message["execution_armed"]?.Type == JTokenType.Boolean
                        && message.Value<bool>("execution_armed") == false;
                    Status = "任务停止：" + AgentFailureMessages.Describe(message.Value<string>("error_code"));
                }
            }
            await _worker.WaitForExitAsync(); await stderr;
            if (_worker.ExitCode != 0 && !_stopping && !reportedError)
                Status = "任务未完成，请检查游戏主界面、BetterGI 战斗设置及模型部署配置";
        }
        catch (Exception e) when (e is IOException or InvalidOperationException or System.ComponentModel.Win32Exception or CryptographicException or JsonException)
        { await StopAsync(); Status = $"启动失败（{e.GetType().Name}），请检查安装文件、设置和游戏权限"; }
        finally
        {
            var finalStatus = Status;
            if (_host is { HasExited: false }) await StopAsync();
            Status = finalStatus;
            _worker?.Dispose(); _worker = null;
        }
    }

    [RelayCommand]
    private async Task StopAsync()
    {
        _stopping = true;
        var acknowledged = false;
        try
        {
            var start = new ProcessStartInfo(Path.Combine(_root, "agent", "genshin-agent.exe"))
                { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
            foreach (var arg in new[] { "stop", "--pipe", _pipe, "--config", Path.Combine(_home, "settings.json") }) start.ArgumentList.Add(arg);
            using var stop = Process.Start(start);
            if (stop != null)
            {
                await stop.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(3));
                acknowledged = stop.ExitCode == 0;
            }
        }
        catch (Exception e) when (e is IOException or TimeoutException or System.ComponentModel.Win32Exception) { }
        // An explicit startup failure proves this worker never armed game input.
        // Active or ambiguous runs still require acknowledgement; otherwise keep the watchdog alive.
        if ((acknowledged || _hostNeverArmed) && _host is { HasExited: false }) _host.Kill(true);
        if (_worker is { HasExited: false }) _worker.Kill(true);
        Status = "已停止接管";
    }

    [RelayCommand]
    private async Task ToggleVoiceAsync()
    {
        if (_voice != null)
        {
            _voice.Dispose(); _voice = null; VoiceButtonText = "开启语音"; VoiceStatus = "麦克风未开启"; return;
        }
        VoiceStatus = "正在加载本地中文语音模型";
        try
        {
            _voice = await Task.Run(() => new LocalVoice(Path.Combine(_root, "voice-model")));
            _voice.Recognized += text => Application.Current.Dispatcher.InvokeAsync(async () =>
            {
                VoiceStatus = "听到：" + text;
                var intent = VoiceIntent.Parse(text);
                if (intent == "stop") await StopAsync();
                else if (intent == "start" && !StartCommand.IsRunning)
                { CommandText = "开始每日任务"; await StartCommand.ExecuteAsync(null); }
                else if (intent == "unknown") VoiceStatus += " · 请说“开始每日任务”或“停止”";
            });
            _voice.Faulted += text => Application.Current.Dispatcher.InvokeAsync(() => VoiceStatus = text);
            _voice.Start(); VoiceButtonText = "关闭语音"; VoiceStatus = "正在聆听 · 说“开始每日任务”或“停止”";
        }
        catch (Exception e) when (e is IOException or InvalidOperationException or DllNotFoundException or NAudio.MmException)
        { _voice?.Dispose(); _voice = null; VoiceStatus = "语音启动失败，请检查麦克风权限或安装文件"; }
    }

    [RelayCommand]
    private void OpenRecords() => Process.Start(new ProcessStartInfo("explorer.exe", Path.Combine(_home, "runs")) { UseShellExecute = true });

    public async Task CloseAsync()
    {
        if (_closing) return;
        _closing = true; _voice?.Dispose(); _voice = null;
        await StopAsync();
        if (StartCommand.ExecutionTask is { } execution) await execution;
        _host?.Dispose(); Closed = true;
    }
}
