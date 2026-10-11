"""Windows autostart, with the same three-tier fallback quota-burndown uses.

  1. a scheduled task at logon plus a one-minute repeating trigger, which is
     both the starter and the watchdog (IgnoreNew means the repeat is a no-op
     while the service is alive)
  2. if task registration is denied, a separate watchdog task that curls the
     status endpoint and relaunches on failure
  3. if that is denied too, a per-user Startup shortcut running `serve
     --supervise`, which supervises itself

Registration is an install-time act; the serving runtime never installs itself.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .config import DEFAULT_PORT, SERVICE_TASK_NAME, WATCHDOG_TASK_NAME, project_root

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def launcher_path() -> Path:
    return project_root() / "ado-swimlane.py"


def _quote(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _powershell(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", "$ErrorActionPreference='Stop'; " + script],
        capture_output=True,
        text=True,
        creationflags=NO_WINDOW,
    )


def _interpreter() -> Path:
    # pythonw.exe keeps the windowless service from flashing a console at logon.
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    return windowless if windowless.exists() else exe


def startup_shortcut() -> Path:
    roaming = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return roaming / "Microsoft/Windows/Start Menu/Programs/Startup" / (SERVICE_TASK_NAME + ".lnk")


def _install_startup(interpreter: Path, arguments: str) -> str:
    shortcut = startup_shortcut()
    shortcut.parent.mkdir(parents=True, exist_ok=True)
    supervisor = arguments + " --supervise"
    script = (
        "$shell=New-Object -ComObject WScript.Shell; "
        f"$link=$shell.CreateShortcut({_quote(shortcut)}); "
        f"$link.TargetPath={_quote(interpreter)}; $link.Arguments={_quote(supervisor)}; "
        f"$link.WorkingDirectory={_quote(project_root())}; $link.WindowStyle=7; $link.Save(); "
        f"Start-Process -FilePath {_quote(interpreter)} -ArgumentList {_quote(supervisor)} -WindowStyle Hidden"
    )
    result = _powershell(script)
    if result.returncode:
        return f"startup install failed ({result.returncode}): {(result.stderr or result.stdout).strip()}"
    return f"installed per-user Startup supervisor {shortcut}; Task Scheduler registration was denied"


def _install_watchdog(interpreter: Path, arguments: str, port: int) -> str:
    health = (
        f"try {{ Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:{port}/v1/status' -TimeoutSec 3 | Out-Null }} "
        f"catch {{ Start-Process -FilePath {_quote(interpreter)} -ArgumentList {_quote(arguments)} -WindowStyle Hidden }}"
    )
    watchdog_args = subprocess.list2cmdline(
        ["-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", health]
    )
    script = (
        f"$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument {_quote(watchdog_args)}; "
        "$logon = New-ScheduledTaskTrigger -AtLogOn; "
        "$watch = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) "
        "-RepetitionInterval (New-TimeSpan -Minutes 1); "
        "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 1); "
        f"Register-ScheduledTask -TaskName '{WATCHDOG_TASK_NAME}' -Action $a -Trigger @($logon,$watch) "
        "-Settings $s -Force | Out-Null; "
        f"Start-ScheduledTask -TaskName '{WATCHDOG_TASK_NAME}'"
    )
    result = _powershell(script)
    if result.returncode:
        return f"watchdog install failed ({result.returncode}): {(result.stderr or result.stdout).strip()}"
    return f"installed {WATCHDOG_TASK_NAME}; a one-minute health check keeps the service up"


def install(apply: bool = False, home: str | None = None, port: int = DEFAULT_PORT) -> str:
    if os.name != "nt":
        return "Start `ado-swimlane serve` from your user service manager (systemd --user, launchd)."
    interpreter = _interpreter()
    arguments = subprocess.list2cmdline(
        ["-B", str(launcher_path()), *(["--home", home] if home else []), "serve", "--port", str(port)]
    )
    script = (
        f"$a = New-ScheduledTaskAction -Execute {_quote(interpreter)} -Argument {_quote(arguments)}; "
        "$logon = New-ScheduledTaskTrigger -AtLogOn; "
        "$watch = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) "
        "-RepetitionInterval (New-TimeSpan -Minutes 1); "
        "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) "
        "-RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1); "
        f"Register-ScheduledTask -TaskName '{SERVICE_TASK_NAME}' -Action $a -Trigger @($logon,$watch) "
        "-Settings $s -Force | Out-Null; "
        f"Start-ScheduledTask -TaskName '{SERVICE_TASK_NAME}'"
    )
    if not apply:
        return (f"would register {SERVICE_TASK_NAME} (logon + 1-minute watchdog) running:\n"
                f"  {interpreter} {arguments}\nRe-run with --apply to install.")
    result = _powershell(script)
    if result.returncode:
        denied = "0x80070005" in (result.stderr or "") or "Access is denied" in (result.stderr or "")
        if denied:
            watchdog = _install_watchdog(interpreter, arguments, port)
            if not watchdog.startswith("watchdog install failed"):
                return watchdog
            return _install_startup(interpreter, arguments)
        return f"install failed ({result.returncode}): {(result.stderr or result.stdout).strip()}"
    return f"installed and started {SERVICE_TASK_NAME}; logon trigger plus a one-minute watchdog"


def uninstall(apply: bool = False) -> str:
    if os.name != "nt":
        return "Nothing installed by this tool on this platform."
    messages = []
    shortcut = startup_shortcut()
    if shortcut.exists():
        if apply:
            shortcut.unlink()
            messages.append(f"removed {shortcut}")
        else:
            messages.append(f"would remove {shortcut}")
    for name in (SERVICE_TASK_NAME, WATCHDOG_TASK_NAME):
        if not apply:
            messages.append(f"would remove scheduled task {name}")
            continue
        subprocess.run(["schtasks", "/End", "/TN", name], capture_output=True, text=True)
        result = subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"], capture_output=True, text=True)
        messages.append(f"removed {name}" if result.returncode == 0 else f"{name}: not installed")
    messages.append("a running process stays up until it is stopped or you sign out")
    return "\n".join(messages)


def status() -> str:
    if os.name != "nt":
        return "n/a (not Windows)"
    lines = []
    for name in (SERVICE_TASK_NAME, WATCHDOG_TASK_NAME):
        result = subprocess.run(["schtasks", "/Query", "/TN", name, "/FO", "LIST"], capture_output=True, text=True)
        if result.returncode:
            lines.append(f"{name}: not installed")
            continue
        state = next(
            (line.split(":", 1)[1].strip() for line in result.stdout.splitlines() if line.lower().startswith("status:")),
            "unknown",
        )
        lines.append(f"{name}: {state}")
    lines.append(f"startup shortcut: {'present' if startup_shortcut().exists() else 'absent'}")
    return "\n".join(lines)
