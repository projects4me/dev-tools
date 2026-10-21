"""A headless Chromium (Edge or Chrome on Windows; Chromium, Chrome or Edge on Linux and macOS) that turns a local
HTML page into a PNG screenshot."""

import os
import shutil
import signal
import subprocess
from pathlib import Path

from ..config import TalosError

TIMEOUT = 60


class RenderError(TalosError):
    pass


def find(configured=None):
    """config `browserBin`, then the usual places."""
    if configured:
        if not Path(configured).exists():
            raise RenderError(f"browserBin '{configured}' does not exist")
        return configured
    if os.name == "nt":
        for root in filter(None, (os.environ.get(k) for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"))):
            for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe",
                        r"Chromium\Application\chrome.exe"):
                if (Path(root) / rel).exists():
                    return str(Path(root) / rel)
    else:
        for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "microsoft-edge",
                     "microsoft-edge-stable"):
            found = shutil.which(name)
            if found:
                return found
        mac = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        if mac.exists():
            return str(mac)
    raise RenderError("no Chromium-based browser found to render screens (install Chrome/Chromium or Edge, or "
                      "set browserBin in dev-tools/talos/config.local.json)")


def command(browser, html_path, png_path, width, height, profile, as_root=None):
    """A throwaway profile keeps it apart from a browser the engineer has open."""
    cmd = [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
           "--no-default-browser-check", "--disable-extensions", "--disable-dev-shm-usage",
           "--disable-background-networking", "--disable-component-update", f"--user-data-dir={profile}",
           f"--window-size={int(width)},{int(height)}", "--virtual-time-budget=5000", f"--screenshot={png_path}",
           Path(html_path).resolve().as_uri()]
    if as_root is None:
        as_root = os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0
    if as_root:
        cmd.insert(1, "--no-sandbox")          # Chromium refuses to run as root with its sandbox
    return cmd


def screenshot(browser, html_path, png_path, width, height, workdir):
    """Run the browser; its output goes to a file (its helper processes can outlive it and would hold pipes open).
    The whole process tree is ended afterwards, finished or not."""
    log_path = Path(workdir) / "browser.log"
    with open(log_path, "wb") as out:
        proc = subprocess.Popen(command(browser, html_path, png_path, width, height, Path(workdir) / "profile"),
                                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                start_new_session=os.name != "nt")
        try:
            proc.wait(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            raise RenderError(f"the browser took longer than {TIMEOUT}s")
        finally:
            kill_tree(proc)
    png = Path(png_path)
    if not png.exists() or png.stat().st_size == 0:
        detail = log_path.read_text(encoding="utf-8", errors="replace").strip()[-300:]
        raise RenderError(f"the browser produced no image ({detail or f'exit {proc.returncode}'})")
    return png.read_bytes()


def kill_tree(proc):
    """End the process and everything it started (helpers may still run after the parent exited), then reap it."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
