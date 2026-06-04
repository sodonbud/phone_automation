DEVICES = {
    "Phone1": "SERIAL_A",  # replace with your actual serial from `adb devices`
    # "Phone2": "SERIAL_B",  # uncomment when you add a second phone
}

EXCEL_PATH = "test_cases.xlsx"
SHEET_NAME = "TestCases"
ADB_PATH = "adb"  # or full path if not in PATH, e.g. "/usr/local/bin/adb"

ADB_TIMEOUT = 30  # seconds per ADB command

LOG_DIR = "logs"
