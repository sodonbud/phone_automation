DEVICES = {
    "Phone1": "RF8Y606FYXX",
    "Phone2": "4B270DLAQ003DE",
}

# SIM MSISDN per device (E.164). Used in Excel Number/Code when calling/SMS between phones.
PHONE_NUMBERS = {
    "Phone1": "+97694310456",
    "Phone2": "+97695091051",
}

EXCEL_PATH = "test_cases.xlsx"
SHEET_NAME = "TestCases"
ADB_PATH = "C:/Users/DELL/Documents/GitHub/platform-tools/adb.exe"  # or full path if not in PATH, e.g. "/usr/local/bin/adb"

ADB_TIMEOUT = 30  # seconds per ADB command

LOG_DIR = "logs"
