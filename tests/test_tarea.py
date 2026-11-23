"""Scripts de la tarea programada: sintaxis válida y parámetros clave (no se registra nada)."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def test_install_script_has_required_settings():
    s = (SCRIPTS / "instalar_tarea.ps1").read_text(encoding="utf-8-sig")
    for needle in ["Register-ScheduledTask", "-StartWhenAvailable", "-RunOnlyIfNetworkAvailable",
                   "pythonw.exe", "-WorkingDirectory", "-m radar.run", '"07:30"', "-Daily"]:
        assert needle in s, needle
    assert "Unregister-ScheduledTask" in (SCRIPTS / "desinstalar_tarea.ps1").read_text(encoding="utf-8-sig")


@pytest.mark.skipif(sys.platform != "win32" or not shutil.which("powershell"), reason="solo en Windows")
@pytest.mark.parametrize("name", ["instalar_tarea.ps1", "desinstalar_tarea.ps1"])
def test_ps1_parses(name):
    cmd = ("$t=$null;$e=$null;[System.Management.Automation.Language.Parser]::ParseFile("
           f"'{SCRIPTS / name}',[ref]$t,[ref]$e)|Out-Null;$e.Count")
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                         capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "0", out.stdout + out.stderr
