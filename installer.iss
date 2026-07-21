; ProxyForge installer (Inno Setup)
;
; Per-user install (no admin needed): the app writes settings, models and
; output next to its exe, so it must NOT live in Program Files.
; Build:  ISCC installer.iss   (after building ProxyForge.exe)

#define AppName "ProxyForge"
#define AppVersion "2.3.1"
#define AppExe "ProxyForge.exe"

[Setup]
AppId={{C67040F2-FADE-4552-945E-767B7B25618C}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Boffo90
AppPublisherURL=https://github.com/Boffo90/proxyforge
DefaultDirName={localappdata}\{#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=installer
; underscore matters: release assets are listed alphabetically by the GitHub
; API, and older clients pick the FIRST .exe — "_" sorts after ".exe" so the
; bare app exe always comes first
OutputBaseFilename=ProxyForge_Setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Files]
Source: "{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{userprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; downloaded engine/models and user config live next to the exe
Type: filesandordirs; Name: "{app}\models"
Type: filesandordirs; Name: "{app}\_temp"
Type: files; Name: "{app}\realesrgan-ncnn-vulkan.exe"
Type: files; Name: "{app}\vcomp140.dll"
Type: files; Name: "{app}\settings.json"
; keep {app}\output — never delete the user's work
