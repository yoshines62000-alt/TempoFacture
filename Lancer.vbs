' Lance l'interface graphique de TempoFacture sans ouvrir de console.
' Double-cliquez simplement sur ce fichier pour demarrer l'application.

Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

Set shell = CreateObject("WScript.Shell")
command = "pythonw.exe """ & scriptDir & "\gui.py"""
shell.CurrentDirectory = scriptDir

On Error Resume Next
shell.Run command, 0, False
If Err.Number <> 0 Then
    MsgBox "Impossible de lancer l'application." & vbCrLf & _
           "Verifiez que Python est installe et accessible (pythonw.exe)," & vbCrLf & _
           "et que les dependances sont installees (pip install -r requirements.txt)." & vbCrLf & vbCrLf & _
           "Detail : " & Err.Description, vbExclamation, "TempoFacture"
End If
On Error Goto 0
