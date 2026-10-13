Option Explicit
Dim args, shell, command, code
Set args = WScript.Arguments
If args.Count <> 4 Then WScript.Quit 2
Function Quote(value)
    Quote = Chr(34) & Replace(value, Chr(34), Chr(34) & Chr(34)) & Chr(34)
End Function
command = Quote(args(0)) & " -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File " & Quote(args(1)) & " -Server " & Quote(args(2)) & " -Destination " & Quote(args(3))
Set shell = CreateObject("WScript.Shell")
code = shell.Run(command, 0, True)
WScript.Quit code
