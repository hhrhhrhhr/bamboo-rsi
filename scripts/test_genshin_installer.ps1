$ErrorActionPreference = 'Stop'
$installer = (Get-ChildItem dist/installer/*.exe | Select-Object -First 1).FullName
$destination = Join-Path $env:RUNNER_TEMP 'StartLux Genshin Installed'
$install = Start-Process $installer -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/DIR=`"$destination`"") -PassThru -Wait
if ($install.ExitCode -ne 0) { throw "Installer exited $($install.ExitCode)" }
$runtime = & "$destination/agent/genshin-agent.exe" self-test | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $runtime.status -ne 'runtime_ready') { throw 'Frozen runtime failed' }
if ($runtime.platform_version -ne '0.3.0') { throw 'Installer did not use the new platform runtime' }
$voiceResult = Join-Path $env:RUNNER_TEMP 'startlux-voice-native.json'
$voiceTest = Start-Process "$destination/StartLux.Genshin.exe" -ArgumentList @('--self-test-voice', "`"$voiceResult`"") -PassThru -Wait
if ($voiceTest.ExitCode -ne 0 -or -not (Test-Path $voiceResult)) { throw 'Bundled Chinese model failed to load' }
$voice = Get-Content $voiceResult | ConvertFrom-Json
if (-not $voice.model_loaded -or -not $voice.native_decoder_verified) { throw 'Chinese decoder verification failed' }
# Run the actual WPF program, with its bundled .NET runtime, from an installed path with spaces.
$app = Start-Process "$destination/StartLux.Genshin.exe" -PassThru
try {
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 500
        $app.Refresh()
        if ($app.HasExited) { throw 'Desktop exited before showing its window' }
        if ($app.MainWindowHandle -ne 0) { $ready = $true; break }
    }
    if (-not $ready) { throw 'Desktop window did not appear' }
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    $window = [System.Windows.Automation.AutomationElement]::FromHandle($app.MainWindowHandle)
    $buttons = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button))
    $names = @($buttons | ForEach-Object { $_.Current.Name })
    if ('启动并执行' -notin $names -or '停止接管' -notin $names -or '开启语音' -notin $names) { throw 'Desktop controls are missing' }
    Add-Type -AssemblyName System.Drawing
    Add-Type -AssemblyName System.Windows.Forms
    $bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
    $bitmap = [System.Drawing.Bitmap]::new($bounds.Width, $bounds.Height)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    try {
        $graphics.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
        $bitmap.Save((Join-Path $env:GITHUB_WORKSPACE 'dist/installer/desktop.png'))
    } finally { $graphics.Dispose(); $bitmap.Dispose() }
    # Load the shipped Vosk Chinese model through the real voice command control.
    $voiceButton = $buttons | Where-Object { $_.Current.Name -eq '开启语音' } | Select-Object -First 1
    $invoke = $voiceButton.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
    $invoke.Invoke()
    Start-Sleep -Seconds 3
    $app.Refresh()
    if ($app.HasExited) { throw 'Voice initialization crashed the desktop' }
    # CI has no microphone: this proves graceful startup, not speech accuracy.
    $app.CloseMainWindow() | Out-Null
    if (-not $app.WaitForExit(10000)) { throw 'Desktop failed to shut down' }
    if ($app.ExitCode -ne 0) { throw 'Desktop shutdown failed' }
} finally { if (-not $app.HasExited) { $app.Kill() } }
$uninstall = Start-Process "$destination/unins000.exe" -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -PassThru -Wait
if ($uninstall.ExitCode -ne 0 -or (Test-Path "$destination/StartLux.Genshin.exe")) { throw 'Uninstall failed' }
$hash = (Get-FileHash $installer -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -Path "$installer.sha256" -Value "$hash  $([IO.Path]::GetFileName($installer))" -Encoding ascii
@{ installer_sha256 = $hash; installation_verified = $true; frozen_runtime_verified = $true;
   source_commit = $env:GITHUB_SHA; platform_version = $runtime.platform_version;
   desktop_controls_verified = $true; uninstall_verified = $true; microphone_verified = $false;
   genshin_live_verified = $false; asr_accuracy_verified = $false; chinese_decoder_verified = $true } | ConvertTo-Json | Set-Content dist/installer/verification.json -Encoding utf8
