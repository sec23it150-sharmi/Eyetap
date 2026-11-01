"""
gaze_executor.py — Gaze-aware action executor.

Drop-in replacement for PyAutoGuiExecutor that moves the OS mouse
to the current gaze point BEFORE executing every action, so that
clicks, scrolls, and link-opens all happen where the user is looking.

Special commands:
    open_link   → Ctrl+Click at gaze point (opens link in new tab)
    scroll      → scrolls at the gaze point (not wherever mouse was)
    click/etc.  → moves to gaze first, then performs action
"""
from __future__ import annotations

import platform
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gaze_cursor import GazeCursor
    from voice_test import Command


def os_keys(keys: tuple) -> tuple:
    """Translate Windows/Linux-style shortcuts for macOS (ctrl→command)."""
    if platform.system() != "Darwin" or ("tab" in keys and "alt" not in keys):
        return keys
    return tuple({"ctrl": "command", "alt": "command"}.get(k, k) for k in keys)


# ---- Import the same helpers from voice_test so we don't duplicate --------
from voice_test import (
    SYSTEM_KEYS,
    MEDIA_KEYS,
    open_app,
    open_chrome,
)


class GazeExecutor:
    """
    Executes voice commands at the current gaze position.

    Parameters
    ----------
    gaze : GazeCursor
        Live gaze-tracking object. `gaze.get_point()` is called before
        each action to read the current (x, y) on screen.
    dry_run : bool
        If True, print actions instead of performing them.
    pre_move_delay : float
        Seconds to wait after moving mouse before acting (lets OS register
        the new position, especially for hover-based UI elements).
    """

    def __init__(self, gaze: "GazeCursor", dry_run: bool = False,
                 pre_move_delay: float = 0.05):
        self.gaze = gaze
        self.dry_run = dry_run
        self.pre_move_delay = pre_move_delay
        self._pg = None
        if not dry_run:
            import pyautogui
            pyautogui.FAILSAFE = True
            pyautogui.PAUSE = 0.02
            self._pg = pyautogui

    # ------------------------------------------------------------------
    # Core
    # ------------------------------------------------------------------

    def _move_to_gaze(self) -> tuple:
        """Move OS mouse to current gaze point; return (x, y)."""
        x, y = self.gaze.get_point()
        if self.dry_run:
            print(f"  [gaze] → ({x}, {y})")
        else:
            self._pg.moveTo(x, y, duration=0)
            if self.pre_move_delay:
                time.sleep(self.pre_move_delay)
        return x, y

    def run(self, cmd: "Command") -> None:
        if self.dry_run:
            x, y = self.gaze.get_point()
            print(f"  [dry-run @ ({x},{y})] {cmd.action} "
                  f"{cmd.arg if cmd.arg is not None else ''}")
            return

        pg = self._pg
        a = cmd.action

        # --- mouse actions (always move to gaze first) -------------------
        if a in ("click", "double_click", "right_click", "middle_click",
                 "mouse_down", "mouse_up"):
            self._move_to_gaze()
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

        # --- open link: Ctrl+Click at gaze point -------------------------
        elif a == "open_link":
            x, y = self._move_to_gaze()
            # Ctrl+Click opens link in a new tab in all major browsers
            pg.keyDown("ctrl")
            time.sleep(0.05)
            pg.click(x, y)
            time.sleep(0.05)
            pg.keyUp("ctrl")

        # --- scroll at gaze position -------------------------------------
        elif a == "scroll":
            direction, n = cmd.arg
            x, y = self._move_to_gaze()
            amount = n * 100
            if direction in ("up", "down"):
                pg.scroll(amount if direction == "up" else -amount, x=x, y=y)
            else:
                pg.hscroll(amount if direction == "right" else -amount, x=x, y=y)

        # --- hotkeys (keyboard only – no mouse move needed) --------------
        elif a == "hotkey":
            pg.hotkey(*os_keys(cmd.arg))

        elif a == "system_key":
            pg.hotkey(*SYSTEM_KEYS[cmd.arg][platform.system()])

        elif a == "media_key":
            pg.press(cmd.arg)

        # --- nudge mouse (voice-only fallback – relative to gaze) --------
        elif a == "move_mouse":
            x, y = self._move_to_gaze()
            d, px = cmd.arg
            dx, dy = {"up": (0, -px), "down": (0, px),
                      "left": (-px, 0), "right": (px, 0)}[d]
            pg.moveRel(dx, dy, duration=0.15)

        # --- typing ------------------------------------------------------
        elif a == "type":
            pg.write(cmd.arg, interval=0.01)

        # --- app / url ---------------------------------------------------
        elif a == "open_app":
            open_app(cmd.arg)

        elif a == "open_url":
            open_chrome(cmd.arg)
