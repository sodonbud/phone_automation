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


def _get_call_state(serial: str) -> int:
    """Return telephony call state: 0=IDLE, 1=RINGING, 2=OFFHOOK.

    Uses dumpsys telephony.registry which is reliable on all Android versions.
    """
    ok, out = _run(_serial_args(serial) + ["shell", "dumpsys", "telephony.registry"])
    if not ok:
        return 0
    for line in out.splitlines():
        line = line.strip()
        if "mCallState" in line:
            m = re.search(r"mCallState=(\d)", line)
            if m:
                return int(m.group(1))
    return 0


def _has_incoming_call(serial: str) -> bool:
    """Return True if the device has an incoming (ringing) call — state=1."""
    return _get_call_state(serial) == 1


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


def _get_screen_size(serial: str) -> tuple[int, int]:
    """Return (width, height) of the device screen."""
    ok, out = _run(_serial_args(serial) + ["shell", "wm", "size"])
    m = re.search(r"(\d+)x(\d+)", out)
    if m:
        return int(m.group(1)), int(m.group(2))
    return 1080, 2400  # safe default


def wake_and_unlock(serial: str) -> Result:
    """Wake the screen and dismiss the keyguard so ADB commands take effect.

    Handles stock Android and Xiaomi HyperOS/MIUI which blocks wm dismiss-keyguard.
    Does NOT bypass PIN/password — only works with swipe/no lock screen.
    """
    log = get_logger()

    # ── 1. Wake the screen ────────────────────────────────────────────
    ok, out = _run(_serial_args(serial) + ["shell", "dumpsys", "power"])
    screen_on = "mWakefulness=Awake" in out or "mHoldingDisplaySuspendBlocker=true" in out
    if not screen_on:
        _run(_serial_args(serial) + ["shell", "input", "keyevent", "KEYCODE_WAKEUP"])
        time.sleep(1)
        log.info("Screen woken on %s", serial)

    # ── 2. Dismiss keyguard ───────────────────────────────────────────
    # Standard Android
    _run(_serial_args(serial) + ["shell", "wm", "dismiss-keyguard"])

    # Xiaomi HyperOS / MIUI: wm dismiss-keyguard is blocked.
    # Use the am start trick to force the keyguard away.
    ok, out = _run(_serial_args(serial) + [
        "shell", "am", "start", "-n",
        "com.miui.securityadd/.MainSecurityActivity",
    ])
    # That may fail on non-Xiaomi — ignore the error and check lock state.

    # ── 3. Check if still locked and swipe to unlock ──────────────────
    _, res = _run(_serial_args(serial) + ["shell", "dumpsys", "window"])
    locked = "mDreamingLockscreen=true" in res or "isStatusBarKeyguard=true" in res
    if locked:
        w, h = _get_screen_size(serial)
        cx = w // 2
        # Swipe upward from 80 % height to 30 % height
        y_start = int(h * 0.80)
        y_end   = int(h * 0.30)
        _run(_serial_args(serial) + [
            "shell", "input", "swipe",
            str(cx), str(y_start), str(cx), str(y_end), "300",
        ])
        time.sleep(0.5)
        log.info("Swipe-to-unlock performed on %s (%dx%d)", serial, w, h)

    # ── 4. Xiaomi HyperOS: ensure ADB input is trusted ───────────────
    # HyperOS may pop up a "USB debugging active" notification that steals focus.
    # Pressing HOME then BACK clears transient overlays.
    _run(_serial_args(serial) + ["shell", "input", "keyevent", "KEYCODE_HOME"])
    time.sleep(0.3)

    return True, "Screen ready"


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


def wait_for_incoming_call(serial: str, timeout: int = 5) -> Result:
    """Block until an incoming call is ringing on *serial*, up to *timeout* seconds.

    Uses dumpsys telephony.registry (mCallState=1) — reliable regardless of UI.
    """
    log = get_logger()
    log.info("Waiting up to %ds for incoming call on %s ...", timeout, serial)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _has_incoming_call(serial):
            log.info("Incoming call detected on %s (mCallState=1)", serial)
            return True, "Incoming call detected"
        time.sleep(1)
    # Last check
    state = _get_call_state(serial)
    if state == 1:
        return True, "Incoming call detected"
    return False, f"No incoming call on {serial} within {timeout}s (last state={state}: 0=idle 1=ringing 2=offhook)"


