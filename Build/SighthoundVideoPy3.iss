; ****************************************************************************
;
;  SighthoundVideoPy3.iss
;    Inno Setup script for the Sighthound Video Py3 installer.
;
; ****************************************************************************
;
;  This file is part of the Sighthound Video Python 3 port.
;
;  Licensed under the GNU GPLv3 license found at
;  https://www.gnu.org/licenses/gpl-3.0.txt
;
; ****************************************************************************
;
;  Compile with build\build_installer.ps1, which stages the payload first --
;  ISCC on its own would package whatever happens to be in build\stage\payload.
;
;  What this installs is a COMPLETE program: the app, a private CPython 3.12.10
;  with every dependency, the AI models, and the SHLaunchPY3 service. Nothing is
;  downloaded, nothing is compiled, and the user is asked for nothing beyond the
;  usual wizard pages (and not even those with /VERYSILENT).
;
;  Command-line switches beyond Inno's own:
;
;    /DATADIR="D:\SighthoundData"    where databases, logs and settings live
;                                    (default: the installing user's
;                                    %LOCALAPPDATA%\Sighthound Video Py3, which
;                                    is where an existing install already
;                                    keeps them)
;    /SERVICEUSER=".\Name"           run the service as this account instead of
;    /SERVICEPASSWORD="secret"       LocalSystem -- needed only when video
;                                    storage is on a network share
;
;  Silent, no questions, nothing on screen:
;    SighthoundVideoPy3-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART

#define AppName "Sighthound Video Py3"
#define AppPublisher "Sighthound Video Py3"
#define AppExeName "SighthoundVideoPy3.exe"
#define ServiceName "SHLaunchPY3"
#define PayloadDir "stage\payload"
#define InterpreterConsole "python\SighthoundPy3c.exe"

; Taskbar identity, stamped on the shortcuts below.  MUST match
; kWindowsAppUserModelId in appCommon\CommonStrings.py, which the front end
; applies to itself at startup: Windows ties a running window to a shortcut by
; comparing the two, and that is what makes "Pin to taskbar" pin the app rather
; than the private interpreter hosting it.  Never version it and never change
; it -- existing pins are keyed on this value.
#define AppUserModelId "Sighthound.SighthoundVideoPy3"

; Both are passed by build_installer.ps1, straight from the app's own
; kVersionString. The defaults below only apply when this script is compiled by
; hand -- keep them in step with appCommon\CommonStrings.py if you do that.
#ifndef AppVersion
  #define AppVersion "2026.08.01"
#endif
#ifndef AppVersionFour
  #define AppVersionFour "2026.08.01.0"
#endif

; build_installer.ps1 defines FastCompress for a quick iteration build; the
; shipped installer is built without it.
#ifdef FastCompress
  #define CompressionMode "lzma2/fast"
#else
  #define CompressionMode "lzma2/max"
#endif

