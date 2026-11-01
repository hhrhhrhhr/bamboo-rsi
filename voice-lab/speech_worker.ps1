param([Parameter(Mandatory=$true)][string]$OutputRoot)
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$root = [IO.Path]::GetFullPath($OutputRoot)
[IO.Directory]::CreateDirectory($root) | Out-Null
$category = New-Object -ComObject SAPI.SpObjectTokenCategory
$category.SetId('HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech_OneCore\Voices', $false)
$tokens = @($category.EnumerateTokens('Language=804', ''))
$speaker = New-Object -ComObject SAPI.SpVoice
while ($null -ne ($line = [Console]::ReadLine())) {
    $stream = $null
    try {
        $request = $line | ConvertFrom-Json
        if ($request.name -notmatch '^[a-zA-Z0-9_-]{1,140}$') { throw 'Invalid audio name' }
        if ($request.voice -notin @('Huihui','Kangkang','Yaoyao')) { throw 'Unknown voice' }
        if ([int]$request.rate -lt -3 -or [int]$request.rate -gt 3) { throw 'Rate outside lab limits' }
        if ([string]::IsNullOrWhiteSpace($request.text) -or $request.text.Length -gt 1600) { throw 'Invalid speech text' }
        $target = [IO.Path]::GetFullPath((Join-Path $root ($request.name + '.wav')))
        if (-not $target.StartsWith($root+[IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Output outside root' }
        if (Test-Path -LiteralPath $target) { throw 'Audio evidence already exists' }
        $token = $tokens | Where-Object { $_.Id -like ('*'+$request.voice+'M') } | Select-Object -First 1
        if ($null -eq $token) { throw 'Voice unavailable' }
        $watch = [Diagnostics.Stopwatch]::StartNew()
        $speaker.Voice = $token
        $speaker.Rate = [int]$request.rate
        $speaker.Volume = 100
        $stream = New-Object -ComObject SAPI.SpFileStream
        $stream.Open($target,3,$false)
        $speaker.AudioOutputStream = $stream
        # SVSFIsNotXML: treat all model text as text, never as speech markup.
        $speaker.Speak([string]$request.text,16) | Out-Null
        $stream.Close()
        $stream = $null
        $watch.Stop()
        @{status='ok';path=$target;voice=$token.GetDescription();elapsedMs=$watch.Elapsed.TotalMilliseconds;microphone=$false} | ConvertTo-Json -Compress
    } catch {
        if ($null -ne $stream) { try { $stream.Close() } catch {} }
        @{status='failed';error=$_.Exception.GetType().Name;detail=$_.Exception.Message} | ConvertTo-Json -Compress
    }
}
