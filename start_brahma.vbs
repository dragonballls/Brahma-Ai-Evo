Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)

venvPython = root & "\.venv\Scripts\pythonw.exe"
mainPy = root & "\main.py"
supervisorPy = root & "\core\process_supervisor.py"
supervisorExe = root & "\BrahmaEvoSupervisor.exe"
bootstrap = root & "\bootstrap.ps1"
powershell = shell.ExpandEnvironmentStrings("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")

If fso.FileExists(supervisorExe) Then
  ' Installed build: the lightweight supervisor owns the app lifecycle.
  shell.Run Chr(34) & supervisorExe & Chr(34), 0, False
ElseIf fso.FileExists(bootstrap) And fso.FileExists(mainPy) Then
  ' Source checkout: always enter the canonical bootstrap first. It validates
  ' Python/Node, repairs a damaged .venv/dependencies, then launches the
  ' independent supervisor. This prevents a present-but-broken venv from
  ' bypassing repair and leaving the app unstartable.
  shell.Run Chr(34) & powershell & Chr(34) & " -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " & Chr(34) & bootstrap & Chr(34), 0, False
ElseIf fso.FileExists(venvPython) And fso.FileExists(supervisorPy) And fso.FileExists(mainPy) Then
  ' Last-resort source launch when the bootstrap script is unavailable.
  shell.Run Chr(34) & venvPython & Chr(34) & " " & Chr(34) & supervisorPy & Chr(34), 0, False
Else
  MsgBox "Brahma Evo bootstrap.ps1 was not found.", 16, "Brahma Evo"
End If
