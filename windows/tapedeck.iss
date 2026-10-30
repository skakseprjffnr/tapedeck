; Inno Setup script for Tapedeck's Windows installer (Inno Setup 6.3 or newer).
; build.ps1 passes AppVersion, SourceDir and OutputDir.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\build\windows\dist\Tapedeck"
#endif
#ifndef OutputDir
  #define OutputDir "out"
#endif

[Setup]
AppId={{8C1F4A2E-5B7D-4E3A-9F61-2D7C8B4E6A13}
AppName=Tapedeck
AppVersion={#AppVersion}
AppVerName=Tapedeck {#AppVersion}
AppPublisher=Tapedeck
; Installs for the current user without asking for admin rights (in
; %LOCALAPPDATA%\Programs); people can still choose an install for everyone.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\Tapedeck
DisableDirPage=auto
DefaultGroupName=Tapedeck
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=Tapedeck-{#AppVersion}-windows-setup
SetupIconFile=tapedeck.ico
UninstallDisplayIcon={app}\Tapedeck.exe
UninstallDisplayName=Tapedeck
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Tapedeck"; Filename: "{app}\Tapedeck.exe"
Name: "{autodesktop}\Tapedeck"; Filename: "{app}\Tapedeck.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Tapedeck.exe"; Description: "{cm:LaunchProgram,Tapedeck}"; Flags: nowait postinstall skipifsilent
