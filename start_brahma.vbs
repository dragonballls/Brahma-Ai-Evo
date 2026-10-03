Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)

venvPython = root & "\.venv\Scripts\pythonw.exe"
mainPy = root & "\main.py"
bootstrap = root & "\bootstrap.ps1"
powershell = shell.ExpandEnvironmentStrings("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")

If fso.FileExists(venvPython) And fso.FileExists(mainPy) Then
  ' Normal launch uses the repository's verified Python 3.12 virtual environment.
  shell.Run Chr(34) & venvPython & Chr(34) & " " & Chr(34) & mainPy & Chr(34) & " --startup", 0, False
ElseIf fso.FileExists(bootstrap) Then
  ' Missing runtime: let the canonical bootstrap provision Python/Node/venv.
  shell.Run Chr(34) & powershell & Chr(34) & " -NoProfile -ExecutionPolicy Bypass -File " & Chr(34) & bootstrap & Chr(34), 0, False
Else
  MsgBox "Brahma Evo bootstrap.ps1 was not found.", 16, "Brahma Evo"
End If
