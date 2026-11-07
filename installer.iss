[Setup]
AppName=MemeBot
AppVersion=1.0
DefaultDirName={autopf}\MemeBot
DefaultGroupName=MemeBot
OutputBaseFilename=MemeBot-Setup
Compression=lzma
SolidCompression=yes
PrivilegesRequired=lowest

[Files]
Source: "dist\MemeBot\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\MemeBot"; Filename: "{app}\MemeBot.exe"
Name: "{autodesktop}\MemeBot"; Filename: "{app}\MemeBot.exe"

[Run]
Filename: "{app}\MemeBot.exe"; Description: "Launch MemeBot"; Flags: nowait postinstall skipifsilent
