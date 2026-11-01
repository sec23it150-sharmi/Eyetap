"""
eyetap_main.py - EyeTap: Gaze Cursor + Voice Control combined launcher.

Usage:
    # Full EyeTap (webcam gaze + voice):
    python eyetap_main.py --model model/

    # Run calibration first (recommended on first use):
    python eyetap_main.py --model model/ --calibrate

    # Voice only, no gaze (control with normal mouse):
    python eyetap_main.py --model model/ --no-gaze

    # Test mode - no mic or gaze needed, type commands:
    python eyetap_main.py --model model/ --text --dry-run

Voice commands (say any of these):
    click | double click | right click
    open link | follow link | click link   (acts at gaze point)
    scroll up | scroll down | scroll left | scroll right [one..ten]
    press enter | press escape | press tab | press space | press backspace
    copy | paste | cut | undo | redo | select all | save | find
    new tab | close tab | next tab | previous tab | go back
    zoom in | zoom out | switch window
    open <app name>           (e.g. "open chrome", "open notepad")
    go to <website>           (e.g. "go to youtube")
    search for <words>        (Google search)
    type <words>
    start typing ... stop typing   (dictation mode)
    stop listening | start listening
    close window | minimize | maximize | show desktop | screenshot
    volume up | volume down | mute
    quit voice control
"""
from __future__ import annotations

import argparse
import sys
import time

# Force UTF-8 output on Windows so print() never crashes on special chars
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="EyeTap: Gaze-driven, voice-controlled hands-free computing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # Vosk model
    ap.add_argument("--model", metavar="PATH",
                    help="Path to an unzipped Vosk model directory "
                         "(required unless --text is used)")
    ap.add_argument("--cmd-model", metavar="PATH",
                    help="Optional separate (small) Vosk model for the command grammar")
    ap.add_argument("--min-conf", type=float, default=0.7,
                    help="Ignore voice guesses below this confidence (default 0.7)")

    # Gaze
    ap.add_argument("--no-gaze", action="store_true",
                    help="Disable webcam gaze tracking; use normal mouse")
    ap.add_argument("--cam", type=int, default=0,
                    help="Webcam index (default 0 = built-in laptop camera)")
    ap.add_argument("--calibrate", action="store_true",
                    help="Run 9-point calibration before starting")
    ap.add_argument("--smooth", type=float, default=0.12,
                    help="Gaze smoothing factor 0–1 (lower = smoother, default 0.12)")

    # Debug
    ap.add_argument("--verbose", action="store_true",
                    help="Show everything the recogniser heard")
    ap.add_argument("--text", action="store_true",
                    help="Type commands instead of speaking (no mic/model needed)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print actions instead of performing them")
    args = ap.parse_args()

    # ------------------------------------------------------------------
    # 1. Set up gaze cursor (or skip if --no-gaze)
    # ------------------------------------------------------------------
    gaze = None
    if not args.no_gaze:
        print("[EyeTap] Loading gaze cursor (MediaPipe)...")
        try:
            from gaze_cursor import GazeCursor
            gaze = GazeCursor(cam_index=args.cam, smooth=args.smooth,
                              move_mouse=not args.dry_run)
        except ImportError as e:
            print(f"[EyeTap] WARNING: could not import gaze module: {e}")
            print("[EyeTap] Falling back to --no-gaze mode.")
            gaze = None

    if gaze and args.calibrate:
        print("[EyeTap] Running calibration… (look at each green dot)")
        gaze.calibrate()

    # ------------------------------------------------------------------
    # 2. Choose executor
    # ------------------------------------------------------------------
    if gaze is not None:
        try:
            from gaze_executor import GazeExecutor
            executor = GazeExecutor(gaze=gaze, dry_run=args.dry_run)
            print("[EyeTap] Using GazeExecutor - actions will happen at gaze point")
        except Exception as e:
            print(f"[EyeTap] WARNING: GazeExecutor failed ({e}), using fallback")
            from voice_test import PyAutoGuiExecutor
            executor = PyAutoGuiExecutor(dry_run=args.dry_run)
    else:
        from voice_test import PyAutoGuiExecutor
        executor = PyAutoGuiExecutor(dry_run=args.dry_run)
        print("[EyeTap] Using standard executor (no gaze tracking)")

    # ------------------------------------------------------------------
    # 3. Start voice controller
    # ------------------------------------------------------------------
    from voice_test import VoiceController

    def on_command(cmd):
        # Optional: HUD/overlay hook - you can display cmd on screen here
        pass

    vc = VoiceController(
        model_path=args.model,
        cmd_model_path=args.cmd_model,
        on_command=on_command,
        executor=executor,
        dry_run=False,          # executor handles dry_run itself
        min_conf=args.min_conf,
        verbose=args.verbose,
    )

    # ------------------------------------------------------------------
    # 4. Start gaze tracking thread
    # ------------------------------------------------------------------
    if gaze:
        gaze.start()

    # ------------------------------------------------------------------
    # 5. Run
    # ------------------------------------------------------------------
    banner = (
        "\n"
        "=" * 64 + "\n"
        "         EyeTap Voice + Gaze Control" + "\n"
        "=" * 64 + "\n"
        "  Look where you want to act, then say a command:\n"
        '    "click"           - click where you look\n'
        '    "open link"       - Ctrl+click the link you see (new tab)\n'
        '    "scroll up/down"  - scroll where you are looking\n'
        '    "open <app>"      - launch any app\n'
        '    "go to <site>"    - navigate browser to site\n'
        '    "scroll up/down/left/right [one..ten]"\n'
        '    "stop listening" / "start listening" - mute/unmute\n'
        '    "quit voice control" - exit\n'
        + "=" * 64 + "\n"
    )
    print(banner)

    if args.text:
        print("Text mode. Type commands, Ctrl+C to exit.")
        if not args.model and not args.dry_run:
            pass  # text mode doesn't need the model
        try:
            for line in sys.stdin:
                vc.handle_text(line.strip())
                if vc._stop.is_set():
                    break
        except KeyboardInterrupt:
            pass
        if gaze:
            gaze.stop()
        return

    if not args.model:
        ap.error("--model is required unless --text is used.\n"
                 "  Example: python eyetap_main.py --model model/")

    try:
        vc.start()
        vc.wait()
    except KeyboardInterrupt:
        vc.stop()
    finally:
        if gaze:
            gaze.stop()
        print("\n[EyeTap] Goodbye.")


if __name__ == "__main__":
    main()
