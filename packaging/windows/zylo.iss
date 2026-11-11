; Inno Setup script for the Zylo Windows installer.
; Build:  iscc /DVersion=0.5.0 packaging\windows\zylo.iss   (expects dist\zylo.exe)
#ifndef Version
  #define Version "0.0.0"
#endif

[Setup]
AppId={{5B1E6F0A-8C7D-4E55-9C7B-2D6E4B7A91C3}
AppName=Zylo
AppVersion={#Version}
AppPublisher=Zylo
AppPublisherURL=https://github.com/Darkvus/zylo
DefaultDirName={autopf}\Zylo
DefaultGroupName=Zylo
DisableProgramGroupPage=yes
OutputDir=..\..\dist
OutputBaseFilename=zylo-setup-x86_64
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
ChangesEnvironment=yes
LicenseFile=..\..\LICENSE
UninstallDisplayIcon={app}\zylo.exe

[Files]
Source: "..\..\dist\zylo.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Registry]
; Put the install folder on the user's PATH (only if it is not already there)
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Check: NeedsAddPath(ExpandConstant('{app}'))

[Code]
function NeedsAddPath(Param: string): Boolean;
var
  OrigPath: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', OrigPath) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Uppercase(Param) + ';', ';' + Uppercase(OrigPath) + ';') = 0;
end;

function RustIsInstalled: Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{cmd}'), '/C where rustc', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ErrorCode: Integer;
begin
  if (CurStep = ssDone) and (not WizardSilent) and (not RustIsInstalled) then
  begin
    if MsgBox('Zylo compiles programs through Rust, and "rustc" was not found on this computer.' + #13#10 + #13#10 +
              'Open https://rustup.rs to install it now?', mbConfirmation, MB_YESNO) = IDYES then
      ShellExec('open', 'https://rustup.rs', '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Path, App: string;
  P: Integer;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    App := ExpandConstant('{app}');
    if RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Path) then
    begin
      P := Pos(';' + Uppercase(App), Uppercase(Path));
      if P > 0 then
      begin
        Delete(Path, P, Length(App) + 1);
        RegWriteExpandStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Path);
      end;
    end;
  end;
end;
