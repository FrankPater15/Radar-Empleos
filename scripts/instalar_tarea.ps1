<#
.SYNOPSIS
  Registra la tarea programada diaria del Radar de Empleo CO (nivel de usuario, sin administrador).

.DESCRIPTION
  Cada día a las 7:30 (o al encender el PC si estaba apagado) ejecuta
  "pythonw.exe -m radar.run" con el directorio de trabajo en la raíz del proyecto.
  Solo trae y puntúa ofertas: nunca postula ni contacta a nadie.
  Los detalles de cada corrida quedan en logs\radar.log.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\instalar_tarea.ps1
  powershell -ExecutionPolicy Bypass -File scripts\instalar_tarea.ps1 -Hora 08:00 -Argumentos "--limit 60"
#>
[CmdletBinding()]
param(
    [string]$NombreTarea = "RadarEmpleoCO",
    [string]$Hora = "07:30",
    [string]$Argumentos = ""
)

$ErrorActionPreference = "Stop"

$proyecto = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonw = Join-Path $proyecto ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) {
    throw "No encuentro $pythonw. Crea primero el entorno: python -m venv .venv; .venv\Scripts\pip install -r requirements.txt"
}

$argumentosRadar = ("-m radar.run " + $Argumentos).Trim()
$accion = New-ScheduledTaskAction -Execute $pythonw -Argument $argumentosRadar -WorkingDirectory $proyecto
$disparador = New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($Hora, "HH:mm", $null))
$ajustes = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)
# Interactive: corre con tu sesión iniciada (así se ve la notificación) y no guarda contraseña
$usuario = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $usuario -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $NombreTarea -Action $accion -Trigger $disparador -Settings $ajustes `
    -Principal $principal -Force `
    -Description "Radar de Empleo CO: ingesta y puntuación diaria. No postula ni contacta a nadie." | Out-Null

Write-Host "Tarea '$NombreTarea' registrada: todos los días a las $Hora (o al encender el PC)."
Write-Host "Probarla ahora:    Start-ScheduledTask -TaskName $NombreTarea"
Write-Host "Último resultado:  Get-ScheduledTaskInfo -TaskName $NombreTarea | Select-Object LastRunTime, LastTaskResult"
Write-Host "Log:               Get-Content `"$proyecto\logs\radar.log`" -Tail 30"
Write-Host "Quitarla:          powershell -ExecutionPolicy Bypass -File scripts\desinstalar_tarea.ps1"
