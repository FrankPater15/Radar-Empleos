<#
.SYNOPSIS
  Elimina la tarea programada del Radar de Empleo CO (no borra la base de datos ni los logs).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\desinstalar_tarea.ps1
#>
[CmdletBinding()]
param(
    [string]$NombreTarea = "RadarEmpleoCO"
)

$ErrorActionPreference = "Stop"

if (Get-ScheduledTask -TaskName $NombreTarea -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $NombreTarea -Confirm:$false
    Write-Host "Tarea '$NombreTarea' eliminada."
} else {
    Write-Host "No existe la tarea '$NombreTarea'; no hay nada que quitar."
}
