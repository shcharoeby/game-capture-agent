#define MyAppName      "OldenLeague Game Capture"
#define MyAppPublisher "OldenLeague"
#define MyAppExeName   "game-capture.exe"
#define MyAppId        "{{B3F2A1D4-9C7E-4B3A-8F2D-1E6A5C3B0D9F}"

; Version is injected by release.sh:  /DMyAppVersion=1.2.3
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif

; Source directory (build output), injected by release.sh:  /DSourceDir=...
#ifndef SourceDir
  #define SourceDir "D:\share\game-capture-agent"
#endif

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL=https://github.com/shcharoeby/game-capture-agent
AppUpdatesURL=https://github.com/shcharoeby/game-capture-agent/releases
VersionInfoVersion={#MyAppVersion}

; Install to %LOCALAPPDATA%\Programs\GameCapture — no UAC needed
DefaultDirName={localappdata}\Programs\GameCapture
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes

; Silently upgrade existing installation
; (same AppId → Inno Setup detects previous install and replaces files)
CloseApplications=yes
CloseApplicationsFilter=game-capture.exe,capture-debug.exe

OutputDir={#SourceDir}
OutputBaseFilename=GameCapture-Setup-{#MyAppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
DisableWelcomePage=no
LicenseFile=

; Ensure the installer itself needs no elevation
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Ярлык на рабочем столе"; GroupDescription: "Дополнительно:"

[Files]
; App binaries — always overwrite
Source: "{#SourceDir}\game-capture.exe";  DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\capture-debug.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\ffmpeg.exe";        DestDir: "{app}"; Flags: ignoreversion

; NOTE: config.yaml and logs are stored in %APPDATA%\GameCapture (managed by app itself)
; The installer deliberately does NOT touch them.

[Icons]
; {userprograms} and {userdesktop} — user's own Start Menu / desktop, no admin needed
Name: "{userprograms}\{#MyAppName}";          Filename: "{app}\{#MyAppExeName}"
Name: "{userprograms}\Удалить {#MyAppName}";  Filename: "{uninstallexe}"
Name: "{userdesktop}\{#MyAppName}";           Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить {#MyAppName}"; \
          Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Clean up .new files left by the auto-updater if any
Type: files; Name: "{app}\*.new"
Type: files; Name: "{app}\update.bat"
