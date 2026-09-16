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
        ":send", "/send",   # Google Messages: Compose:Draft:Send
        "draft:send",
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


def get_device_phone_number(serial: str) -> str:
    """Try several methods to read the SIM phone number (MSISDN).

    Returns the number string (e.g. +97694310546) or "" if unavailable.
    Works on most Samsung and Pixel devices; restricted on some Android 12+ builds.
    """
    # ── 1. dumpsys iphonesubinfo ─────────────────────────────────────
    ok, out = _run(_serial_args(serial) + ["shell", "dumpsys", "iphonesubinfo"])
    if ok and out:
        # "Line 1 Phone Number = +97694310546" style
        for pattern in [
            r"(?:line 1 phone number|phone number|msisdn|subscriber number)\s*=\s*([+\d][\d\s\-]{6,})",
            r"(?:Line1Number|getLine1Number)\s*[=:]\s*([+\d][\d]{6,})",
        ]:
            m = re.search(pattern, out, re.IGNORECASE)
            if m:
                num = re.sub(r"\s", "", m.group(1)).strip()
                if len(num) >= 7:
                    return num

    # ── 2. service call iphonesubinfo (slot 0) ───────────────────────
    # Different Android versions use different transaction codes
    for code in ["11", "15", "13"]:
        ok2, raw = _run(_serial_args(serial) + ["shell", "service", "call", "iphonesubinfo", code])
        if ok2 and raw:
            # Output: Result: Parcel(...) '...+97694310546...'
            chars = re.findall(r"'(.)'", raw)
            candidate = "".join(chars).strip().replace("\x00", "")
            candidate = re.sub(r"[^\d+]", "", candidate)
            if candidate and len(candidate) >= 7:
                return candidate

    # ── 3. getprop — generic + Samsung-specific props ───────────────────
    for prop in [
        "gsm.sim.ril.number.1", "gsm.sim.ril.number",
        "ril.msisdn.1", "ril.msisdn",
        "persist.radio.msisdn.1", "persist.radio.msisdn",
        # Samsung One UI props
        "ril.phone_number1", "ril.phone_number2",
        "gsm.ril.phone_number.1", "gsm.ril.phone_number.2",
        "persist.ril.phone_number1",
        "sys.smartcard.phonenum.1",
    ]:
        ok3, val = _run(_serial_args(serial) + ["shell", "getprop", prop])
        val = (val or "").strip()
        if ok3 and val and re.match(r"[+\d]{7,}", val):
            return val

    # ── 4. SIM own number from telephony settings ────────────────────────
    ok4, out4 = _run(_serial_args(serial) + [
        "shell", "content", "query",
        "--uri", "content://telephony/siminfo",
        "--projection", "icc_id:number:display_name",
    ])
    if ok4 and out4:
        m = re.search(r"number=([+\d]{7,})", out4)
        if m:
            return m.group(1)

    # ── 5. service call with SIM slot index 1 and 2 ──────────────────────
    for code in ["16", "12", "14"]:
        ok5, raw5 = _run(_serial_args(serial) + ["shell", "service", "call", "iphonesubinfo", code, "i32", "1"])
        if ok5 and raw5:
            chars = re.findall(r"'(.)'", raw5)
            candidate = re.sub(r"[^\d+]", "", "".join(chars).replace("\x00", ""))
            if candidate and len(candidate) >= 7:
                return candidate

    return ""


def get_device_phone_numbers(serial: str) -> tuple[str, str]:
    """Return (sim1_number, sim2_number) by reading all SIM slots.

    Uses content://telephony/siminfo (Android 9+) to get each slot's number,
    then falls back to get_device_phone_number() for SIM1 only.
    """
    # Primary: content://telephony/siminfo with slot index
    ok, out = _run(_serial_args(serial) + [
        "shell", "content", "query",
        "--uri", "content://telephony/siminfo",
        "--projection", "sim_slot_index:number:display_name",
    ], timeout=8)
    sim1, sim2 = "", ""
    if ok and out:
        for line in out.splitlines():
            if "sim_slot_index=" not in line:
                continue
            slot_m = re.search(r"sim_slot_index=(\d)", line)
            num_m  = re.search(r"\bnumber=([+\d]{7,})", line)
            if slot_m and num_m:
                idx = int(slot_m.group(1))
                num = num_m.group(1)
                if idx == 0:
                    sim1 = num
                elif idx == 1:
                    sim2 = num
        if sim1 or sim2:
            return sim1, sim2

    # Fallback: single-number method covers SIM1
    sim1 = get_device_phone_number(serial)
    return sim1, ""


def get_device_model(serial: str) -> str:
    """Return a human-readable model name for display (e.g. 'Samsung Galaxy S23')."""
    ok, brand = _run(_serial_args(serial) + ["shell", "getprop", "ro.product.brand"])
    ok2, model = _run(_serial_args(serial) + ["shell", "getprop", "ro.product.model"])
    brand = (brand or "").strip()
    model = (model or "").strip()
    if brand and model:
        return f"{brand.title()} {model}"
    return model or serial


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


def go_home(serial: str) -> Result:
    """Send device to home screen."""
    _run(_serial_args(serial) + ["shell", "input", "keyevent", "KEYCODE_HOME"])
    return True, "Home screen"


def make_call(serial: str, number: str) -> Result:
    """Trigger an outgoing call via the Android dialer."""
    import time
    uri = f"tel:{number.strip()}"
    # ACTION_DIAL pre-fills the number without triggering Android's emergency-number block.
    # KEYCODE_CALL then initiates the call, mimicking a physical button press.
    ok, out = _run(_serial_args(serial) + ["shell", "am", "start", "-a", "android.intent.action.DIAL", "-d", uri])
    if not ok:
        return ok, out
    time.sleep(1.5)
    return _run(_serial_args(serial) + ["shell", "input", "keyevent", "KEYCODE_CALL"])


