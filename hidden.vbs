' Runs a command with no visible console window (used by the scheduled tasks)
Set sh = CreateObject("Wscript.Shell")
sh.Run """" & WScript.Arguments(0) & """", 0, False