def answer_call(serial: str) -> Result:
    """Wake the screen, unlock, then answer the incoming call.

    Priority:
      1. telecom accept-ringing-call  (Android 6+, most reliable)
      2. KEYCODE_CALL keyevent         (universal fallback)
    """
    log = get_logger()

    # Ensure screen is on and unlocked before issuing any commands
    wake_and_unlock(serial)
    time.sleep(0.5)

    # Primary: telecom service command — works regardless of dialer UI
    ok, out = _run(_serial_args(serial) + ["shell", "telecom", "accept-ringing-call"])
    if ok:
        log.info("Call answered via telecom accept-ringing-call on %s", serial)
        return True, "Call answered (telecom)"

    # Fallback 1: KEYCODE_CALL
    log.warning("telecom accept-ringing-call failed (%s) — trying KEYCODE_CALL", out)
    ok, out = _run(_serial_args(serial) + ["shell", "input", "keyevent", "KEYCODE_CALL"])
    if ok:
        # Verify the call actually moved to OFFHOOK state
        time.sleep(1)
        if _get_call_state(serial) == 2:
            return True, "Call answered via KEYCODE_CALL"

    # Fallback 2: Xiaomi HyperOS / MIUI — tap the green answer button by coordinates
    # derived from screen size so it works on any resolution
    log.warning("KEYCODE_CALL did not answer — trying Xiaomi coordinate tap")
    w, h = _get_screen_size(serial)
    # On MIUI/HyperOS the green answer button sits at ~25% from left, ~75% from top
    ax, ay = int(w * 0.25), int(h * 0.75)
    _run(_serial_args(serial) + ["shell", "input", "tap", str(ax), str(ay)])
    time.sleep(1)
    if _get_call_state(serial) == 2:
        return True, f"Call answered via coordinate tap ({ax},{ay})"

    return False, (
        "Could not answer call automatically. "
        "On Xiaomi HyperOS go to: Settings → Additional settings → Developer options → "
        "enable 'Disable permission monitoring' and 'USB debugging (Security settings)'."
    )


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


# Tracks background tinycap PIDs per serial
_tinycap_pids: dict[str, str] = {}

# ACR package name (Another Call Recorder)
_ACR_PKG = "com.nll.acr"

# OEM call-recording settings: (namespace, key, enable_value)
_OEM_REC_SETTINGS = [
    ("system", "call_recording_state",          "1"),   # generic
    ("system", "voice_call_record",             "1"),   # some Samsung
    ("system", "call_record_state",             "1"),   # Xiaomi
    ("secure", "call_recording_automatic",      "1"),   # Oppo/OnePlus
]


def _tinycap_available(serial: str) -> bool:
    ok, out = _run(_serial_args(serial) + ["shell", "which", "tinycap"])
    return ok and bool(out.strip())


def _acr_installed(serial: str) -> bool:
    ok, out = _run(_serial_args(serial) + ["shell", "pm", "list", "packages", _ACR_PKG])
    return ok and _ACR_PKG in out


def _find_latest_recording(serial: str) -> str | None:
    """Return device path of the most recently modified audio file in common recording dirs."""
    search_dirs = [
        "/sdcard/Recordings/Call",
        "/sdcard/CallRecordings",
        "/sdcard/MIUI/sound_recorder/call_rec",
        "/sdcard/PhoneRecord",
        "/sdcard/Android/data/com.nll.acr/files",   # ACR
        "/sdcard/AudioRecorder",
        "/sdcard/Sounds",
        "/sdcard/Music",
    ]
    candidates = []
    for d in search_dirs:
        ok, out = _run(
            _serial_args(serial)
            + ["shell", "find", d, "-maxdepth", "2",
               "\\(", "-name", "*.mp3", "-o", "-name", "*.mp4",
               "-o", "-name", "*.m4a", "-o", "-name", "*.amr",
               "-o", "-name", "*.3gp", "-o", "-name", "*.wav", "\\)"],
            timeout=10,
        )
        if ok and out.strip():
            candidates.extend(f.strip() for f in out.splitlines() if f.strip())
    if not candidates:
        return None
    # Ask the device to sort by modification time and return the newest
    ok, out = _run(_serial_args(serial) + ["shell", "ls", "-t"] + candidates)
    if ok and out.strip():
        return out.splitlines()[0].strip()
    return candidates[0]


