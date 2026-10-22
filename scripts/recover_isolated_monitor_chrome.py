"""Recover the dedicated loopback-only Chrome used by rarity monitoring."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import psutil

CHROME = Path(r"C:/Program Files/Google/Chrome/Application/chrome.exe")
PROFILE = Path(r"__HERMES_HOME__/isolated-monitor-chrome")
NODE = Path(r"__HERMES_HOME__/node/node.exe")
PLAYWRIGHT = r"__HERMES_HOME__/hermes-agent/node_modules/playwright"
CDP = "http://127.0.0.1:9222"


def cdp_probe(timeout_seconds: int = 6) -> bool:
    script = (
        "const {chromium}=require(" + repr(PLAYWRIGHT) + ");"
        "(async()=>{const b=await chromium.connectOverCDP(" + repr(CDP)
        + ",{timeout:5000});if(!b.contexts().length)throw Error('no context');"
        "process.stdout.write('ok');process.exit(0)})().catch(e=>{process.stderr.write(String(e));process.exit(1)})"
    )
    try:
        result = subprocess.run(
            [str(NODE), "-e", script],
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout == "ok"


def _profile_chrome_processes() -> list[psutil.Process]:
    profile_flag = f"--user-data-dir={PROFILE}".lower().replace("\\", "/")
    matches: list[psutil.Process] = []
    for process in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (process.info.get("name") or "").lower()
            command = " ".join(process.info.get("cmdline") or []).lower().replace("\\", "/")
            if name == "chrome.exe" and profile_flag in command:
                matches.append(process)
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return matches


def _stop_profile_chrome() -> None:
    processes = _profile_chrome_processes()
    for process in processes:
        try:
            process.terminate()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
    _, alive = psutil.wait_procs(processes, timeout=5)
    for process in alive:
        try:
            process.kill()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
    if alive:
        psutil.wait_procs(alive, timeout=5)


def _start_profile_chrome() -> None:
    if not CHROME.is_file():
        raise RuntimeError("dedicated Chrome executable is missing")
    PROFILE.mkdir(parents=True, exist_ok=True)
    flags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        flags |= subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [
            str(CHROME),
            "--headless=new",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=9222",
            f"--user-data-dir={PROFILE}",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )


def recover_isolated_browser(*, deadline_seconds: int = 30) -> bool:
    """Return True when restart was needed; raise if CDP stays unhealthy."""
    if cdp_probe():
        return False
    _stop_profile_chrome()
    _start_profile_chrome()
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if cdp_probe():
            return True
        time.sleep(0.5)
    raise RuntimeError("dedicated Chrome CDP remained unhealthy after restart")


if __name__ == "__main__":
    restarted = recover_isolated_browser()
    print("restarted" if restarted else "healthy")
