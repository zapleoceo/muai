# Установка слушателя: venv, зависимости, задача планировщика, конфиг.
# Запускать из СВОЕЙ сессии Windows (обычный PowerShell, без админа).
[CmdletBinding()]
param(
    [string]$Root = "$env:LOCALAPPDATA\vera-listener",
    [string]$TaskName = "VeraListener",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$source = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Задача $TaskName снята. Очередь и конфиг остались в $Root"
    return
}

$python = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python3.12 -ErrorAction SilentlyContinue }
if (-not $python) {
    throw "Нет Python 3.12. Поставь: winget install --id Python.Python.3.12 --scope user"
}

$venv = Join-Path $Root "venv"
if (-not (Test-Path $venv)) {
    Write-Host "Создаю venv в $venv"
    & $python.FullName -m venv $venv
}
$venvPy = Join-Path $venv "Scripts\python.exe"
$venvPyw = Join-Path $venv "Scripts\pythonw.exe"

Write-Host "Ставлю зависимости (это надолго: faster-whisper тянет ~300 МБ)"
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install --quiet $source

& $venvPy -m vera_listener --setup

# Одна задача с двумя триггерами: вход в систему поднимает слушателя, а
# повтор раз в 15 минут работает вотчдогом — новый запуск игнорируется, пока
# процесс жив, и подхватывает его, если тот умер.
$action = New-ScheduledTaskAction -Execute $venvPyw -Argument "-m vera_listener" -WorkingDirectory $Root
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration ([TimeSpan]::MaxValue)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
# Interactive: слушателю нужны аудио-устройства СВОЕЙ сессии Windows. Служба в
# сессии 0 их не слышит вообще — это свойство Windows, а не настройка.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($atLogon, $watchdog) `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Host ""
Write-Host "Готово. Дальше:"
Write-Host "  1. впиши INTERNAL_SECRET в $env:USERPROFILE\.vera\listener.env"
Write-Host "  2. проверь захват:  $venvPy -m vera_listener --probe 15"
Write-Host "  3. запусти задачу:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "  логи: $Root\listener.log   очередь: $Root\queue"
