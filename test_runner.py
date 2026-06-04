"""Orchestrates test execution: routes each row to the correct ADB method."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import adb_controller as adb
import config
from excel_client import ExcelClient
from logger import get_logger

# Map Action column values → handler functions
# Each handler receives (serial, row_dict) and returns (success, output)
SUPPORTED_ACTIONS = {"CALL", "SMS", "USSD", "SET_CONFIG", "GET_CONFIG"}


class TestRunner:
    def __init__(
        self,
        excel_client: ExcelClient,
        dry_run: bool = False,
        sheet_name: str = config.SHEET_NAME,
    ) -> None:
        self.ec = excel_client
        self.dry_run = dry_run
        self.log = get_logger()

        self.total = 0
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def _resolve_serial(self, target_phone: str) -> str | None:
        serial = config.DEVICES.get(target_phone)
        if not serial:
            self.log.warning("Unknown Target Phone '%s' in config", target_phone)
        return serial

    def _run_action(self, row: dict[str, Any]) -> tuple[bool, str]:
        action = row.get("Action", "").upper()
        target = row.get("Target Phone", "")
        number_code = row.get("Number/Code", "")
        value = row.get("Value", "")

        serial = self._resolve_serial(target)
        if not serial:
            return False, f"No serial configured for '{target}'"

        if action == "CALL":
            return adb.make_call(serial, number_code)

        if action == "SMS":
            return adb.send_sms(serial, number_code, value)

        if action == "USSD":
            return adb.dial_ussd(serial, number_code)

        if action == "SET_CONFIG":
            # Number/Code encodes "namespace/key"
            namespace, _, key = number_code.partition("/")
            if not key:
                return False, f"SET_CONFIG requires 'namespace/key' in Number/Code, got '{number_code}'"
            return adb.set_config(serial, namespace, key, value)

        if action == "GET_CONFIG":
            namespace, _, key = number_code.partition("/")
            if not key:
                return False, f"GET_CONFIG requires 'namespace/key' in Number/Code, got '{number_code}'"
            return adb.get_config(serial, namespace, key)

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
        # No expected result specified — treat any successful execution as PASS
        return "PASS"

    # ------------------------------------------------------------------
    # Single step
    # ------------------------------------------------------------------

    def run_step(self, row: dict[str, Any]) -> str:
        """Execute one test row. Returns 'PASS', 'FAIL', or 'SKIP'."""
        row_idx = row["_row"]
        action = row.get("Action", "").upper()
        step = row.get("Step", row_idx)
        self.total += 1

        if not action or action not in SUPPORTED_ACTIONS:
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

        self.log.info(
            "Step %s — executing %s on %s", step, action, row.get("Target Phone")
        )
        success, output = self._run_action(row)
        result = self._evaluate(row, success, output)
        notes = output[:500]  # cap to avoid huge cells

        self.log.info("Step %s — %s | output: %s", step, result, output[:200])
        self.ec.write_result(row_idx, output[:500], result, notes)

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
            if not self.dry_run:
                time.sleep(0.5)  # brief pause between steps

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
