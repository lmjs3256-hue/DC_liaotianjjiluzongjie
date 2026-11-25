' DTAO community fetch: keep the real-Edge CDP guard alive (hidden, no console).
' profile = copy of logged-in Edge; token expires ~2026-12-02, re-login + recopy then.
Set ws = CreateObject("WScript.Shell")
dir = "D:\UGit\DC_price_talkzongjie\DC_liaotianjjiluzongjie"
pyw = "C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
If Not CreateObject("Scripting.FileSystemObject").FileExists(pyw) Then WScript.Quit
ws.CurrentDirectory = dir
ws.Environment("PROCESS")("DTT_EDGE_PROFILE") = "C:\Users\Administrator\dtt_edge_profile"
ws.Environment("PROCESS")("DTT_CDP_PORT") = "9444"
ws.Run """" & pyw & """ """ & dir & "\dtt_cdp_guard.py""", 0, False
