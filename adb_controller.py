"""All ADB command wrappers. Every method returns (success: bool, output: str)."""

import os
import re
import subprocess
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Tuple

import config
from logger import get_logger

Result = Tuple[bool, str]

# Tracks background screenrecord PIDs keyed by serial
_recording_pids: dict[str, str] = {}


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
        return proc.returncode == 0, output
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
# UI helpers
# ---------------------------------------------------------------------------

def _dump_ui_tree(serial: str) -> ET.Element | None:
    """Run uiautomator dump and return the parsed XML root, or None on failure."""
    remote_xml = "/sdcard/_ui_dump.xml"
    ok, _ = _run(_serial_args(serial) + ["shell", "uiautomator", "dump", remote_xml])
    if not ok:
        return None
    ok, xml_text = _run(_serial_args(serial) + ["shell", "cat", remote_xml])
    if not ok or not xml_text:
        return None
    try:
        return ET.fromstring(xml_text)
    except ET.ParseError as exc:
        get_logger().debug("UI dump parse error: %s", exc)
        return None


def _find_clickable(root: ET.Element, text_keywords: set[str], id_suffixes: set[str]) -> tuple[int, int] | None:
    """Return (x, y) centre of the first clickable node matching keywords or resource-id suffixes."""
    for node in root.iter("node"):
        if node.get("clickable") != "true":
            continue
        text = (node.get("text") or "").lower()
        desc = (node.get("content-desc") or "").lower()
        res_id = (node.get("resource-id") or "").lower()
        if text in text_keywords or desc in text_keywords or any(res_id.endswith(s) for s in id_suffixes):
            nums = re.findall(r"\d+", node.get("bounds", ""))
            if len(nums) == 4:
                return (int(nums[0]) + int(nums[2])) // 2, (int(nums[1]) + int(nums[3])) // 2
    return None


def _find_send_button(serial: str) -> tuple[int, int] | None:
    root = _dump_ui_tree(serial)
    if root is None:
        return None
    keywords = {"send", "sent", "send sms", "পাঠান", "enviar", "отправить"}
    id_suffixes = {":send", "/send", "send"}
    coords = _find_clickable(root, keywords, id_suffixes)
    if coords:
        get_logger().debug("Send button found at %s", coords)
    return coords


def _find_call_button(serial: str) -> tuple[int, int] | None:
    root = _dump_ui_tree(serial)
    if root is None:
        return None
    keywords = {"call", "dial", "voice call", "дозвониться", "llamar"}
    id_suffixes = {":fab", "/fab", ":call_button", "/call_button",
                   ":dialpad_floating_action_button", "/dialpad_floating_action_button",
                   ":call", "/call"}
    coords = _find_clickable(root, keywords, id_suffixes)
    if coords:
        get_logger().debug("Call button found at %s", coords)
    return coords


def _find_answer_button(serial: str) -> tuple[int, int] | None:
    root = _dump_ui_tree(serial)
    if root is None:
        return None
    keywords = {"answer", "accept", "принять", "atender"}
    id_suffixes = {":answer_action_view", "/answer_action_view",
                   ":answer", "/answer",
                   ":floating_action_button", "/floating_action_button"}
    coords = _find_clickable(root, keywords, id_suffixes)
    if coords:
        get_logger().debug("Answer button found at %s", coords)
    return coords


def _has_incoming_call_ui(serial: str) -> bool:
    """Return True if the device is currently showing an incoming call screen."""
    root = _dump_ui_tree(serial)
    if root is None:
        return False
    call_indicators = {"incoming call", "answer", "decline", "incoming"}
    for node in root.iter("node"):
        text = (node.get("text") or "").lower()
        desc = (node.get("content-desc") or "").lower()
        res_id = (node.get("resource-id") or "").lower()
        if any(k in text for k in call_indicators) or any(k in desc for k in call_indicators):
            return True
        if "incall" in res_id or "incoming" in res_id or "answer" in res_id:
            return True
    return False


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


def wait_for_incoming_call(serial: str, timeout: int = 30) -> Result:
    """Block until an incoming call UI appears on *serial*, up to *timeout* seconds."""
    log = get_logger()
    log.info("Waiting up to %ds for incoming call on %s ...", timeout, serial)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _has_incoming_call_ui(serial):
            log.info("Incoming call detected on %s", serial)
            return True, "Incoming call detected"
        time.sleep(2)
    return False, f"No incoming call detected on {serial} within {timeout}s"


