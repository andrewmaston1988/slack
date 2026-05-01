#!/usr/bin/env pythonw
"""
Slack bridge system-tray launcher.

Run with pythonw (no console window). Shows a coloured dot in the
Windows notification area:
  green  — bridge running
  grey   — bridge stopped / crashed

Right-click menu: Restart Bridge · View Log · Quit

Bridge stdout/stderr → logs/slack_bridge.log
"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPTS_DIR   = Path(__file__).resolve().parent
BRIDGE_SCRIPT = SCRIPTS_DIR / "slack_bridge.py"
LOG_FILE      = SCRIPTS_DIR / "logs" / "slack_bridge.log"
PYTHON        = Path(sys.executable).with_name("python.exe")   # use python, not pythonw, for the bridge

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_proc: subprocess.Popen | None = None
_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Icon helpers
# ---------------------------------------------------------------------------

def _make_icon(online: bool) -> Image.Image:
    img  = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    colour = (34, 197, 94) if online else (107, 114, 128)   # green / grey
    draw.ellipse([8, 8, 56, 56], fill=colour)
    return img

# ---------------------------------------------------------------------------
# Bridge process management
# ---------------------------------------------------------------------------

def _start_bridge() -> "subprocess.Popen":
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(LOG_FILE, "a", encoding="utf-8")
    return subprocess.Popen(
        [str(PYTHON), str(BRIDGE_SCRIPT)],
        stdout=log_fh,
        stderr=log_fh,
        cwd=str(SCRIPTS_DIR),
        env=os.environ.copy(),
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def _stop_bridge(proc: "subprocess.Popen") -> None:
    """Terminate bridge and wait up to 5 s."""
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

# ---------------------------------------------------------------------------
# Monitor thread — updates icon colour every 3 s
# ---------------------------------------------------------------------------

def _monitor(icon: pystray.Icon) -> None:
    global _proc
    while True:
        with _lock:
            alive = _proc is not None and _proc.poll() is None
            if not alive and _proc is not None:
                # Bridge exited (clean restart or crash) — relaunch it
                _proc = _start_bridge()
                alive = True
        icon.icon  = _make_icon(alive)
        icon.title = (
            "Claude Slack Bridge — online"
            if alive else
            "Claude Slack Bridge — stopped"
        )
        time.sleep(3)

# ---------------------------------------------------------------------------
# Menu actions
# ---------------------------------------------------------------------------

def _action_restart(icon: pystray.Icon, item) -> None:
    global _proc
    with _lock:
        _stop_bridge(_proc)
        _proc = _start_bridge()


def _action_view_log(icon: pystray.Icon, item) -> None:
    os.startfile(str(LOG_FILE))


def _action_quit(icon: pystray.Icon, item) -> None:
    global _proc
    with _lock:
        _stop_bridge(_proc)
    icon.stop()

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    global _proc
    _proc = _start_bridge()

    icon = pystray.Icon(
        name  = "slack_bridge",
        icon  = _make_icon(True),
        title = "Claude Slack Bridge — online",
        menu  = pystray.Menu(
            pystray.MenuItem("Restart Bridge", _action_restart),
            pystray.MenuItem("View Log",       _action_view_log),
            pystray.MenuItem("Quit",           _action_quit),
        ),
    )

    threading.Thread(target=_monitor, args=(icon,), daemon=True).start()
    icon.run()


if __name__ == "__main__":
    main()
