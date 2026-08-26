# Автозапуск синка сессий Claude Code: задача VeraClaudeSync, раз в час.
#
# Регистрация — через schtasks /XML, а не Register-ScheduledTask: CIM-путь на
# этой машине отбивается политикой с 0x80070005 (поймано вживую 2026-08-26,
# та же команда с теми же правами через schtasks проходит).
#
# ФАЙЛ ОБЯЗАН БЫТЬ В UTF-8 С BOM И С CRLF: Windows PowerShell 5.1 читает .ps1
# без BOM как ANSI. `.gitattributes` в корне держит `*.ps1 text eol=crlf`.
[CmdletBinding()]
param(
    [string]$TaskName = "VeraClaudeSync"
)

$ErrorActionPreference = "Stop"
$script = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "claude_chat_sync.py"
if (-not (Test-Path $script)) { throw "Нет $script" }

$python = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python.exe -ErrorAction SilentlyContinue }
if (-not $python) { throw "Не нашёл Python в PATH" }

$user = "$env:USERDOMAIN\$env:USERNAME"
$start = (Get-Date).Date.AddDays(-1).ToString("yyyy-MM-ddTHH:mm:ss")

# Часовой повтор + запуск при входе; параллельный экземпляр игнорируется
# (IgnoreNew), иначе второй проход возьмётся за те же сессии. Лимит времени
# 4 часа: осмысление большого бэкфилла — это долго, но не навсегда.
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>$start</StartBoundary>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
      <Repetition>
        <Interval>PT1H</Interval>
        <Duration>P1D</Duration>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <Enabled>true</Enabled>
    </CalendarTrigger>
    <LogonTrigger>
      <UserId>$user</UserId>
      <Delay>PT5M</Delay>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$user</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT4H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$($python.Source)</Command>
      <Arguments>"$script"</Arguments>
      <WorkingDirectory>$(Split-Path -Parent $script)</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

$xmlFile = Join-Path $env:TEMP "VeraClaudeSync.xml"
# Unicode обязателен: в шапке XML заявлен UTF-16, schtasks читает буквально.
$xml | Out-File $xmlFile -Encoding Unicode
schtasks /Create /F /TN $TaskName /XML $xmlFile | Out-Null
Remove-Item $xmlFile

Write-Host "Задача $TaskName зарегистрирована: раз в час + при входе."
Write-Host "Запустить сейчас:  schtasks /Run /TN $TaskName"
Write-Host "Посмотреть, что отправит: python `"$script`" --dry-run"
