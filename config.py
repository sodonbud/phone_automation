DEVICES = {
    "Phone1": "adb-RF8Y606FYXX-ALTQKQ._adb-tls-connect._tcp",
    "Phone2": "adb-RZ8R70VGAXW-5Gt8FX._adb-tls-connect._tcp",
}

# SIM MSISDN per device (E.164). Used in Excel Number/Code when calling/SMS between phones.
PHONE_NUMBERS = {
    "Phone1": "+97695389902",
    "Phone2": "+97694001105",
}

EXCEL_PATH = "test_cases.xlsx"
SHEET_NAME = "TestCases"
ADB_PATH = "C:/Users/DELL/Documents/GitHub/platform-tools/adb.exe"

ADB_TIMEOUT = 30  # seconds per ADB command

LOG_DIR = "logs"
