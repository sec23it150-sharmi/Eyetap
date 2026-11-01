from voice_test import CommandParser

p = CommandParser()
tests = [
    "open link",
    "follow link",
    "click link",
    "scroll up",
    "scroll down",
    "scroll up three",
    "click",
    "double click",
    "right click",
    "new tab",
    "close tab",
    "go to youtube",
    "search for python tutorials",
    "open chrome",
    "copy",
    "paste",
]
print("=== Command Parser Verification ===")
all_ok = True
for t in tests:
    cmd = p.parse(t, strict=True)
    status = "OK" if cmd else "MISSING"
    if not cmd:
        all_ok = False
    action = cmd.action if cmd else "None"
    arg = str(cmd.arg) if cmd and cmd.arg else ""
    print(f"  [{status}]  {t!r:35s} -> {action}  {arg}")

print()
print("ALL PASS" if all_ok else "SOME COMMANDS FAILED - check above")
