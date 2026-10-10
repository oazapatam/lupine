; Inno Setup script for the Windows LUPINE server.
;
; Build with:
;   iscc /DAppVersion=1.2.3 /DCudaVersion=13.3.1 /DSourceDir=<dir with exes> lupine-server.iss
; SourceDir must hold lupine_driver_server.exe and lupine_tray.exe.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef CudaVersion
  #define CudaVersion "unknown"
#endif
#ifndef SourceDir
  #define SourceDir "."
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif

[Setup]
AppId={{6F0B8C52-4E1A-4C4F-9B7E-2D6A5C1E7A31}
AppName=LUPINE Server
AppVersion={#AppVersion}
AppPublisher=LUPINE
DefaultDirName={autopf}\Lupine Server
DefaultGroupName=LUPINE Server
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
OutputDir={#OutputDir}
OutputBaseFilename=lupine-server-windows-cuda-{#CudaVersion}-x86_64-setup
Compression=lzma2
SolidCompression=yes
UninstallDisplayIcon={app}\lupine_tray.exe
CloseApplications=yes
WizardStyle=modern

[Tasks]
Name: "autostart"; Description: "Start the server tray when I sign in to Windows"; GroupDescription: "Startup:"
Name: "firewall"; Description: "Allow the server through Windows Firewall (any port it is configured to listen on)"; GroupDescription: "Network:"

[Files]
Source: "{#SourceDir}\lupine_driver_server.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\lupine_tray.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\LUPINE Server"; Filename: "{app}\lupine_tray.exe"
Name: "{group}\Uninstall LUPINE Server"; Filename: "{uninstallexe}"

[Registry]
Root: HKLM; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "LupineServer"; ValueData: """{app}\lupine_tray.exe"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""LUPINE Server"" dir=in action=allow program=""{app}\lupine_driver_server.exe"" protocol=TCP"; Flags: runhidden; Tasks: firewall
Filename: "{app}\lupine_tray.exe"; Description: "Start LUPINE Server now"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""LUPINE Server"""; Flags: runhidden; RunOnceId: "RemoveFirewallRule"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM lupine_tray.exe"; Flags: runhidden; RunOnceId: "KillTray"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM lupine_driver_server.exe"; Flags: runhidden; RunOnceId: "KillServer"
