# actions/reminder.py

import subprocess
import json
import os
import sys
import uuid
from datetime import datetime
from xml.sax.saxutils import escape as xml_escape


def reminder(
    parameters: dict,
    response: str | None = None,
    player=None,
    session_memory=None
) -> str:
    """
    Sets a timed reminder using Windows Task Scheduler.

    parameters:
        - date    (str) YYYY-MM-DD
        - time    (str) HH:MM
        - message (str)

    Returns a result string — Live API voices it automatically.
    No edge_speak needed.
    """

    date_str = parameters.get("date")
    time_str = parameters.get("time")
    message  = parameters.get("message", "Reminder")

    if not date_str or not time_str:
        return "I need both a date and a time to set a reminder."

    try:
        target_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")

        if target_dt <= datetime.now():
            return "That time is already in the past."

        task_name    = f"MARKReminder_{target_dt.strftime('%Y%m%d_%H%M')}_{uuid.uuid4().hex[:8]}"
        safe_message = str(message or "Reminder").strip()[:200]
        safe_message = "".join(
            ch for ch in safe_message
            if ch in "\t\r\n" or ord(ch) >= 0x20
        )
        message_literal = json.dumps(safe_message, ensure_ascii=False)
        xml_message = xml_escape(safe_message)

        python_exe = sys.executable
        if python_exe.lower().endswith("python.exe"):
            pythonw = python_exe.replace("python.exe", "pythonw.exe")
            if os.path.exists(pythonw):
                python_exe = pythonw

        temp_dir      = os.environ.get("TEMP", "C:\\Temp")
        notify_script = os.path.join(temp_dir, f"{task_name}.pyw")
        project_root  = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..")
        )

        project_root_literal = json.dumps(project_root, ensure_ascii=False)
        script_code = f'''import sys, os, time
sys.path.insert(0, {project_root_literal})

try:
    import winsound
    for freq in [800, 1000, 1200]:
        winsound.Beep(freq, 200)
        time.sleep(0.1)
except Exception:
    pass

try:
    from win10toast import ToastNotifier
    ToastNotifier().show_toast(
        "MARK Reminder",
        {message_literal},
        duration=15,
        threaded=False
    )
except Exception:
    try:
        import subprocess
        subprocess.run(["msg", "*", "/TIME:30", {message_literal}], shell=False)
    except Exception:
        pass

time.sleep(3)
try:
    os.remove(__file__)
except Exception:
    pass
'''
        with open(notify_script, "w", encoding="utf-8") as f:
            f.write(script_code)

        xml_content = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>MARK Reminder: {xml_message}</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>{target_dt.strftime("%Y-%m-%dT%H:%M:%S")}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Actions>
    <Exec>
      <Command>{xml_escape(python_exe)}</Command>
      <Arguments>{xml_escape(json.dumps(notify_script, ensure_ascii=False))}</Arguments>
    </Exec>
  </Actions>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <WakeToRun>true</WakeToRun>
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Principals>
    <Principal>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
</Task>'''

        xml_path = os.path.join(temp_dir, f"{task_name}.xml")
        with open(xml_path, "w", encoding="utf-16") as f:
            f.write(xml_content)

        result = subprocess.run(
            ["schtasks", "/Create", "/TN", task_name, "/XML", xml_path, "/F"],
            shell=False,
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        try:
            os.remove(xml_path)
        except Exception:
            pass

        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()
            print(f"[Reminder] ❌ schtasks failed: {err}")
            try:
                os.remove(notify_script)
            except Exception:
                pass
            return "I couldn't schedule the reminder due to a system error."

        verification = subprocess.run(
            ["schtasks", "/Query", "/TN", task_name, "/FO", "LIST"],
            shell=False,
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        verification_text = (verification.stdout or "").strip()
        if verification.returncode != 0 or task_name not in verification_text:
            verify_err = verification.stderr.strip() or verification_text or "task was not present in the scheduler query result"
            print(f"[Reminder] ❌ scheduled task verification failed: {verify_err}")
            cleanup = subprocess.run(
                ["schtasks", "/Delete", "/TN", task_name, "/F"],
                shell=False,
                capture_output=True,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if cleanup.returncode != 0:
                print(f"[Reminder] ⚠️ unable to remove unverified scheduled task: {cleanup.stderr.strip() or cleanup.stdout.strip()}")
            try:
                os.remove(notify_script)
            except Exception:
                pass
            return "I couldn't verify that the reminder was registered in Windows Task Scheduler."

        if player:
            player.write_log(f"[reminder] set for {date_str} {time_str}")

        return f"Reminder set for {target_dt.strftime('%B %d at %I:%M %p')}."

    except ValueError:
        return "I couldn't understand that date or time format."

    except Exception as e:
        return f"Something went wrong while scheduling the reminder: {str(e)[:80]}"