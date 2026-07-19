' Launch the text-to-speech tool without a console window.
' Double-click this file to start. To restart: right-click the
' on-screen icon, choose Exit, then double-click this file again.
' NOTE: keep this file ASCII-only (wscript reads VBS as ANSI).
Set fso = CreateObject("Scripting.FileSystemObject")
Set WshShell = CreateObject("WScript.Shell")

q = Chr(34) ' double quote
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
mainPy = scriptDir & "\main.py"
home = WshShell.ExpandEnvironmentStrings("%USERPROFILE%")

' Use the real pythonw.exe (pyenv-win shims are .bat files that
' WshShell.Run cannot resolve from a bare "pythonw" command).
pythonw = home & "\.pyenv\pyenv-win\versions\3.10.11\pythonw.exe"

If fso.FileExists(pythonw) Then
    WshShell.Run q & pythonw & q & " " & q & mainPy & q, 0, False
Else
    ' Fallback after a Python upgrade: run the shim through cmd.
    shim = home & "\.pyenv\pyenv-win\shims\pythonw.bat"
    WshShell.Run "cmd /c " & q & q & shim & q & " " & q & mainPy & q & q, 0, False
End If

Set WshShell = Nothing
Set fso = Nothing
