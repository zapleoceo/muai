# Автозапуск синка сессий Claude Code: задача VeraClaudeSync, раз в час.
#
# ФАЙЛ ОБЯЗАН БЫТЬ В UTF-8 С BOM И С CRLF: Windows PowerShell 5.1 читает .ps1
# без BOM как ANSI, кириллица в комментариях рассыпается и файл не парсится.
# `.gitattributes` в корне держит `*.ps1 text eol=crlf`.
[CmdletBinding()]
param(
    [string]$TaskName = "VeraClaudeSync",
    [int]$EveryMinutes = 60
)

$ErrorActionPreference = "Stop"
$script = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "claude_chat_sync.py"
if (-not (Test-Path $script)) { throw "Нет $script" }

$python = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python.exe -ErrorAction SilentlyContinue }
if (-not $python) { throw "Не нашёл Python в PATH" }

# Осмысление длинной сессии — несколько вызовов модели подряд, поэтому проход
# бывает долгим. Ограничение времени снимаем, а параллельный запуск глушим:
# иначе второй проход возьмётся за те же сессии.
$action = New-ScheduledTaskAction -Execute $python.Source `
    -Argument "`"$script`"" -WorkingDirectory (Split-Path -Parent $script)
$trigger = New-ScheduledTaskTrigger -AtLogOn
$trigger.Delay = "PT5M"
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 365)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 4)

Register-ScheduledTask -TaskName $TaskName -Action $action `
    -Trigger @($trigger, $repeat) -Settings $settings -Force | Out-Null

Write-Host "Задача $TaskName зарегистрирована: раз в $EveryMinutes мин."
Write-Host "Запустить сейчас:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Посмотреть, что отправит: python `"$script`" --dry-run"
