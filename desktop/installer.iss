#ifndef Payload
  #define Payload "..\dist\genshin-desktop"
#endif
[Setup]
AppId={{8516418F-AC32-4329-A383-793572104BC1}
AppName=StartLux 原神语音助手
AppVersion=0.3.0-preview
AppPublisher=StartLux
DefaultDirName={localappdata}\Programs\StartLuxGenshin
DefaultGroupName=StartLux
OutputDir=..\dist\installer
OutputBaseFilename=StartLux-Genshin-0.3.0-preview-win-x64
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.22621
PrivilegesRequired=admin
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\StartLux.Genshin.exe
CloseApplications=yes
[Files]
Source: "{#Payload}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{autoprograms}\StartLux 原神语音助手"; Filename: "{app}\StartLux.Genshin.exe"
Name: "{autodesktop}\StartLux 原神语音助手"; Filename: "{app}\StartLux.Genshin.exe"
[Run]
Filename: "{app}\StartLux.Genshin.exe"; Description: "启动 StartLux 原神语音助手"; Flags: nowait postinstall skipifsilent runascurrentuser
