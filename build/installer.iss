; Inno Setup script for SPX Income Trader
;
; Prerequisites:
;   1. Install Inno Setup 6+ from https://jrsoftware.org/isinfo.php
;   2. Run build/build_windows.py to produce dist/SPXIncomeTrader/
;   3. Compile this script via Inno Setup Compiler (ISCC.exe)
;
; Output: build/Output/SPXIncomeTrader_Setup.exe

#define AppName      "SPX Income Trader"
#define AppExeName   "SPXIncomeTrader.exe"
#define AppVersion   "1.0.0"
#define AppPublisher "SPX Income Trader"
#define AppURL       "https://github.com/your-org/spx-income-trader"
#define SourceDir    "..\dist\SPXIncomeTrader"
#define IconFile     "..\assets\icon.ico"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=SPXIncomeTrader_Setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Bundle the entire PyInstaller output directory
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Start Menu shortcut
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\{#AppExeName}"
; Desktop shortcut (optional, unchecked by default)
Name: "{commondesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon; IconFilename: "{app}\{#AppExeName}"

[Run]
; Offer to launch after install
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
