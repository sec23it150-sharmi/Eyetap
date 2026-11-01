"""
voice_control.py - standalone, offline voice control for hands-free computing.

Designed to be merged later with gaze cursor control (e.g. on AR glasses):
  * Gaze moves the cursor; voice acts AT the cursor ("click", "scroll down", ...).
  * No gaze code is needed here. The only coupling points are:
      - VoiceController(on_command=...)  -> receive every parsed Command
      - VoiceController(executor=...)    -> swap in your own action backend
        (e.g. one that uses the gaze point instead of the OS cursor)

Install:
    pip install vosk sounddevice pyautogui
    Download a model from https://alphacephei.com/vosk/models
    (small: vosk-model-small-en-us-0.15) and unzip it.

Run:
    python voice_control.py --model path/to/vosk-model-small-en-us-0.15
    python voice_control.py --text --dry-run        # test without mic/model

Commands (say them naturally):
    click | double click | right click | middle click
    grab / drop                      (press & hold / release: drag with gaze)
    scroll up|down|left|right [n]
    press <key>                      (enter, escape, tab, space, backspace,
                                      delete, up, down, left, right, home, end)
    copy | paste | cut | undo | redo | select all | save | find
    next tab | previous tab | close tab | new tab | switch window | go back
    zoom in | zoom out
    open <ANY app>                   ("open spotify", "open word", "open settings"...)
    close window | minimize | maximize | show desktop | screenshot
    volume up | volume down | mute
    move up|down|left|right [n]      (nudge mouse, for voice-only use)
    search for <words>               (Google search in Chrome)
    go to <site>                     (e.g. "go to youtube")
    type <words>                     (types the words)
    start typing ... stop typing     (dictation mode; "new line" presses enter)
    stop listening | start listening (mute / unmute commands)
    quit voice control
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote_plus
from dataclasses import dataclass
from typing import Callable, Optional

NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
KEY_ALIASES = {
    "enter": "enter", "return": "enter", "escape": "esc", "tab": "tab",
    "space": "space", "backspace": "backspace", "delete": "delete",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "home": "home", "end": "end", "page up": "pageup", "page down": "pagedown",
}


FILLERS = {"the", "a", "uh", "um", "huh", "hmm", "oh", "and", "it", "i", "in", "that", "is"}
CHROME_NAMES = {"chrome", "google chrome", "browser", "web browser"}
_APPS_CACHE: Optional[dict] = None


def _discover_apps() -> dict:
    """Index every installed app on this laptop: spoken-name -> launch target."""
    apps: dict = {}
    system = platform.system()
    try:
        if system == "Windows":
            roots = [Path(os.environ.get("ProgramData", "x")) / "Microsoft/Windows/Start Menu/Programs",
                     Path(os.environ.get("APPDATA", "x")) / "Microsoft/Windows/Start Menu/Programs"]
            for r in roots:
                for f in r.rglob("*.lnk"):
                    apps[f.stem.lower()] = str(f)
        elif system == "Darwin":
            for r in ("/Applications", "/System/Applications",
                      "/System/Applications/Utilities", str(Path.home() / "Applications")):
                for f in Path(r).glob("*.app"):
                    apps[f.stem.lower()] = str(f)
        else:
            for r in ("/usr/share/applications", "/var/lib/flatpak/exports/share/applications",
                      str(Path.home() / ".local/share/applications")):
                for f in Path(r).glob("*.desktop"):
                    for line in f.read_text(errors="ignore").splitlines():
                        if line.startswith("Name="):
                            apps[line[5:].strip().lower()] = str(f)
                            break
    except Exception:
        pass
    return apps


def get_apps() -> dict:
    global _APPS_CACHE
    if _APPS_CACHE is None:
        _APPS_CACHE = _discover_apps()
    return _APPS_CACHE


def match_app(spoken: str, apps: dict) -> Optional[str]:
    """Exact -> substring -> fuzzy match of what you said against installed apps."""
    s = spoken.lower().strip()
    if s in apps:
        return apps[s]
    subs = sorted((n for n in apps if s in n), key=len)
    if subs:
        return apps[subs[0]]
    close = difflib.get_close_matches(s, list(apps), n=1, cutoff=0.6)
    return apps[close[0]] if close else None


def _launch_path(path: str) -> None:
    system = platform.system()
    if system == "Windows":
        os.startfile(path)  # type: ignore[attr-defined]
    elif system == "Darwin":
        subprocess.Popen(["open", path])
    else:
        try:
            subprocess.Popen(["gtk-launch", Path(path).stem])
        except FileNotFoundError:
            subprocess.Popen(["gio", "launch", path])


def _os_search_launch(name: str) -> None:
    """Last resort: use the OS launcher (Start menu / Spotlight / Super key)."""
    import pyautogui
    system = platform.system()
    if system == "Darwin":
        pyautogui.hotkey("command", "space")
    else:
        pyautogui.press("win")
    time.sleep(0.7)
    pyautogui.write(name, interval=0.02)
    time.sleep(0.9)
    pyautogui.press("enter")


def open_chrome(url: Optional[str] = None) -> None:
    """Launch Chrome (optionally at a URL); fall back to the default browser."""
    system, extra = platform.system(), ([url] if url else [])
    try:
        if system == "Windows":
            subprocess.Popen(["cmd", "/c", "start", "", "chrome"] + extra)
        elif system == "Darwin":
            subprocess.Popen(["open", "-a", "Google Chrome"] + (["--args", url] if url else []))
        else:
            exe = (shutil.which("google-chrome") or shutil.which("chromium")
                   or shutil.which("chromium-browser"))
            if not exe:
                raise FileNotFoundError("Chrome not found")
            subprocess.Popen([exe] + extra)
    except Exception:
        webbrowser.open(url or "about:blank")


def open_app(spoken: str) -> None:
    """Open ANY app by name: installed-app index first, OS search as fallback."""
    s = spoken.lower().strip()
    if s in CHROME_NAMES:
        return open_chrome()
    path = match_app(s, get_apps())
    if path:
        _launch_path(path)
    else:
        _os_search_launch(s)


SYSTEM_KEYS = {  # action name -> {OS: hotkey}
    "close window": {"Windows": ("alt", "f4"), "Darwin": ("command", "w"), "Linux": ("alt", "f4")},
    "minimize": {"Windows": ("win", "down"), "Darwin": ("command", "m"), "Linux": ("super", "down")},
    "maximize": {"Windows": ("win", "up"), "Darwin": ("ctrl", "command", "f"), "Linux": ("super", "up")},
    "show desktop": {"Windows": ("win", "d"), "Darwin": ("fn", "f11"), "Linux": ("super", "d")},
    "screenshot": {"Windows": ("win", "shift", "s"), "Darwin": ("command", "shift", "4"),
                   "Linux": ("shift", "printscreen")},
}
MEDIA_KEYS = {"volume up": "volumeup", "volume down": "volumedown", "mute": "volumemute"}


def os_keys(keys: tuple) -> tuple:
    """Translate Windows/Linux-style shortcuts for macOS (ctrl->command)."""
    if platform.system() != "Darwin" or ("tab" in keys and "alt" not in keys):
        return keys
    return tuple({"ctrl": "command", "alt": "command"}.get(k, k) for k in keys)


@dataclass
class Command:
    action: str
    arg: object = None
    raw: str = ""


# ----------------------------------------------------------------- parsing
class CommandParser:
    """Turns recognized text into Command objects. Pure logic, easy to test."""

    HOTKEYS = {
        "copy": ("ctrl", "c"), "paste": ("ctrl", "v"), "cut": ("ctrl", "x"),
        "undo": ("ctrl", "z"), "redo": ("ctrl", "y"),
        "select all": ("ctrl", "a"), "save": ("ctrl", "s"), "find": ("ctrl", "f"),
        "next tab": ("ctrl", "tab"), "previous tab": ("ctrl", "shift", "tab"),
        "close tab": ("ctrl", "w"), "new tab": ("ctrl", "t"),
        "switch window": ("alt", "tab"), "go back": ("alt", "left"),
        "zoom in": ("ctrl", "="), "zoom out": ("ctrl", "-"),
    }
    MOUSE = {
        "click": "click", "left click": "click", "double click": "double_click",
        "right click": "right_click", "middle click": "middle_click",
        "grab": "mouse_down", "drop": "mouse_up", "release": "mouse_up",
        "open link": "open_link", "follow link": "open_link",
        "click link": "open_link",
    }
    CONTROL = {
        "stop listening": "listen_off", "start listening": "listen_on",
        "start typing": "dictate_on", "stop typing": "dictate_off",
        "quit voice control": "quit",
    }

    def parse(self, text: str, dictating: bool = False,
              listening: bool = True, strict: bool = False) -> Optional[Command]:
        t = re.sub(r"\s+", " ", text.lower().strip())
        if not t:
            return None

        # Muted: only the unmute phrase is honored.
        if not listening:
            return Command("listen_on", raw=t) if t == "start listening" else None

        if dictating:
            if t in ("stop typing", "stop dictation"):
                return Command("dictate_off", raw=t)
            if t == "new line":
                return Command("hotkey", ("enter",), t)
            return Command("type", t + " ", t)

        if t in self.CONTROL:
            return Command(self.CONTROL[t], raw=t)
        if t in self.MOUSE:
            return Command(self.MOUSE[t], raw=t)
        if t in self.HOTKEYS:
            return Command("hotkey", self.HOTKEYS[t], t)

        m = re.fullmatch(r"scroll (up|down|left|right)(?: (\w+))?", t)
        if m:
            n = NUMBERS.get(m.group(2), None)
            if n is None and m.group(2) and m.group(2).isdigit():
                n = int(m.group(2))
            return Command("scroll", (m.group(1), n or 3), t)

        m = re.fullmatch(r"(?:press|hit) (.+)", t)
        if m and m.group(1) in KEY_ALIASES:
            return Command("hotkey", (KEY_ALIASES[m.group(1)],), t)

        if t in SYSTEM_KEYS:
            return Command("system_key", t, t)
        if t in MEDIA_KEYS:
            return Command("media_key", MEDIA_KEYS[t], t)

        m = re.fullmatch(r"move (up|down|left|right)(?: (\w+))?", t)
        if m:
            n = NUMBERS.get(m.group(2)) or (int(m.group(2)) if (m.group(2) or "").isdigit() else 3)
            return Command("move_mouse", (m.group(1), n * 50), t)

        m = re.fullmatch(r"(?:open|launch|run) (.+)", t)
        if m:  # any app on the laptop
            return Command("open_app", m.group(1), t)

        m = re.fullmatch(r"(?:search for|google) (.+)", t)
        if m:
            return Command("open_url",
                           "https://www.google.com/search?q=" + quote_plus(m.group(1)), t)

        m = re.fullmatch(r"go to (.+)", t)
        if m:
            site = m.group(1).replace(" dot ", ".").replace(" ", "")
            if "." not in site:
                site += ".com"
            return Command("open_url", "https://" + site, t)

        m = re.fullmatch(r"type (.+)", t)
        if m:
            return Command("type", m.group(1), t)

        return None if strict else Command("unknown", raw=t)

    def parse_free(self, text: str) -> Optional[Command]:
        """Free-text results may only trigger commands that carry free words."""
        cmd = self.parse(text, strict=True)
        return cmd if cmd and cmd.action in ("open_app", "open_url", "type") else None

    def vocabulary(self, extra_phrases=()) -> list:
        """Closed word list for the recognizer: it can only 'hear' commands."""
        phrases = (list(self.HOTKEYS) + list(self.MOUSE) + list(self.CONTROL)
                   + list(SYSTEM_KEYS) + list(MEDIA_KEYS) + list(NUMBERS)
                   + list(KEY_ALIASES) + list(extra_phrases)
                   + ["scroll", "move", "up", "down", "left", "right", "press", "hit",
                      "open", "launch", "run", "search", "for", "google", "go", "to",
                      "type", "stop", "dictation", "new", "line", "dot",
                      "link", "follow", "click"])
        return sorted({w for p in phrases for w in p.lower().split()}) + ["[unk]"]


# --------------------------------------------------------------- execution
class PyAutoGuiExecutor:
    """Performs actions at the current OS cursor (which gaze will control)."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self._pg = None
        if not dry_run:
            import pyautogui
            pyautogui.FAILSAFE = True  # slam mouse into a corner to abort
            pyautogui.PAUSE = 0.02
            self._pg = pyautogui

    def run(self, cmd: Command) -> None:
        if self.dry_run:
            print(f"  [dry-run] {cmd.action} {cmd.arg if cmd.arg is not None else ''}")
            return
        pg, a = self._pg, cmd.action
        if a == "click":
            pg.click()
        elif a == "double_click":
            pg.doubleClick()
        elif a == "right_click":
            pg.rightClick()
        elif a == "middle_click":
            pg.middleClick()
        elif a == "mouse_down":
            pg.mouseDown()
        elif a == "mouse_up":
            pg.mouseUp()
        elif a == "open_link":
            # Ctrl+Click at current mouse position to open link in new tab
            import time as _time
            pg.keyDown("ctrl")
            _time.sleep(0.05)
            pg.click()
            _time.sleep(0.05)
            pg.keyUp("ctrl")
        elif a == "scroll":
            direction, n = cmd.arg
            amount = n * 100
            if direction in ("up", "down"):
                pg.scroll(amount if direction == "up" else -amount)
            else:
                pg.hscroll(amount if direction == "right" else -amount)
        elif a == "hotkey":
            pg.hotkey(*os_keys(cmd.arg))
        elif a == "system_key":
            pg.hotkey(*SYSTEM_KEYS[cmd.arg][platform.system()])
        elif a == "media_key":
            pg.press(cmd.arg)
        elif a == "move_mouse":
            d, px = cmd.arg
            dx, dy = {"up": (0, -px), "down": (0, px), "left": (-px, 0), "right": (px, 0)}[d]
            pg.moveRel(dx, dy, duration=0.15)
        elif a == "type":
            pg.write(cmd.arg, interval=0.01)
        elif a == "open_app":
            open_app(cmd.arg)
        elif a == "open_url":
            open_chrome(cmd.arg)


