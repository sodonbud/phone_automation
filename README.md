# Phone Test Automation — ADB + Python

Automated phone testing via raw ADB commands. Test cases live in an Excel file; results (Pass/Fail, actual output, timestamp) are written back to the same file in real time.

---

## Requirements

* Python 3.9+
* Android SDK Platform-Tools (`adb` in `PATH`)
* USB debugging **or** ADB over Wi-Fi enabled on each device
* `pip install openpyxl`

---

## Setup

### 1 — Enable ADB on Android

1. **Settings → About phone** → tap **Build number** 7 times to unlock Developer options.
2. **Settings → Developer options** → enable **USB debugging**.
3. Connect the phone with a USB cable and accept the RSA key prompt on the device.
4. Verify: `adb devices` should list the serial with status `device`.

### 2 — Connect over Wi-Fi (optional)

```bash
# While USB is still connected, switch ADB to TCP mode on port 5555
adb -s <SERIAL> tcpip 5555

# Disconnect USB, then connect wirelessly
adb connect <PHONE_IP>:5555

# Verify
adb devices
```

Replace `<PHONE_IP>` with the phone's IP (found in Settings → About phone → Status → IP address).

### 3 — Configure device serials

Edit `config.py`:

```python
DEVICES = {
    "Phone1": "192.168.1.101:5555",   # or USB serial like "emulator-5554"
    "Phone2": "192.168.1.102:5555",
}
EXCEL_PATH = "test_cases.xlsx"
SHEET_NAME = "TestCases"
ADB_PATH   = "adb"                    # or "/usr/local/bin/adb"
```

### 4 — Install dependencies

```bash
pip install -r requirements.txt
```

---

## Excel test case file

Generate a fresh template (headers + one example row per action type):

```bash
python main.py --generate-template
# or with a custom path:
python main.py --generate-template --file my_tests.xlsx
```

### Column reference

| Column | Description |
|---|---|
| `Step` | Human-readable step number (informational) |
| `Action` | `CALL`, `SMS`, `USSD`, `SET_CONFIG`, `GET_CONFIG` |
| `Target Phone` | Must match a key in `config.DEVICES` (e.g. `Phone1`) |
| `Number/Code` | Phone number, USSD code, or `namespace/key` for config actions |
| `Value` | SMS body, or setting value for `SET_CONFIG` |
| `Expected Result` | Substring to match in ADB output (leave blank → any success is PASS) |
| `Actual Result` | **Written by the tool** — raw ADB output |
| `Pass/Fail` | **Written by the tool** — `PASS` / `FAIL` / `SKIP` (colour-coded) |
| `Notes` | **Written by the tool** — truncated ADB output or error reason |
| `Timestamp` | **Written by the tool** — wall-clock time of execution |

### Action examples

| Action | Number/Code | Value | Notes |
|---|---|---|---|
| `CALL` | `+97699001122` | _(blank)_ | Triggers outgoing call |
| `SMS` | `+97699001122` | `Hello world` | Opens SMS composer |
| `USSD` | `*100#` | _(blank)_ | Launches dialer with code |
| `SET_CONFIG` | `global/wifi_on` | `1` | `settings put global wifi_on 1` |
| `GET_CONFIG` | `global/wifi_on` | _(blank)_ | `settings get global wifi_on` |

---

## Usage

### Run all test cases

```bash
python main.py
```

### Dry run — print actions without touching devices

```bash
python main.py --dry-run
```

### Run a single step (by Excel row number, data starts at row 2)

```bash
python main.py --step 3
```

### Limit to one phone

```bash
python main.py --phone Phone1
python main.py --phone Phone2
```

### Use a different sheet or file

```bash
python main.py --sheet Regression --file regression_cases.xlsx
```

### Combine flags

```bash
python main.py --file nightly.xlsx --sheet Smoke --phone Phone1 --dry-run
```

---

## Logs

Every run writes a timestamped log to `logs/run_YYYYMMDD_HHMMSS.log`.
The log contains every ADB command issued and its raw output.

---

## Project structure

```
phone_automation/
├── main.py              # CLI entry point
├── config.py            # Device serials, paths
├── adb_controller.py    # ADB wrappers (make_call, send_sms, …)
├── excel_client.py      # Read test cases / write results
├── test_runner.py       # Orchestration logic
├── logger.py            # Console + file logging
├── requirements.txt
├── test_cases.xlsx      # Generated template
└── logs/                # Per-run log files
```

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | All executed steps passed |
| `1` | One or more steps failed, or a configuration error occurred |
