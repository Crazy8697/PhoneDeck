; SPDX-License-Identifier: Apache-2.0
; Copyright 2026 DarkRelay
; PhoneDeck Windows installer. Built by build.ps1 -Setup with Inno Setup 6 (ISCC):
;   ISCC /DAppVersion=<phonedeck.VERSION> /DDistDir=<dist\PhoneDeck> /O<output folder> installer\phonedeck.iss
; The PyInstaller onedir build (dist\PhoneDeck, with scrcpy + adb in its scrcpy\ subfolder) goes to
; Program Files (64-bit). Inno Setup is a build tool only: nothing of it ships.
;
; dist\PhoneDeck carries LICENSE.txt, NOTICE.txt, THIRD-PARTY-NOTICES.md and licenses\ (the
; third-party license texts, read by Help > Licenses), so {app} gets them with everything else.
;
; Uninstall keeps every piece of user data: settings (HKCU\Software\PhoneDeck), favorites and logs
; (%APPDATA%\PhoneDeck).
;
; Updates: the same AppId upgrades the installed copy in place (tasks, shortcuts, Apps entry).
; CloseApplications closes a running PhoneDeck first. /RELAUNCH=1 starts PhoneDeck again afterwards
; as the original, unelevated user (PhoneDeck's own updater uses it, the Lazarus PDF pattern).

#ifndef AppVersion
  #error AppVersion must be given: ISCC /DAppVersion=x.y.z
#endif
#ifndef DistDir
  #define DistDir "..\dist\PhoneDeck"
#endif
#define ExeName "PhoneDeck.exe"
#define AppGuid "{9C4B2E7A-3D51-4F8C-A2E6-7B1D9F0C5E42}"
#define ProductName "PhoneDeck"
#define SetupBase "PhoneDeck-Setup-"

[Setup]
AppId={{#AppGuid}
AppName={#ProductName}
AppVersion={#AppVersion}
AppVerName={#ProductName} {#AppVersion}
AppPublisher=DarkRelay
AppPublisherURL=https://darkrelay.net
AppSupportURL=https://github.com/Crazy8697/PhoneDeck/issues
AppUpdatesURL=https://github.com/Crazy8697/PhoneDeck/releases
AppComments=Run your Android phone's apps in windows on your PC
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#ProductName} Setup
VersionInfoProductName={#ProductName}
DefaultDirName={autopf}\PhoneDeck
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
SetupIconFile=..\phonedeck.ico
UninstallDisplayIcon={app}\{#ExeName}
UninstallDisplayName={#ProductName}
OutputBaseFilename={#SetupBase}{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[InstallDelete]
; older PhoneDeck installers put the Start menu shortcuts in a "PhoneDeck" group folder
Type: filesandordirs; Name: "{autoprograms}\PhoneDeck"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#ProductName}"; Filename: "{app}\{#ExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#ExeName}"; Comment: "Run your Android phone's apps in windows on your PC"
Name: "{autodesktop}\{#ProductName}"; Filename: "{app}\{#ExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#ExeName}"; Comment: "Run your Android phone's apps in windows on your PC"; Tasks: desktopicon

[Run]
; offered on the last page; postinstall entries run as the original (unelevated) user
Filename: "{app}\{#ExeName}"; Description: "Launch PhoneDeck"; Flags: postinstall nowait skipifsilent
; an update from inside the program (/RELAUNCH=1): start it again when done, unelevated
Filename: "{app}\{#ExeName}"; Check: ShouldRelaunch; Flags: nowait runasoriginaluser

[Code]
function ShouldRelaunch(): Boolean;
begin
  Result := ExpandConstant('{param:relaunch|0}') = '1';
end;
