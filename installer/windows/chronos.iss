#define AppName "chronos"
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #error SourceDir must be defined on the ISCC command line.
#endif
#ifndef OutputDir
  #error OutputDir must be defined on the ISCC command line.
#endif

[Setup]
AppId={{6AF86F4E-3E6A-4A0B-9AF6-4FC6B00D6671}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=chronos
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=chronos-{#AppVersion}-windows-installer
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\chronos.exe

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\chronos"; Filename: "{app}\chronos.exe"

[Run]
Filename: "{app}\chronos.exe"; Description: "Launch chronos"; Flags: nowait postinstall skipifsilent
