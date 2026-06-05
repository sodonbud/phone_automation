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
            timeout=timeout,
        )
        stdout = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
        stderr = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
        output = (stdout + stderr).strip()
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


def _node_center(node: ET.Element) -> tuple[int, int] | None:
    nums = re.findall(r"\d+", node.get("bounds", ""))
    if len(nums) == 4:
        return (int(nums[0]) + int(nums[2])) // 2, (int(nums[1]) + int(nums[3])) // 2
    return None


def _node_matches(
    node: ET.Element,
    text_keywords: set[str],
    id_suffixes: set[str],
    id_contains: set[str] | None = None,
) -> bool:
    text = (node.get("text") or "").lower()
    desc = (node.get("content-desc") or "").lower()
    res_id = (node.get("resource-id") or "").lower()
    if any(kw in text or kw in desc for kw in text_keywords):
        return True
    if any(res_id.endswith(s) for s in id_suffixes):
        return True
    if id_contains and any(part in res_id for part in id_contains):
        return True
    return False


def _find_clickable(
    root: ET.Element,
    text_keywords: set[str],
    id_suffixes: set[str],
    id_contains: set[str] | None = None,
) -> tuple[int, int] | None:
    """Return (x, y) centre of the first clickable node matching keywords or resource-id."""
    for node in root.iter("node"):
        if node.get("clickable") != "true":
            continue
        if _node_matches(node, text_keywords, id_suffixes, id_contains):
            coords = _node_center(node)
            if coords:
                return coords
    return None


def _find_send_button(serial: str) -> tuple[int, int] | None:
    """Locate the compose-area Send button, avoiding message-history 'Sent' rows."""
    root = _dump_ui_tree(serial)
    if root is None:
        return None

    _, h = _get_screen_size(serial)
    compose_y_min = int(h * 0.70)
    id_contains = (
        "send_message_button", "send_button", "send_btn", "btn_send",
        "compose_send", "send_message", "send_pan",
    )
    desc_keywords = (
        "send message", "send sms", "send mms", "send",
        "илгээх", "послать", "enviar", "отправить",
    )

    # Pass 1: known compose send-button resource-ids in the bottom compose bar
    for node in root.iter("node"):
        if node.get("clickable") != "true":
            continue
        res_id = (node.get("resource-id") or "").lower()
        if not any(part in res_id for part in id_contains):
            continue
        coords = _node_center(node)
        if coords and coords[1] >= compose_y_min:
            get_logger().debug("Send button found by resource-id %s at %s", res_id, coords)
            return coords

    # Pass 2: content-desc/text in bottom compose area (exclude chat history nodes)
    for node in root.iter("node"):
        if node.get("clickable") != "true":
            continue
        res_id = (node.get("resource-id") or "").lower()
        if "message_item" in res_id or "message_status" in res_id:
            continue
        text = (node.get("text") or "").lower().strip()
        desc = (node.get("content-desc") or "").lower().strip()
        label = desc or text
        if not label:
            continue
        if not any(kw == label or label.startswith(kw + " ") for kw in desc_keywords):
            continue
        coords = _node_center(node)
        if coords and coords[1] >= compose_y_min:
            get_logger().debug("Send button found by label '%s' at %s", label, coords)
            return coords

    return None