[Setup]
; Never change AppId: it is what lets a new build upgrade an old install in
; place instead of leaving two entries in Programs and Features.
AppId={{9C0F5E6B-2F1C-4B7E-9E2D-6F2A3D8B41A7}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersionFour}
; The numeric field above cannot hold the leading zeros; this one is free text
; and is what Explorer's Details tab shows for the setup .exe.
VersionInfoTextVersion={#AppVersion}
VersionInfoProductName={#AppName}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}
OutputDir=out
OutputBaseFilename=SighthoundVideoPy3-Setup-{#AppVersion}
Compression={#CompressionMode}
SolidCompression=yes
#ifdef Spanned
; A single setup.exe cannot exceed ~2.1 GB. When the compressed payload does,
; build_installer.ps1 re-runs the compile with Spanned defined: the data then
; lives in .bin slices beside the executable, which must travel with it.
DiskSpanning=yes
DiskSliceSize=2100000000
SlicesPerDisk=1
#endif
; 64-bit only: the whole stack (torch/CUDA, wx, OpenCV) is amd64.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
PrivilegesRequired=admin
LicenseFile=..\COPYING
WizardStyle=modern
ShowLanguageDialog=no
DisableWelcomePage=no
DisableProgramGroupPage=yes
AllowNoIcons=yes
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "service"; Description: "Run the back end as a Windows service ({#ServiceName}) so cameras keep recording when the app is closed"; GroupDescription: "Back end:"

[Files]
; The staged image IS the install directory; see build\make_payload.py.
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; AppUserModelID is what lets the taskbar match a running window to this
; shortcut; without it Windows falls back to the hosting exe (the interpreter).
; IconFilename is explicit so a pin created from the shortcut keeps the app icon
; even though the shortcut points at the launcher stub.
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; \
    IconFilename: "{app}\icons\SmartVideoApp.ico"; AppUserModelID: "{#AppUserModelId}"
Name: "{group}\{#AppName} logs"; Filename: "{code:GetDataDir}\logs"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; \
    IconFilename: "{app}\icons\SmartVideoApp.ico"; AppUserModelID: "{#AppUserModelId}"; Tasks: desktopicon

[Run]
; The service is registered and started from [Code] (InstallBackEndService), not
; from here -- a [Run] entry discards both the exit code and the output, which
; is how a registration that failed outright still produced an install the
; wizard called a success.

; Let the LAN record viewer and the camera discovery traffic through the
; firewall on private/domain networks. Without a rule the back end -- which runs
; as a service, and so never gets the interactive "allow access?" prompt -- just
; silently fails to accept LAN connections. Public networks are left alone.
Filename: "{sys}\netsh.exe"; \
    Parameters: "advfirewall firewall add rule name=""{#AppName}"" dir=in action=allow program=""{app}\python\SighthoundPy3.exe"" enable=yes profile=private,domain"; \
    StatusMsg: "Allowing LAN access through the firewall..."; \
    Flags: runhidden waituntilterminated

Filename: "{app}\{#AppExeName}"; Description: "Start {#AppName} now"; \
    WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Stop and unregister the service before the files it runs from are deleted.
Filename: "{app}\{#InterpreterConsole}"; Parameters: "-s -m launch.InstallService remove"; \
    WorkingDir: "{app}"; RunOnceId: "RemoveService"; Flags: runhidden waituntilterminated

Filename: "{sys}\netsh.exe"; \
    Parameters: "advfirewall firewall delete rule name=""{#AppName}"""; \
    RunOnceId: "RemoveFirewallRule"; Flags: runhidden waituntilterminated

[UninstallDelete]
; Written after install, so Inno does not track it.
Type: files; Name: "{app}\datadir.txt"
Type: filesandordirs; Name: "{app}\python\Lib\site-packages\__pycache__"

[Code]
var
  DataDirPage: TInputDirWizardPage;

// ---------------------------------------------------------------------------
// Command-line helpers. Inno parses /NAME=value itself for its own switches
// only, so ours are read straight out of the parameter list.
// ---------------------------------------------------------------------------
function GetCommandLineValue(const Name: String): String;
var
  I: Integer;
  Param, Prefix: String;
begin
  Result := '';
  Prefix := '/' + Uppercase(Name) + '=';
  for I := 1 to ParamCount do
  begin
    Param := ParamStr(I);
    if Pos(Prefix, Uppercase(Param)) = 1 then
    begin
      Result := Copy(Param, Length(Prefix) + 1, MaxInt);
      // Strip quotes the shell may have left in place.
      if (Length(Result) >= 2) and (Result[1] = '"') then
        Result := Copy(Result, 2, Length(Result) - 2);
      Exit;
    end;
  end;
end;

// ---------------------------------------------------------------------------
// The data directory: databases, logs, face enrollments, settings.
//
// Defaults to the installing user's %LOCALAPPDATA%\Sighthound Video Py3, which
// is exactly where a copy running from a source checkout already keeps them --
// so installing over an existing setup adopts that data instead of starting an
// empty one.
// ---------------------------------------------------------------------------
function GetDataDir(Param: String): String;
begin
  Result := GetCommandLineValue('DATADIR');
  if Result = '' then
  begin
    if (DataDirPage <> nil) and (DataDirPage.Values[0] <> '') then
      Result := DataDirPage.Values[0]
    else
      Result := ExpandConstant('{localappdata}\{#AppName}');
  end;
end;

function GetGrantUser(Param: String): String;
begin
  // The account allowed to start/stop the service without elevation, so the
  // app's own "restart the back end" path works for a normal user.
  Result := '.\' + GetUserNameString;
end;

function GetServiceAccountArgs(Param: String): String;
var
  User, Password: String;
begin
  User := GetCommandLineValue('SERVICEUSER');
  Password := GetCommandLineValue('SERVICEPASSWORD');
  if User <> '' then
    Result := '--user "' + User + '" --password "' + Password + '"'
  else
    Result := '--local-system';
end;

// ---------------------------------------------------------------------------
// Wizard
// ---------------------------------------------------------------------------
procedure InitializeWizard;
begin
  DataDirPage := CreateInputDirPage(wpSelectDir,
    'Data location',
    'Where should recordings, databases and settings be stored?',
    'Video is stored under the location you pick inside the app; this is where '
    + 'the databases, logs, rules and face enrollments live.'#13#10#13#10
    + 'Leave it as it is unless you have a reason to move it -- an existing '
    + 'installation''s data is already here.',
    False, '');
  DataDirPage.Add('');
  DataDirPage.Values[0] := ExpandConstant('{localappdata}\{#AppName}');
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  // A data directory given on the command line wins; do not ask about it.
  Result := (DataDirPage <> nil) and (PageID = DataDirPage.ID)
            and (GetCommandLineValue('DATADIR') <> '');
end;

// ---------------------------------------------------------------------------
// Make room for the new files: a running back end holds the databases and the
// cameras' RTSP sessions, and a running front end holds the DLLs we are about
// to overwrite.
// ---------------------------------------------------------------------------
procedure StopRunningInstance;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\sc.exe'), 'stop {#ServiceName}', '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);
  // Give the service a moment to take its back end down cleanly before the
  // blunt instrument below.
  Sleep(3000);
  Exec(ExpandConstant('{sys}\taskkill.exe'),
       '/F /T /IM SighthoundPy3.exe /IM SighthoundPy3c.exe '
       + '/IM SighthoundPy3Service.exe /IM SighthoundPy3-ffmpeg.exe '
       + '/IM {#AppExeName}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopRunningInstance;
  Result := '';
end;

// ---------------------------------------------------------------------------
// Record the data directory where the app, the launcher and the service can
// all read it, and make sure it exists and is writable by the user (the
// service runs as LocalSystem and would otherwise create it with whatever
// inherited rights the parent happened to have).
// ---------------------------------------------------------------------------
procedure WriteDataDirRecord;
var
  DataDir: String;
  ResultCode: Integer;
begin
  DataDir := GetDataDir('');
  ForceDirectories(DataDir);
  ForceDirectories(DataDir + '\logs');
  SaveStringToFile(ExpandConstant('{app}\datadir.txt'), AnsiString(DataDir),
                   False);

  // Only widen rights when the directory is NOT inside the user's own profile,
  // where they already have them.
  if Pos(Uppercase(ExpandConstant('{localappdata}')), Uppercase(DataDir)) <> 1 then
    Exec(ExpandConstant('{sys}\icacls.exe'),
         '"' + DataDir + '" /grant "*S-1-5-32-545":(OI)(CI)M /T /C /Q', '',
         SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

// ---------------------------------------------------------------------------
// Service registration.
//
// Deliberately not [Run] entries. A [Run] entry is fire-and-forget: it runs
// hidden, its exit code is thrown away and its output goes nowhere, so a
// registration that failed on its first line still left the wizard reporting a
// clean install and the user hunting for a service that was never created.
// Here the exit code decides whether the user is told, and the child's own
// output -- which is where the actual reason lives -- is kept.
//
// A failure is a warning, not a fatal error: without the service the app still
// runs, it just cannot keep recording once the front end is closed. Refusing to
// install over it would be a worse trade.
// ---------------------------------------------------------------------------
function RunServiceCommand(const Args, Transcript: String;
                           var Output: String): Integer;
var
  LogFile, CommandLine: String;
  Captured: AnsiString;
  Code: Integer;
begin
  LogFile := GetDataDir('') + '\logs\' + Transcript;
  DeleteFile(LogFile);

  // Through cmd.exe purely for the redirection: Exec() hands back an exit code
  // and nothing else, and an exit code on its own is what made the original
  // failure undiagnosable. The doubled outer quotes are cmd's /C convention --
  // it strips the first and last one, leaving the inner quoting intact.
  CommandLine := '/C ""' + ExpandConstant('{app}\{#InterpreterConsole}') + '" '
                 + Args + ' > "' + LogFile + '" 2>&1"';

  if Exec(ExpandConstant('{cmd}'), CommandLine, ExpandConstant('{app}'),
          SW_HIDE, ewWaitUntilTerminated, Code) then
    Result := Code
  else
    Result := -1;  // Could not even start cmd.exe.

  Captured := '';
  LoadStringFromFile(LogFile, Captured);
  Output := Trim(String(Captured));
end;

procedure ReportServiceFailure(const What, Transcript, Output: String;
                               Code: Integer);
var
  Detail: String;
begin
  Log('ERROR: {#ServiceName} could not be ' + What + ' (exit code '
      + IntToStr(Code) + ').');

  Detail := Output;
  if Detail = '' then
    Detail := '(the command produced no output)'
  else
  begin
    Log('{#ServiceName} output: ' + Detail);
    // Keep the tail: a Python traceback puts the useful line last.
    if Length(Detail) > 1000 then
      Detail := '...' + Copy(Detail, Length(Detail) - 1000, MaxInt);
  end;

  SuppressibleMsgBox(
    'The {#ServiceName} service could not be ' + What + '.'#13#10#13#10
    + '{#AppName} is installed and will run, but the back end will only run '
    + 'while the app is open -- cameras will not keep recording after you '
    + 'close it.'#13#10#13#10
    + Detail + #13#10#13#10
    + 'The same detail is in the setup log and in:'#13#10
    + GetDataDir('') + '\logs\' + Transcript,
    mbError, MB_OK, IDOK);
end;

procedure SetStatus(const Message: String);
begin
  // WizardForm exists but is hidden under /SILENT, and there is no promise it
  // is there at all; the status line is decoration, so never fail over it.
  if WizardForm <> nil then
    WizardForm.StatusLabel.Caption := Message;
end;

procedure InstallBackEndService;
var
  Args, Output: String;
  Code: Integer;
begin
  if not WizardIsTaskSelected('service') then
  begin
    Log('{#ServiceName}: service task not selected, skipping registration.');
    Exit;
  end;

  // --local-system (see GetServiceAccountArgs) keeps this promptless: a service
  // running under a named account needs that account's password, and the whole
  // point here is an install that asks the user for nothing. The data directory
  // is passed explicitly precisely BECAUSE the account is LocalSystem -- see
  // launch\InstallService.py for why deriving it from a profile is wrong.
  Args := '-s -m launch.InstallService install ' + GetServiceAccountArgs('')
          + ' --install-dir "' + ExpandConstant('{app}') + '"'
          + ' --data-dir "' + GetDataDir('') + '"'
          + ' --grant-user "' + GetGrantUser('') + '"';

  SetStatus('Registering the {#ServiceName} service...');
  Code := RunServiceCommand(Args, 'SHLaunchPY3-install.log', Output);
  if Code <> 0 then
  begin
    ReportServiceFailure('registered', 'SHLaunchPY3-install.log', Output, Code);
    Exit;
  end;

  SetStatus('Starting the {#ServiceName} service...');
  Code := RunServiceCommand('-s -m launch.InstallService start',
                            'SHLaunchPY3-start.log', Output);
  if Code <> 0 then
    ReportServiceFailure('started', 'SHLaunchPY3-start.log', Output, Code)
  else
    Log('{#ServiceName} registered and started.');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    // Order matters: the service is told where the data directory is, and
    // RunServiceCommand writes its transcript into that directory's logs.
    WriteDataDirRecord;
    InstallBackEndService;
  end;
end;

// ---------------------------------------------------------------------------
// Uninstall: stop everything first, and leave the user's data alone unless
// they say otherwise.
// ---------------------------------------------------------------------------
function InitializeUninstall: Boolean;
begin
  StopRunningInstance;
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Recorded: AnsiString;
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    Recorded := '';
    LoadStringFromFile(ExpandConstant('{app}\datadir.txt'), Recorded);
    DataDir := Trim(String(Recorded));
    if DataDir = '' then
      DataDir := ExpandConstant('{localappdata}\{#AppName}');
    if DirExists(DataDir) then
      if SuppressibleMsgBox('Remove recordings index, settings, rules and face '
           + 'enrollments as well?'#13#10#13#10 + DataDir
           + #13#10#13#10 + 'Choose No to keep them for a future install.',
           mbConfirmation, MB_YESNO, IDNO) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
