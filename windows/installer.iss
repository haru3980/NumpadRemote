; テンキーレシーバー インストーラー (Inno Setup 6)
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppName "テンキーレシーバー"
#define MyExe "NumpadReceiver.exe"

[Setup]
AppId={{01FB1ABF-7E8B-4484-9418-5A67DD5CD210}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\NumpadReceiver
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
OutputDir=Output
OutputBaseFilename=NumpadReceiver-Setup
Compression=lzma2
SolidCompression=yes
UninstallDisplayIcon={app}\{#MyExe}
#ifexist "..\feather.ico"
SetupIconFile=..\feather.ico
#endif

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"

[Tasks]
Name: "desktopicon"; Description: "デスクトップにアイコンを作成する"; Flags: unchecked
Name: "startup"; Description: "Windows起動時に自動で起動する"; Flags: unchecked

[Files]
Source: "..\dist\{#MyExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyExe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyExe}"; Tasks: desktopicon
Name: "{commonstartup}\{#MyAppName}"; Filename: "{app}\{#MyExe}"; Tasks: startup

[Run]
; ファイアウォールの許可 (プライベートネットワークのみ)。初回起動時の確認画面が出なくなります
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""NumpadReceiver TCP"" dir=in action=allow protocol=TCP localport=52110 profile=private program=""{app}\{#MyExe}"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""NumpadReceiver UDP"" dir=in action=allow protocol=UDP localport=52111 profile=private program=""{app}\{#MyExe}"""; Flags: runhidden
Filename: "{app}\{#MyExe}"; Description: "テンキーレシーバーを起動する"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""NumpadReceiver TCP"""; Flags: runhidden; RunOnceId: "DelFwTcp"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""NumpadReceiver UDP"""; Flags: runhidden; RunOnceId: "DelFwUdp"