def _find_call_button(serial: str) -> tuple[int, int] | None:
    """Find the dialpad call/send button, preferring buttons in the bottom half of the screen
    to avoid accidentally matching contacts or recent-calls icons at the top."""
    root = _dump_ui_tree(serial)
    if root is None:
        return None
    _, h = _get_screen_size(serial)
    mid_y = h // 2
    keywords = {"call", "dial", "voice call", "дозвониться", "llamar"}
    id_suffixes = {":fab", "/fab", ":call_button", "/call_button",
                   ":dialpad_floating_action_button", "/dialpad_floating_action_button"}
    # First pass: bottom-half only (avoids top-bar icons)
    for node in root.iter("node"):
        if node.get("clickable") != "true":
            continue
        text  = (node.get("text") or "").lower()
        desc  = (node.get("content-desc") or "").lower()
        res   = (node.get("resource-id") or "").lower()
        id_ok = any(res.endswith(s) for s in id_suffixes)
        if text in keywords or desc in keywords or id_ok:
            nums = re.findall(r"\d+", node.get("bounds", ""))
            if len(nums) == 4:
                cx = (int(nums[0]) + int(nums[2])) // 2
                cy = (int(nums[1]) + int(nums[3])) // 2
                if cy >= mid_y:
                    get_logger().debug("Call button found at (%d,%d)", cx, cy)
                    return cx, cy
    # Second pass: anywhere (fallback)
    coords = _find_clickable(root, keywords, id_suffixes)
    if coords:
        get_logger().debug("Call button found (any position) at %s", coords)
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

    Checks all SIM slots and takes the highest state so the idle SIM
    doesn't mask an active one. Falls back to dumpsys telecom on Pixel/AOSP.
    """
    ok, out = _run(_serial_args(serial) + ["shell", "dumpsys", "telephony.registry"])
    if ok:
        states = [int(m.group(1)) for m in re.finditer(r"mCallState=(\d)", out)]
        if states:
            best = max(states)
            if best > 0:
                return best

    ok, out = _run(_serial_args(serial) + ["shell", "dumpsys", "telecom"])
    if ok:
        out_l = out.lower()
        if "state: ringing" in out_l:
            return 1
        if "state: active" in out_l or "state: dialing" in out_l:
            return 2

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


def _wait_for_offhook(serial: str, wait: float = 2.0) -> bool:
    """Return True if call state reaches OFFHOOK (2) within *wait* seconds."""
    deadline = time.time() + wait
    while time.time() < deadline:
        if _get_call_state(serial) == 2:
            return True
        time.sleep(0.5)
    return False


def answer_call(serial: str) -> Result:
    """Wake the screen, unlock, then answer the incoming call.

    Every method is verified by checking mCallState=2 (OFFHOOK) afterwards
    so a silent failure (exit 0 but call not answered) is caught.
    """
    log = get_logger()

    wake_and_unlock(serial)
    time.sleep(0.5)

    w, h = _get_screen_size(serial)

    # ── 1. KEYCODE_HEADSETHOOK ───────────────────────────────────────
    # Simulates headset button press — answers floating notification calls on HyperOS.
    # uiautomator cannot see the floating call banner; keyevents still reach the system.
    _run(_serial_args(serial) + ["shell", "input", "keyevent", "KEYCODE_HEADSETHOOK"])
    if _wait_for_offhook(serial):
        log.info("Call answered via KEYCODE_HEADSETHOOK on %s", serial)
        return True, "Call answered (HEADSETHOOK)"

    # ── 2. telecom accept-ringing-call ───────────────────────────────
    log.warning("HEADSETHOOK did not answer (state=%d) — trying telecom", _get_call_state(serial))
    _run(_serial_args(serial) + ["shell", "telecom", "accept-ringing-call"])
    if _wait_for_offhook(serial):
        return True, "Call answered (telecom)"

    # ── 3. KEYCODE_CALL ──────────────────────────────────────────────
    log.warning("telecom did not answer (state=%d) — trying KEYCODE_CALL", _get_call_state(serial))
    _run(_serial_args(serial) + ["shell", "input", "keyevent", "KEYCODE_CALL"])
    if _wait_for_offhook(serial):
        return True, "Call answered via KEYCODE_CALL"

    # ── 4. Tap the floating notification answer button ────────────────
    # HyperOS floating call banner: ~top 10% of screen, answer button on right side
    log.warning("KEYCODE_CALL did not answer — tapping notification answer area")
    tap_x = int(w * 0.82)
    tap_y = int(h * 0.09)
    _run(_serial_args(serial) + ["shell", "input", "tap", str(tap_x), str(tap_y)])
    if _wait_for_offhook(serial):
        return True, f"Call answered via notification tap ({tap_x},{tap_y})"

    # ── 5. Expand to full-screen call UI then tap green button ────────
    log.warning("Notification tap did not answer — expanding to full-screen call")
    _run(_serial_args(serial) + [
        "shell", "am", "start",
        "-a", "android.intent.action.MAIN",
        "-c", "android.intent.category.CALL_PRIVILEGED",
    ])
    time.sleep(1.5)
    ax, ay = int(w * 0.25), int(h * 0.75)
    _run(_serial_args(serial) + ["shell", "input", "tap", str(ax), str(ay)])
    if _wait_for_offhook(serial):
        return True, f"Call answered via full-screen tap ({ax},{ay})"

    state = _get_call_state(serial)
    return False, (
        f"Could not answer call (final state={state}: 0=idle 1=ringing 2=offhook). "
        "On Xiaomi HyperOS enable: Settings → Additional settings → Developer options → "
        "'USB debugging (Security settings)' and 'Disable permission monitoring'."
    )


def _normalise_msisdn(num: str) -> str:
    return re.sub(r"\D", "", num)[-8:]


def _parse_sms_row(line: str) -> tuple[str, str] | None:
    """Extract (address, body) from a `content query` Row line."""
    if "address=" not in line:
        return None
    addr_match = re.search(r"address=([^,]+)", line)
    if not addr_match:
        return None
    body_match = re.search(
        r"body=(.+?)(?:,\s*(?:date|type|_id|thread_id|read|status|date_sent|"
        r"protocol|reply_path_present|subject|service_center|locked|"
        r"error_code|seen|sub_id|creator|person)=|$)",
        line,
    )
    body = body_match.group(1).strip() if body_match else ""
    return addr_match.group(1).strip(), body


def _numbers_match(addr: str, target: str) -> bool:
    a = _normalise_msisdn(addr)
    t = _normalise_msisdn(target)
    return (
        a == t
        or a.endswith(t) or t.endswith(a)
        or (len(t) >= 6 and a[-6:] == t[-6:])
    )


def _verify_sms_sent_in_ui(serial: str, message: str) -> bool:
    """OnePlus/Google Messages show the just-sent bubble with '..., Sent' in content-desc."""
    root = _dump_ui_tree(serial)
    if root is None:
        return False
    needle = message.lower().strip()
    for node in root.iter("node"):
        desc = (node.get("content-desc") or "").lower()
        if "sent" not in desc:
            continue
        if not needle or needle in desc:
            get_logger().debug("SMS send verified in UI: %s", desc[:120])
            return True
    return False


def _verify_sms_sent(serial: str, number: str, message: str, wait_secs: int = 15) -> Result:
    """Check sent box / chat UI for a message to *number* with matching body."""
    log = get_logger()
    target = _normalise_msisdn(number)
    needle = message.lower().strip()
    uris = ["content://sms/sent", "content://sms/outbox"]
    deadline = time.time() + wait_secs

    while time.time() < deadline:
        for uri in uris:
            # Do NOT use --sort on OnePlus/OxygenOS — it breaks `content query`.
            ok, raw = _run(
                _serial_args(serial)
                + ["shell", "content", "query", "--uri", uri,
                   "--projection", "address:body:date:type"]
            )
            if not ok or "Row:" not in raw:
                log.debug("SMS sent query %s empty/failed: %s", uri, raw[:200])
                continue
            best_date = -1
            best_body = ""
            for line in raw.splitlines():
                parsed = _parse_sms_row(line)
                if not parsed:
                    continue
                addr, body = parsed
                if not _numbers_match(addr, number):
                    continue
                date_match = re.search(r"date=(\d+)", line)
                msg_date = int(date_match.group(1)) if date_match else 0
                if msg_date >= best_date:
                    best_date = msg_date
                    best_body = body
            if best_body and (not needle or needle in best_body.lower()):
                log.info("SMS send verified in %s to %s: %s", uri, number, best_body[:80])
                return True, f"SMS sent and verified | body={best_body[:120]}"

        if _verify_sms_sent_in_ui(serial, message):
            return True, f"SMS sent and verified in chat UI | body={message[:120]}"

        time.sleep(1.5)

    return False, f"SMS composer action completed but no sent message to {number} found within {wait_secs}s"


def send_sms(serial: str, number: str, message: str) -> Result:
    """Open the SMS composer, pre-fill number + body, then tap the Send button.

    Tries smsto: then sms: URI schemes for compatibility with Samsung/Pixel/AOSP.
    The message body is base64-encoded to avoid shell word-splitting issues
    (e.g. "from" in the body being parsed as a package flag).
    """
    log = get_logger()
    import base64

    wake_and_unlock(serial)
    encoded_number = urllib.parse.quote(number)
    # Encode message to avoid shell interpretation of special words like "from"
    b64_msg = base64.b64encode(message.encode()).decode()
    # Shell command: decode base64 then pass as sms_body via env var trick
    # We use a temp file to pass the body safely
    tmp_body = "/sdcard/_sms_body.txt"
    _run(_serial_args(serial) + ["shell", f"echo '{b64_msg}' | base64 -d > {tmp_body}"])

    launched = False
    last_out = ""
    for uri in [f"smsto:{encoded_number}", f"sms:{encoded_number}"]:
        # Read body from the temp file in the shell to avoid any word-splitting
        shell_cmd = (
            f"am start -a android.intent.action.SENDTO -d '{uri}' "
            f"--es sms_body \"$(cat {tmp_body})\" "
            f"--ez exit_on_sent true"
        )
        ok, out = _run(_serial_args(serial) + ["shell", shell_cmd])
        last_out = out
        if ok and "Error" not in out and "unable to resolve" not in out.lower():
            launched = True
            log.info("SMS composer opened with URI %s", uri)
            break
        log.warning("URI %s failed: %s", uri, out)

    if not launched:
        return False, f"Could not open SMS composer: {last_out}"

    time.sleep(2.5)

    sent = False
    for attempt in range(3):
        coords = _find_send_button(serial)
        if coords:
            x, y = coords
            tap_ok, tap_out = _run(_serial_args(serial) + ["shell", "input", "tap", str(x), str(y)])
            if not tap_ok:
                return False, f"Send button tap failed: {tap_out}"
            log.info("SMS Send button tapped at (%d, %d) attempt %d", x, y, attempt + 1)
            sent = True
            break
        time.sleep(1)

    if not sent:
        log.warning("Send button not found — trying ENTER keyevent fallback")
        _run(_serial_args(serial) + ["shell", "input", "keyevent", "66"])

    verified, verify_out = _verify_sms_sent(serial, number, message)
    if verified:
        return True, verify_out
    if sent:
        return False, f"Send button tapped but verification failed: {verify_out}"
    return False, f"Send button not found and ENTER fallback did not send: {verify_out}"


def check_sms_received(serial: str, from_number: str, expected_text: str = "", timeout: int = 15) -> Result:
    """Poll the SMS inbox until a message from *from_number* arrives, up to *timeout* seconds.

    Strips country-code prefixes when comparing so +97699001122 matches 99001122.
    Returns (True, message_body) if found, (False, reason) otherwise.
    """
    log = get_logger()

    def normalise(num: str) -> str:
        return re.sub(r"\D", "", num)[-8:]

    def _query_inbox() -> tuple[bool, str] | None:
        # Try /inbox first (confirmed working on Pixel 9), fall back to /sms
        raw = ""
        for uri in ["content://sms/inbox", "content://sms"]:
            ok, out = _run(
                _serial_args(serial)
                + ["shell", "content", "query", "--uri", uri]
            )
            if ok and "Row:" in out:
                raw = out
                break
            log.debug("SMS query %s failed or empty: %s", uri, out[:200])

        if not raw:
            return False, "SMS query returned no rows"

        target = normalise(from_number)
        log.debug("SMS raw (first 500): %s", raw[:500])
        for line in raw.splitlines():
            if "address=" not in line:
                continue
            addr_match = re.search(r"address=(\S+)", line)
            if not addr_match:
                continue
            addr = normalise(addr_match.group(1))
            body_match = re.search(
                r"body=(.+?)(?:,\s*(?:type|date|_id|thread_id|read|status|"
                r"protocol|reply_path_present|subject|service_center|locked|"
                r"error_code|seen|sub_id|creator)=|$)",
                line,
            )
            body = body_match.group(1).strip() if body_match else ""
            log.debug("SMS row: addr=%s (norm=%s) target=%s body=%s", addr_match.group(1), addr, target, body[:60])
            matched = (addr == target
                       or addr.endswith(target) or target.endswith(addr)
                       or (len(target) >= 6 and addr[-6:] == target[-6:]))
            if matched:
                if expected_text and expected_text.lower() not in body.lower():
                    log.warning("SMS found from %s but body '%s' doesn't contain '%s'",
                                from_number, body, expected_text)
                    return False, f"SMS received but content mismatch. Got: {body}"
                log.info("SMS from %s found: %s", from_number, body)
                return True, body
        return None  # not found yet

    log.info("Waiting up to %ds for SMS from %s on %s ...", timeout, from_number, serial)
    deadline = time.time() + timeout
    last_fail = f"No SMS from {from_number} found in inbox"
    while time.time() < deadline:
        result = _query_inbox()
        if result is not None:
            return result
        time.sleep(3)
    # Final check
    result = _query_inbox()
    if result is not None:
        return result
    return False, last_fail


def _read_ussd_response(serial: str, wait_secs: int = 15) -> str | None:
    """Poll the UI until a USSD response dialog appears, then return its message text.

    Collects ALL text fragments from matching nodes and joins them so that
    multi-node responses (e.g. "MSISDN:" in one node and the number in another)
    are returned as a single string.
    """
    log = get_logger()
    response_ids = {
        "android:id/message",
        "com.android.phone:id/message",
        "com.google.android.dialer:id/ussd_response",
    }
    not_ussd = {"calling", "calling...", "calling…", "connecting",
                "dialing", "ringing", "on hold", "disconnected",
                "ussd code running", "ussd code running…", "ussd code running...", ""}
    deadline = time.time() + wait_secs
    while time.time() < deadline:
        time.sleep(2)
        root = _dump_ui_tree(serial)
        if root is None:
            continue

        # ── Pass 1: collect all nodes with known USSD resource-ids ──────
        fragments: list[str] = []
        for node in root.iter("node"):
            res_id = node.get("resource-id") or ""
            text = (node.get("text") or "").strip()
            if res_id in response_ids and text:
                fragments.append(text)
        if fragments:
            result = " ".join(fragments)
            log.info("USSD response (resource-id): %s", result)
            return result

        # ── Pass 2: all TextViews in telephony/dialer package ───────────
        fragments = []
        for node in root.iter("node"):
            pkg = node.get("package") or ""
            cls = node.get("class") or ""
            text = (node.get("text") or "").strip()
            if (("phone" in pkg or "dialer" in pkg) and "TextView" in cls
                    and text and text.lower() not in not_ussd):
                fragments.append(text)
        if fragments:
            result = " ".join(fragments)
            if len(result) > 5:
                log.info("USSD response (fallback TextView): %s", result)
                return result

    log.warning("USSD response dialog not detected within %ds", wait_secs)
    return None


def _dismiss_ussd_dialog(serial: str) -> None:
    """Tap the OK/Close/Dismiss button on the USSD response dialog."""
    log = get_logger()
    root = _dump_ui_tree(serial)
    if root is None:
        return
    ok_keywords = {"ok", "close", "dismiss", "cancel", "done"}
    id_suffixes = {":button1", "/button1", ":button2", "/button2",
                   ":ok", "/ok", ":close", "/close"}
    coords = _find_clickable(root, ok_keywords, id_suffixes)
    if coords:
        x, y = coords
        _run(_serial_args(serial) + ["shell", "input", "tap", str(x), str(y)])
        log.info("USSD dialog dismissed via tap at (%d, %d)", x, y)
    else:
        # Fallback: press BACK key
        _run(_serial_args(serial) + ["shell", "input", "keyevent", "KEYCODE_BACK"])
        log.info("USSD dialog dismissed via BACK key")


def check_call_log(serial: str, number: str, call_type: str = "") -> Result:
    """Verify the most recent call to/from *number* exists in the call log.

    call_type: "INCOMING", "OUTGOING", or "" (any).
    Returns (True, "duration Xs | type=...") on success.
    """
    log = get_logger()

    def normalise(n: str) -> str:
        return re.sub(r"\D", "", n)[-8:]

    ok, raw = _run(
        _serial_args(serial)
        + ["shell", "content", "query", "--uri", "content://call_log/calls"]
    )
    if not ok or "Row:" not in raw:
        return False, f"Call log query failed or empty: {raw[:200]}"

    # type values: 1=incoming, 2=outgoing, 3=missed, 4=voicemail
    type_map = {"1": "INCOMING", "2": "OUTGOING", "3": "MISSED", "4": "VOICEMAIL"}
    target = normalise(number)

    for line in raw.splitlines():
        if "number=" not in line and "name=" not in line:
            continue
        num_match = re.search(r"\bnumber=(\S+)", line)
        if not num_match:
            continue
        num = normalise(num_match.group(1))
        if not (num == target or num.endswith(target) or target.endswith(num)
                or (len(target) >= 6 and num[-6:] == target[-6:])):
            continue

        dur_match = re.search(r"\bduration=(\d+)", line)
        type_match = re.search(r"\btype=(\d+)", line)
        duration = int(dur_match.group(1)) if dur_match else 0
        ctype = type_map.get(type_match.group(1) if type_match else "", "UNKNOWN")

        if call_type and call_type.upper() not in (ctype, "ANY"):
            log.debug("Call found but type mismatch: got %s expected %s", ctype, call_type)
            continue

        if duration == 0:
            return False, f"Call to/from {number} found but duration=0 (call may not have connected)"

        msg = f"Call verified | duration={duration}s | type={ctype} | number={num_match.group(1)}"
        log.info(msg)
        return True, msg

    return False, f"No call log entry found for {number}"


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
    _dismiss_ussd_dialog(serial)
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
    """Return device path of the most recently modified call-recording audio file.

    Only searches known call-recording directories, intentionally excluding
    /sdcard/Music and /sdcard/Ringtones to avoid picking up ringtone files.
    """
    search_dirs = [
        "/sdcard/CallRecordings",                        # com.nll.cb (ACR variant)
        "/sdcard/Recordings/Call",                       # Samsung, stock Android
        "/sdcard/Call",
        "/sdcard/MIUI/sound_recorder/call_rec",
        "/sdcard/PhoneRecord",
        "/sdcard/Android/data/com.nll.acr/files",        # ACR classic
        "/sdcard/Android/data/com.nll.cb/files",         # ACR (com.nll.cb)
        "/sdcard/Android/data/com.samsung.android.incallui/files",
        "/sdcard/Recordings",                            # broad Samsung fallback
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
    ok, out = _run(_serial_args(serial) + ["shell", "ls", "-t"] + candidates)
    if ok and out.strip():
        return out.splitlines()[0].strip()
    return candidates[0]


def _tap_incall_record_button(serial: str) -> bool:
    """Tap the Record button in the in-call UI. Returns True if tapped."""
    log = get_logger()
    root = _dump_ui_tree(serial)
    if root is None:
        return False
    record_keywords = {"record", "recording", "start recording", "rec"}
    record_id_suffixes = {
        ":incall_record_button", "/incall_record_button",
        ":record_button", "/record_button",
        ":record", "/record",
        ":btn_record", "/btn_record",
    }
    # Also scan all clickable nodes for any attribute containing "record"
    for node in root.iter("node"):
        if node.get("clickable") != "true":
            continue
        text = (node.get("text") or "").lower()
        desc = (node.get("content-desc") or "").lower()
        res  = (node.get("resource-id") or "").lower()
        if (text in record_keywords or desc in record_keywords
                or any(res.endswith(s) for s in record_id_suffixes)
                or "record" in res):
            nums = re.findall(r"\d+", node.get("bounds", ""))
            if len(nums) == 4:
                x = (int(nums[0]) + int(nums[2])) // 2
                y = (int(nums[1]) + int(nums[3])) // 2
                _run(_serial_args(serial) + ["shell", "input", "tap", str(x), str(y)])
                log.info("In-call Record button tapped at (%d, %d) res=%s", x, y, res)
                return True
    log.warning("Record button not found in UI dump for %s — is call recording enabled in Phone app settings?", serial)
    return False


def start_call_recording(serial: str) -> Result:
    """Start fully-automated call audio recording.

    Priority:
      1. In-call Record button — works on Pixel (Google Phone) and Samsung with
         built-in call recording enabled.  No extra app needed.
      2. tinycap  — low-level mic capture, works on AOSP/most stock ROMs.
      3. ACR      — Another Call Recorder (install once from Play Store).
      4. OEM setting — known per-OEM settings keys (Samsung, Xiaomi, Oppo).
    """
    log = get_logger()

    # ── 1. Tap in-call Record button ─────────────────────────────────
    time.sleep(1)  # give the in-call UI a moment to fully render
    if _tap_incall_record_button(serial):
        return True, "Recording started via in-call Record button"

    # ── 2. tinycap ────────────────────────────────────────────────────
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

    # ── 3. ACR broadcast ─────────────────────────────────────────────
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

    # ── 4. OEM settings key ──────────────────────────────────────────
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
        "  A) On Pixel 9: open Phone app → Menu (⋮) → Settings → Call recording\n"
        "     and enable 'Always record' or 'Record automatically'.\n"
        "  B) Install ACR (Another Call Recorder) from the Play Store,\n"
        "     then re-run — the tool will use it automatically."
    )


def stop_call_recording(serial: str, local_path: str) -> Result:
    """Stop recording and pull the audio file to *local_path*."""
    log = get_logger()
    os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)

    # ── In-call Record button (toggle off) ───────────────────────────
    # Only tap if call is still active; after END_CALL the in-call UI is
    # gone and ACR stops automatically when the call ends.
    if _get_call_state(serial) == 2:
        _tap_incall_record_button(serial)
        time.sleep(1)

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
