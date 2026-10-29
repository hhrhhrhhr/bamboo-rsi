param(
    [Parameter(Position=0)]
    [ValidateSet('doctor','init','status','step','run','diagnose','propose','evaluate','recover','reject','rollback')]
    [string]$Command = 'doctor',
    [Alias('Home')][string]$ExperimentHome,
    [string]$Config,
    [ValidateRange(0,900)][int]$WaitSeconds = 0,
    [switch]$Prospective,
    [string]$Reason
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $ExperimentHome) { $ExperimentHome = Join-Path $repoRoot '.runtime/lol-lab/experiment-v1' }
if (-not $Config) { $Config = Join-Path $repoRoot '.runtime/lol-lab/config.json' }
$pythonExecutable = Join-Path $repoRoot 'platform/.venv/Scripts/python.exe'
$previousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = @('lol-lab/src', 'rsi-core/src', 'delta-lab/src' | ForEach-Object { Join-Path $repoRoot $_ }) -join [IO.Path]::PathSeparator
try {
    $cliArguments = @('-m', 'bamboo_lol.cli', $Command, '--home', $ExperimentHome)
    if ($Command -in @('doctor','init')) { $cliArguments += @('--config', $Config) }
    if ($Command -in @('step','run','diagnose')) { $cliArguments += @('--wait-seconds', [string]$WaitSeconds) }
    if ($Prospective) {
        if ($Command -ne 'run') { throw 'Prospective is only valid with run' }
        $cliArguments += '--prospective'
    }
    if ($Command -in @('reject','rollback')) {
        if (-not $Reason) { throw 'A reason is required' }
        $cliArguments += @('--reason', $Reason)
    }
    & $pythonExecutable @cliArguments
    exit $LASTEXITCODE
} finally {
    if ($null -eq $previousPythonPath) { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }
    else { $env:PYTHONPATH = $previousPythonPath }
}
