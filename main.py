#!/usr/bin/env python3
"""Entry point for the phone test automation system."""

from __future__ import annotations

import argparse
import sys

import adb_controller as adb
import config
from excel_client import ExcelClient
from logger import get_logger
from test_runner import TestRunner


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Phone test automation via ADB + Excel",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                            # run all steps on all phones
  python main.py --dry-run                  # print actions without executing
  python main.py --step 2                   # run only row 2
  python main.py --phone Phone1             # filter to Phone1 steps only
  python main.py --sheet Regression         # use a different sheet
  python main.py --file custom_cases.xlsx   # use a different Excel file
  python main.py --generate-template        # write a fresh test_cases.xlsx
""",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print actions without executing ADB commands")
    parser.add_argument("--step", type=int, metavar="ROW", help="Run only the step at this Excel row number (1-based, data starts at row 2)")
    parser.add_argument("--phone", default="both", metavar="Phone1|Phone2|both", help="Limit execution to a specific phone (default: both)")
    parser.add_argument("--sheet", default=config.SHEET_NAME, help=f"Sheet name to read (default: {config.SHEET_NAME})")
    parser.add_argument("--file", default=config.EXCEL_PATH, dest="excel_file", help=f"Path to the Excel file (default: {config.EXCEL_PATH})")
    parser.add_argument("--generate-template", action="store_true", help="Generate a fresh template Excel file and exit")
    return parser.parse_args()


def check_devices(log) -> None:
    """Warn if any configured device is not currently connected."""
    connected = adb.get_connected_devices()
    log.info("Connected devices: %s", connected if connected else "(none)")
    for name, serial in config.DEVICES.items():
        if serial not in connected:
            log.warning(
                "Expected device %s (%s) is NOT connected or offline.", name, serial
            )


def main() -> int:
    args = parse_args()
    log = get_logger()

    # ── Template generation ───────────────────────────────────────────
    if args.generate_template:
        ExcelClient.create_template(args.excel_file, args.sheet)
        return 0

    # ── Device check ─────────────────────────────────────────────────
    log.info("Checking connected ADB devices...")
    check_devices(log)

    # ── Run tests ─────────────────────────────────────────────────────
    log.info("Starting test run | file=%s sheet=%s dry_run=%s", args.excel_file, args.sheet, args.dry_run)

    with ExcelClient(args.excel_file, args.sheet) as ec:
        runner = TestRunner(ec, dry_run=args.dry_run, sheet_name=args.sheet)

        # If a phone filter is set, patch DEVICES so only that phone is accessible
        if args.phone != "both":
            if args.phone not in config.DEVICES:
                log.error(
                    "Unknown phone '%s'. Configured phones: %s",
                    args.phone,
                    list(config.DEVICES.keys()),
                )
                return 1
            # Temporarily restrict; runner skips rows whose serial resolves to None
            config.DEVICES = {args.phone: config.DEVICES[args.phone]}

        try:
            runner.run_all(step_filter=args.step)
        except KeyboardInterrupt:
            log.warning("Interrupted by user.")
        # ExcelClient.__exit__ will call workbook.save() via try/finally

    runner.print_summary()

    return 0 if runner.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