# -------------------------------------------------------------- controller
class VoiceController:
    """
    Mic -> Vosk (offline) -> CommandParser -> executor.

    on_command(cmd):  called for every non-empty parsed command (incl. unknown)
    on_partial(text): live partial transcript, handy for an AR HUD overlay
    """

    SAMPLE_RATE = 16000

    def __init__(self, model_path: Optional[str] = None,
                 on_command: Optional[Callable[[Command], None]] = None,
                 on_partial: Optional[Callable[[str], None]] = None,
                 executor=None, dry_run: bool = False,
                 cmd_model_path: Optional[str] = None,
                 min_conf: float = 0.7, verbose: bool = False):
        self.model_path = model_path
        self.cmd_model_path = cmd_model_path or model_path
        self.min_conf = min_conf
        self.verbose = verbose
        self.on_command = on_command
        self.on_partial = on_partial
        self.executor = executor or PyAutoGuiExecutor(dry_run=dry_run)
        self.parser = CommandParser()
        self.listening = True
        self.dictating = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._audio: "queue.Queue[bytes]" = queue.Queue()

    # public API ----------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._mic_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def handle_text(self, text: str) -> None:
        """Feed text directly (testing, or another ASR source)."""
        cmd = self.parser.parse(text, self.dictating, self.listening, strict=True)
        if cmd:
            self._dispatch(cmd)

    def handle_utterance(self, cmd_text: str, free_text: str) -> None:
        """cmd_text: grammar-limited result (accurate). free_text: open vocabulary."""
        if self.verbose:
            print(f"  heard: cmd={cmd_text!r} free={free_text!r}")
        if not self.listening or self.dictating:
            self.handle_text(free_text if self.dictating else (cmd_text or free_text))
            return
        cmd = None
        if cmd_text and "[unk]" not in cmd_text:
            cmd = self.parser.parse(cmd_text, strict=True)
        if cmd is None and free_text:
            cmd = self.parser.parse_free(free_text)
        if cmd:
            self._dispatch(cmd)

    def _dispatch(self, cmd: Command) -> None:
        print(f"> {cmd.raw!r} -> {cmd.action}")
        if cmd.action == "listen_on":
            self.listening = True
        elif cmd.action == "listen_off":
            self.listening = False
        elif cmd.action == "dictate_on":
            self.dictating = True
        elif cmd.action == "dictate_off":
            self.dictating = False
        elif cmd.action == "quit":
            self._stop.set()
        else:
            self.executor.run(cmd)
        if self.on_command:
            self.on_command(cmd)

    def wait(self) -> None:
        try:
            while not self._stop.is_set():
                self._stop.wait(0.2)
        except KeyboardInterrupt:
            self.stop()

    # internals -----------------------------------------------------------
    @staticmethod
    def _clean(res: dict, min_conf: float) -> str:
        """Drop empty results, filler words and low-confidence guesses (noise)."""
        text = res.get("text", "").strip()
        if not text or text in FILLERS:
            return ""
        words = res.get("result") or []
        if words and sum(w.get("conf", 1.0) for w in words) / len(words) < min_conf:
            return ""
        return text

    def _mic_loop(self) -> None:
        import sounddevice as sd
        from vosk import KaldiRecognizer, Model

        free_model = Model(self.model_path)
        cmd_model = free_model if self.cmd_model_path == self.model_path else Model(self.cmd_model_path)
        grammar = json.dumps(self.parser.vocabulary(get_apps().keys()))
        free = KaldiRecognizer(free_model, self.SAMPLE_RATE)
        cmd = KaldiRecognizer(cmd_model, self.SAMPLE_RATE, grammar)
        free.SetWords(True)
        cmd.SetWords(True)

        def callback(indata, frames, time_info, status):
            self._audio.put(bytes(indata))

        print("Voice control ready. Say 'quit voice control' to exit.")
        with sd.RawInputStream(samplerate=self.SAMPLE_RATE, blocksize=4000,
                               dtype="int16", channels=1, callback=callback):
            while not self._stop.is_set():
                try:
                    data = self._audio.get(timeout=0.2)
                except queue.Empty:
                    continue
                d1, d2 = cmd.AcceptWaveform(data), free.AcceptWaveform(data)
                if d1 or d2:  # either side hit end-of-speech: sync both
                    cj = json.loads(cmd.Result() if d1 else cmd.FinalResult())
                    fj = json.loads(free.Result() if d2 else free.FinalResult())
                    self.handle_utterance(self._clean(cj, self.min_conf),
                                          self._clean(fj, 0.5))
                elif self.on_partial:
                    self.on_partial(json.loads(free.PartialResult()).get("partial", ""))


# --------------------------------------------------------------------- CLI
def main() -> None:
    ap = argparse.ArgumentParser(description="Offline voice control")
    ap.add_argument("--model", help="path to an unzipped Vosk model")
    ap.add_argument("--cmd-model", help="optional separate (small) model for the command grammar")
    ap.add_argument("--min-conf", type=float, default=0.7,
                    help="ignore command guesses below this confidence (raise if noise triggers actions)")
    ap.add_argument("--verbose", action="store_true", help="show what the recognizer heard")
    ap.add_argument("--text", action="store_true",
                    help="type commands instead of speaking (no mic/model needed)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print actions instead of performing them")
    args = ap.parse_args()

    vc = VoiceController(args.model, dry_run=args.dry_run, cmd_model_path=args.cmd_model,
                         min_conf=args.min_conf, verbose=args.verbose)

    if args.text:
        print("Text mode. Type commands, Ctrl+C to exit.")
        try:
            for line in sys.stdin:
                vc.handle_text(line)
                if vc._stop.is_set():
                    break
        except KeyboardInterrupt:
            pass
        return

    if not args.model:
        ap.error("--model is required unless --text is used")
    vc.start()
    vc.wait()


if __name__ == "__main__":
    main()