def start_call_recording(serial: str) -> Result:
    """Start fully-automated call audio recording.

    Priority:
      1. tinycap  — low-level mic capture, works on AOSP/most stock ROMs
      2. ACR      — Another Call Recorder (must be installed once via Play Store
                    or: adb install ACR.apk).  No UI needed; ACR auto-records
                    all calls; we send it a broadcast to mark the start.
      3. OEM setting — try known per-OEM settings keys that auto-enable call
                    recording for the next call (Samsung, Xiaomi, Oppo etc.)

    Falls back gracefully with a clear message if none are available.
    """
    log = get_logger()

    # ── 1. tinycap ────────────────────────────────────────────────────
    if _tinycap_available(serial):
        _run(_serial_args(serial) + ["shell", "input", "keyevent", "KEYCODE_SPEAKERPHONE"])
        time.sleep(1)
        bg_cmd = (
            f'"{config.ADB_PATH}" -s {serial} shell '
            f'"tinycap /sdcard/_automation_call_rec.wav '
            f'-D 0 -d 0 -c 1 -r 16000 -b 16 >/dev/null 2>&1 & echo $!"'
        )
        try:
            proc = subprocess.run(bg_cmd, shell=True, capture_output=True, text=True, timeout=10)
            pid = proc.stdout.strip()
            if pid.isdigit():
                _tinycap_pids[serial] = pid
                log.info("tinycap recording started on %s PID=%s", serial, pid)
                return True, f"Recording started via tinycap (PID {pid})"
        except Exception as exc:  # noqa: BLE001
            log.warning("tinycap launch error: %s", exc)

    # ── 2. ACR broadcast ─────────────────────────────────────────────
    if _acr_installed(serial):
        ok, out = _run(_serial_args(serial) + [
            "shell", "am", "broadcast",
            "-a", "com.nll.acr.MANUAL_RECORD",
            "-n", f"{_ACR_PKG}/.receiver.RecordingReceiver",
        ])
        if ok:
            log.info("ACR recording triggered on %s", serial)
            return True, "Recording started via ACR"
        log.warning("ACR broadcast failed: %s", out)

    # ── 3. OEM settings key ──────────────────────────────────────────
    for ns, key, val in _OEM_REC_SETTINGS:
        ok, _ = _run(_serial_args(serial) + ["shell", "settings", "put", ns, key, val])
        if ok:
            log.info("OEM call-recording setting %s/%s=%s applied on %s", ns, key, val, serial)
            return True, (
                f"OEM call recording enabled ({ns}/{key}={val}). "
                "Recording will be saved automatically by the dialer."
            )

    return False, (
        "No recording method available on this device.\n"
        "Options:\n"
        "  A) Install ACR (Another Call Recorder) from the Play Store on this phone,\n"
        "     then re-run — the tool will use it automatically.\n"
        "  B) Manually enable call recording in Phone app → Settings → Call recording\n"
        "     and use PULL_RECORDING action after the call to fetch the file."
    )


def stop_call_recording(serial: str, local_path: str) -> Result:
    """Stop recording and pull the audio file to *local_path*."""
    log = get_logger()
    os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)

    # ── tinycap stop ─────────────────────────────────────────────────
    pid = _tinycap_pids.pop(serial, None)
    if pid:
        _run(_serial_args(serial) + ["shell", "kill", "-INT", pid])
        time.sleep(1)
        _run(_serial_args(serial) + ["shell", "input", "keyevent", "KEYCODE_SPEAKERPHONE"])
        ok, out = _run(_serial_args(serial) + ["pull", "/sdcard/_automation_call_rec.wav", local_path])
        if ok:
            log.info("tinycap recording saved to '%s'", local_path)
            return True, f"Recording saved to {local_path}"
        return False, f"Pull failed: {out}"

    # ── ACR stop ─────────────────────────────────────────────────────
    if _acr_installed(serial):
        _run(_serial_args(serial) + [
            "shell", "am", "broadcast",
            "-a", "com.nll.acr.MANUAL_RECORD",
            "-n", f"{_ACR_PKG}/.receiver.RecordingReceiver",
        ])
        time.sleep(2)

    # ── Pull most recent file (ACR or OEM dialer) ────────────────────
    remote = _find_latest_recording(serial)
    if not remote:
        return False, "Could not locate a recording file on the device."
    ok, out = _run(_serial_args(serial) + ["pull", remote, local_path])
    if ok:
        log.info("Recording pulled from '%s' to '%s'", remote, local_path)
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
