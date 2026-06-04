"""All ADB command wrappers. Every method returns (success: bool, output: str)."""

import subprocess
import urllib.parse
from typing import Tuple

import config
from logger import get_logger

Result = Tuple[bool, str]


def _run(args: list[str], timeout: int = config.ADB_TIMEOUT) -> Result:
    log = get_logger()
    cmd = [config.ADB_PATH] + args
    log.debug("ADB CMD: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (proc.stdout + proc.stderr).strip()
        log.debug("ADB OUT: %s", output)
        success = proc.returncode == 0
        return success, output
    except FileNotFoundError:
        msg = f"ADB not found at '{config.ADB_PATH}'. Install Android SDK platform-tools and add to PATH."
        log.error(msg)
        return False, msg
    except subprocess.TimeoutExpired:
        msg = f"ADB command timed out after {timeout}s: {' '.join(cmd)}"
        log.error(msg)
        return False, msg
    except Exception as exc:  # noqa: BLE001
        msg = f"Unexpected error running ADB: {exc}"
        log.error(msg)
        return False, msg


def _serial_args(serial: str) -> list[str]:
    return ["-s", serial]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_connected_devices() -> list[str]:
    """Return list of serials that are online (not offline/unauthorized)."""
    success, output = _run(["devices"])
    if not success:
        return []
    serials = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def make_call(serial: str, number: str) -> Result:
    """Trigger an outgoing call via the Android dialer."""
    uri = f"tel:{urllib.parse.quote(number)}"
    return _run(
        _serial_args(serial)
        + ["shell", "am", "start", "-a", "android.intent.action.CALL", "-d", uri]
    )


def end_call(serial: str) -> Result:
    """End an active call by sending the ENDCALL keyevent."""
    return _run(_serial_args(serial) + ["shell", "input", "keyevent", "KEYCODE_ENDCALL"])


def send_sms(serial: str, number: str, message: str) -> Result:
    """Open the SMS composer via intent (pre-fills number and body)."""
    encoded_body = urllib.parse.quote(message)
    uri = f"smsto:{urllib.parse.quote(number)}"
    return _run(
        _serial_args(serial)
        + [
            "shell",
            "am",
            "start",
            "-a",
            "android.intent.action.SENDTO",
            "-d",
            uri,
            "--es",
            "sms_body",
            encoded_body,
            "--ez",
            "exit_on_sent",
            "true",
        ]
    )


def dial_ussd(serial: str, code: str) -> Result:
    """Launch the dialer with a USSD/MMI code (e.g. *100#)."""
    # '#' must be encoded as %23 in the URI
    encoded = urllib.parse.quote(code, safe="*+")
    uri = f"tel:{encoded}"
    return _run(
        _serial_args(serial)
        + ["shell", "am", "start", "-a", "android.intent.action.DIAL", "-d", uri]
    )


def set_config(serial: str, namespace: str, key: str, value: str) -> Result:
    """Set a device setting via `adb shell settings put`."""
    return _run(_serial_args(serial) + ["shell", "settings", "put", namespace, key, value])


def get_config(serial: str, namespace: str, key: str) -> Result:
    """Read a device setting via `adb shell settings get`."""
    return _run(_serial_args(serial) + ["shell", "settings", "get", namespace, key])


def screenshot(serial: str, save_path: str) -> Result:
    """Capture the screen and pull it to *save_path* on the host."""
    remote = "/sdcard/_automation_screenshot.png"
    ok, out = _run(_serial_args(serial) + ["shell", "screencap", "-p", remote])
    if not ok:
        return False, out
    return _run(_serial_args(serial) + ["pull", remote, save_path])
