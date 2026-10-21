$ErrorActionPreference = 'Stop'
$workspaceRoot = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $workspaceRoot 'Start-Genshin-RSI.cmd'
$shortcutPath = Join-Path $workspaceRoot 'Start-Genshin-RSI-Admin.lnk'
$commandPath = Join-Path $env:SystemRoot 'System32\cmd.exe'
$arguments = '/d /c ""' + $launcher + '""'
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw 'RSI launcher is missing.'
}
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
if ((Test-Path -LiteralPath $shortcutPath) -and
    (($shortcut.TargetPath -ine $commandPath) -or ($shortcut.Arguments -cne $arguments))) {
    throw 'An unrelated shortcut already exists; it was preserved.'
}
$shortcut.TargetPath = $commandPath
$shortcut.Arguments = $arguments
$shortcut.WorkingDirectory = $workspaceRoot
$shortcut.Description = 'Start Genshin RSI with Windows administrator consent'
$shortcut.IconLocation = $commandPath + ',0'
$shortcut.Save()

# Standard Shell Link RunAsUser flag. This prepares a manual UAC request;
# it does not execute the shortcut, change UAC policy, or supply credentials.
# https://devblogs.microsoft.com/oldnewthing/20071219-00/?p=24103
$shortcutBytes = [System.IO.File]::ReadAllBytes($shortcutPath)
if (($shortcutBytes.Length -lt 0x4c) -or
    ([BitConverter]::ToUInt32($shortcutBytes, 0) -ne 0x4c)) {
    throw 'Unexpected Shell Link header.'
}
$shortcutBytes[0x15] = $shortcutBytes[0x15] -bor 0x20
[System.IO.File]::WriteAllBytes($shortcutPath, $shortcutBytes)
$savedBytes = [System.IO.File]::ReadAllBytes($shortcutPath)
if (([BitConverter]::ToUInt32($savedBytes, 0x14) -band 0x2000) -eq 0) {
    throw 'The administrator request flag was not saved.'
}
$verified = $shell.CreateShortcut($shortcutPath)
if (($verified.TargetPath -ine $commandPath) -or ($verified.Arguments -cne $arguments) -or
    ($verified.WorkingDirectory -ine $workspaceRoot)) {
    throw 'Shortcut verification failed.'
}
Write-Output ('Verified manual administrator shortcut: ' + $shortcutPath)
