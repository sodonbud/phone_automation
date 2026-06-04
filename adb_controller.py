"""All ADB command wrappers. Every method returns (success: bool, output: str)."""

import re
import subprocess
import time
import urllib.parse
import xml.etree.ElementTree as ET
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


def _find_send_button(serial: str) -> tuple[int, int] | None:
    """Dump the UI hierarchy and return (x, y) centre of the Send button, or None."""
    log = get_logger()
    remote_xml = "/sdcard/_ui_dump.xml"
    ok, _ = _run(_serial_args(serial) + ["shell", "uiautomator", "dump", remote_xml])
    if not ok:
        return None
    ok, xml_text = _run(_serial_args(serial) + ["shell", "cat", remote_xml])
    if not ok or not xml_text:
        return None

    # Keywords that identify the Send button across common SMS apps
    send_keywords = {"send", "sent", "পাঠান", "enviar", "отправить"}

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.debug("UI dump parse error: %s", exc)
        return None

    for node in root.iter("node"):
        text = (node.get("text") or "").lower()
        desc = (node.get("content-desc") or "").lower()
        clickable = node.get("clickable") == "true"
        if clickable and (text in send_keywords or desc in send_keywords):
            bounds = node.get("bounds", "")
            # bounds format: [x1,y1][x2,y2]
            nums = re.findall(r"\d+", bounds)
            if len(nums) == 4:
                x = (int(nums[0]) + int(nums[2])) // 2
                y = (int(nums[1]) + int(nums[3])) // 2
                log.debug("Send button found at (%d, %d) bounds=%s", x, y, bounds)
                return x, y
    return None


def send_sms(serial: str, number: str, message: str) -> Result:
    """Open the SMS composer, pre-fill number + body, then tap the Send button."""
    log = get_logger()

    # 1. Launch the composer
    uri = f"smsto:{urllib.parse.quote(number)}"
    ok, out = _run(
        _serial_args(serial)
        + [
            "shell", "am", "start",
            "-a", "android.intent.action.SENDTO",
            "-d", uri,
            "--es", "sms_body", message,
            "--ez", "exit_on_sent", "true",
        ]
    )
    if not ok:
        return False, out

    # 2. Wait for the SMS app to fully render
    time.sleep(2)

    # 3. Locate and tap the Send button via UI hierarchy dump
    coords = _find_send_button(serial)
    if coords:
        x, y = coords
        tap_ok, tap_out = _run(_serial_args(serial) + ["shell", "input", "tap", str(x), str(y)])
        if tap_ok:
            log.info("SMS Send button tapped at (%d, %d)", x, y)
            return True, f"{out} | Send tapped at ({x},{y})"
        else:
            log.warning("Send button found but tap failed: %s", tap_out)
            return False, f"Tap failed: {tap_out}"

    # 4. Fallback: press Enter (works on some SMS apps when the text field is focused)
    log.warning("Send button not found in UI dump — trying ENTER keyevent as fallback")
    _run(_serial_args(serial) + ["shell", "input", "keyevent", "66"])
    return False, f"{out} | Send button not found; tried ENTER fallback. Check the screen manually."


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