def answer_call(serial: str) -> Result:
    """Answer an incoming call — tries UI tap first, falls back to CALL keyevent."""
    log = get_logger()
    coords = _find_answer_button(serial)
    if coords:
        x, y = coords
        ok, out = _run(_serial_args(serial) + ["shell", "input", "tap", str(x), str(y)])
        if ok:
            log.info("Answer button tapped at (%d, %d)", x, y)
            return True, f"Call answered (tapped {x},{y})"
        return False, f"Answer tap failed: {out}"
    # Fallback: swipe from left to right (common answer gesture on stock Android)
    log.warning("Answer button not found — trying CALL keyevent fallback")
    ok, out = _run(_serial_args(serial) + ["shell", "input", "keyevent", "KEYCODE_CALL"])
    if ok:
        return True, "Call answered via CALL keyevent"
    return False, f"Could not answer call: {out}"


def send_sms(serial: str, number: str, message: str) -> Result:
    """Open the SMS composer, pre-fill number + body, then tap the Send button."""
    log = get_logger()
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

    time.sleep(2)

    coords = _find_send_button(serial)
    if coords:
        x, y = coords
        tap_ok, tap_out = _run(_serial_args(serial) + ["shell", "input", "tap", str(x), str(y)])
        if tap_ok:
            log.info("SMS Send button tapped at (%d, %d)", x, y)
            return True, f"{out} | Send tapped at ({x},{y})"
        return False, f"Tap failed: {tap_out}"

    log.warning("Send button not found — trying ENTER keyevent fallback")
    _run(_serial_args(serial) + ["shell", "input", "keyevent", "66"])
    return False, f"{out} | Send button not found; tried ENTER fallback."


def check_sms_received(serial: str, from_number: str, expected_text: str = "") -> Result:
    """Query the SMS inbox for the most recent message from *from_number*.

    Strips country-code prefixes when comparing so +97699001122 matches 99001122.
    Returns (True, message_body) if found, (False, reason) otherwise.
    """
    log = get_logger()
    ok, raw = _run(
        _serial_args(serial)
        + ["shell", "content", "query", "--uri", "content://sms/inbox",
           "--projection", "address,body,date",
           "--sort", "date DESC"]
    )
    if not ok:
        return False, f"SMS inbox query failed: {raw}"

    # Normalise a phone number to digits only (last 8 digits for loose matching)
    def normalise(num: str) -> str:
        return re.sub(r"\D", "", num)[-8:]

    target = normalise(from_number)
    for line in raw.splitlines():
        if "address=" not in line:
            continue
        addr_match = re.search(r"address=([^,]+)", line)
        body_match = re.search(r"body=(.+?)(?:,\s*date=|$)", line)
        if not addr_match:
            continue
        addr = normalise(addr_match.group(1))
        body = body_match.group(1).strip() if body_match else ""
        if addr == target or addr.endswith(target) or target.endswith(addr):
            if expected_text and expected_text.lower() not in body.lower():
                log.warning("SMS found from %s but body '%s' doesn't contain '%s'",
                            from_number, body, expected_text)
                return False, f"SMS received but content mismatch. Got: {body}"
            log.info("SMS from %s found: %s", from_number, body)
            return True, body

    return False, f"No SMS from {from_number} found in inbox"


def _read_ussd_response(serial: str, wait_secs: int = 15) -> str | None:
    """Poll the UI until a USSD response dialog appears, then return its message text."""
    log = get_logger()
    response_ids = {
        "android:id/message",
        "com.android.phone:id/message",
        "com.google.android.dialer:id/ussd_response",
    }
    deadline = time.time() + wait_secs
    while time.time() < deadline:
        time.sleep(2)
        root = _dump_ui_tree(serial)
        if root is None:
            continue
        for node in root.iter("node"):
            res_id = node.get("resource-id") or ""
            text = (node.get("text") or "").strip()
            if res_id in response_ids and text:
                log.info("USSD response via resource-id '%s': %s", res_id, text)
                return text
        # Broader fallback: TextView in telephony/dialer package
        for node in root.iter("node"):
            pkg = node.get("package") or ""
            cls = node.get("class") or ""
            text = (node.get("text") or "").strip()
            if ("phone" in pkg or "dialer" in pkg) and "TextView" in cls and len(text) > 3:
                log.info("USSD response (fallback pkg=%s): %s", pkg, text)
                return text
    log.warning("USSD response dialog not detected within %ds", wait_secs)
    return None


