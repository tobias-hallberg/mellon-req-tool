; Inno Setup script — run via packaging/build.ps1, which passes
; /DAppVersion=<x.y.z> and /DSourceDir=<path to dist\Mellon>.

#ifndef AppVersion
  #error AppVersion must be defined (/DAppVersion=...)
#endif
#ifndef SourceDir
  #error SourceDir must be defined (/DSourceDir=...)
#endif

[Setup]
; Never change AppId: upgrades are matched on it.
AppId={{4EF6A63C-75D0-4E61-B025-2FF7095CAFD8}
AppName=Mellon
AppVersion={#AppVersion}
AppPublisher=Tobias Hallberg
; Per-user install into %LOCALAPPDATA%\Programs\Mellon — no admin rights, and
; the bundled TeX cache under tools\ stays writable. The fixed path is what the
; MCP client configs in INSTALL.md point at, so the directory page is hidden.
PrivilegesRequired=lowest
DefaultDirName={autopf}\Mellon
DisableDirPage=yes
DefaultGroupName=Mellon
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\mellon.exe
LicenseFile={#SourceDir}\LICENSE
OutputBaseFilename=Mellon-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[InstallDelete]
; Drop the previous version's runtime so stale DLLs/modules never linger.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Mellon"; Filename: "{app}\mellon.exe"

[Run]
Filename: "{app}\mellon.exe"; Description: "Launch Mellon"; Flags: nowait postinstall skipifsilent

[Code]
// [InstallDelete] removes the old runtime before the new files are copied. If
// a program still runs from {app} (mellon-mcp.exe stays up while Claude Code or
// Claude Desktop is open, and Restart Manager can't close it), the copy then
// fails on its locked .exe and leaves a half-deleted, broken install. So Setup
// refuses here, before anything is deleted.
function ProgramsRunningFromApp(): String;
var
  Locator, Service, Procs, Proc: Variant;
  AppDir, ExePath, Name: String;
  I: Integer;
begin
  Result := '';
  AppDir := Lowercase(AddBackslash(ExpandConstant('{app}')));
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Service := Locator.ConnectServer('.', 'root\CIMV2');
    Procs := Service.ExecQuery(
      'SELECT Name, ExecutablePath FROM Win32_Process WHERE ExecutablePath IS NOT NULL');
    for I := 0 to Procs.Count - 1 do
    begin
      Proc := Procs.ItemIndex(I);
      ExePath := Proc.ExecutablePath;
      if Pos(AppDir, Lowercase(ExePath)) = 1 then
      begin
        Name := Proc.Name;
        if Pos(Name, Result) = 0 then
        begin
          if Result <> '' then
            Result := Result + ', ';
          Result := Result + Name;
        end;
      end;
    end;
  except
    // WMI unavailable: leave it to Setup's own files-in-use handling.
    Result := '';
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Running: String;
begin
  Result := '';
  Running := ProgramsRunningFromApp();
  if Running <> '' then
    Result := 'Mellon is still running (' + Running + ').' + #13#10#13#10 +
      'Quit Mellon, and Claude Code and Claude Desktop (they keep ' +
      'mellon-mcp.exe running), then run Setup again.';
end;
