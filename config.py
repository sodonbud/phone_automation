DEVICES = {
    "Phone1": "RF8Y606FYXX",
    "Phone2": "4B270DLAQ003DE",
}

# SIM MSISDN per device (E.164). Used in Excel Number/Code when calling/SMS between phones.
PHONE_NUMBERS = {
    "Phone1": "+97694310546",   # Samsung Galaxy
    "Phone2": "+97695091051",   # Pixel 9
}

EXCEL_PATH = "test_cases.xlsx"
SHEET_NAME = "TestCases"
ADB_PATH = "C:/Users/DELL/Documents/GitHub/platform-tools/adb.exe"

ADB_TIMEOUT = 30  # seconds per ADB command

LOG_DIR = "logs"