def end_call(serial: str) -> Result:
    """End an active call."""
    import time
    # Primary: hardware ENDCALL key
    ok, out = _run(_serial_args(serial) + ["shell", "input", "keyevent", "KEYCODE_ENDCALL"])
    time.sleep(0.8)
    if _get_call_state(serial) == 0:
        return True, "Call ended via KEYCODE_ENDCALL"
    # Fallback: tap the on-screen end-call button (needed for service/short numbers)
    root = _dump_ui_tree(serial)
    if root is not None:
        keywords = {"end call", "hang up", "disconnect", "decline"}
        id_suffixes = {":end_call", "/end_call", ":hangup", "/hangup",
                       ":end_button", "/end_button", ":reject", "/reject"}
        id_contains = {"end_call", "hangup", "end_button"}
        coords = _find_clickable(root, keywords, id_suffixes, id_contains)
        if coords:
            _run(_serial_args(serial) + ["shell", "input", "tap", str(coords[0]), str(coords[1])])
            time.sleep(0.5)
            return True, "Call ended via end-call button tap"
    return ok, out


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
    """Poll the SMS inbox until a *recent* message from *from_number* arrives.

    Only considers messages received in the last 10 minutes to avoid matching
    SMS from previous test runs.
    """
    log = get_logger()

    def normalise(num: str) -> str:
        return re.sub(r"\D", "", num)[-8:]

    # Only match messages received within the last 10 minutes (in ms epoch)
    cutoff_ms = int((time.time() - 600) * 1000)

    def _query_inbox() -> tuple[bool, str] | None:
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
            # Skip messages older than 10 minutes
            date_match = re.search(r"\bdate=(\d+)", line)
            if date_match and int(date_match.group(1)) < cutoff_ms:
                continue
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
            if not matched:
                continue
            # Address matches — check body. Keep scanning all rows so an old
            # OTP/unrelated message doesn't shadow the actual test SMS.
            if not expected_text or expected_text.lower() in body.lower():
                log.info("SMS from %s found: %s", from_number, body)
                return True, body
            log.debug("Address match but body mismatch (looking for '%s'): %s", expected_text, body[:80])
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

    On Pixel 9 the response is split across multiple nodes (e.g. "MSISDN:" in
    one node, the number in another). This function collects ALL visible text
    in the dialog area and joins fragments to form the complete response.
    """
    log = get_logger()
    response_ids = {
        "android:id/message",
        "com.android.phone:id/message",
        "com.google.android.dialer:id/ussd_response",
    }
    not_ussd = {
        "calling", "calling...", "calling…", "connecting", "dialing",
        "ringing", "on hold", "disconnected",
        "ussd code running", "ussd code running…", "ussd code running...",
        "ok", "cancel", "close", "dismiss", "",
    }
    deadline = time.time() + wait_secs
    while time.time() < deadline:
        time.sleep(2)
        root = _dump_ui_tree(serial)
        if root is None:
            continue

        # ── Pass 1: collect ALL nodes with known USSD resource-ids ──────
        # Also include any sibling/child text nodes in the same dialog.
        fragments: list[str] = []
        dialog_found = False
        for node in root.iter("node"):
            res_id = node.get("resource-id") or ""
            text = (node.get("text") or "").strip()
            if res_id in response_ids:
                dialog_found = True
                if text:
                    fragments.append(text)
        if dialog_found:
            # Also collect any adjacent TextView text in the same dialog package
            pkg_hint = None
            for node in root.iter("node"):
                if (node.get("resource-id") or "") in response_ids:
                    pkg_hint = node.get("package")
                    break
            if pkg_hint:
                for node in root.iter("node"):
                    if node.get("package") != pkg_hint:
                        continue
                    text = (node.get("text") or "").strip()
                    res_id = node.get("resource-id") or ""
                    if (text and text not in fragments
                            and text.lower() not in not_ussd
                            and res_id not in response_ids  # already added
                            and "button" not in (node.get("class") or "").lower()):
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
        if "number=" not in line:
            continue
        num_match = re.search(r"\bnumber=([^,\s]+)", line)
        if not num_match:
            continue
        raw_num = num_match.group(1).strip().rstrip(",")
        num = normalise(raw_num)
        if not num:  # skip empty number fields
            continue
        if not (num == target or num.endswith(target) or target.endswith(num)
                or (len(target) >= 6 and num[-6:] == target[-6:])):
            continue

        dur_match  = re.search(r"\bduration=(\d+)", line)
        type_match = re.search(r"\btype=(\d+)", line)
        id_match   = re.search(r"\b_id=(\d+)", line)
        duration = int(dur_match.group(1)) if dur_match else 0
        ctype    = type_map.get(type_match.group(1) if type_match else "", "UNKNOWN")
        entry_id = id_match.group(1) if id_match else None

        if call_type and call_type.upper() not in (ctype, "ANY"):
            log.debug("Call found but type mismatch: got %s expected %s", ctype, call_type)
            continue

        if duration == 0:
            return False, f"Call to/from {number} found but duration=0 (call may not have connected)"

        # Delete the matched entry so repeated test runs don't see stale log entries
        if entry_id:
            _run(_serial_args(serial) + [
                "shell", "content", "delete",
                "--uri", "content://call_log/calls",
                "--where", f"_id={entry_id}",
            ])
            log.info("Deleted call log entry _id=%s for %s", entry_id, number)

        msg = f"Call verified | duration={duration}s | type={ctype} | number={raw_num}"
        log.info(msg)
        return True, msg

    return False, f"No call log entry found for {number}"


def check_volte(serial: str) -> Result:
    """Check whether VoLTE is active / supported on the device.

    Tries four independent signal sources and returns the first conclusive answer.
    Works on Samsung (Android 12-14) and Pixel (Android 13-15).
    """
    log = get_logger()

    _NET_LABELS = {13: "LTE", 19: "NR_NSA", 20: "NR_SA"}
    _VOLTE_NETS = {13, 19, 20}

    # ── 1. telephony.registry — most reliable cross-device source ────
    ok, reg = _run(_serial_args(serial) + ["shell", "dumpsys", "telephony.registry"])
    if ok and reg:
        call_states  = [int(x) for x in re.findall(r"mCallState\s*=\s*(\d+)", reg)]
        voice_types  = [int(x) for x in re.findall(r"mVoiceNetworkType\s*=\s*(\d+)", reg)]
        data_types   = [int(x) for x in re.findall(r"mDataNetworkType\s*=\s*(\d+)", reg)]
        net_types    = voice_types or data_types
        in_call      = any(s in (1, 2) for s in call_states)
        on_lte       = bool(set(net_types) & _VOLTE_NETS)
        active_net   = next((n for n in net_types if n in _VOLTE_NETS), net_types[0] if net_types else 0)
        net_name     = _NET_LABELS.get(active_net, f"type {active_net}")

        # imsCallType inside mCallStateLists: 0=none, 1=video, 2=voice(VoLTE), 3=UT
        # This is the most definitive VoLTE signal — present on Samsung & Pixel
        ims_call_types = [int(x) for x in re.findall(r"imsCallType\s*:\s*(\d+)", reg)]
        ims_svc_types  = [int(x) for x in re.findall(r"imsCallServiceType\s*:\s*(\d+)", reg)]
        volte_by_ims   = any(t in (1, 2) for t in ims_call_types)  # 1=video, 2=voice

        log.info(
            "telephony.registry: call_states=%s voice_types=%s data_types=%s "
            "ims_call_types=%s ims_svc_types=%s",
            call_states, voice_types, data_types, ims_call_types, ims_svc_types,
        )

        if in_call:
            if volte_by_ims:
                call_type_name = {1: "Video (ViLTE)", 2: "Voice (VoLTE)"}.get(
                    next(t for t in ims_call_types if t in (1, 2)), "IMS"
                )
                return True, f"VoLTE ON — {call_type_name} call via IMS"
            if on_lte:
                return True, f"VoLTE ON — call in progress on {net_name}"
            return True, f"VoLTE OFF — call active but not on IMS (network={net_name})"

        # No active call — check network readiness
        if ims_call_types:
            log.info("No active call but imsCallType entries exist: %s", ims_call_types)
        if on_lte:
            return True, f"VoLTE ON — on {net_name} (no active call to confirm IMS)"

    # ── 2. dumpsys ims — IMS service registration ────────────────────
    ok2, ims = _run(_serial_args(serial) + ["shell", "dumpsys", "ims"])
    if ok2 and ims:
        ims_l = ims.lower()
        log.debug("dumpsys ims snippet: %s", ims_l[:400])

        _REG_TRUE = [
            r"isregistered\s*[=:]\s*true",
            r"misismsregistered\s*=\s*true",
            r"registered\s*=\s*true",
            r"registrationstate\s*=\s*2",
            r"state\s*=\s*registered",
            r"isregistered:\s*true",
        ]
        _REG_FALSE = [
            r"isregistered\s*[=:]\s*false",
            r"misismsregistered\s*=\s*false",
            r"registered\s*=\s*false",
            r"registrationstate\s*=\s*[013]",
            r"state\s*=\s*not_registered",
        ]
        _VOLTE_TRUE = [
            r"mmtel",
            r"feature_tag",
            r"isvolteenabled\s*=\s*true",
            r"voice.*capable\s*=\s*true",
            r"volte.*enabled\s*=\s*true",
            r"audio.*capable\s*=\s*true",
        ]

        is_registered = any(re.search(p, ims_l) for p in _REG_TRUE)
        is_not_reg    = any(re.search(p, ims_l) for p in _REG_FALSE)
        has_volte_cap = any(re.search(p, ims_l) for p in _VOLTE_TRUE)

        log.info("IMS: registered=%s not_reg=%s volte_cap=%s", is_registered, is_not_reg, has_volte_cap)

        if is_registered:
            detail = "MMTEL registered" if has_volte_cap else "IMS registered"
            return True, f"VoLTE ON — {detail}"
        if is_not_reg:
            return True, "VoLTE OFF — IMS not registered"

    # ── 3. System properties ──────────────────────────────────────────
    volte_props = [
        "persist.radio.volte",
        "persist.vendor.radio.volte",
        "ro.config.volte_mode",
        "persist.dbg.volte_avail_ovr",
    ]
    for prop in volte_props:
        ok3, val = _run(_serial_args(serial) + ["shell", "getprop", prop])
        if ok3 and val.strip() and val.strip() not in ("", "0", "false"):
            log.info("getprop %s = %s", prop, val.strip())
            return True, f"VoLTE ON — {prop}={val.strip()}"

    # ── 4. Settings DB ────────────────────────────────────────────────
    for cmd in [
        ["shell", "settings", "get", "global", "volte_vt_enabled"],
        ["shell", "settings", "get", "global", "enhanced_4g_mode_enabled"],
    ]:
        ok4, val = _run(_serial_args(serial) + cmd)
        if ok4 and val.strip() == "1":
            log.info("settings %s = 1", cmd[-1])
            return True, f"VoLTE ON — {cmd[-1]}=1"
        if ok4 and val.strip() == "0":
            log.info("settings %s = 0", cmd[-1])
            return True, f"VoLTE OFF — {cmd[-1]}=0"

    # ── Fallback: report network type ────────────────────────────────
    if ok and reg:
        if on_lte:
            return True, f"VoLTE ON — on {net_name}"
        return True, f"VoLTE OFF — network={net_name}, not on LTE"

    return False, "VoLTE unknown — could not read IMS/telephony state (check ADB connection)"


def check_network(serial: str) -> Result:
    """Return current network type, operator, signal strength and service state."""
    NET_PROP_MAP = {
        "LTE": "LTE (4G)", "NR_NSA": "NR NSA (5G)", "NR_SA": "NR SA (5G)", "NR": "NR (5G)",
        "UMTS": "UMTS (3G)", "HSDPA": "HSDPA (3.5G)", "HSUPA": "HSUPA (3.5G)",
        "HSPA": "HSPA (3.5G)", "HSPAP": "HSPA+ (3.5G)", "TD_SCDMA": "TD-SCDMA (3G)",
        "EDGE": "EDGE (2G)", "GPRS": "GPRS (2G)", "GSM": "GSM (2G)",
    }
    NET_INT_MAP = {
        1: "GPRS (2G)", 2: "EDGE (2G)", 3: "UMTS (3G)", 8: "HSDPA (3.5G)",
        9: "HSUPA (3.5G)", 10: "HSPA (3.5G)", 13: "LTE (4G)", 15: "HSPA+ (3.5G)",
        16: "GSM (2G)", 17: "TD-SCDMA (3G)", 19: "NR NSA (5G)", 20: "NR SA (5G)",
    }
    SVC_STATES = {0: "IN_SERVICE", 1: "OUT_OF_SERVICE", 2: "EMERGENCY_ONLY", 3: "RADIO_OFF"}

    # ── 1. Operator ──────────────────────────────────────────────────────
    _, op_raw = _run(_serial_args(serial) + ["shell", "getprop", "gsm.operator.alpha"])
    operator = (op_raw or "").strip().split(",")[0]

    # ── 2. Network type — getprop (most readable, Samsung-reliable) ──────
    _, net_prop = _run(_serial_args(serial) + ["shell", "getprop", "gsm.network.type"])
    net_label = NET_PROP_MAP.get((net_prop or "").strip().split(",")[0].upper(), "")

    # ── 3. Network type fallback — dumpsys phone (has label in parentheses) ──
    if not net_label:
        _, phone = _run(_serial_args(serial) + ["shell", "dumpsys", "phone"])
        if phone:
            m = re.search(r"(?:mDataNetworkType|mVoiceNetworkType)\s*=\s*\d+\s*\(([^)]+)\)", phone)
            if m:
                net_label = m.group(1).strip()

    # ── 4. Network type + service state — telephony.registry ─────────────
    ok, reg = _run(_serial_args(serial) + ["shell", "dumpsys", "telephony.registry"])
    svc_state  = -1
    signal_dbm = None

    if ok and reg:
        svc_states = [int(x) for x in re.findall(r"mServiceState\s*=\s*(\d+)", reg)]
        svc_state  = min(svc_states) if svc_states else -1

        if not net_label:
            data_types  = [int(x) for x in re.findall(r"mDataNetworkType\s*=\s*(\d+)", reg)]
            voice_types = [int(x) for x in re.findall(r"mVoiceNetworkType\s*=\s*(\d+)", reg)]
            net_int     = max(data_types + voice_types) if (data_types or voice_types) else 0
            if net_int:
                net_label = NET_INT_MAP.get(net_int, f"type {net_int}")

        for pat in [r"rsrp\s*=\s*(-?\d+)", r"mDbm\s*=\s*(-?\d+)"]:
            m = re.search(pat, reg, re.IGNORECASE)
            if m:
                v = int(m.group(1))
                if -130 <= v <= -30:
                    signal_dbm = v
                    break

    parts = []
    # Only surface service state when it indicates a problem; skip IN_SERVICE / unreadable
    svc_label = SVC_STATES.get(svc_state, "")
    if svc_label and svc_label != "IN_SERVICE":
        parts.append(svc_label)
    if operator:
        parts.append(f"Operator: {operator}")
    parts.append(f"Network: {net_label or 'Unknown'}")
    if signal_dbm:
        parts.append(f"Signal: {signal_dbm} dBm")

    msg = " | ".join(parts)
    if svc_state == -1 and not operator and not net_label:
        return False, "Could not read network state (check ADB connection)"
    return True, msg


def _switch_row_text(nodes: list, sw_cy: int, tolerance: int = 60) -> str:
    """Collect all text on the same horizontal row as a switch (within tolerance px)."""
    parts = []
    for n in nodes:
        txt = (n.get("text") or "").strip()
        if not txt:
            continue
        nums = re.findall(r"\d+", n.get("bounds", ""))
        if len(nums) == 4:
            ny = (int(nums[1]) + int(nums[3])) // 2
            if abs(ny - sw_cy) <= tolerance:
                parts.append(txt.lower())
    return " ".join(parts)


def set_volte(serial: str, state: str) -> Result:
    """Enable or disable VoLTE via the Settings UI (works without root)."""
    enable = state.lower() in ("on", "1", "true", "enable")
    val    = "1" if enable else "0"
    label  = "ON" if enable else "OFF"
    log    = get_logger()

    _run(_serial_args(serial) + ["shell", "settings", "put", "global", "volte_vt_enabled", val])
    _run(_serial_args(serial) + ["shell", "settings", "put", "global", "enhanced_4g_mode_enabled", val])

    wake_and_unlock(serial)

    volte_kws = {"volte", "volte calls", "advanced calling", "hd calls",
                 "4g calling", "enhanced 4g", "lte calling", "vt calls"}

    root = None
    for intent_args in [
        ["-n", "com.android.phone/.settings.VoLteSettingActivity"],
        ["-n", "com.android.settings/.Settings$MobileNetworkActivity"],
        ["-a", "android.settings.NETWORK_OPERATOR_SETTINGS"],
    ]:
        _run(_serial_args(serial) + ["shell", "am", "start"] + intent_args)
        time.sleep(2)
        root = _dump_ui_tree(serial)
        if root is None:
            continue
        all_text = " ".join((n.get("text") or "").lower() for n in root.iter("node"))
        if any(kw in all_text for kw in volte_kws):
            break
        root = None
    else:
        return False, f"VoLTE {label} — could not open a screen with VoLTE settings"

    nodes = list(root.iter("node"))

    # Find the VoLTE switch by matching each Switch node to its row label (Y proximity)
    volte_switch = None
    for node in nodes:
        if "Switch" not in (node.get("class") or ""):
            continue
        coords = _node_center(node)
        if not coords:
            continue
        row_text = _switch_row_text(nodes, coords[1])
        if any(kw in row_text for kw in volte_kws):
            volte_switch = (node, coords)
            break

    # Fallback: find Switch near any VoLTE keyword node by flat index
    if volte_switch is None:
        for i, node in enumerate(nodes):
            text = (node.get("text") or "").lower()
            desc = (node.get("content-desc") or "").lower()
            if not any(kw in text or kw in desc for kw in volte_kws):
                continue
            for j in range(max(0, i - 3), min(len(nodes), i + 10)):
                nb = nodes[j]
                if "Switch" in (nb.get("class") or ""):
                    c = _node_center(nb)
                    if c:
                        volte_switch = (nb, c)
                        break
            if volte_switch:
                break

    if volte_switch is None:
        _run(_serial_args(serial) + ["shell", "input", "keyevent", "KEYCODE_BACK"])
        return False, f"VoLTE {label} — settings DB written but toggle not found in UI"

    sw_node, (sx, sy) = volte_switch
    checked_str = sw_node.get("checked", "")

    if checked_str:
        current_on = checked_str.lower() == "true"
        if current_on == enable:
            log.info("VoLTE already %s (checked=%s), no tap", label, checked_str)
            _run(_serial_args(serial) + ["shell", "input", "keyevent", "KEYCODE_BACK"])
            return True, f"VoLTE already {label}"

    _run(_serial_args(serial) + ["shell", "input", "tap", str(sx), str(sy)])
    log.info("VoLTE Switch tapped at (%d,%d) → %s (checked='%s')", sx, sy, label, checked_str)
    time.sleep(0.5)
    _run(_serial_args(serial) + ["shell", "input", "keyevent", "KEYCODE_BACK"])
    return True, f"VoLTE {label} — UI toggle set"


def check_wifi_calling(serial: str) -> Result:
    """Check whether WiFi Calling (WFC/VoWiFi) is enabled and IMS-registered.
    Always returns True (PASS) — result message shows ON or OFF status.
    """
    log = get_logger()

    _, wfc_en   = _run(_serial_args(serial) + ["shell", "settings", "get", "global", "wfc_ims_enabled"])
    _, wfc_mode = _run(_serial_args(serial) + ["shell", "settings", "get", "global", "wfc_ims_mode"])
    wfc_en   = (wfc_en   or "").strip()
    wfc_mode = (wfc_mode or "").strip()

    mode_labels = {"0": "WiFi only", "1": "Prefer cellular", "2": "Prefer WiFi"}
    mode_str = mode_labels.get(wfc_mode, f"mode={wfc_mode}")

    log.info("wfc_ims_enabled=%s wfc_ims_mode=%s", wfc_en, wfc_mode)

    if wfc_en == "0":
        return True, "WiFi Calling OFF"
    if wfc_en in ("null", ""):
        return True, "WiFi Calling OFF — not supported or not configured"

    # wfc_en == "1": check IMS registration
    _, ims = _run(_serial_args(serial) + ["shell", "dumpsys", "ims"])
    ims_l = (ims or "").lower()

    wfc_registered = bool(
        re.search(r"wfc.*regist|vowifi.*regist", ims_l)
        or re.search(r"feature.*tag.*mmtel", ims_l)
    )
    wfc_capable = bool(re.search(r"wfc|vowifi|wifi.*call|wifi.*capable", ims_l))

    if wfc_registered:
        return True, f"WiFi Calling ON — registered ({mode_str})"
    if wfc_capable:
        return True, f"WiFi Calling ON — enabled ({mode_str}), not yet registered"
    return True, f"WiFi Calling ON — enabled ({mode_str})"


def set_wifi_calling(serial: str, state: str) -> Result:
    """Enable or disable WiFi Calling via the Settings UI (works without root)."""
    parts  = state.lower().split(":")
    enable = parts[0] in ("on", "1", "true", "enable")
    val    = "1" if enable else "0"
    label  = "ON" if enable else "OFF"
    mode   = parts[1] if len(parts) > 1 and parts[1].isdigit() else ("2" if enable else "0")
    log    = get_logger()

    _run(_serial_args(serial) + ["shell", "settings", "put", "global", "wfc_ims_enabled", val])
    if enable:
        _run(_serial_args(serial) + ["shell", "settings", "put", "global", "wfc_ims_mode", mode])

    mode_labels = {"0": "WiFi only", "1": "Prefer cellular", "2": "Prefer WiFi"}
    mode_str = mode_labels.get(mode, f"mode={mode}")

    wake_and_unlock(serial)

    wfc_kws = {"wifi calling", "wi-fi calling", "wfc", "vowifi",
               "calls over wi-fi", "wi-fi calls"}

    root = None
    for intent_args in [
        ["-n", "com.android.phone/.settings.WifiCallingSettingActivity"],
        ["-n", "com.android.settings/.Settings$WifiCallingSettingsActivity"],
        ["-n", "com.samsung.android.settings/.Settings$WifiCallingSettingsActivity"],
        ["-a", "android.settings.WIRELESS_SETTINGS"],   # Samsung: Settings > Connections
    ]:
        _run(_serial_args(serial) + ["shell", "am", "start"] + intent_args)
        time.sleep(2)
        root = _dump_ui_tree(serial)
        if root is None:
            continue
        all_text = " ".join((n.get("text") or "").lower() for n in root.iter("node"))
        if any(kw in all_text for kw in wfc_kws):
            break
        root = None
    else:
        return False, f"WiFi Calling {label} — could not open WiFi Calling settings"

    nodes = list(root.iter("node"))

    # Find the WiFi Calling switch by Y-row matching
    wfc_switch = None
    for node in nodes:
        if "Switch" not in (node.get("class") or ""):
            continue
        coords = _node_center(node)
        if not coords:
            continue
        row_text = _switch_row_text(nodes, coords[1])
        if any(kw in row_text for kw in wfc_kws):
            wfc_switch = (node, coords)
            break

    # Fallback: flat index search
    if wfc_switch is None:
        for i, node in enumerate(nodes):
            text = (node.get("text") or "").lower()
            desc = (node.get("content-desc") or "").lower()
            if not any(kw in text or kw in desc for kw in wfc_kws):
                continue
            for j in range(max(0, i - 3), min(len(nodes), i + 10)):
                nb = nodes[j]
                if "Switch" in (nb.get("class") or ""):
                    c = _node_center(nb)
                    if c:
                        wfc_switch = (nb, c)
                        break
            if wfc_switch:
                break

    if wfc_switch is None:
        _run(_serial_args(serial) + ["shell", "input", "keyevent", "KEYCODE_BACK"])
        return False, f"WiFi Calling {label} — settings DB written but toggle not found in UI"

    sw_node, (sx, sy) = wfc_switch
    checked_str = sw_node.get("checked", "")

    if checked_str:
        current_on = checked_str.lower() == "true"
        if current_on == enable:
            log.info("WiFi Calling already %s (checked=%s), no tap", label, checked_str)
            _run(_serial_args(serial) + ["shell", "input", "keyevent", "KEYCODE_BACK"])
            return True, f"WiFi Calling already {label}"

    _run(_serial_args(serial) + ["shell", "input", "tap", str(sx), str(sy)])
    log.info("WiFi Calling Switch tapped at (%d,%d) → %s (checked='%s')", sx, sy, label, checked_str)
    time.sleep(0.5)
    _run(_serial_args(serial) + ["shell", "input", "keyevent", "KEYCODE_BACK"])

    msg = f"WiFi Calling {label}"
    if enable:
        msg += f" ({mode_str}) — UI toggle set"
    else:
        msg += " — UI toggle set"
    return True, msg


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


def set_network_type(serial: str, network: str) -> Result:
    """Switch the preferred network type by navigating the Mobile Networks settings UI.

    network: "2G", "3G", "4G", "5G", "4G5G", or "AUTO"
    Works on Samsung (com.samsung.android.app.telephonyui) and Pixel/AOSP.
    """
    log = get_logger()

    # Keywords matched with text.startswith(kw) — prevents "3g" matching "lte/3g/2g"
    # Ordered most-specific first within each network type
    OPTION_KEYWORDS: dict[str, list[str]] = {
        "5G":   ["5g (recommended)", "5g/lte/3g/2g", "nr/lte/3g/2g", "5g/lte", "nr/lte", "5g", "nr"],
        "4G5G": ["5g (recommended)", "5g/lte/3g/2g", "nr/lte/3g/2g", "5g/lte", "nr/lte"],
        "4G":   ["lte (recommended)", "lte/3g/2g", "lte/wcdma/gsm", "4g/3g/2g", "4g (recommended)",
                 "4g only", "lte only", "lte preferred", "lte", "4g"],
        "3G":   ["3g only", "3g/2g", "3g (recommended)", "3g preferred", "wcdma/gsm", "3g"],
        "2G":   ["2g only", "gsm only", "2g (recommended)", "2g preferred", "2g"],
        "AUTO": ["5g (recommended)", "5g/lte/3g/2g", "lte/3g/2g", "nr/lte/3g/2g",
                 "lte preferred", "lte/wcdma preferred", "automatic", "auto"],
    }
    # Words that confirm a valid network-type dialog
    NETWORK_INDICATORS = {"lte", "5g", "nr", "3g", "2g", "wcdma", "gsm", "umts", "4g", "auto"}

    key = network.upper().replace(" ", "")
    if key in OPTION_KEYWORDS:
        keywords = OPTION_KEYWORDS[key]
        raw_label: str | None = None
    else:
        # Raw on-screen text from get_network_options() — skip keyword matching, do direct tap
        keywords = []
        raw_label = network.strip()

    # ── Step 1: open Mobile Networks settings ────────────────────────
    # Try Samsung intent first, fall back to AOSP/Pixel intent
    for intent_args in [
        ["-n", "com.android.settings/.Settings$MobileNetworkActivity"],
        ["-a", "android.settings.NETWORK_OPERATOR_SETTINGS"],
    ]:
        _run(_serial_args(serial) + ["shell", "am", "start"] + intent_args)
        time.sleep(2)
        root = _dump_ui_tree(serial)
        if root is None:
            continue
        # Check if the right screen opened (has network mode or preferred network type)
        all_text = " ".join((n.get("text") or "").lower() for n in root.iter("node"))
        if "network mode" in all_text or "preferred network type" in all_text:
            break

    # ── Step 2: find and tap "Network mode" / "Preferred network type" row ──
    if root is None:
        return False, "Could not dump UI for Mobile Networks screen"

    network_mode_coords = None
    for node in root.iter("node"):
        text = (node.get("text") or "").strip()
        if text.lower() in ("network mode", "preferred network type", "network type"):
            # Find the parent clickable row
            nums = re.findall(r"\d+", node.get("bounds", ""))
            if len(nums) == 4:
                network_mode_coords = (
                    (int(nums[0]) + int(nums[2])) // 2,
                    (int(nums[1]) + int(nums[3])) // 2,
                )
            break

    if not network_mode_coords:
        # Fallback: clickable row whose subtexts contain network-mode keywords
        # but NOT billing/data-usage terms (those open a date picker, not a network dialog)
        EXCLUDE = {"billing", "cycle", "usage", "roaming", "access point", "apn"}
        for node in root.iter("node"):
            if node.get("clickable") != "true":
                continue
            subtexts = " ".join((n.get("text") or "") for n in node.iter("node")).lower()
            if not ("network mode" in subtexts or "preferred network" in subtexts
                    or "network type" in subtexts):
                continue
            if any(excl in subtexts for excl in EXCLUDE):
                continue
            nums = re.findall(r"\d+", node.get("bounds", ""))
            if len(nums) == 4:
                network_mode_coords = (
                    (int(nums[0]) + int(nums[2])) // 2,
                    (int(nums[1]) + int(nums[3])) // 2,
                )
            break

    if not network_mode_coords:
        return False, "Could not find 'Network mode' row in settings UI"

    x, y = network_mode_coords
    _run(_serial_args(serial) + ["shell", "input", "tap", str(x), str(y)])
    log.info("Tapped 'Network mode' at (%d, %d)", x, y)
    time.sleep(1.5)

    # ── Step 3: find and tap the target option ────────────────────────
    root2 = _dump_ui_tree(serial)
    if root2 is None:
        return False, "Could not dump UI for network mode dialog"

    # Guard: if the dialog doesn't look like a network selection, we tapped the wrong row
    dialog_texts_raw = [(n.get("text") or "").strip().lower() for n in root2.iter("node")]
    if not any(ind in t for t in dialog_texts_raw for ind in NETWORK_INDICATORS):
        _run(_serial_args(serial) + ["shell", "input", "keyevent", "KEYCODE_BACK"])
        visible = [t for t in dialog_texts_raw if len(t) > 1][:10]
        return False, (
            f"Wrong dialog opened after tapping (expected network selection, "
            f"got: {visible}). Check that Settings > Mobile Networks > Network mode exists."
        )

    def _tap_node(node) -> bool:
        nums = re.findall(r"\d+", node.get("bounds", ""))
        if len(nums) != 4:
            return False
        ox = (int(nums[0]) + int(nums[2])) // 2
        oy = (int(nums[1]) + int(nums[3])) // 2
        _run(_serial_args(serial) + ["shell", "input", "tap", str(ox), str(oy)])
        log.info("Tapped network option at (%d, %d)", ox, oy)
        time.sleep(1)
        _run(_serial_args(serial) + ["shell", "input", "keyevent", "KEYCODE_BACK"])
        return True

    if raw_label:
        # Raw on-screen text path: direct case-insensitive match against dialog items
        for node in root2.iter("node"):
            text = (node.get("text") or "").strip()
            if text.lower() == raw_label.lower():
                if _tap_node(node):
                    return True, f"Network type set to '{text}'"
        # Nothing left open — no BACK needed, return error
        available = [
            (n.get("text") or "").strip()
            for n in root2.iter("node")
            if (n.get("text") or "").strip() and len((n.get("text") or "").strip()) > 1
        ]
        _run(_serial_args(serial) + ["shell", "input", "keyevent", "KEYCODE_BACK"])
        return False, f"Option '{raw_label}' not found in dialog. Available: {available[:15]}"

    # Keyword matching path (internal labels: 2G, 3G, 4G, 5G, 4G5G, AUTO)
    for kw in keywords:
        for node in root2.iter("node"):
            text = (node.get("text") or "").strip().lower()
            if text.startswith(kw):
                if _tap_node(node):
                    return True, f"Network type set to {network} (option: '{text}')"

    # AUTO fallback: if no keyword matched, pick the first available network option
    if key == "AUTO":
        for node in root2.iter("node"):
            text = (node.get("text") or "").strip()
            if not text or len(text) < 2:
                continue
            if any(ind in text.lower() for ind in NETWORK_INDICATORS):
                if _tap_node(node):
                    return True, f"Network type set to AUTO (selected: '{text}')"

    # Do NOT press BACK here — _run_step() screenshots the open dialog first, then presses back
    available = [
        (n.get("text") or "").strip()
        for n in root2.iter("node")
        if (n.get("text") or "").strip() and len((n.get("text") or "").strip()) > 1
    ]
    return False, (
        f"Option for '{network}' not found in dialog. "
        f"Available texts: {available[:15]}"
    )


def get_network_options(serial: str) -> list:
    """Open the network-type picker, read available options, close dialog.

    Returns the raw on-screen option strings (e.g. ["LTE/3G/2G (Recommended)", "3G Only"]).
    Returns [] on failure or if the device is offline.
    """
    log = get_logger()
    NETWORK_INDICATORS = {"lte", "5g", "nr", "3g", "2g", "wcdma", "gsm", "umts", "4g", "auto"}
    EXCLUDE = {"billing", "cycle", "usage", "roaming", "access point", "apn"}

    if serial not in get_connected_devices():
        return []

    # Step 1: open Mobile Networks settings
    root = None
    for intent_args in [
        ["-n", "com.android.settings/.Settings$MobileNetworkActivity"],
        ["-a", "android.settings.NETWORK_OPERATOR_SETTINGS"],
    ]:
        _run(_serial_args(serial) + ["shell", "am", "start"] + intent_args)
        time.sleep(2)
        root = _dump_ui_tree(serial)
        if root is None:
            continue
        all_text = " ".join((n.get("text") or "").lower() for n in root.iter("node"))
        if "network mode" in all_text or "preferred network type" in all_text:
            break

    if root is None:
        _run(_serial_args(serial) + ["shell", "input", "keyevent", "KEYCODE_BACK"])
        return []

    # Step 2: find and tap the "Network mode" / "Preferred network type" row
    tapped = False
    for node in root.iter("node"):
        text = (node.get("text") or "").strip().lower()
        if text in ("network mode", "preferred network type", "network type"):
            nums = re.findall(r"\d+", node.get("bounds", ""))
            if len(nums) == 4:
                x = (int(nums[0]) + int(nums[2])) // 2
                y = (int(nums[1]) + int(nums[3])) // 2
                _run(_serial_args(serial) + ["shell", "input", "tap", str(x), str(y)])
                tapped = True
                break

    if not tapped:
        for node in root.iter("node"):
            if node.get("clickable") != "true":
                continue
            subtexts = " ".join((n.get("text") or "") for n in node.iter("node")).lower()
            if not ("network mode" in subtexts or "preferred network" in subtexts
                    or "network type" in subtexts):
                continue
            if any(excl in subtexts for excl in EXCLUDE):
                continue
            nums = re.findall(r"\d+", node.get("bounds", ""))
            if len(nums) == 4:
                x = (int(nums[0]) + int(nums[2])) // 2
                y = (int(nums[1]) + int(nums[3])) // 2
                _run(_serial_args(serial) + ["shell", "input", "tap", str(x), str(y)])
                tapped = True
                break

    if not tapped:
        _run(_serial_args(serial) + ["shell", "input", "keyevent", "KEYCODE_BACK"])
        return []

    time.sleep(1.5)

    # Step 3: read the dialog, then close it
    root2 = _dump_ui_tree(serial)
    _run(_serial_args(serial) + ["shell", "input", "keyevent", "KEYCODE_BACK"])
    _run(_serial_args(serial) + ["shell", "input", "keyevent", "KEYCODE_BACK"])

    if root2 is None:
        return []

    # Validate it's really a network-type dialog
    dialog_lower = [(n.get("text") or "").strip().lower() for n in root2.iter("node")]
    if not any(ind in t for t in dialog_lower for ind in NETWORK_INDICATORS):
        return []

    # Collect option texts that contain network-type keywords
    options: list[str] = []
    seen: set[str] = set()
    for node in root2.iter("node"):
        text = (node.get("text") or "").strip()
        if not text or len(text) < 2 or text in seen:
            continue
        if any(ind in text.lower() for ind in NETWORK_INDICATORS):
            options.append(text)
            seen.add(text)

    log.info("Network options for %s: %s", serial, options)
    return options


def set_config(serial: str, namespace: str, key: str, value: str) -> Result:
    """Set a device setting via `adb shell settings put`."""
    return _run(_serial_args(serial) + ["shell", "settings", "put", namespace, key, value])


def get_config(serial: str, namespace: str, key: str) -> Result:
    """Read a device setting via `adb shell settings get`."""
    return _run(_serial_args(serial) + ["shell", "settings", "get", namespace, key])


def press_back(serial: str) -> Result:
    """Press the Back key on the device."""
    return _run(_serial_args(serial) + ["shell", "input", "keyevent", "KEYCODE_BACK"])


def _get_network_status(serial: str) -> dict:
    """Return current network info: operator, data_state, service_state."""
    info = {}
    _, op = _run(_serial_args(serial) + ["shell", "getprop", "gsm.operator.alpha"])
    info["operator"] = (op or "").strip()
    _, dtype = _run(_serial_args(serial) + ["shell", "getprop", "gsm.network.type"])
    info["type"] = (dtype or "").strip()
    # service state: 0=IN_SERVICE 1=OUT_OF_SERVICE 2=EMERGENCY_ONLY 3=RADIO_OFF
    _, svc = _run(_serial_args(serial) + [
        "shell", "dumpsys", "telephony.registry",
    ])
    m = re.search(r"mServiceState=(\d+)", svc or "")
    info["service_state"] = int(m.group(1)) if m else -1
    return info


def set_airplane_mode(serial: str, state: str, wait_secs: int = 8) -> Result:
    """Toggle airplane mode and verify network state after the change."""
    turning_on = state.lower() in ("on", "1", "true")
    val  = "1" if turning_on else "0"
    flag = "true" if turning_on else "false"

    # Try Android 11+ cmd connectivity first (actually changes system state visibly)
    ok, out = _run(_serial_args(serial) + [
        "shell", "cmd", "connectivity", "airplane-mode",
        "enable" if turning_on else "disable",
    ])
    if not ok or "Error" in out or "Unknown" in out:
        # Fallback: settings + broadcast (Android 9/10)
        _run(_serial_args(serial) + ["shell", "settings", "put", "global", "airplane_mode_on", val])
        _run(_serial_args(serial) + [
            "shell", "am", "broadcast",
            "-a", "android.intent.action.AIRPLANE_MODE",
            "--ez", "state", flag,
        ])

    if turning_on:
        return True, "Airplane mode ON"

    # Airplane OFF — wait and verify network comes back
    time.sleep(wait_secs)
    net = _get_network_status(serial)
    svc      = net["service_state"]
    operator = net["operator"] or ""
    net_type = net["type"] or "—"

    if svc == 0 and operator:
        return True, f"Airplane mode OFF ✓ | Network: {operator} ({net_type})"
    elif operator:
        return True, f"Airplane mode OFF ✓ | Operator: {operator} ({net_type})"
    else:
        return False, f"Airplane mode OFF — no network after {wait_secs}s (state={svc})"


def _ls_download(serial: str) -> dict[str, int]:
    """Return {filename: size_bytes} for all files in the Downloads folder."""
    result: dict[str, int] = {}
    for path in ("/sdcard/Download/", "/storage/emulated/0/Download/"):
        _, ls_out = _run(_serial_args(serial) + ["shell", "ls", "-la", path])
        for line in (ls_out or "").splitlines():
            parts = line.split()
            # Android ls -la format: permissions links owner group size date time name
            # minimum 8 parts; name is last, size is parts[4]
            if len(parts) < 8:
                continue
            fname = parts[-1]
            if fname in (".", ".."):
                continue
            try:
                size = int(parts[4])
            except (IndexError, ValueError):
                continue
            result[fname] = size
        if result:
            break  # first path that returned files is the right one
    return result


def download_file(serial: str, url: str, expected_mb: float = 0, timeout: int = 120) -> Result:
    """
    Download a file via Android browser intent, then poll the Downloads folder.
    Samsung DownloadManager saves as .pending-XXXXXXXX-filename while downloading,
    then renames to the final filename when complete.
    Value format: "https://url/file.bin"  or  "https://url/file.bin:50"  (URL:expected MB)
    """
    import urllib.parse as _up

    def _is_temp(n: str) -> bool:
        return (n.endswith((".part", ".crdownload", ".tmp"))
                or bool(re.match(r"\.pending-\d+-", n)))

    def _pending_final(n: str) -> str | None:
        """'.pending-1785392499-50MB.zip' → '50MB.zip'"""
        m = re.match(r"\.pending-\d+-(.+)", n)
        return m.group(1) if m else None

    # 1. Snapshot existing files
    before: dict[str, int] = _ls_download(serial)

    # Predict final filename from URL
    url_fname: str | None = _up.urlparse(url).path.rstrip("/").split("/")[-1] or None

    # 2. Open URL — DownloadManager picks it up
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    ok, out = _run(_serial_args(serial) + [
        "shell", "am", "start",
        "-a", "android.intent.action.VIEW",
        "-d", url,
    ])
    if not ok:
        return False, f"Failed to open URL: {out}"

    # 3. Poll Downloads folder every 3s
    deadline      = time.time() + timeout
    pending_file  = None   # .pending-* filename being watched
    target_file   = None   # final (non-temp) filename
    last_size     = -1
    stable_count  = 0

    while time.time() < deadline:
        time.sleep(3)
        current = _ls_download(serial)

        # --- Track .pending-* (Samsung in-progress download) ---
        for fname in list(current):
            if re.match(r"\.pending-\d+-", fname) and fname not in before:
                if pending_file is None:
                    pending_file = fname
                    final_guess = _pending_final(fname)
                    if final_guess and url_fname is None:
                        url_fname = final_guess

        # --- Detect completion: pending file vanished → renamed to final ---
        if pending_file and pending_file not in current:
            # Look for the final file (by URL name or any new non-temp file)
            for fname, size in current.items():
                if _is_temp(fname):
                    continue
                if fname in before and size <= before.get(fname, 0):
                    continue  # existed before and hasn't grown
                size_mb = size / (1024 * 1024)
                if expected_mb > 0 and size_mb < expected_mb * 0.9:
                    return False, (
                        f"Download incomplete: '{fname}' is {size_mb:.1f} MB, "
                        f"expected ~{expected_mb} MB"
                    )
                return True, f"Downloaded '{fname}' ({size_mb:.1f} MB)"
            # pending gone but final not found yet — wait one more poll
            pending_file = None

        # --- Fallback: direct completion (no pending phase, file appeared directly) ---
        for fname, size in current.items():
            if _is_temp(fname):
                continue
            is_new     = fname not in before
            is_updated = url_fname and fname == url_fname and size > before.get(fname, 0)
            if not (is_new or is_updated):
                continue
            if target_file is None:
                target_file = fname
            if fname != target_file:
                continue
            if size > 0 and size == last_size:
                stable_count += 1
                if stable_count >= 2:
                    size_mb = size / (1024 * 1024)
                    if expected_mb > 0 and size_mb < expected_mb * 0.9:
                        return False, (
                            f"Download incomplete: '{fname}' is {size_mb:.1f} MB, "
                            f"expected ~{expected_mb} MB"
                        )
                    return True, f"Downloaded '{fname}' ({size_mb:.1f} MB)"
            else:
                last_size    = size
                stable_count = 0

    fname_hint = target_file or pending_file or "unknown"
    return False, f"Download timeout ({timeout}s) — '{fname_hint}' may be incomplete"


def download_file(serial: str, url: str, timeout_secs: int = 60,
                  timeout: int = 0, expected_mb: float = 0.0) -> tuple[bool, str]:
    """Download url on device via curl (discard data) to generate traffic.
    timeout overrides timeout_secs if provided. expected_mb is used only for reporting.
    Returns (ok, summary_string).
    """
    t = timeout or timeout_secs
    args = _serial_args(serial) + [
        "shell", "curl", "-o", "/dev/null", "-s", "-w",
        r"%{http_code} %{size_download}B in %{time_total}s",
        "--max-time", str(t), url,
    ]
    ok, out = _run(args, timeout=t + 5)
    summary = out.strip()
    if ok and summary:
        if expected_mb:
            summary += f" (expected ~{expected_mb}MB)"
        return True, summary
    # fallback: wget
    args2 = _serial_args(serial) + [
        "shell", "wget", "-O", "/dev/null", "-q", "--timeout", str(t), url,
    ]
    ok2, out2 = _run(args2, timeout=t + 5)
    return ok2, out2.strip() or f"wget exit ok={ok2}"


def open_browser(serial: str, url: str, wait_secs: int = 8,
                 screenshot_dir: str = "") -> Result:
    """Open a URL in the default browser, wait to verify it loaded, then screenshot."""
    import os as _os
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    ok, out = _run(_serial_args(serial) + [
        "shell", "am", "start",
        "-a", "android.intent.action.VIEW",
        "-d", url,
    ])
    if not ok:
        return False, out

    time.sleep(wait_secs)
    ok_b, browser_out = check_browser(serial, expected="", wait_secs=0)

    # Take screenshot
    shot_token = ""
    if screenshot_dir:
        _os.makedirs(screenshot_dir, exist_ok=True)
        import re as _re, time as _t
        fname = _re.sub(r"[^\w]", "_", url)[:40] + f"_{int(_t.time())}.png"
        save_path = _os.path.join(screenshot_dir, fname)
        ok_s, _ = screenshot(serial, save_path)
        if ok_s:
            shot_token = f"\n[screenshot:{save_path}]"

    return ok_b, browser_out + shot_token


def check_browser(serial: str, expected: str = "", wait_secs: int = 5) -> Result:
    """
    Check whether the browser loaded a page successfully.
    - Reads the URL bar and visible page text from a UI dump.
    - Fails if common network/DNS error strings are detected.
    - If *expected* is given, also verifies it appears in the URL or page text.
    """
    import time as _time

    _time.sleep(wait_secs)

    ERROR_STRINGS = (
        "err_name_not_resolved",
        "err_connection_refused",
        "err_connection_timed_out",
        "err_internet_disconnected",
        "err_network_changed",
        "err_ssl",
        "no internet",
        "no connection",
        "can't reach this page",
        "this site can't be reached",
        "page not available",
        "net::err",
        "connection failed",
        "unable to connect",
        "dns_probe",
        "website not available",
        "холболт алдаа",        # Mongolian
        "интернет байхгүй",
    )

    root = _dump_ui_tree(serial)
    if root is None:
        return False, "UI dump failed — browser may not be open"

    # Collect all visible text from the screen
    all_text = []
    url_bar_text = ""
    for node in root.iter("node"):
        txt  = (node.get("text")         or "").strip()
        desc = (node.get("content-desc") or "").strip()
        res  = (node.get("resource-id")  or "").lower()

        if txt:
            all_text.append(txt)
        if desc and desc != txt:
            all_text.append(desc)

        # Identify address / URL bar
        if any(k in res for k in ("url_bar", "address_bar", "omnibox", "location_bar",
                                   "url_field", "addressbar", "search_box")):
            url_bar_text = txt or desc

    full_screen = " ".join(all_text).lower()

    # Check for error indicators
    for err in ERROR_STRINGS:
        if err in full_screen:
            return False, f"Browser error detected: '{err}' — page did not load"

    # Build result summary
    result_parts = []
    if url_bar_text:
        result_parts.append(f"URL: {url_bar_text}")

    # Check expected keyword if provided
    if expected:
        exp_lower = expected.lower()
        if exp_lower in full_screen or exp_lower in url_bar_text.lower():
            result_parts.append(f"'{expected}' found ✓")
        else:
            return False, f"Expected '{expected}' not found on page. URL bar: {url_bar_text or '(not detected)'}"

    if not result_parts:
        # No URL bar detected — check if there's any substantial page content
        content_nodes = [t for t in all_text if len(t) > 10]
        if content_nodes:
            result_parts.append(f"Page loaded ({len(content_nodes)} content elements)")
        else:
            return False, "Could not confirm page load — no URL bar or content detected"

    return True, " | ".join(result_parts)


def run_speedtest(serial: str, wait_secs: int = 60) -> Result:
    """Launch G-World Speedtest (org.zwanoo.android.speedtest.gworld), tap GO, wait for result."""
    import time as _time

    pkg = "org.zwanoo.android.speedtest.gworld"

    _, launch_out = _run(_serial_args(serial) + [
        "shell", "monkey", "-p", pkg,
        "-c", "android.intent.category.LAUNCHER", "1",
    ])
    if "No activities found" in launch_out:
        return False, f"Speedtest app not installed ({pkg})"

    # wait for app to fully load
    _time.sleep(5)

    GO_KEYWORDS = ("go", "start", "begin", "test", "시작", "эхлэх")

    def _find_and_tap_go() -> bool:
        root = _dump_ui_tree(serial)
        if root is None:
            return False
        for node in root.iter("node"):
            txt  = (node.get("text")         or "").lower().strip()
            desc = (node.get("content-desc") or "").lower().strip()
            cls  = (node.get("class")        or "")
            # exact "GO" match first
            if txt == "go" or desc == "go":
                xy = _node_center(node)
                if xy:
                    _run(_serial_args(serial) + ["shell", "input", "tap", str(xy[0]), str(xy[1])])
                    return True
            # broader keyword match on clickable nodes
            if node.get("clickable") == "true":
                if any(k in txt or k in desc for k in GO_KEYWORDS):
                    xy = _node_center(node)
                    if xy:
                        _run(_serial_args(serial) + ["shell", "input", "tap", str(xy[0]), str(xy[1])])
                        return True
        return False

    tapped = _find_and_tap_go()
    if not tapped:
        # fallback: tap screen center (GO button is usually centered)
        w, h = _get_screen_size(serial)
        _run(_serial_args(serial) + ["shell", "input", "tap", str(w // 2), str(int(h * 0.55))])

    # Poll until both download AND upload results appear, or timeout
    deadline = _time.time() + wait_secs
    poll_interval = 3

    def _scrape_results() -> dict:
        """Return dict with ping/download/upload found in UI, empty if test still running."""
        root = _dump_ui_tree(serial)
        if root is None:
            return {}

        found = {}
        nodes = list(root.iter("node"))

        # G-World / Ookla layout: numeric value node is followed by a unit/label node
        # Strategy 1: find nodes whose text looks like a speed/ping number,
        #              then peek at sibling/nearby text for the label.
        for i, node in enumerate(nodes):
            txt = (node.get("text") or "").strip()
            desc = (node.get("content-desc") or "").strip().lower()

            # Numeric value like "45.2" or "120"
            try:
                val = float(txt.replace(",", "."))
            except ValueError:
                val = None

            if val is not None:
                # look at nearby nodes for context label
                context = " ".join(
                    (nodes[j].get("text") or "") + " " + (nodes[j].get("content-desc") or "")
                    for j in range(max(0, i - 3), min(len(nodes), i + 4))
                ).lower()
                if "ping" in context or "ms" in context:
                    found.setdefault("ping", f"{txt} ms")
                elif "download" in context or "dl" in context:
                    found.setdefault("download", f"{txt} kBps")
                elif "upload" in context or "ul" in context:
                    found.setdefault("upload", f"{txt} kBps")

            # Strategy 2: content-desc already contains the label+value
            if "download" in desc and "mbps" in desc:
                found.setdefault("download", txt or desc)
            elif "upload" in desc and "mbps" in desc:
                found.setdefault("upload", txt or desc)
            elif "ping" in desc and ("ms" in desc or val is not None):
                found.setdefault("ping", txt or desc)

            # Strategy 3: plain text contains unit
            tl = txt.lower()
            if "mbps" in tl:
                # guess direction from nearby text
                context = " ".join(
                    (nodes[j].get("text") or "")
                    for j in range(max(0, i - 5), min(len(nodes), i + 5))
                ).lower()
                if "upload" in context or "ul" in context:
                    found.setdefault("upload", txt)
                else:
                    found.setdefault("download", txt)
            elif "ms" in tl and val is None:
                # e.g. "23 ms"
                found.setdefault("ping", txt)

        return found

    result_data = {}
    while _time.time() < deadline:
        _time.sleep(poll_interval)
        data = _scrape_results()
        # consider test done when we have at least download + upload
        if data.get("download") and data.get("upload"):
            result_data = data
            break
        # keep latest partial result
        if data:
            result_data = data

    if result_data:
        parts = []
        if result_data.get("ping"):
            parts.append(f"Ping: {result_data['ping']}")
        if result_data.get("download"):
            parts.append(f"Download: {result_data['download']}")
        if result_data.get("upload"):
            parts.append(f"Upload: {result_data['upload']}")
        return True, " | ".join(parts)

    return False, f"Speedtest тест дууссан боловч үр дүн олдсонгүй ({wait_secs}s хүлээсэн)"


def _select_apn_via_settings(serial: str, apn_name: str) -> str:
    """Open APN settings UI and tap the row matching apn_name. Returns status string."""
    import time as _time

    args = _serial_args(serial)

    # Open APN settings — try multiple known intents
    opened = False
    for intent in [
        ["shell", "am", "start", "-a", "android.intent.action.MAIN",
         "-n", "com.android.settings/.Settings$ApnSettingsActivity"],
        ["shell", "am", "start", "-a", "android.settings.APN_SETTINGS"],
        ["shell", "am", "start", "-a", "android.intent.action.MAIN",
         "-n", "com.android.phone/.settings.ApnSettings"],
    ]:
        ok, out = _run(args + intent, timeout=5)
        if ok and "Error" not in out and "does not exist" not in out:
            opened = True
            break

    if not opened:
        return "⚠ could not open APN settings"

    _time.sleep(2)

    # Dump UI and find the APN row by name
    _run(args + ["shell", "uiautomator", "dump", "/sdcard/_apn.xml"], timeout=10)
    _, dump = _run(args + ["shell", "cat", "/sdcard/_apn.xml"], timeout=5)

    # Find node containing apn_name text
    found = None
    for node in re.finditer(r"<node\b[^>]*>", dump or ""):
        n = node.group(0)
        if apn_name.lower() in n.lower() and 'bounds=' in n:
            m = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', n)
            if m:
                found = (int(m.group(1)) + int(m.group(3))) // 2, \
                        (int(m.group(2)) + int(m.group(4))) // 2
                break

    if not found:
        _run(args + ["shell", "input", "keyevent", "KEYCODE_BACK"], timeout=3)
        return f"⚠ APN '{apn_name}' not found in settings UI"

    _run(args + ["shell", "input", "tap", str(found[0]), str(found[1])], timeout=5)
    _time.sleep(1)
    _run(args + ["shell", "input", "keyevent", "KEYCODE_BACK"], timeout=3)
    _time.sleep(0.5)
    _run(args + ["shell", "input", "keyevent", "KEYCODE_BACK"], timeout=3)
    return f"✓ selected via Settings UI"


def get_preferred_apn_id(serial: str) -> Result:
    """Return the _id of the current preferred APN."""
    ok, out = _run(_serial_args(serial) + [
        "shell", "content", "query",
        "--uri", "content://telephony/carriers/preferapn",
        "--projection", "_id",
    ])
    m = re.search(r"_id=(\d+)", out)
    if m:
        return True, m.group(1)
    return False, "No preferred APN found"


def set_preferred_apn_by_id(serial: str, apn_id: str) -> Result:
    """Set preferred APN by its _id."""
    ok, out = _run(_serial_args(serial) + [
        "shell", "content", "update",
        "--uri", "content://telephony/carriers/preferapn",
        "--bind", f"apn_id:i:{apn_id}",
    ])
    return ok, out


def _find_apn_id_by_name(serial: str, apn_name: str, mcc_mnc: str) -> str | None:
    """Return _id of first APN matching apn_name for this carrier, or None."""
    _, out = _run(_serial_args(serial) + [
        "shell", "content", "query",
        "--uri", "content://telephony/carriers",
        "--where", f"numeric='{mcc_mnc}'",
        "--projection", "_id:name",
    ])
    for line in out.splitlines():
        if f"name={apn_name}" in line or f"name={apn_name}," in line:
            m = re.search(r"_id=(\d+)", line)
            if m:
                return m.group(1)
    return None


def set_apn(serial: str, apn_name: str, apn_value: str,
            mcc_mnc: str = "", apn_type: str = "default,supl") -> Result:
    """Set APN as preferred. Reuses existing entry if found, else inserts a new one."""
    import time as _time

    if not mcc_mnc:
        _, mccmnc_raw = _run(_serial_args(serial) + ["shell", "getprop", "gsm.operator.numeric"])
        mcc_mnc = mccmnc_raw.strip().split(",")[0][:6]

    if not mcc_mnc:
        return False, "Could not detect MCC+MNC"

    # Check if APN already exists by name (query all for this carrier, filter in Python)
    apn_id = _find_apn_id_by_name(serial, apn_name, mcc_mnc)
    reused = apn_id is not None

    if not apn_id:
        # APN doesn't exist — insert it
        ok, out = _run(_serial_args(serial) + [
            "shell", "content", "insert",
            "--uri", "content://telephony/carriers",
            "--bind", f"name:s:{apn_name}",
            "--bind", f"apn:s:{apn_value}",
            "--bind", f"numeric:s:{mcc_mnc}",
            "--bind", f"mcc:s:{mcc_mnc[:3]}",
            "--bind", f"mnc:s:{mcc_mnc[3:]}",
            "--bind", f"type:s:{apn_type}",
            "--bind", "protocol:s:IPV4V6",
            "--bind", "roaming_protocol:s:IPV4V6",
            "--bind", "carrier_enabled:i:1",
        ])
        if not ok:
            return False, out
        _time.sleep(1)
        apn_id = _find_apn_id_by_name(serial, apn_name, mcc_mnc)
        if not apn_id:
            return False, f"Inserted APN '{apn_name}' but could not find its _id"

    # Set as preferred
    _run(_serial_args(serial) + [
        "shell", "content", "update",
        "--uri", "content://telephony/carriers/preferapn",
        "--bind", f"apn_id:i:{apn_id}",
    ])

    # Verify
    _, vout = _run(_serial_args(serial) + [
        "shell", "content", "query",
        "--uri", "content://telephony/carriers/preferapn",
        "--projection", "_id:name",
    ])
    confirmed = f"_id={apn_id}" in vout or f"name={apn_name}" in vout
    status = "✓" if confirmed else f"⚠ preferapn={vout.strip()[:60]}"
    action = "reused" if reused else "inserted"

    return True, f"APN '{apn_name}' set as preferred (id={apn_id}, {action}) {status}"


def delete_apn_by_name(serial: str, apn_name: str) -> Result:
    """Delete APN entry matching the display name (looks up _id first)."""
    _, mccmnc_raw = _run(_serial_args(serial) + ["shell", "getprop", "gsm.operator.numeric"])
    mcc_mnc = mccmnc_raw.strip().split(",")[0][:6]
    apn_id = _find_apn_id_by_name(serial, apn_name, mcc_mnc) if mcc_mnc else None
    if not apn_id:
        return False, f"APN '{apn_name}' not found"
    ok, out = _run(_serial_args(serial) + [
        "shell", "content", "delete",
        "--uri", "content://telephony/carriers",
        "--where", f"_id={apn_id}",
    ])
    if ok:
        return True, f"Deleted APN '{apn_name}' (id={apn_id})"
    return False, out


def check_connectivity(serial: str, ip: str, port: str = "") -> Result:
    """Ping IP 3 times and show individual results. If port given, also do TCP check."""
    # Ping 3 times
    ok_ping, ping_out = _run(
        _serial_args(serial) + ["shell", "ping", "-c", "3", "-W", "2", ip],
        timeout=20,
    )
    ping_lines = [ln for ln in ping_out.splitlines()
                  if "bytes from" in ln or "Request timeout" in ln or "unreachable" in ln.lower()]
    loss_m = re.search(r"(\d+)% packet loss", ping_out)
    loss = int(loss_m.group(1)) if loss_m else 100
    rtt_m = re.search(r"rtt.*?=\s*([\d.]+)/([\d.]+)/([\d.]+)", ping_out)
    rtt_summary = f"avg={rtt_m.group(2)}ms" if rtt_m else ""

    ping_result = "\n".join(ping_lines) if ping_lines else ping_out.strip()[:120]
    summary = f"Ping {ip} — {loss}% loss" + (f" {rtt_summary}" if rtt_summary else "")

    if port:
        ok_tcp, tcp_out = _run(
            _serial_args(serial) + [
                "shell", f"nc -w 3 {ip} {port} < /dev/null; echo __exit:$?",
            ],
            timeout=10,
        )
        m = re.search(r"__exit:(\d+)", tcp_out)
        exit_code = int(m.group(1)) if m else (0 if ok_tcp else 1)
        err = tcp_out.replace(f"__exit:{exit_code}", "").strip()
        if exit_code == 0:
            tcp_note = f"TCP :{port} open"
        elif "refused" in err.lower():
            tcp_note = f"TCP :{port} refused (host up)"
        else:
            tcp_note = f"TCP :{port} unreachable"
        full_msg = f"{summary} | {tcp_note}\n{ping_result}"
        overall_ok = loss < 100 or exit_code == 0 or "refused" in err.lower()
        return overall_ok, full_msg

    full_msg = f"{summary}\n{ping_result}"
    return loss < 100, full_msg


def port_scan(serial: str, ip: str, ports: str, timeout_secs: int = 2) -> tuple[bool, str]:
    """Scan ports on IP from the device using nc. ports = '80,443,8000-8100' etc."""
    # Parse port list
    port_list: list[int] = []
    for part in ports.split(","):
        part = part.strip()
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                port_list.extend(range(int(a), int(b) + 1))
            except ValueError:
                pass
        elif part.isdigit():
            port_list.append(int(part))

    if not port_list:
        return False, "No valid ports specified"
    if len(port_list) > 200:
        return False, f"Too many ports ({len(port_list)}), limit 200"

    open_ports: list[int] = []
    refused_ports: list[int] = []
    filtered_ports: list[int] = []

    for port in port_list:
        ok, out = _run(
            _serial_args(serial) + [
                "shell", f"nc -w {timeout_secs} {ip} {port} < /dev/null; echo __exit:$?",
            ],
            timeout=timeout_secs + 3,
        )
        m = re.search(r"__exit:(\d+)", out)
        exit_code = int(m.group(1)) if m else (0 if ok else 1)
        err = out.replace(f"__exit:{exit_code}", "").strip().lower()
        if exit_code == 0:
            open_ports.append(port)
        elif "refused" in err:
            refused_ports.append(port)
        else:
            filtered_ports.append(port)

    lines = [f"Port scan {ip} — {len(port_list)} ports checked"]
    if open_ports:
        lines.append(f"  OPEN    : {', '.join(str(p) for p in open_ports)}")
    if refused_ports:
        lines.append(f"  REFUSED : {', '.join(str(p) for p in refused_ports)}")
    if filtered_ports:
        lines.append(f"  FILTERED: {', '.join(str(p) for p in filtered_ports)}")

    any_reachable = bool(open_ports or refused_ports)
    return any_reachable, "\n".join(lines)


def get_mobile_ip(serial: str) -> Result:
    """Return the IP address on the active mobile data interface (rmnet/ccmni/wwan)."""
    ok, out = _run(_serial_args(serial) + ["shell", "ip", "-4", "addr", "show"], timeout=5)
    if not ok:
        return False, out
    current = ""
    for line in out.splitlines():
        m = re.match(r"\d+:\s+(\S+?)[@:]", line)
        if m:
            current = m.group(1)
        if re.search(r"rmnet|ccmni|pdp|wwan", current, re.I):
            m = re.search(r"inet\s+([\d.]+)", line)
            if m:
                return True, f"{m.group(1)} ({current})"
    return False, "No mobile data IP found"


def pingtools_check(serial: str, ip: str, port: str = "") -> Result:
    """Launch PingTools, open drawer, tap Ping, enter IP, tap Start, wait 10s, read results."""
    import time as _time

    args = _serial_args(serial)
    PKG = "ua.com.streamsoft.pingtools"
    PING_RES_ID = f"{PKG}:id/pingFragment"

    def _dump():
        _run(args + ["shell", "uiautomator", "dump", "/sdcard/_pt.xml"], timeout=10)
        _, out = _run(args + ["shell", "cat", "/sdcard/_pt.xml"], timeout=5)
        return out or ""

    def _find(dump, text=None, res_id=None, cls=None, desc=None):
        for node in re.finditer(r"<node\b[^>]*>", dump):
            n = node.group(0)
            if text is not None and f'text="{text}"' not in n:
                continue
            if res_id is not None and f'resource-id="{res_id}"' not in n:
                continue
            if cls is not None and f'class="{cls}"' not in n:
                continue
            if desc is not None and f'content-desc="{desc}"' not in n:
                continue
            m = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', n)
            if m:
                return (int(m.group(1)) + int(m.group(3))) // 2, \
                       (int(m.group(2)) + int(m.group(4))) // 2
        return None

    def _tap(xy):
        _run(args + ["shell", "input", "tap", str(xy[0]), str(xy[1])], timeout=5)
        _time.sleep(0.7)

    def _clear_and_type(xy, text):
        _tap(xy)
        _time.sleep(0.5)
        # Move to end then fire 40 backspaces in one shell call to clear any existing text
        del_seq = "input keyevent KEYCODE_MOVE_END; " + "input keyevent KEYCODE_DEL; " * 40
        _run(args + ["shell", del_seq], timeout=15)
        _time.sleep(0.4)
        _run(args + ["shell", "input", "text", text], timeout=5)
        _time.sleep(0.3)

    # ── verify installed ─────────────────────────────────────────────────────
    ok, out = _run(args + ["shell", "pm", "list", "packages"], timeout=10)
    if PKG not in out:
        return False, f"PingTools ({PKG}) not found on device"

    # ── clear logcat so only fresh ping output is captured later ─────────────
    _run(args + ["logcat", "-c"], timeout=5)

    # ── launch via monkey (finds correct launcher activity automatically) ────
    ok, out = _run(args + ["shell", "monkey", "-p", PKG, "-c",
                            "android.intent.category.LAUNCHER", "1"], timeout=8)
    if not ok or "Events injected: 1" not in out:
        return False, f"Could not launch PingTools: {out[:200]}"
    _time.sleep(2.5)

    # Verify PingTools is in foreground
    _, fg = _run(args + ["shell", "dumpsys", "window", "windows"], timeout=5)
    if PKG not in fg:
        return False, f"PingTools did not come to foreground (check if installed and not disabled)"

    # ── open navigation drawer if not already open ───────────────────────────
    dump = _dump()
    drawer_btn = _find(dump, desc="Open navigation drawer")
    if drawer_btn:
        _tap(drawer_btn)
        _time.sleep(0.8)
        dump = _dump()

    # ── tap Ping menu item (resource-id confirmed from UI dump) ───────────────
    ping_item = _find(dump, res_id=PING_RES_ID)
    if not ping_item:
        ping_item = _find(dump, text="Ping", cls="android.widget.CheckedTextView")
    if not ping_item:
        return False, "Could not find Ping menu item in drawer"
    _tap(ping_item)
    _time.sleep(1.5)

    # ── enter IP in host EditText ────────────────────────────────────────────
    dump = _dump()
    host_field = _find(dump, cls="android.widget.EditText")
    if not host_field:
        return False, "Could not find host input on Ping screen"
    _clear_and_type(host_field, ip)

    # ── tap Start button ─────────────────────────────────────────────────────
    dump = _dump()
    start_btn = (
        _find(dump, text="Start") or
        _find(dump, desc="Start") or
        _find(dump, text="PING") or
        _find(dump, text="Go")
    )
    if not start_btn:
        return False, "Could not find Start button on Ping screen"
    _tap(start_btn)

    # ── wait 10 seconds for ping to complete ─────────────────────────────────
    _time.sleep(10)

    # ── read results from logcat (canvas UI is not accessible via dump) ───────
    _, log = _run(args + ["logcat", "-d"], timeout=10)
    ping_lines = [
        ln.strip() for ln in log.splitlines()
        if re.search(r"bytes from|icmp_seq|ttl=|time=\d|packet loss|\d+ ms", ln, re.I)
    ]
    if ping_lines:
        # Show last 5 unique lines (most recent results)
        return True, " | ".join(dict.fromkeys(ping_lines[-5:]))

    # Fallback: UI dump text nodes
    dump = _dump()
    all_texts = [t for t in re.findall(r'text="([^"]+)"', dump) if t.strip()]
    result_lines = [
        t for t in all_texts
        if re.search(r"\d+\s*ms|\d+%|icmp|ttl|time=|reachable|bytes from", t, re.I)
    ]
    if result_lines:
        return True, " | ".join(dict.fromkeys(result_lines[:6]))

    return True, f"PingTools ran for {ip} — no result captured (canvas view)"


def screenshot(serial: str, save_path: str) -> Result:
    """Capture the screen and pull it to *save_path* on the host."""
    remote = "/sdcard/_automation_screenshot.png"
    ok, out = _run(_serial_args(serial) + ["shell", "screencap", "-p", remote])
    if not ok:
        return False, out
    return _run(_serial_args(serial) + ["pull", remote, save_path])


def get_device_status(serial: str) -> dict:
    """Lightweight status poll for the monitoring dashboard. No UI dumps."""
    NET_MAP = {
        "LTE": "LTE (4G)", "NR_NSA": "5G NSA", "NR_SA": "5G SA", "NR": "5G",
        "UMTS": "3G", "HSDPA": "3.5G", "HSUPA": "3.5G", "HSPA": "3.5G",
        "HSPAP": "HSPA+", "TD_SCDMA": "TD-SCDMA", "EDGE": "EDGE (2G)",
        "GPRS": "GPRS (2G)", "GSM": "GSM (2G)",
    }
    status: dict = {"serial": serial, "online": False}

    connected = get_connected_devices()
    if serial not in connected:
        return status
    status["online"] = True

    # Model
    ok, out = _run(_serial_args(serial) + ["shell", "getprop", "ro.product.model"], timeout=5)
    if ok:
        status["model"] = out.strip()

    # Network type
    ok, out = _run(_serial_args(serial) + ["shell", "getprop", "gsm.network.type"], timeout=5)
    if ok and out.strip():
        status["network"] = NET_MAP.get(out.strip().upper(), out.strip())
    else:
        status["network"] = "Unknown"

    # Operator
    ok, out = _run(_serial_args(serial) + ["shell", "getprop", "gsm.operator.alpha"], timeout=5)
    if ok:
        status["operator"] = out.strip()

    # Battery (level + charging status)
    ok, out = _run(_serial_args(serial) + ["shell", "dumpsys", "battery"], timeout=6)
    if ok:
        m = re.search(r"level:\s*(\d+)", out)
        if m:
            status["battery"] = int(m.group(1))
        m_st = re.search(r"status:\s*(\d+)", out)
        status["charging"] = bool(m_st and m_st.group(1) == "2")

    # Signal strength (LTE RSRP preferred, fallback to SS RSSI)
    ok, out = _run(_serial_args(serial) + ["shell", "dumpsys", "telephony.registry"], timeout=6)
    if ok:
        m = re.search(r"mLteRsrp\s*=\s*(-?\d+)", out)
        if not m:
            m = re.search(r"mSignalStrength.*?(-\d{2,3})", out)
        if m:
            status["signal_dbm"] = int(m.group(1))

    # VoLTE and WiFi Calling from settings DB (fast read)
    ok, out = _run(_serial_args(serial) + ["shell", "settings", "get", "global", "volte_vt_enabled"], timeout=5)
    if ok and out.strip() in ("0", "1"):
        status["volte"] = out.strip() == "1"

    ok, out = _run(_serial_args(serial) + ["shell", "settings", "get", "global", "wfc_ims_enabled"], timeout=5)
    if ok and out.strip() in ("0", "1"):
        status["wifi_calling"] = out.strip() == "1"

    return status
