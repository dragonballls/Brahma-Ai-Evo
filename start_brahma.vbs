Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)

venvPython = root & "\.venv\Scripts\pythonw.exe"
mainPy = root & "\main.py"
supervisorExe = root & "\BrahmaEvo_Supervisor.exe"
supervisorPy = root & "\scripts\recovery_supervisor.py"
bootstrap = root & "\bootstrap.ps1"
powershell = shell.ExpandEnvironmentStrings("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")

If fso.FileExists(supervisorExe) Then
  ' Packaged installs launch the external supervisor so a Brahma crash does not end recovery.
  shell.Run Chr(34) & supervisorExe & Chr(34), 0, False
ElseIf fso.FileExists(venvPython) And fso.FileExists(supervisorPy) Then
  ' Source launches use the verified Python 3.12 environment plus the external recovery supervisor.
  shell.Run Chr(34) & venvPython & Chr(34) & " " & Chr(34) & supervisorPy & Chr(34), 0, False
ElseIf fso.FileExists(venvPython) And fso.FileExists(mainPy) Then
  ' Emergency fallback: start Brahma directly if the supervisor is missing.
  shell.Run Chr(34) & venvPython & Chr(34) & " " & Chr(34) & mainPy & Chr(34) & " --startup", 0, False
ElseIf fso.FileExists(bootstrap) Then
  ' Missing runtime: let the canonical bootstrap provision Python/Node/venv.
  shell.Run Chr(34) & powershell & Chr(34) & " -NoProfile -ExecutionPolicy Bypass -File " & Chr(34) & bootstrap & Chr(34), 0, False
Else
  MsgBox "Brahma Evo bootstrap.ps1 was not found.", 16, "Brahma Evo"
End If