def dial_ussd(serial: str, code: str) -> Result:
    """Dial a USSD code and return the network's response text."""
    log = get_logger()
    encoded = urllib.parse.quote(code, safe="*+")
    uri = f"tel:{encoded}"
    ok, out = _run(
        _serial_args(serial)
        + ["shell", "am", "start", "-a", "android.intent.action.DIAL", "-d", uri]
    )
    if not ok:
        return False, out

    time.sleep(2)
    coords = _find_call_button(serial)
    if coords:
        x, y = coords
        tap_ok, tap_out = _run(_serial_args(serial) + ["shell", "input", "tap", str(x), str(y)])
        if not tap_ok:
            return False, f"Call button tap failed: {tap_out}"
        log.info("USSD call button tapped at (%d, %d)", x, y)
    else:
        log.warning("Call button not found — trying CALL keyevent fallback")
        _run(_serial_args(serial) + ["shell", "input", "keyevent", "5"])

    response = _read_ussd_response(serial, wait_secs=15)
    if response:
        return True, response
    return False, f"{out} | USSD sent but no response dialog detected."


def start_recording(serial: str, remote_path: str = "/sdcard/_automation_record.mp4") -> Result:
    """Start screenrecord in the background on *serial*. Call stop_recording() to finish.

    Note: screenrecord captures video. Audio during calls is recorded if the device
    supports --audio (Android 10+). The file is pulled to the host by stop_recording().
    """
    log = get_logger()
    if serial in _recording_pids:
        return False, f"Recording already in progress on {serial} (PID {_recording_pids[serial]})"

    # Run screenrecord as a background shell process and capture its PID
    bg_cmd = (
        f"{config.ADB_PATH} -s {serial} shell "
        f"\"screenrecord --verbose {remote_path} >/dev/null 2>&1 & echo $!\""
    )
    try:
        proc = subprocess.run(bg_cmd, shell=True, capture_output=True, text=True, timeout=10)
        pid = proc.stdout.strip()
        if not pid.isdigit():
            # Try without --verbose (older Android)
            bg_cmd2 = (
                f"{config.ADB_PATH} -s {serial} shell "
                f"\"screenrecord {remote_path} >/dev/null 2>&1 & echo $!\""
            )
            proc = subprocess.run(bg_cmd2, shell=True, capture_output=True, text=True, timeout=10)
            pid = proc.stdout.strip()
        if not pid.isdigit():
            return False, f"Failed to start screenrecord (output: {proc.stdout!r})"
        _recording_pids[serial] = pid
        log.info("Recording started on %s (PID %s) → %s", serial, pid, remote_path)
        return True, f"Recording started (PID {pid})"
    except Exception as exc:  # noqa: BLE001
        return False, f"Error starting recording: {exc}"


def stop_recording(serial: str, local_path: str) -> Result:
    """Stop the background screenrecord on *serial* and pull the file to *local_path*."""
    log = get_logger()
    pid = _recording_pids.pop(serial, None)
    remote_path = "/sdcard/_automation_record.mp4"

    if pid:
        # SIGINT causes screenrecord to finalise the mp4 before exiting
        _run(_serial_args(serial) + ["shell", "kill", "-INT", pid])
        time.sleep(2)  # give it time to write the file trailer
    else:
        # No tracked PID — try killing by name
        log.warning("No tracked recording PID for %s; killing screenrecord by name", serial)
        _run(_serial_args(serial) + ["shell", "pkill", "-INT", "screenrecord"])
        time.sleep(2)

    os.makedirs(os.path.dirname(local_path) if os.path.dirname(local_path) else ".", exist_ok=True)
    ok, out = _run(_serial_args(serial) + ["pull", remote_path, local_path])
    if ok:
        log.info("Recording saved to '%s'", local_path)
        return True, f"Recording saved to {local_path}"
    return False, f"Pull failed: {out}"


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
