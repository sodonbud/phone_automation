"""Orchestrates test execution: routes each row to the correct ADB method."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import adb_controller as adb
import config
from excel_client import ExcelClient
from logger import get_logger

SUPPORTED_ACTIONS = {
    "CALL", "END_CALL",
    "ANSWER_CALL",
    "SMS", "CHECK_SMS",
    "CHECK_CALL",
    "CHECK_VOLTE",
    "USSD",
    "SET_NETWORK",
    "SET_CONFIG", "GET_CONFIG",
    "START_RECORD", "STOP_RECORD",
    "WAIT", "WAKE",
}


class TestRunner:
    def __init__(self, excel_client: ExcelClient, dry_run: bool = False) -> None:
        self.ec = excel_client
        self.dry_run = dry_run
        self.log = get_logger()

        self.total = 0
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_serial(self, target_phone: str) -> str | None:
        serial = config.DEVICES.get(target_phone)
        if not serial:
            self.log.warning("Unknown Target Phone '%s' — not in config.DEVICES", target_phone)
        return serial

    # ------------------------------------------------------------------
    # Action routing
    # ------------------------------------------------------------------

    def _run_action(self, row: dict[str, Any]) -> tuple[bool, str]:
        action = row.get("Action", "").upper()
        target = row.get("Target Phone", "")
        number_code = row.get("Number/Code", "")
        value = row.get("Value", "")

        serial = self._resolve_serial(target)
        if not serial:
            return False, f"No serial configured for '{target}'"

        # ── Basic telephony ──────────────────────────────────────────
        if action == "CALL":
            return adb.make_call(serial, number_code)

        if action == "END_CALL":
            return adb.end_call(serial)

        # ── Incoming call: wait then answer ──────────────────────────
        if action == "ANSWER_CALL":
            timeout = int(value) if value.isdigit() else 30
            ok, out = adb.wait_for_incoming_call(serial, timeout=timeout)
            if not ok:
                return False, out
            return adb.answer_call(serial)

        # ── SMS ───────────────────────────────────────────────────────
        if action == "SMS":
            return adb.send_sms(serial, number_code, value)

        if action == "CHECK_SMS":
            # Number/Code = sender number, Value = expected text (optional)
            return adb.check_sms_received(serial, number_code, expected_text=value)

        if action == "CHECK_CALL":
            # Number/Code = other party's number, Value = INCOMING/OUTGOING (optional)
            return adb.check_call_log(serial, number_code, call_type=value)

        if action == "CHECK_VOLTE":
            return adb.check_volte(serial)

        if action == "SET_NETWORK":
            # Number/Code = network type: 2G / 3G / 4G / 5G / 4G5G / AUTO
            return adb.set_network_type(serial, number_code)

        # ── USSD ─────────────────────────────────────────────────────
        if action == "USSD":
            return adb.dial_ussd(serial, number_code)

        # ── Device settings ──────────────────────────────────────────
        if action == "SET_CONFIG":
            namespace, _, key = number_code.partition("/")
            if not key:
                return False, f"SET_CONFIG needs 'namespace/key' in Number/Code, got '{number_code}'"
            return adb.set_config(serial, namespace, key, value)

        if action == "GET_CONFIG":
            namespace, _, key = number_code.partition("/")
            if not key:
                return False, f"GET_CONFIG needs 'namespace/key' in Number/Code, got '{number_code}'"
            return adb.get_config(serial, namespace, key)

        # ── Voice recording (in-call Record button) ──────────────────
        if action == "START_RECORD":
            return adb.start_call_recording(serial)

        if action == "STOP_RECORD":
            local_path = value or f"recordings/{target}_{datetime.now().strftime('%H%M%S')}.mp3"
            return adb.stop_call_recording(serial, local_path=local_path)

        # ── Wake / unlock screen ─────────────────────────────────────
        if action == "WAKE":
            return adb.wake_and_unlock(serial)

        # ── Pause ────────────────────────────────────────────────────
        if action == "WAIT":
            secs = float(number_code) if number_code else 3.0
            self.log.info("Waiting %.1fs ...", secs)
            time.sleep(secs)
            return True, f"Waited {secs}s"

        return False, f"Unknown action '{action}'"

    # ------------------------------------------------------------------
    # Result evaluation
    # ------------------------------------------------------------------

    def _evaluate(self, row: dict[str, Any], success: bool, output: str) -> str:
        expected = row.get("Expected Result", "").strip()
        if not success:
            return "FAIL"
        if expected:
            return "PASS" if expected.lower() in output.lower() else "FAIL"
        return "PASS"

    # ------------------------------------------------------------------
    # Single step
    # ------------------------------------------------------------------

    def run_step(self, row: dict[str, Any]) -> str:
        row_idx = row["_row"]
        action = row.get("Action", "").upper()
        step = row.get("Step", row_idx)
        self.total += 1

        if not action or action not in SUPPORTED_ACTIONS:
            # Silently skip blank/section-header rows — don't write to merged cells
            if not action:
                self.total -= 1
                return "SKIP"
            reason = f"Unsupported or missing action '{action}'"
            self.log.warning("Step %s — SKIP: %s", step, reason)
            self.ec.write_result(row_idx, "", "SKIP", reason)
            self.skipped += 1
            return "SKIP"

        if self.dry_run:
            msg = (
                f"[DRY-RUN] Step {step}: action={action} "
                f"phone={row.get('Target Phone')} "
                f"number/code={row.get('Number/Code')} "
                f"value={row.get('Value')}"
            )
            self.log.info(msg)
            self.ec.write_result(row_idx, "(dry-run)", "SKIP", "Dry-run mode")
            self.skipped += 1
            return "SKIP"

        self.log.info("Step %s — executing %s on %s", step, action, row.get("Target Phone"))
        success, output = self._run_action(row)
        result = self._evaluate(row, success, output)
        self.log.info("Step %s — %s | output: %s", step, result, output[:200])
        self.ec.write_result(row_idx, output[:500], result, output[:500])

        if result == "PASS":
            self.passed += 1
        else:
            self.failed += 1
        return result

    # ------------------------------------------------------------------
    # Full run
    # ------------------------------------------------------------------

    def run_all(self, step_filter: int | None = None) -> None:
        test_cases = self.ec.get_test_cases()
        if not test_cases:
            self.log.warning("No test cases found in the sheet.")
            return
        for row in test_cases:
            if step_filter is not None and row["_row"] != step_filter:
                continue
            self.run_step(row)

        # Return all devices to home screen after run completes
        if step_filter is None:
            for name, serial in config.DEVICES.items():
                try:
                    adb.go_home(serial)
                    self.log.info("Sent %s (%s) to home screen", name, serial)
                except Exception as exc:
                    self.log.warning("go_home failed for %s: %s", name, exc)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def print_summary(self) -> None:
        sep = "─" * 40
        print(sep)
        print(f"  Total steps : {self.total}")
        print(f"  Passed      : {self.passed}")
        print(f"  Failed      : {self.failed}")
        print(f"  Skipped     : {self.skipped}")
        print(sep)
