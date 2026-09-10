; Inno Setup script for PhoneDeck
; Build:  ISCC.exe installer\phonedeck.iss   (run from the repo root)

#define MyAppName "PhoneDeck"
#define MyAppVersion "1.3.0"
#define MyAppPublisher "Adam Dreher"
#define MyAppExeName "PhoneDeck.exe"

[Setup]
AppId={{9C4B2E7A-3D51-4F8C-A2E6-7B1D9F0C5E42}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\PhoneDeck
DefaultGroupName=PhoneDeck
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=.
OutputBaseFilename=PhoneDeck-Setup-{#MyAppVersion}
SetupIconFile=..\phonedeck.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
; let an in-app update close/replace a running PhoneDeck cleanly
CloseApplications=yes
RestartApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
; PhoneDeck app (PyInstaller onedir output)
Source: "..\dist\PhoneDeck\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; bundled scrcpy + adb (the app looks for {app}\scrcpy first)
Source: "C:\Program Files\scrcpy-win64-v4.1\*"; DestDir: "{app}\scrcpy"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\PhoneDeck"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall PhoneDeck"; Filename: "{uninstallexe}"
Name: "{autodesktop}\PhoneDeck"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch PhoneDeck"; Flags: nowait postinstall skipifsilent
