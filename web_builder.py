#!/usr/bin/env python3
"""Web-based test sheet builder — drag-and-drop workflow UI."""

from __future__ import annotations

import importlib
import io
import json
import os
import re
import subprocess
import sys
import time
import threading
from datetime import datetime

from flask import Flask, Response, jsonify, render_template_string, request, send_file, stream_with_context

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")

# ── Load config safely ────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def _load_config():
    def _migrate_phones(raw: dict) -> dict:
        """Upgrade old {PhoneN: number} format to {PhoneN SIM1/SIM2: number}."""
        out = {}
        for k, v in raw.items():
            if " SIM" in k:
                out[k] = v
            else:
                out[k + " SIM1"] = v
                out[k + " SIM2"] = ""
        return out

    try:
        import config as _cfg
        importlib.reload(_cfg)
        return {
            "PHONE_NUMBERS": _migrate_phones(dict(getattr(_cfg, "PHONE_NUMBERS", {}))),
            "DEVICES": dict(getattr(_cfg, "DEVICES", {})),
            "ADB_PATH": getattr(_cfg, "ADB_PATH", "adb"),
        }
    except ImportError:
        return {
            "PHONE_NUMBERS": {"Phone1 SIM1": "+97699001111", "Phone1 SIM2": "",
                               "Phone2 SIM1": "+97699002222", "Phone2 SIM2": ""},
            "DEVICES": {"Phone1": "SERIALABC123", "Phone2": "SERIALDEF456"},
            "ADB_PATH": "adb",
        }

_cfg_data = _load_config()
PHONE_NUMBERS = _cfg_data["PHONE_NUMBERS"]
DEVICES_MAP   = _cfg_data["DEVICES"]
DEVICE_NAMES  = list(DEVICES_MAP.keys())
ADB_PATH      = _cfg_data["ADB_PATH"]


def _save_config(new_phones: dict, new_serials: dict) -> None:
    """Rewrite PHONE_NUMBERS and DEVICES blocks in config.py in-place."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        src = f.read()

    # Build replacement blocks
    phones_block = "PHONE_NUMBERS = {\n"
    for k, v in new_phones.items():
        phones_block += f'    "{k}": "{v}",\n'
    phones_block += "}"

    serials_block = "DEVICES = {\n"
    for k, v in new_serials.items():
        serials_block += f'    "{k}": "{v}",\n'
    serials_block += "}"

    src = re.sub(r"DEVICES\s*=\s*\{[^}]*\}", serials_block, src, count=1)
    src = re.sub(r"PHONE_NUMBERS\s*=\s*\{[^}]*\}", phones_block, src, count=1)

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(src)

    # Reload module-level vars
    global PHONE_NUMBERS, DEVICES_MAP, DEVICE_NAMES
    d = _load_config()
    PHONE_NUMBERS = d["PHONE_NUMBERS"]
    DEVICES_MAP   = d["DEVICES"]
    DEVICE_NAMES  = list(DEVICES_MAP.keys())


# ── Data storage ──────────────────────────────────────────────────────────────
DATA_DIR        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
HISTORY_FILE    = os.path.join(DATA_DIR, "history.json")

SECTIONS_FILE   = os.path.join(DATA_DIR, "sections.json")
TEMPLATES_FILE  = os.path.join(DATA_DIR, "templates.json")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)


def _load_sections() -> list:
    try:
        with open(SECTIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _write_sections(sections: list) -> None:
    with open(SECTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(sections, f, indent=2)


def _load_templates() -> list:
    try:
        with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _write_templates(templates: list) -> None:
    with open(TEMPLATES_FILE, "w", encoding="utf-8") as f:
        json.dump(templates, f, indent=2)


def _load_history() -> list:
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_run(run: dict) -> None:
    history = _load_history()
    history.insert(0, run)
    history = history[:50]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


# ── Action metadata ───────────────────────────────────────────────────────────
ACTIONS = [
    {
        "id": "CALL",
        "label": "Make Call",
        "color": "#27ae60",
        "icon": "📞",
        "fields": ["target", "number"],
        "hints": {"number": "Destination number (e.g. Phone2 number)"},
        "desc": "Dial a phone number from the target device.",
    },
    {
        "id": "ANSWER_CALL",
        "label": "Answer Call",
        "color": "#2ecc71",
        "icon": "📲",
        "fields": ["target", "value"],
        "hints": {"value": "Timeout in seconds to wait for ring (e.g. 20)"},
        "desc": "Wait for incoming call and answer automatically.",
    },
    {
        "id": "END_CALL",
        "label": "End Call",
        "color": "#e74c3c",
        "icon": "📵",
        "fields": ["target"],
        "hints": {},
        "desc": "Hang up the active call.",
    },
    {
        "id": "WAIT",
        "label": "Wait",
        "color": "#95a5a6",
        "icon": "⏳",
        "fields": ["number"],
        "hints": {"number": "Seconds to wait (e.g. 5)"},
        "desc": "Pause execution for N seconds.",
    },
    {
        "id": "SMS",
        "label": "Send SMS",
        "color": "#2980b9",
        "icon": "💬",
        "fields": ["target", "number", "value"],
        "hints": {
            "number": "Recipient number",
            "value": "Message text",
        },
        "desc": "Send an SMS from the target device.",
    },
    {
        "id": "CHECK_SMS",
        "label": "Check SMS",
        "color": "#3498db",
        "icon": "🔍",
        "fields": ["target", "number", "value", "expected"],
        "hints": {
            "number": "Sender number to look for",
            "value": "Expected text in message body",
            "expected": "Expected result label (e.g. Hello from Phone1)",
        },
        "desc": "Verify an SMS was received from a given sender.",
    },
    {
        "id": "CHECK_CALL",
        "label": "Check Call Log",
        "color": "#16a085",
        "icon": "📋",
        "fields": ["target", "number", "value", "expected"],
        "hints": {
            "number": "Other party's number",
            "value": "INCOMING or OUTGOING",
            "expected": "Expected result label (e.g. Call verified)",
        },
        "desc": "Verify a call entry exists in the call log.",
    },
    {
        "id": "CHECK_VOLTE",
        "label": "Check VoLTE",
        "color": "#0891b2",
        "icon": "📶",
        "fields": ["target", "expected"],
        "hints": {
            "expected": "e.g. VoLTE ACTIVE (leave blank to just capture result)",
        },
        "desc": "Verify VoLTE is active via IMS registration and network type.",
    },
    {
        "id": "USSD",
        "label": "Dial USSD",
        "color": "#8e44ad",
        "icon": "📡",
        "fields": ["target", "number"],
        "hints": {"number": "USSD code (e.g. *100#)"},
        "desc": "Dial a USSD code and capture the network response.",
    },
    {
        "id": "SET_NETWORK",
        "label": "Set Network",
        "color": "#d35400",
        "icon": "📶",
        "fields": ["target", "number"],
        "hints": {"number": "Network type: 2G / 3G / 4G / 5G / 4G5G / AUTO"},
        "default": {"number": "4G"},
        "desc": "Change the preferred network mode.",
    },
    {
        "id": "WAKE",
        "label": "Wake Screen",
        "color": "#f39c12",
        "icon": "💡",
        "fields": ["target"],
        "hints": {},
        "desc": "Wake and unlock the device screen.",
    },
    {
        "id": "SET_CONFIG",
        "label": "Set Config",
        "color": "#7f8c8d",
        "icon": "⚙️",
        "fields": ["target", "number", "value"],
        "hints": {
            "number": "namespace/key (e.g. global/airplane_mode_on)",
            "value": "Value to set",
        },
        "desc": "Set an Android settings key via ADB.",
    },
    {
        "id": "GET_CONFIG",
        "label": "Get Config",
        "color": "#7f8c8d",
        "icon": "🔧",
        "fields": ["target", "number"],
        "hints": {"number": "namespace/key (e.g. global/airplane_mode_on)"},
        "desc": "Read an Android settings key via ADB.",
    },
    {
        "id": "AIRPLANE_MODE",
        "label": "Airplane Mode",
        "color": "#0369a1",
        "icon": "✈️",
        "fields": ["target", "value"],
        "hints": {"value": "on  or  off"},
        "desc": "Toggle airplane mode on or off.",
    },
    {
        "id": "OPEN_BROWSER",
        "label": "Open Browser",
        "color": "#0284c7",
        "icon": "🌐",
        "fields": ["target", "value"],
        "hints": {"value": "URL to open (e.g. https://google.com)"},
        "desc": "Open a URL in the device browser.",
    },
    {
        "id": "SPEEDTEST",
        "label": "Speed Test",
        "color": "#7c3aed",
        "icon": "⚡",
        "fields": ["target", "value"],
        "hints": {"value": "Seconds to wait for result (e.g. 60)"},
        "desc": "Launch Ookla Speedtest app and start the test.",
    },
    {
        "id": "DOWNLOAD_FILE",
        "label": "Download File",
        "color": "#0e7490",
        "icon": "⬇️",
        "fields": ["target", "value"],
        "hints": {"value": "URL or URL:expectedMB (e.g. https://example.com/file.bin:50)"},
        "default": {"value": "http://ipv4.download.thinkbroadband.com/50MB.zip"},
        "desc": "Download a file via browser and verify it completes.",
    },
    {
        "id": "SET_VOLTE",
        "label": "Set VoLTE",
        "color": "#0f766e",
        "icon": "📶",
        "fields": ["target", "value"],
        "hints": {"value": "on or off"},
        "default": {"value": "on"},
        "desc": "Enable or disable VoLTE (Enhanced 4G LTE Mode).",
    },
    {
        "id": "CHECK_WIFI_CALLING",
        "label": "Check WiFi Calling",
        "color": "#0369a1",
        "icon": "📡",
        "fields": ["target"],
        "hints": {},
        "desc": "Check whether WiFi Calling (WFC/VoWiFi) is enabled and registered.",
    },
    {
        "id": "CHECK_NETWORK",
        "label": "Check Network",
        "color": "#0e7490",
        "icon": "📶",
        "fields": ["target"],
        "hints": {},
        "desc": "Check current network type (2G/3G/4G/5G), operator, signal strength and service state.",
    },
    {
        "id": "SET_WIFI_CALLING",
        "label": "Set WiFi Calling",
        "color": "#075985",
        "icon": "📡",
        "fields": ["target", "value"],
        "hints": {"value": "on or off, optionally on:0/1/2 (0=WiFi only, 1=prefer cellular, 2=prefer WiFi)"},
        "default": {"value": "on"},
        "desc": "Enable or disable WiFi Calling. Value: on / off / on:2",
    },
    {
        "id": "SET_APN",
        "label": "Set APN",
        "color": "#b45309",
        "icon": "📡",
        "fields": ["target", "number", "value"],
        "hints": {
            "number": "APN name (e.g. MobiCom Internet)",
            "value": "APN string (e.g. internet)",
        },
        "desc": "Insert a new APN entry via the telephony content provider.",
    },
    # ── Data Package (API) ───────────────────────────────────────────────────
    {
        "id": "DP_CREATE",
        "label": "Create Package",
        "color": "#059669",
        "icon": "📦",
        "fields": ["number", "value"],
        "hints": {
            "number": "Subscriber SIM (e.g. Phone1 SIM1)",
            "value": "Package to create",
        },
        "desc": "Create a data package for a subscriber via API.",
    },
    {
        "id": "DP_DELETE",
        "label": "Delete Package",
        "color": "#dc2626",
        "icon": "🗑️",
        "fields": ["number", "value"],
        "hints": {
            "number": "Subscriber SIM (e.g. Phone1 SIM1)",
            "value": "Package code (auto-filled)",
        },
        "desc": "Delete a data package for a subscriber via API.",
    },
    {
        "id": "DP_MODIFY",
        "label": "Modify Package",
        "color": "#d97706",
        "icon": "✏️",
        "fields": ["number", "value2"],
        "hints": {
            "number": "Subscriber SIM (e.g. Phone1 SIM1)",
            "value2": "Threshold",
        },
        "desc": "Modify/update a data package for a subscriber via API.",
    },
    {
        "id": "DP_CHECK",
        "label": "Check Package",
        "color": "#0891b2",
        "icon": "🔍",
        "fields": ["number"],
        "hints": {
            "number": "Subscriber SIM (e.g. Phone1 SIM1)",
        },
        "desc": "Check/query a specific data package for a subscriber via API.",
    },
    {
        "id": "DP_SELECT",
        "label": "Select Package",
        "color": "#7c3aed",
        "icon": "📋",
        "fields": ["number"],
        "hints": {
            "number": "Subscriber SIM (e.g. Phone1 SIM1)",
        },
        "desc": "Get/select the list of data packages for a subscriber via API.",
    },
]

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Phone Test Builder</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #0f1117;
    --surface: #1a1d27;
    --surface2: #22263a;
    --border: #2e3350;
    --text: #e2e8f0;
    --muted: #718096;
    --accent: #5c6ef8;
    --accent2: #7c3aed;
    --danger: #e53e3e;
    --success: #38a169;
  }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; height: 100vh; overflow: hidden; display: flex; flex-direction: column; }
  header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 8px 16px; display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
  header h1 { font-size: 1rem; font-weight: 700; color: var(--text); flex: 1; }
  .badge { background: var(--accent); color: #fff; font-size: 0.6rem; padding: 2px 7px; border-radius: 99px; font-weight: 600; letter-spacing: .5px; }
  .toolbar { display: flex; gap: 6px; }
  .btn { border: none; border-radius: 7px; padding: 5px 13px; font-size: 0.78rem; font-weight: 600; cursor: pointer; transition: opacity .15s, transform .1s; }
  .btn:active { transform: scale(.97); }
  .btn-primary { background: var(--accent); color: #fff; }
  .btn-primary:hover { opacity: .88; }
  .btn-ghost { background: transparent; color: var(--muted); border: 1px solid var(--border); }
  .btn-ghost:hover { background: var(--surface2); color: var(--text); }
  .btn-danger { background: var(--danger); color: #fff; }
  .btn-success { background: var(--success); color: #fff; }

  .main { display: flex; flex: 1; overflow: hidden; min-height: 0; }

  /* ── Palette ── */
  .palette { width: 230px; min-width: 200px; background: var(--surface); border-right: 1px solid var(--border); display: flex; flex-direction: column; overflow: hidden; }
  .palette-actions { flex: 1; overflow-y: auto; padding: 8px 8px 4px; }
  .palette h2 { font-size: .65rem; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); margin-bottom: 6px; padding: 0 4px; }
  .palette-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; margin-bottom: 2px; }
  .action-card {
    display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 2px;
    padding: 5px 4px; border-radius: 7px;
    cursor: grab; user-select: none;
    border: 1px solid transparent;
    transition: background .15s, transform .1s;
    font-size: .67rem; font-weight: 600; color: #fff;
    text-align: center; min-height: 42px;
  }
  .action-card:active { cursor: grabbing; transform: scale(.97); }
  .action-card .icon { font-size: 1rem; flex-shrink: 0; }
  .action-card .info { flex: 1; line-height: 1.15; }
  .action-card .desc { display: none; }

  /* ── Canvas split ── */
  .canvas-wrap { flex: 1; display: flex; flex-direction: column; overflow: hidden; position: relative; }
  .canvas-toolbar { background: var(--surface); border-bottom: 1px solid var(--border); padding: 5px 12px; display: flex; gap: 6px; align-items: center; flex-shrink: 0; }
  .canvas-toolbar span { color: var(--muted); font-size: .78rem; }
  .canvas-toolbar .spacer { flex: 1; }
  .pane-label { font-size: .68rem; font-weight: 700; text-transform: uppercase; letter-spacing: .8px; color: var(--muted); padding: 6px 16px 0; }
  .canvas-body { flex: 1; display: flex; overflow: hidden; }
  .editor-pane { flex: 1; overflow-y: auto; padding: 16px 20px; border-right: 1px solid var(--border); min-width: 0; }
  .preview-pane { width: 44%; min-width: 320px; overflow-y: auto; padding: 16px 16px; background: var(--bg); }
  .drop-hint {
    border: 2px dashed var(--border); border-radius: 12px;
    padding: 60px 20px; text-align: center; color: var(--muted);
    font-size: .9rem; margin-top: 20px;
    transition: border-color .2s, background .2s;
  }
  .drop-hint.over { border-color: var(--accent); background: rgba(92,110,248,.06); }

  /* ── Preview table ── */
  .preview-table { width: 100%; border-collapse: collapse; font-size: .72rem; }
  .preview-table th { background: var(--surface); color: var(--muted); font-weight: 700; text-transform: uppercase; letter-spacing: .5px; padding: 6px 8px; border-bottom: 2px solid var(--border); text-align: left; white-space: nowrap; }
  .preview-table td { padding: 5px 8px; border-bottom: 1px solid var(--border); vertical-align: middle; color: var(--text); word-break: break-word; }
  .preview-table tr:hover td { background: var(--surface2); }
  .preview-table .section-hdr td { background: var(--surface2); color: var(--accent); font-weight: 700; font-size: .7rem; padding: 6px 8px; }
  .preview-table .step-num-cell { color: var(--muted); font-weight: 700; min-width: 28px; }
  .action-pill { display: inline-block; padding: 2px 7px; border-radius: 99px; font-size: .65rem; font-weight: 700; color: #fff; white-space: nowrap; }
  .preview-empty { text-align: center; color: var(--muted); padding: 60px 20px; font-size: .85rem; }

  .step-list { display: flex; flex-direction: column; gap: 8px; }

  /* ── Run button ── */
  .btn-run { background: linear-gradient(135deg,#5c6ef8,#7c3aed); color: #fff; }
  .btn-run:hover { opacity: .88; }
  .btn-run.running { background: var(--danger); animation: pulse 1.2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.7} }

  /* ── Run panel ── */
  /* ── Run panel (full-height overlay) ── */
  .run-panel {
    background: #0a0c14;
    display: flex; flex-direction: column;
    position: absolute; inset: 0;
    z-index: 10;
    opacity: 0; pointer-events: none;
    transform: translateY(12px);
    transition: opacity .25s ease, transform .25s ease;
  }
  .run-panel.open { opacity: 1; pointer-events: all; transform: translateY(0); }
  .run-panel-header {
    display: flex; align-items: center; gap: 10px;
    padding: 10px 18px; background: var(--surface);
    border-bottom: 1px solid var(--border); flex-shrink: 0;
  }
  .run-title { font-size: .85rem; font-weight: 700; color: var(--text); }
  .run-summary { font-size: .78rem; color: var(--muted); display: flex; gap: 10px; }
  .run-body { display:flex; flex:1; overflow:hidden; min-height:0; }
  .run-log { flex: 1; overflow-y: auto; padding: 10px 20px; font-family: 'Cascadia Code','Consolas',monospace; font-size: .75rem; }
  .monitor-col { width:260px; flex-shrink:0; border-left:1px solid var(--border); overflow-y:auto; background:var(--surface); display:none; }
  .monitor-col.visible { display:block; }
  .mon-phone { padding:12px; border-bottom:1px solid var(--border); }
  .mon-phone-name { font-size:.78rem; font-weight:700; display:flex; align-items:center; gap:6px; margin-bottom:8px; flex-wrap:wrap; }
  .mon-row { display:flex; justify-content:space-between; align-items:center; font-size:.7rem; padding:3px 0; border-bottom:1px solid rgba(255,255,255,.03); }
  .mon-row:last-child { border-bottom:none; }
  .mon-label { color:var(--muted); }
  .mon-val { font-weight:600; }
  .mon-val.on { color:#48bb78; }
  .mon-val.off { color:var(--muted); }
  .mon-val.ringing { color:#f6e05e; animation:pulse .8s ease-in-out infinite; }
  .mon-val.active { color:#48bb78; }
  .log-row { display: flex; align-items: flex-start; gap: 10px; padding: 5px 0; border-bottom: 1px solid rgba(255,255,255,.04); }
  .log-step { color: var(--muted); min-width: 26px; flex-shrink: 0; text-align: right; }
  .log-action { min-width: 130px; flex-shrink: 0; }
  .log-target { color: var(--muted); min-width: 75px; flex-shrink: 0; }
  .log-output { color: #a0aec0; flex: 1; word-break: break-word; }
  .log-badge { display: inline-block; padding: 2px 9px; border-radius: 99px; font-size: .65rem; font-weight: 800; letter-spacing: .5px; min-width: 46px; text-align: center; flex-shrink: 0; }
  .badge-pass { background: #1a4731; color: #48bb78; border: 1px solid #276749; }
  .badge-fail { background: #4a1515; color: #fc8181; border: 1px solid #742a2a; }
  .badge-skip { background: #3d3200; color: #f6e05e; border: 1px solid #744210; }
  .badge-run  { background: #1a1f3c; color: #90cdf4; border: 1px solid #2c5282; }
  .log-section { color: var(--accent); font-weight: 700; padding: 8px 0 3px; font-size: .72rem; letter-spacing: .3px; }
  .log-done-banner { text-align:center; padding: 18px 0 6px; color: var(--muted); font-size: .8rem; border-top: 1px solid var(--border); margin-top: 8px; }

  /* ── Report modal ── */
  .modal-backdrop { position:fixed; inset:0; background:rgba(0,0,0,.7); z-index:100; display:flex; align-items:center; justify-content:center; opacity:0; pointer-events:none; transition:opacity .2s; }
  .modal-backdrop.open { opacity:1; pointer-events:all; }
  .modal { background:var(--surface); border:1px solid var(--border); border-radius:14px; width:min(820px,96vw); max-height:88vh; display:flex; flex-direction:column; overflow:hidden; box-shadow:0 24px 60px rgba(0,0,0,.6); transform:scale(.96); transition:transform .2s; }
  .modal-backdrop.open .modal { transform:scale(1); }
  .modal-header { padding:16px 22px; border-bottom:1px solid var(--border); display:flex; align-items:center; gap:12px; flex-shrink:0; }
  .modal-header h2 { font-size:1rem; font-weight:700; flex:1; }
  .modal-body { overflow-y:auto; padding:20px 22px; }
  .report-stats { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:22px; }
  .stat-card { background:var(--surface2); border:1px solid var(--border); border-radius:10px; padding:14px 16px; text-align:center; }
  .stat-num { font-size:2rem; font-weight:800; line-height:1; }
  .stat-label { font-size:.68rem; text-transform:uppercase; letter-spacing:.8px; color:var(--muted); margin-top:4px; }
  .stat-total .stat-num { color:var(--text); }
  .stat-pass  .stat-num { color:#48bb78; }
  .stat-fail  .stat-num { color:#fc8181; }
  .stat-skip  .stat-num { color:#f6e05e; }
  .report-bar { height:8px; border-radius:99px; background:var(--border); overflow:hidden; margin-bottom:22px; display:flex; }
  .bar-pass { background:#276749; transition:width .5s; }
  .bar-fail { background:#742a2a; transition:width .5s; }
  .bar-skip { background:#744210; transition:width .5s; }
  .report-table { width:100%; border-collapse:collapse; font-size:.78rem; }
  .report-table th { background:var(--surface2); color:var(--muted); font-weight:700; text-transform:uppercase; letter-spacing:.5px; padding:8px 10px; border-bottom:2px solid var(--border); text-align:left; white-space:nowrap; position:sticky; top:0; }
  .report-table td { padding:7px 10px; border-bottom:1px solid var(--border); vertical-align:top; }
  .report-table tr:hover td { background:var(--surface2); }
  .report-table .sec-row td { background:var(--surface2); color:var(--accent); font-weight:700; font-size:.72rem; }
  .step {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 0;
    transition: box-shadow .15s, transform .1s;
    cursor: default;
  }
  .step.dragging { opacity: .4; }
  .step.drag-over { box-shadow: 0 0 0 2px var(--accent); }
  .step-header {
    display: flex; align-items: center; gap: 10px;
    padding: 10px 14px;
    border-radius: 10px 10px 0 0;
    cursor: grab;
  }
  .step-header:active { cursor: grabbing; }
  .step-num { font-size: .7rem; font-weight: 700; color: var(--muted); min-width: 22px; }
  .step-icon { font-size: 1rem; }
  .step-label { font-size: .85rem; font-weight: 700; flex: 1; }
  .step-actions { display: flex; gap: 4px; }
  .step-btn { background: transparent; border: none; cursor: pointer; color: var(--muted); font-size: .85rem; padding: 2px 5px; border-radius: 4px; line-height: 1; }
  .step-btn:hover { background: rgba(255,255,255,.08); color: var(--text); }
  .step-fields { padding: 0 14px 12px 46px; display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .field-group { display: flex; flex-direction: column; gap: 3px; }
  .field-group.wide { grid-column: span 2; }
  .field-group label { font-size: .65rem; text-transform: uppercase; letter-spacing: .5px; color: var(--muted); }
  .field-group select, .field-group input {
    background: var(--surface2); border: 1px solid var(--border);
    color: var(--text); border-radius: 6px; padding: 5px 8px;
    font-size: .8rem; outline: none; width: 100%;
    transition: border-color .15s;
  }
  .field-group select:focus, .field-group input:focus { border-color: var(--accent); }

  /* ── Searchable select ── */
  .searchable-wrap { position: relative; }
  .searchable-dropdown {
    position: absolute; top: calc(100% + 2px); left: 0; right: 0; z-index: 200;
    background: var(--surface2); border: 1px solid var(--accent);
    border-radius: 6px; max-height: 180px; overflow-y: auto;
    display: none; box-shadow: 0 6px 20px rgba(0,0,0,.5);
  }
  .searchable-dropdown.open { display: block; }
  .searchable-option {
    padding: 5px 9px; font-size: .78rem; cursor: pointer; color: var(--text);
    border-bottom: 1px solid rgba(255,255,255,.04);
  }
  .searchable-option:last-child { border-bottom: none; }
  .searchable-option:hover, .searchable-option.focused { background: var(--accent); color: #fff; }

  /* Section divider */
  .section-row {
    background: var(--surface2); border: 1px solid var(--accent);
    border-radius: 8px; padding: 8px 14px;
    display: flex; align-items: center; gap: 10px; cursor: pointer;
    user-select: none;
  }
  .section-row input {
    background: transparent; border: none; color: var(--accent);
    font-size: .85rem; font-weight: 700; flex: 1; outline: none;
    min-width: 0; cursor: text;
  }
  .section-row input::placeholder { color: var(--muted); font-weight: 400; }
  .section-row .tag { font-size: .65rem; background: var(--accent); color: #fff; padding: 2px 7px; border-radius: 99px; flex-shrink: 0; }
  .section-arrow { font-size: .8rem; color: var(--accent); transition: transform .15s; flex-shrink: 0; }
  .section-row.collapsed .section-arrow { transform: rotate(-90deg); }
  .section-row.collapsed { opacity: .75; }

  /* ── Template manager modal ── */
  .tpl-list { display: flex; flex-direction: column; gap: 6px; margin-bottom: 16px; max-height: 260px; overflow-y: auto; }
  .tpl-item { display: flex; align-items: center; gap: 8px; background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; padding: 9px 12px; }
  .tpl-name { flex: 1; font-size: .85rem; font-weight: 600; }
  .tpl-meta { font-size: .7rem; color: var(--muted); }
  .tpl-empty { color: var(--muted); font-size: .85rem; text-align: center; padding: 24px; }
  .save-tpl-row { display: flex; gap: 8px; padding-top: 12px; border-top: 1px solid var(--border); }
  .save-tpl-row input { flex: 1; background: var(--surface2); border: 1px solid var(--border); color: var(--text); border-radius: 8px; padding: 7px 12px; font-size: .85rem; outline: none; }
  .save-tpl-row input:focus { border-color: var(--accent); }

  /* Toast */
  .toast { position: fixed; bottom: 20px; right: 20px; background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 12px 18px; font-size: .85rem; color: var(--text); box-shadow: 0 8px 24px rgba(0,0,0,.4); transform: translateY(80px); opacity: 0; transition: all .3s; z-index: 1000; }
  .toast.show { transform: translateY(0); opacity: 1; }
  .toast.success { border-color: var(--success); }
  .toast.error { border-color: var(--danger); }

  /* ── Device panel ── */
  .device-panel { border-top: 1px solid var(--border); padding: 8px; flex-shrink: 0; }
  .device-panel h2 { font-size: .7rem; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); margin-bottom: 8px; padding: 0 4px; display: flex; align-items: center; justify-content: space-between; }
  .device-card { background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; padding: 10px; margin-bottom: 8px; }
  .device-card .device-title { font-size: .78rem; font-weight: 700; margin-bottom: 6px; display: flex; align-items: center; gap: 6px; }
  .device-card .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); flex-shrink: 0; }
  .device-card .dot.online { background: var(--success); }
  .device-label { font-size: .62rem; text-transform: uppercase; letter-spacing: .5px; color: var(--muted); margin-bottom: 2px; margin-top: 6px; }
  .device-input { width: 100%; background: var(--bg); border: 1px solid var(--border); color: var(--text); border-radius: 5px; padding: 4px 7px; font-size: .75rem; outline: none; transition: border-color .15s; }
  .device-input:focus { border-color: var(--accent); }
  .save-config-btn { width: 100%; margin-top: 8px; padding: 6px; font-size: .75rem; font-weight: 600; background: var(--accent); color: #fff; border: none; border-radius: 6px; cursor: pointer; transition: opacity .15s; }
  .save-config-btn:hover { opacity: .85; }

  /* Scrollbar */
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

  /* ── Saved sections dropdown ── */
  .sec-dropdown { position:relative; }
  .sec-menu { display:none; position:absolute; top:calc(100% + 6px); left:0; background:var(--surface); border:1px solid var(--border); border-radius:10px; min-width:280px; max-height:340px; overflow-y:auto; box-shadow:0 8px 24px rgba(0,0,0,.45); z-index:300; padding:8px; }
  .sec-menu.open { display:block; }
  .saved-sec-card { display:flex; align-items:center; gap:6px; background:var(--surface2); border:1px solid var(--border); border-radius:6px; padding:6px 8px; margin-bottom:5px; font-size:.75rem; }
  .saved-sec-name { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .saved-sec-btn { background:none; border:1px solid var(--border); border-radius:4px; color:var(--text); cursor:pointer; font-size:.72rem; padding:2px 6px; }
  .saved-sec-btn:hover { background:var(--surface2); border-color:var(--accent); }
  .log-section-result { display:inline-block; margin-left:8px; font-size:.65rem; font-weight:700; padding:1px 6px; border-radius:10px; }
  .log-section-result.pass { background:#276749; color:#9ae6b4; }
  .log-section-result.fail { background:#742a2a; color:#feb2b2; }

  /* ── History ── */
  .history-card { background:var(--surface2); border:1px solid var(--border); border-radius:8px; padding:12px 14px; cursor:pointer; transition:border-color .15s; }
  .history-card:hover { border-color:var(--accent); }
  .history-detail table { width:100%; border-collapse:collapse; }
  .history-detail td { padding:3px 4px; color:var(--text); }
  .sparkline-bar { display:flex; gap:3px; align-items:flex-end; height:40px; background:var(--surface2); border-radius:6px; padding:6px; }

  /* ── Screenshot link ── */
  .screenshot-link { display:inline-block; margin-top:4px; font-size:.68rem; color:var(--accent); text-decoration:none; }
  .screenshot-link:hover { text-decoration:underline; }

  /* ── Timing pill ── */
  .timing-pill { display:inline-block; background:var(--surface2); border:1px solid var(--border); border-radius:10px; font-size:.65rem; padding:1px 6px; color:var(--muted); margin-left:6px; }

  /* ── Loading spinner ── */
  @keyframes spin { to { transform: rotate(360deg); } }
  .loading-spinner {
    width: 12px; height: 12px; flex-shrink: 0;
    border: 2px solid var(--border); border-top-color: var(--accent);
    border-radius: 50%; animation: spin .7s linear infinite; display: inline-block;
  }
  .field-loading {
    display: flex; align-items: center; gap: 6px;
    font-size: .72rem; color: var(--muted); padding: 5px 8px;
    background: var(--surface2); border: 1px solid var(--border);
    border-radius: 6px; width: 100%;
  }

  /* ── Toggle switch ── */
  .toggle-wrap { display:inline-flex; align-items:center; cursor:pointer; position:relative; }
  .toggle-wrap input[type="checkbox"] { display:none; }
  .toggle-slider {
    width:38px; height:22px; background:var(--border); border-radius:11px;
    position:relative; transition:background .2s;
  }
  .toggle-slider::after {
    content:''; position:absolute; width:16px; height:16px; border-radius:50%;
    background:#fff; top:3px; left:3px; transition:transform .2s;
    box-shadow:0 1px 3px rgba(0,0,0,.3);
  }
  .toggle-wrap input:checked + .toggle-slider { background:var(--success); }
  .toggle-wrap input:checked + .toggle-slider::after { transform:translateX(16px); }
  .toggle-state { font-size:.75rem; font-weight:700; min-width:26px; }

  /* ── Startup overlay ─────────────────────────────────── */
  #startup-overlay {
    position:fixed; inset:0; background:var(--bg); z-index:9999;
    display:flex; flex-direction:column; align-items:center; justify-content:center; gap:16px;
  }
  #startup-overlay .spin { width:48px; height:48px; border:4px solid var(--border);
    border-top-color:var(--accent); border-radius:50%; animation:spin .8s linear infinite; }
  #startup-overlay p { color:var(--muted); font-size:.9rem; }

  /* ── Floating device panel ───────────────────────────── */
  .device-float {
    position:fixed; bottom:0; right:16px; width:270px; z-index:600;
    background:var(--surface); border:1px solid var(--border);
    border-radius:10px 10px 0 0; box-shadow:0 -4px 24px rgba(0,0,0,.4);
  }
  .device-float-header {
    padding:8px 12px; cursor:pointer; display:flex; align-items:center; gap:8px;
    font-size:.72rem; font-weight:700; text-transform:uppercase; letter-spacing:.5px;
    color:var(--muted); border-bottom:1px solid var(--border); user-select:none;
  }
  .device-float-header:hover { color:var(--text); }
  .device-float-body { padding:8px; max-height:380px; overflow-y:auto; }
  .device-float.collapsed .device-float-body { display:none; }
  .device-float-chevron { margin-left:auto; transition:transform .2s; }
  .device-float.collapsed .device-float-chevron { transform:rotate(180deg); }

  /* ── Device status bar (in run panel) ───────────────── */
  .device-status-bar {
    display:flex; gap:8px; flex-wrap:wrap; padding:4px 12px;
    background:var(--surface2); border-bottom:1px solid var(--border); font-size:.7rem;
  }
  .dsb-chip { display:inline-flex; align-items:center; gap:4px; padding:2px 7px;
    border-radius:10px; background:var(--surface); border:1px solid var(--border); }
  .dsb-chip .dot { width:6px; height:6px; border-radius:50%; background:var(--border); flex-shrink:0; }
  .dsb-chip .dot.online { background:#4ade80; }
</style>
</head>
<body>
<div id="startup-overlay">
  <div class="spin"></div>
  <p id="startup-msg">Detecting phones…</p>
</div>
<header>
  <h1>📱 Phone Test Builder</h1>
  <span class="badge">ADB Automation</span>
  <div class="toolbar">
    <button class="btn btn-ghost" onclick="addSection()">+ Section</button>
    <button class="btn btn-ghost" onclick="clearAll()">Clear</button>
    <button class="btn btn-ghost" onclick="openTemplateManager()">📁 Templates</button>
    <div class="sec-dropdown" id="sec-dropdown">
      <button class="btn btn-ghost" onclick="toggleSectionsMenu(event)">📂 Sections ▾</button>
      <div class="sec-menu" id="sec-menu">
        <div id="saved-sections-list"><div style="font-size:.72rem;color:var(--muted);padding:4px">No saved sections yet.</div></div>
      </div>
    </div>
    <button class="btn btn-ghost" onclick="openHistory()">📊 History</button>
    <a class="btn btn-ghost" href="/monitor" target="_blank" style="text-decoration:none">🖥 Monitor</a>
    <button class="btn btn-success" onclick="exportExcel()">⬇ Export Excel</button>
    <button class="btn btn-run" id="run-btn" onclick="toggleRun()">▶ Run Test</button>
  </div>
</header>

<div class="main">
  <!-- Palette -->
  <aside class="palette">
    <div class="palette-actions">
      <h2>Actions</h2>
      <div id="palette"></div>
    </div>
  </aside>

  <!-- Canvas -->
  <div class="canvas-wrap">
    <div class="canvas-toolbar">
      <span id="step-count">0 steps</span>
      <span class="spacer"></span>
      <button class="btn btn-ghost" onclick="moveSelected('up')">▲ Up</button>
      <button class="btn btn-ghost" onclick="moveSelected('down')">▼ Down</button>
      <button class="btn btn-danger" onclick="deleteSelected()">✕ Delete</button>
    </div>
    <div class="canvas-body">
      <!-- Editor pane -->
      <div class="editor-pane" id="canvas" ondragover="onCanvasDragOver(event)" ondrop="onCanvasDrop(event)">
        <div class="drop-hint" id="drop-hint">Drag actions here to build your test workflow</div>
        <div class="step-list" id="step-list"></div>
      </div>
      <!-- Preview pane -->
      <div class="preview-pane">
        <div class="pane-label" style="padding:0 0 8px 0">Preview</div>
        <div id="preview-body"></div>
      </div>
    </div>
  </div>
  <!-- Run panel (collapsible, slides up) -->
  <div class="run-panel" id="run-panel">
    <div class="run-panel-header">
      <span class="run-title" id="run-title">Test Results</span>
      <span class="run-summary" id="run-summary"></span>
      <div style="flex:1"></div>
      <button class="btn btn-ghost" style="font-size:.75rem;padding:4px 10px" onclick="clearRun()">Clear</button>
      <button class="btn btn-ghost" style="font-size:.75rem;padding:4px 10px" onclick="closeRun()">✕</button>
    </div>
    <div class="device-status-bar" id="device-status-bar" style="display:none"></div>
    <div class="run-body">
      <div class="run-log" id="run-log"></div>
      <div class="monitor-col" id="monitor-col"></div>
    </div>
  </div>
</div>

<!-- Floating device panel -->
<div class="device-float" id="device-float">
  <div class="device-float-header" onclick="toggleDeviceFloat()">
    📱 Devices
    <span id="device-float-online" style="color:#4ade80;font-size:.65rem"></span>
    <span style="margin-left:auto;font-size:.6rem;color:var(--accent);cursor:pointer" onclick="event.stopPropagation();refreshDevices()">↻</span>
    <span class="device-float-chevron">▼</span>
  </div>
  <div class="device-float-body">
    <div id="device-cards"></div>
    <button class="save-config-btn" style="background:var(--surface2);color:var(--accent);border:1px solid var(--accent);margin-bottom:6px" onclick="autoDetect()">🔍 Auto Detect</button>
    <button class="save-config-btn" style="background:var(--surface2);color:#a78bfa;border:1px solid #a78bfa;margin-bottom:6px" onclick="openWirelessModal()">📡 Wireless Pair</button>
    <button class="save-config-btn" onclick="saveConfig()">💾 Save config.py</button>
  </div>
</div>

<!-- Device detect modal -->
<div class="modal-backdrop" id="detect-modal" onclick="if(event.target===this)closeDetectModal()">
  <div class="modal" style="max-width:540px">
    <div class="modal-header">
      <h2>🔍 Detected Devices</h2>
      <button class="btn btn-ghost" style="padding:5px 12px;font-size:.78rem" onclick="closeDetectModal()">✕</button>
    </div>
    <div class="modal-body" id="detect-body"></div>
  </div>
</div>

<!-- Template manager modal -->
<div class="modal-backdrop" id="tpl-modal" onclick="if(event.target===this)closeTplModal()">
  <div class="modal" style="max-width:500px">
    <div class="modal-header">
      <h2>📁 Templates</h2>
      <button class="btn btn-ghost" style="padding:5px 12px;font-size:.78rem" onclick="closeTplModal()">✕</button>
    </div>
    <div class="modal-body">
      <div class="tpl-list" id="tpl-list"></div>
      <div class="save-tpl-row">
        <input id="tpl-name-input" placeholder="Template name…" onkeydown="if(event.key==='Enter')saveTpl()">
        <button class="btn btn-primary" onclick="saveTpl()">💾 Save current</button>
      </div>
    </div>
  </div>
</div>

<!-- Detect / Assign modal -->
<div class="modal-backdrop" id="detect-modal" onclick="if(event.target===this)closeDetectModal()">
  <div class="modal" style="max-width:520px">
    <div class="modal-header">
      <h2>📡 Detected Devices</h2>
      <button class="btn btn-ghost" style="padding:5px 12px;font-size:.78rem" onclick="closeDetectModal()">✕</button>
    </div>
    <div class="modal-body" id="detect-body">
    </div>
  </div>
</div>

<!-- Wireless Pair modal -->
<div class="modal-backdrop" id="wireless-modal" onclick="if(event.target===this)closeWirelessModal()">
  <div class="modal" style="max-width:460px">
    <div class="modal-header">
      <h2>📡 Wireless Pair</h2>
      <button class="btn btn-ghost" style="padding:5px 12px;font-size:.78rem" onclick="closeWirelessModal()">✕</button>
    </div>
    <div class="modal-body">
      <div style="font-size:.78rem;color:var(--muted);margin-bottom:16px;line-height:1.6">
        Phone дээр: <b>Settings → Developer options → Wireless debugging → Pair device with pairing code</b><br>
        IP address, port болон 6 оронтой кодыг доор оруулна уу.
      </div>

      <!-- Step 1: Pair -->
      <div style="background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:14px;margin-bottom:12px">
        <div style="font-size:.72rem;text-transform:uppercase;letter-spacing:.5px;color:#a78bfa;margin-bottom:10px;font-weight:700">Step 1 — Pair</div>
        <div style="display:grid;grid-template-columns:1fr 120px;gap:8px;margin-bottom:8px">
          <div>
            <div style="font-size:.65rem;color:var(--muted);margin-bottom:3px">IP Address</div>
            <input id="wp-ip" type="text" placeholder="192.168.1.100" style="width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:6px 8px;font-size:.8rem;outline:none">
          </div>
          <div>
            <div style="font-size:.65rem;color:var(--muted);margin-bottom:3px">Pair Port</div>
            <input id="wp-pair-port" type="text" placeholder="37425" style="width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:6px 8px;font-size:.8rem;outline:none">
          </div>
        </div>
        <div style="margin-bottom:10px">
          <div style="font-size:.65rem;color:var(--muted);margin-bottom:3px">Pairing Code (6 digits)</div>
          <input id="wp-code" type="text" placeholder="123456" maxlength="6" style="width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:6px 8px;font-size:.8rem;outline:none;letter-spacing:3px">
        </div>
        <button class="btn btn-primary" style="width:100%" onclick="doPair()">🔗 Pair</button>
        <div id="wp-pair-result" style="margin-top:8px;font-size:.75rem;min-height:18px"></div>
      </div>

      <!-- Step 2: Connect -->
      <div style="background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:14px">
        <div style="font-size:.72rem;text-transform:uppercase;letter-spacing:.5px;color:#a78bfa;margin-bottom:10px;font-weight:700">Step 2 — Connect</div>
        <div style="font-size:.7rem;color:var(--muted);margin-bottom:8px">
          Pair хийсний дараа Wireless debugging дэлгэц дээрх <b>IP address &amp; Port</b>-ыг оруулна уу (pair port-оос өөр байна).
        </div>
        <div style="display:grid;grid-template-columns:1fr 120px;gap:8px;margin-bottom:10px">
          <div>
            <div style="font-size:.65rem;color:var(--muted);margin-bottom:3px">IP Address</div>
            <input id="wp-conn-ip" type="text" placeholder="192.168.1.100" style="width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:6px 8px;font-size:.8rem;outline:none">
          </div>
          <div>
            <div style="font-size:.65rem;color:var(--muted);margin-bottom:3px">Connect Port</div>
            <input id="wp-conn-port" type="text" placeholder="38123" style="width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:6px 8px;font-size:.8rem;outline:none">
          </div>
        </div>
        <button class="btn btn-primary" style="width:100%;background:#7c3aed;border-color:#7c3aed" onclick="doConnect()">⚡ Connect</button>
        <div id="wp-conn-result" style="margin-top:8px;font-size:.75rem;min-height:18px"></div>
      </div>
    </div>
  </div>
</div>

<!-- Report modal -->
<div class="modal-backdrop" id="report-modal" onclick="if(event.target===this)closeReport()">
  <div class="modal">
    <div class="modal-header">
      <h2>📊 Test Report</h2>
      <span id="report-ts" style="font-size:.72rem;color:var(--muted)"></span>
      <button class="btn btn-success" style="padding:5px 14px;font-size:.78rem;margin-left:auto" onclick="exportResults()">⬇ Export Excel</button>
      <button class="btn btn-ghost" style="padding:5px 12px;font-size:.78rem" onclick="closeReport()">✕ Close</button>
    </div>
    <div class="modal-body" id="report-body"></div>
  </div>
</div>

<!-- History modal -->
<div class="modal-backdrop" id="history-modal" onclick="if(event.target===this)closeHistory()">
  <div class="modal" style="max-width:680px">
    <div class="modal-header">
      <h2>📊 Run History</h2>
      <button class="btn btn-danger" style="padding:5px 12px;font-size:.75rem;margin-left:auto" onclick="clearHistory()">🗑 Clear</button>
      <button class="btn btn-ghost" style="padding:5px 12px;font-size:.78rem" onclick="closeHistory()">✕</button>
    </div>
    <div class="modal-body" id="history-body">
      <div class="tpl-empty">Loading…</div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const ACTIONS = {{ actions|tojson }};
let PHONES = {{ phones|tojson }};
let PHONE_NUMBERS = {{ phone_numbers|tojson }};
let DEVICES_MAP = {{ devices_map|tojson }};
let NETWORK_OPTIONS = {};  // populated once on startup: {Phone1: ["LTE/3G/2G", ...], ...}

// ── Data package list — fetched from API on startup ───────────────────────────
let DP_PACKAGES = [];
let dpPackagesLoading = false;

async function fetchPackageList() {
  dpPackagesLoading = true;
  render();
  try {
    const res = await fetch('/api/package_list');
    const d = await res.json();
    DP_PACKAGES = Array.isArray(d.packages) ? d.packages : [];
    if (!DP_PACKAGES.length && d.error) console.warn('Package list error:', d.error);
  } catch(e) {
    DP_PACKAGES = [];
  }
  dpPackagesLoading = false;
  render();
}
let networkOptionsLoading = false;
let devicesLoading = false;

let steps = [];
let dragSrc = null;      // palette card action id
let dragStepIdx = null;  // step reorder index
let selectedIdx = null;
let stepCounter = 0;

// ── Device panel ──────────────────────────────────────────────────────────────
async function refreshDevices() {
  devicesLoading = true;
  document.getElementById('device-cards').innerHTML =
    '<div style="display:flex;align-items:center;gap:8px;padding:10px 4px;color:var(--muted);font-size:.78rem">' +
    '<span class="loading-spinner"></span><span>Detecting phones…</span></div>';
  try {
    const res = await fetch('/config');
    const d = await res.json();
    PHONES = Object.keys(d.devices);
    PHONE_NUMBERS = d.phone_numbers;
    DEVICES_MAP = d.devices;
    devicesLoading = false;
    buildDeviceCards(d);
  } catch(e) {
    devicesLoading = false;
    buildDeviceCards({ devices: DEVICES_MAP, phone_numbers: PHONE_NUMBERS, online: [] });
  }
}

function buildDeviceCards(d) {
  const el = document.getElementById('device-cards');
  el.innerHTML = '';
  Object.keys(d.devices).forEach(name => {
    const serial = d.devices[name] || '';
    const sim1   = d.phone_numbers[name + ' SIM1'] || '';
    const sim2   = d.phone_numbers[name + ' SIM2'] || '';
    const online = (d.online || []).includes(serial);
    const card = document.createElement('div');
    card.className = 'device-card';
    card.innerHTML = `
      <div class="device-title">
        <span class="dot ${online ? 'online' : ''}"></span>
        <span>${name}</span>
        <span style="font-size:.6rem;color:var(--muted);margin-left:auto">${online ? '🟢 connected' : '⚫ offline'}</span>
        <span style="font-size:.65rem;color:#f87171;cursor:pointer;margin-left:6px" title="Remove slot" onclick="removePhone('${name}')">✕</span>
      </div>
      <div class="device-label">Serial</div>
      <input class="device-input" id="serial_${name}" value="${serial}" placeholder="device serial">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;margin-top:3px">
        <div>
          <div class="device-label">SIM 1</div>
          <input class="device-input" id="sim1_${name}" value="${sim1}" placeholder="+976...">
        </div>
        <div>
          <div class="device-label">SIM 2</div>
          <input class="device-input" id="sim2_${name}" value="${sim2}" placeholder="+976...">
        </div>
      </div>`;
    el.appendChild(card);
  });
}

function addPhone() {
  const next = 'Phone' + (PHONES.length + 1);
  PHONES.push(next);
  DEVICES_MAP[next] = '';
  PHONE_NUMBERS[next + ' SIM1'] = '';
  PHONE_NUMBERS[next + ' SIM2'] = '';
  buildDeviceCards({ devices: DEVICES_MAP, phone_numbers: PHONE_NUMBERS, online: [] });
  render();
}

function removePhone(name) {
  if (PHONES.length <= 1) { toast('At least one phone slot required.', 'error'); return; }
  PHONES = PHONES.filter(p => p !== name);
  delete DEVICES_MAP[name];
  delete PHONE_NUMBERS[name + ' SIM1'];
  delete PHONE_NUMBERS[name + ' SIM2'];
  buildDeviceCards({ devices: DEVICES_MAP, phone_numbers: PHONE_NUMBERS, online: [] });
  render();
}

function openWirelessModal() {
  document.getElementById('wireless-modal').classList.add('open');
}
function closeWirelessModal() {
  document.getElementById('wireless-modal').classList.remove('open');
}

async function doPair() {
  const ip   = document.getElementById('wp-ip').value.trim();
  const port = document.getElementById('wp-pair-port').value.trim();
  const code = document.getElementById('wp-code').value.trim();
  const res  = document.getElementById('wp-pair-result');
  if (!ip || !port || !code) { res.style.color='#f87171'; res.textContent='IP, port болон кодыг бүгдийг оруулна уу.'; return; }
  res.style.color='var(--muted)'; res.textContent='Холбогдож байна…';
  const r = await fetch('/pair', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ip, port, code}) });
  const d = await r.json();
  if (d.ok) {
    res.style.color='#4ade80';
    res.textContent = '✓ ' + (d.output || 'Амжилттай pair хийлээ.');
    // auto-fill connect IP
    document.getElementById('wp-conn-ip').value = ip;
  } else {
    res.style.color='#f87171';
    res.textContent = '✗ ' + (d.output || 'Pair амжилтгүй.');
  }
}

async function doConnect() {
  const ip   = document.getElementById('wp-conn-ip').value.trim();
  const port = document.getElementById('wp-conn-port').value.trim();
  const res  = document.getElementById('wp-conn-result');
  if (!ip || !port) { res.style.color='#f87171'; res.textContent='IP болон connect port оруулна уу.'; return; }
  res.style.color='var(--muted)'; res.textContent='Холбогдож байна…';
  const r = await fetch('/connect', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ip, port}) });
  const d = await r.json();
  if (d.ok) {
    res.style.color='#4ade80';
    res.textContent = '✓ ' + (d.output || 'Амжилттай холбогдлоо.');
    closeWirelessModal();
    toast('Утас холбогдлоо — Auto Detect дарж slot-д оноох боломжтой.', 'success');
  } else {
    res.style.color='#f87171';
    res.textContent = '✗ ' + (d.output || 'Холболт амжилтгүй.');
  }
}

function closeDetectModal() {
  document.getElementById('detect-modal').classList.remove('open');
}

async function autoDetect() {
  toast('Detecting devices…', '');
  const res = await fetch('/detect');
  if (!res.ok) { toast('Detection failed', 'error'); return; }
  const data = await res.json();
  if (!data.devices || data.devices.length === 0) {
    toast('No devices found — check USB cable / wireless debugging', 'error'); return;
  }

  // Build assign UI in modal
  const body = document.getElementById('detect-body');
  body.innerHTML = '';

  // Info banner
  const info = document.createElement('div');
  info.style.cssText = 'font-size:.8rem;color:var(--muted);margin-bottom:14px';
  info.textContent = `${data.devices.length} device(s) found. Assign each to a phone slot then click Apply.`;
  body.appendChild(info);

  // One row per detected device
  data.devices.forEach((dev, i) => {
    const row = document.createElement('div');
    row.style.cssText = 'background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:10px';

    // Model + serial
    const title = document.createElement('div');
    title.style.cssText = 'font-size:.85rem;font-weight:700;color:var(--text);margin-bottom:4px';
    title.textContent = dev.model || dev.serial;
    const serial = document.createElement('div');
    serial.style.cssText = 'font-size:.7rem;color:var(--muted);word-break:break-all;margin-bottom:8px;font-family:monospace';
    serial.textContent = dev.serial;
    row.appendChild(title);
    row.appendChild(serial);

    // Slot label + phone number input
    const slotLabel = document.createElement('div');
    slotLabel.style.cssText = 'font-size:.68rem;color:var(--accent);font-weight:600;margin-bottom:6px';
    slotLabel.textContent = `→ Will be assigned as Phone${i + 1}`;
    row.appendChild(slotLabel);

    const simGrid = document.createElement('div');
    simGrid.style.cssText = 'display:grid;grid-template-columns:1fr 1fr;gap:8px';
    const autoNums = [dev.number1 || '', dev.number2 || ''];
    ['SIM1','SIM2'].forEach((sim, si) => {
      const w = document.createElement('div');
      const autoLabel = autoNums[si] ? `<span style="color:#48bb78;font-size:.6rem;margin-left:4px">● auto</span>` : '';
      w.innerHTML = `<div style="font-size:.65rem;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-bottom:3px">${sim}${autoLabel}</div>`;
      const inp = document.createElement('input');
      inp.id = `detect-${sim.toLowerCase()}-${i}`;
      inp.type = 'text'; inp.placeholder = '+976…';
      inp.value = autoNums[si];
      inp.style.cssText = 'width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:5px 8px;font-size:.8rem;outline:none';
      w.appendChild(inp); simGrid.appendChild(w);
    });
    row.appendChild(simGrid);
    body.appendChild(row);
  });

  // Apply button
  const applyBtn = document.createElement('button');
  applyBtn.className = 'btn btn-primary';
  applyBtn.style.cssText = 'width:100%;margin-top:6px;padding:8px';
  applyBtn.textContent = '✓ Apply Assignment';
  applyBtn.addEventListener('click', () => applyDetect(data.devices));
  body.appendChild(applyBtn);

  document.getElementById('detect-modal').classList.add('open');
}

function applyDetect(devices) {
  // Auto-assign Phone1…PhoneN, replacing current slots entirely
  const newPhones = {}, newSerials = {};
  devices.forEach((dev, i) => {
    const slot = `Phone${i + 1}`;
    const sim1 = document.getElementById(`detect-sim1-${i}`)?.value.trim() || '';
    const sim2 = document.getElementById(`detect-sim2-${i}`)?.value.trim() || '';
    newSerials[slot]          = dev.serial;
    newPhones[slot + ' SIM1'] = sim1;
    newPhones[slot + ' SIM2'] = sim2;
  });

  PHONES        = Object.keys(newSerials);
  DEVICES_MAP   = newSerials;
  PHONE_NUMBERS = newPhones;

  buildDeviceCards({ devices: DEVICES_MAP, phone_numbers: PHONE_NUMBERS, online: [] });
  render();
  closeDetectModal();
  toast(`${PHONES.length} phone(s) assigned as ${PHONES.join(', ')}. Click 💾 Save to config.py to persist.`, 'success');
}

async function saveConfig() {
  const phones = {}, serials = {};
  PHONES.forEach(name => {
    serials[name] = document.getElementById('serial_' + name)?.value || DEVICES_MAP[name] || '';
    phones[name + ' SIM1'] = document.getElementById('sim1_' + name)?.value.trim() || PHONE_NUMBERS[name + ' SIM1'] || '';
    phones[name + ' SIM2'] = document.getElementById('sim2_' + name)?.value.trim() || PHONE_NUMBERS[name + ' SIM2'] || '';
  });
  const res = await fetch('/config', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ phone_numbers: phones, devices: serials }) });
  if (res.ok) {
    PHONE_NUMBERS = phones; DEVICES_MAP = serials;
    toast('Config saved to config.py ✓', 'success');
  } else {
    toast('Save failed: ' + await res.text(), 'error');
  }
}

// ── Palette ───────────────────────────────────────────────────────────────────
const PALETTE_GROUPS = [
  { label: '📞 Call',    ids: ['CALL','ANSWER_CALL','END_CALL','CHECK_CALL','CHECK_VOLTE'] },
  { label: '💬 SMS',    ids: ['SMS','CHECK_SMS'] },
  { label: '📶 Network', ids: ['SET_NETWORK','AIRPLANE_MODE','SET_APN','USSD','SET_VOLTE','CHECK_WIFI_CALLING','SET_WIFI_CALLING','CHECK_NETWORK'] },
  { label: '🌐 Apps',   ids: ['OPEN_BROWSER','SPEEDTEST','DOWNLOAD_FILE'] },
  { label: '⚙️ Device', ids: ['WAKE','WAIT','SET_CONFIG','GET_CONFIG'] },
  { label: '📦 Data Package', ids: ['DP_CREATE','DP_MODIFY','DP_CHECK'] },
];

function buildPalette() {
  const el = document.getElementById('palette');
  el.innerHTML = '';

  PALETTE_GROUPS.forEach(group => {
    const groupActions = group.ids.map(id => ACTIONS.find(a => a.id === id)).filter(Boolean);
    if (!groupActions.length) return;

    const hdr = document.createElement('div');
    hdr.style.cssText = 'font-size:.65rem;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);padding:6px 4px 4px;margin-top:4px';
    hdr.textContent = group.label;
    el.appendChild(hdr);

    const grid = document.createElement('div');
    grid.className = 'palette-grid';
    groupActions.forEach(a => {
      const card = document.createElement('div');
      card.className = 'action-card';
      card.style.background = a.color + '22';
      card.style.borderColor = a.color + '55';
      card.draggable = true;
      card.title = a.desc;
      card.innerHTML = `<span class="icon">${a.icon}</span><div class="info">${a.label}</div>`;
      card.addEventListener('dragstart', e => { dragSrc = a.id; dragStepIdx = null; e.dataTransfer.effectAllowed = 'copy'; });
      card.addEventListener('dblclick', () => addStep(a.id));
      grid.appendChild(card);
    });
    el.appendChild(grid);
  });

  refreshDevices();
  fetchPackageList();
}

// ── Step data ─────────────────────────────────────────────────────────────────
function newStep(actionId) {
  const a = ACTIONS.find(x => x.id === actionId);
  const def = a.default || {};
  const defaultTarget = PHONES[0] || 'Phone1';
  const isDP = actionId.startsWith('DP_');
  let defaultNumber = def.number ?? (a.id === 'WAIT' ? '5' : '');
  if (['CALL', 'CHECK_CALL', 'ANSWER_CALL', 'SMS', 'CHECK_SMS'].includes(actionId) && !defaultNumber) {
    const otherSlots = Object.keys(PHONE_NUMBERS)
      .filter(k => !k.startsWith(defaultTarget) && PHONE_NUMBERS[k]);
    defaultNumber = otherSlots[0] || getOtherPhoneNumber(defaultTarget);
  }
  if (isDP && !defaultNumber) {
    const simSlots = Object.keys(PHONE_NUMBERS).filter(k => PHONE_NUMBERS[k]);
    defaultNumber = simSlots[0] || (PHONES[0] ? PHONES[0] + ' SIM1' : 'Phone1 SIM1');
  }
  return {
    id: stepCounter++,
    action: actionId,
    target: defaultTarget,
    number: defaultNumber,
    value: def.value ?? '',
    value2: '',
    expected: def.expected ?? '',
    _isSection: false,
  };
}

function addStep(actionId) {
  steps.push(newStep(actionId));
  render();
}

function addSection() {
  steps.push({ id: stepCounter++, _isSection: true, label: 'Section Title', collapsed: false });
  render();
}

function clearAll() {
  if (steps.length === 0 || confirm('Clear all steps?')) { steps = []; selectedIdx = null; render(); }
}

// ── Render ────────────────────────────────────────────────────────────────────
function render() {
  const list = document.getElementById('step-list');
  const hint = document.getElementById('drop-hint');
  list.innerHTML = '';
  hint.style.display = steps.length ? 'none' : 'block';

  let stepNum = 0;
  let sectionCollapsed = false;
  steps.forEach((s, idx) => {
    if (s._isSection) {
      sectionCollapsed = s.collapsed || false;

      const row = document.createElement('div');
      row.className = 'section-row' + (sectionCollapsed ? ' collapsed' : '');
      row.title = sectionCollapsed ? 'Click to expand' : 'Click to collapse';
      row.addEventListener('click', e => {
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'BUTTON') return;
        steps[idx].collapsed = !steps[idx].collapsed;
        render();
      });

      const arrow = document.createElement('span');
      arrow.className = 'section-arrow';
      arrow.textContent = '▾';

      const tag = document.createElement('span');
      tag.className = 'tag';
      tag.textContent = 'SECTION';

      const inp = document.createElement('input');
      inp.value = s.label || '';
      inp.placeholder = 'Section title…';
      inp.addEventListener('input', e => { steps[idx].label = e.target.value; renderPreview(); });
      inp.addEventListener('click', e => e.stopPropagation());

      const sav = document.createElement('button');
      sav.className = 'step-btn';
      sav.title = 'Save section to library';
      sav.textContent = '💾';
      sav.addEventListener('click', e => { e.stopPropagation(); saveSection(idx); });

      const dup = document.createElement('button');
      dup.className = 'step-btn';
      dup.title = 'Duplicate section';
      dup.textContent = '⎘';
      dup.addEventListener('click', e => { e.stopPropagation(); dupeSection(idx); });

      const del = document.createElement('button');
      del.className = 'step-btn';
      del.title = 'Remove section';
      del.textContent = '✕';
      del.addEventListener('click', e => { e.stopPropagation(); removeStep(idx); });

      row.appendChild(arrow);
      row.appendChild(tag);
      row.appendChild(inp);
      row.appendChild(sav);
      row.appendChild(dup);
      row.appendChild(del);
      list.appendChild(row);
      return;
    }

    if (sectionCollapsed) return;

    stepNum++;
    const a = ACTIONS.find(x => x.id === s.action) || { label: s.action, icon: '?', color: '#555', fields: [], hints: {} };
    const div = document.createElement('div');
    div.className = 'step' + (selectedIdx === idx ? ' drag-over' : '');
    div.dataset.idx = idx;
    div.draggable = true;
    div.addEventListener('click', e => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA') return;
      if (selectedIdx === idx) return;
      selectedIdx = idx;
      document.querySelectorAll('.step').forEach(el => el.classList.remove('drag-over'));
      div.classList.add('drag-over');
    });
    div.addEventListener('dragstart', e => { dragStepIdx = idx; dragSrc = null; e.dataTransfer.effectAllowed = 'move'; div.classList.add('dragging'); });
    div.addEventListener('dragend', () => div.classList.remove('dragging'));
    div.addEventListener('dragover', e => { e.preventDefault(); div.classList.add('drag-over'); });
    div.addEventListener('dragleave', () => div.classList.remove('drag-over'));
    div.addEventListener('drop', e => { e.preventDefault(); div.classList.remove('drag-over'); onStepDrop(idx); });

    // Header
    const hdr = document.createElement('div');
    hdr.className = 'step-header';
    hdr.style.background = a.color + '22';
    hdr.style.borderBottom = `1px solid ${a.color}44`;
    hdr.innerHTML = `
      <span class="step-num">${stepNum}</span>
      <span class="step-icon">${a.icon}</span>
      <span class="step-label" style="color:${a.color}">${a.label}</span>
      <div class="step-actions">
        <button class="step-btn" title="Move up" onclick="event.stopPropagation();moveStep(${idx},-1)">▲</button>
        <button class="step-btn" title="Move down" onclick="event.stopPropagation();moveStep(${idx},1)">▼</button>
        <button class="step-btn" title="Duplicate" onclick="event.stopPropagation();dupeStep(${idx})">⎘</button>
        <button class="step-btn" title="Remove" onclick="event.stopPropagation();removeStep(${idx})">✕</button>
      </div>`;
    div.appendChild(hdr);

    // Fields
    const fields = document.createElement('div');
    fields.className = 'step-fields';

    if (a.fields.includes('target')) {
      if (['CALL','CHECK_CALL','ANSWER_CALL','SMS','CHECK_SMS'].includes(s.action)) {
        fields.appendChild(makeSelectPhone(idx));
      } else if (s.action === 'SET_NETWORK') {
        fields.appendChild(makeSelect(idx, 'target', 'Target Phone', PHONES, () => render()));
      } else {
        fields.appendChild(makeSelect(idx, 'target', 'Target Phone', PHONES));
      }
    }
    if (a.fields.includes('number')) {
      if (s.action === 'SET_NETWORK') {
        if (networkOptionsLoading && !NETWORK_OPTIONS[s.target]) {
          const g = document.createElement('div');
          g.className = 'field-group';
          const lb = document.createElement('label'); lb.textContent = 'Network Type';
          const ld = document.createElement('div'); ld.className = 'field-loading';
          ld.innerHTML = '<span class="loading-spinner"></span><span>Fetching from phone…</span>';
          g.appendChild(lb); g.appendChild(ld);
          fields.appendChild(g);
        } else {
          fields.appendChild(makeSelectFixed(idx, 'number', 'Network Type', getNetworkOptions(s.target)));
        }
      } else if (s.action.startsWith('DP_')) {
        const simSlots = Object.keys(PHONE_NUMBERS).filter(k => PHONE_NUMBERS[k]);
        fields.appendChild(makeComboInput(idx, 'number', 'Subscriber SIM', simSlots.length ? simSlots : PHONES));
      } else if (['CALL','CHECK_CALL','ANSWER_CALL','SMS','CHECK_SMS'].includes(s.action)) {
        const otherSlots = Object.keys(PHONE_NUMBERS)
          .filter(k => !k.startsWith(s.target) && PHONE_NUMBERS[k]);
        fields.appendChild(makeComboInput(idx, 'number', 'Number', otherSlots.length ? otherSlots : Object.keys(PHONE_NUMBERS)));
      } else {
        fields.appendChild(makeInput(idx, 'number', 'Number / Code', a.hints.number || ''));
      }
    }
    if (a.fields.includes('value')) {
      if (['SET_VOLTE', 'AIRPLANE_MODE', 'SET_WIFI_CALLING'].includes(s.action)) {
        fields.appendChild(makeToggle(idx, 'value', 'Value'));
      } else if (s.action === 'DP_CREATE') {
        if (dpPackagesLoading) {
          const g = document.createElement('div'); g.className = 'field-group';
          const lb = document.createElement('label'); lb.textContent = 'Package';
          const ld = document.createElement('div'); ld.className = 'field-loading';
          ld.innerHTML = '<span class="loading-spinner"></span><span>Loading packages…</span>';
          g.appendChild(lb); g.appendChild(ld); fields.appendChild(g);
        } else {
          fields.appendChild(makeSearchableSelect(idx, 'value', 'Package', DP_PACKAGES));
        }
      } else if (['DP_DELETE'].includes(s.action)) {
        fields.appendChild(makeInputReadonly(idx, 'value', 'Package Code'));
      } else {
        fields.appendChild(makeInput(idx, 'value', 'Value', a.hints.value || '', a.id === 'SMS'));
      }
    }
    if (a.fields.includes('value2')) {
      fields.appendChild(makeInput(idx, 'value2', 'New Value / Params', a.hints.value2 || ''));
    }
    if (a.fields.includes('expected')) {
      fields.appendChild(makeInput(idx, 'expected', 'Expected Result', a.hints.expected || ''));
    }
    div.appendChild(fields);
    list.appendChild(div);
  });

  const n = steps.filter(s => !s._isSection).length;
  document.getElementById('step-count').textContent = `${n} step${n !== 1 ? 's' : ''}`;
  renderPreview();
}

// ── Preview ───────────────────────────────────────────────────────────────────
function renderPreview() {
  const el = document.getElementById('preview-body');
  if (!el) return;
  const dataSteps = steps.filter(s => !s._isSection);
  if (steps.length === 0) {
    el.innerHTML = '<div class="preview-empty">No steps yet — add actions to see preview</div>';
    return;
  }

  let html = '<table class="preview-table"><thead><tr>'
    + '<th>#</th><th>Action</th><th>Target</th><th>Number/Code</th><th>Value</th><th>Expected</th>'
    + '</tr></thead><tbody>';

  let stepNum = 0;
  let secCollapsed = false;
  steps.forEach(s => {
    if (s._isSection) {
      secCollapsed = s.collapsed || false;
      const arrow = secCollapsed ? '▶' : '▼';
      html += `<tr class="section-hdr"><td colspan="6">${arrow} ${esc(s.label || 'Section')}</td></tr>`;
      return;
    }
    if (secCollapsed) return;
    stepNum++;
    const a = ACTIONS.find(x => x.id === s.action) || { color: '#555' };
    const pill = `<span class="action-pill" style="background:${a.color}">${esc(s.action)}</span>`;
    // Target: show phone name + SIM1 number
    const tgtNum = PHONE_NUMBERS[s.target + ' SIM1'] || PHONE_NUMBERS[s.target + ' SIM2'] || '';
    const tgtCell = s.target
      ? esc(s.target) + (tgtNum ? `<br><span style="font-size:.65rem;color:var(--muted)">${esc(tgtNum)}</span>` : '')
      : '<span style="color:var(--muted)">—</span>';
    // Number: show slot key + resolved number below
    const numResolved = PHONE_NUMBERS[s.number] || '';
    const num = s.number
      ? `<code style="font-size:.7rem">${esc(s.number)}</code>` + (numResolved ? `<br><span style="font-size:.65rem;color:var(--muted)">${esc(numResolved)}</span>` : '')
      : '<span style="color:var(--muted)">—</span>';
    const val  = s.value   ? esc(s.value)   : '<span style="color:var(--muted)">—</span>';
    const exp  = s.expected ? esc(s.expected) : '<span style="color:var(--muted)">—</span>';
    html += `<tr>
      <td class="step-num-cell">${stepNum}</td>
      <td>${pill}</td>
      <td>${tgtCell}</td>
      <td>${num}</td>
      <td>${val}</td>
      <td>${exp}</td>
    </tr>`;
  });

  html += '</tbody></table>';
  el.innerHTML = html;
}

function makeComboInput(idx, field, label, options) {
  const g = document.createElement('div');
  g.className = 'field-group';
  const lb = document.createElement('label'); lb.textContent = label;
  const listId = `combo_${idx}_${field}`;
  const row = document.createElement('div');
  row.style.cssText = 'display:flex;gap:4px;align-items:center';
  const inp = document.createElement('input');
  inp.type = 'text';
  inp.setAttribute('list', listId);
  inp.value = steps[idx][field] || '';
  inp.placeholder = 'Select or type…';
  inp.style.flex = '1';
  inp.addEventListener('input', e => { steps[idx][field] = e.target.value; });
  const clr = document.createElement('button');
  clr.type = 'button'; clr.className = 'step-btn'; clr.textContent = '✕'; clr.title = 'Clear';
  clr.addEventListener('click', () => { steps[idx][field] = ''; inp.value = ''; });
  const dl = document.createElement('datalist');
  dl.id = listId;
  options.forEach(o => {
    const num = PHONE_NUMBERS[o];
    const opt = document.createElement('option');
    if (num) {
      opt.value = num;  // actual MSISDN shown in input
      opt.label = o;    // SIM slot shown as hint in dropdown
    } else {
      opt.value = o;
    }
    dl.appendChild(opt);
  });
  row.appendChild(inp); row.appendChild(clr); row.appendChild(dl);
  g.appendChild(lb); g.appendChild(row);
  return g;
}

function makeInputReadonly(idx, field, label) {
  const g = document.createElement('div');
  g.className = 'field-group';
  const lb = document.createElement('label');
  lb.textContent = label;
  const inp = document.createElement('input');
  inp.value = steps[idx][field] || '';
  inp.disabled = true;
  inp.style.cssText = 'opacity:.4;cursor:not-allowed;';
  inp.title = 'Auto-filled from Select Package result';
  g.appendChild(lb); g.appendChild(inp);
  return g;
}

function makeSelect(idx, field, label, options, onChangeCb = null) {
  const g = document.createElement('div');
  g.className = 'field-group';
  const lb = document.createElement('label');
  lb.textContent = label;
  const sel = document.createElement('select');
  options.forEach(o => {
    const opt = document.createElement('option');
    opt.value = o;
    opt.textContent = PHONE_NUMBERS[o] ? `${o}  (${PHONE_NUMBERS[o]})` : o;
    if (steps[idx][field] === o) opt.selected = true;
    sel.appendChild(opt);
  });
  sel.addEventListener('change', e => { steps[idx][field] = e.target.value; if (onChangeCb) onChangeCb(); });
  g.appendChild(lb); g.appendChild(sel);
  return g;
}

function makeSelectFixed(idx, field, label, options) {
  const g = document.createElement('div');
  g.className = 'field-group';
  const lb = document.createElement('label');
  lb.textContent = label;
  const sel = document.createElement('select');
  let matched = false;
  options.forEach(o => {
    const opt = document.createElement('option');
    opt.value = o;
    opt.textContent = o;
    if ((steps[idx][field] || '').toLowerCase() === o.toLowerCase()) { opt.selected = true; matched = true; }
    sel.appendChild(opt);
  });
  if (!matched && options.length) steps[idx][field] = options[0];
  sel.addEventListener('change', e => { steps[idx][field] = e.target.value; renderPreview(); });
  g.appendChild(lb); g.appendChild(sel);
  return g;
}

function makeSearchableSelect(idx, field, label, options) {
  const g = document.createElement('div');
  g.className = 'field-group';
  const lb = document.createElement('label');
  lb.textContent = label;

  const wrap = document.createElement('div');
  wrap.className = 'searchable-wrap';

  const inp = document.createElement('input');
  inp.value = steps[idx][field] || '';
  inp.placeholder = 'Search package…';
  inp.autocomplete = 'off';

  const drop = document.createElement('div');
  drop.className = 'searchable-dropdown';

  function buildList(filter) {
    drop.innerHTML = '';
    const f = (filter || '').toLowerCase();
    const filtered = options.filter(o => !f || o.toLowerCase().includes(f));
    if (!filtered.length) { drop.classList.remove('open'); return; }
    filtered.forEach(o => {
      const item = document.createElement('div');
      item.className = 'searchable-option';
      item.textContent = o;
      if (o === steps[idx][field]) item.classList.add('focused');
      item.addEventListener('mousedown', e => {
        e.preventDefault();
        steps[idx][field] = o;
        inp.value = o;
        drop.classList.remove('open');
        renderPreview();
      });
      drop.appendChild(item);
    });
    drop.classList.add('open');
  }

  inp.addEventListener('focus', () => buildList(''));
  inp.addEventListener('input', () => { steps[idx][field] = inp.value; buildList(inp.value); renderPreview(); });
  inp.addEventListener('blur', () => setTimeout(() => drop.classList.remove('open'), 160));

  inp.addEventListener('keydown', e => {
    const items = [...drop.querySelectorAll('.searchable-option')];
    let cur = items.findIndex(i => i.classList.contains('focused'));
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (cur >= 0) items[cur].classList.remove('focused');
      cur = Math.min(cur + 1, items.length - 1);
      if (cur < 0) cur = 0;
      items[cur]?.classList.add('focused');
      items[cur]?.scrollIntoView({ block: 'nearest' });
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (cur >= 0) items[cur].classList.remove('focused');
      cur = Math.max(cur - 1, 0);
      items[cur]?.classList.add('focused');
      items[cur]?.scrollIntoView({ block: 'nearest' });
    } else if (e.key === 'Enter') {
      const focused = drop.querySelector('.searchable-option.focused');
      if (focused) focused.dispatchEvent(new MouseEvent('mousedown'));
    } else if (e.key === 'Escape') {
      drop.classList.remove('open');
    }
  });

  wrap.appendChild(inp);
  wrap.appendChild(drop);
  g.appendChild(lb);
  g.appendChild(wrap);
  return g;
}

function makeToggle(idx, field, label) {
  const g = document.createElement('div');
  g.className = 'field-group';
  const lb = document.createElement('label');
  lb.textContent = label;

  const row = document.createElement('div');
  row.style.cssText = 'display:flex;align-items:center;gap:8px;margin-top:4px';

  const wrap = document.createElement('label');
  wrap.className = 'toggle-wrap';

  const cb = document.createElement('input');
  cb.type = 'checkbox';
  const curVal = (steps[idx][field] || 'on').toLowerCase();
  cb.checked = curVal === 'on' || curVal.startsWith('on:');

  const slider = document.createElement('span');
  slider.className = 'toggle-slider';

  const stateLabel = document.createElement('span');
  stateLabel.className = 'toggle-state';
  stateLabel.textContent = cb.checked ? 'ON' : 'OFF';
  stateLabel.style.color = cb.checked ? 'var(--success)' : 'var(--muted)';

  cb.addEventListener('change', e => {
    steps[idx][field] = e.target.checked ? 'on' : 'off';
    stateLabel.textContent = e.target.checked ? 'ON' : 'OFF';
    stateLabel.style.color = e.target.checked ? 'var(--success)' : 'var(--muted)';
    renderPreview();
  });

  wrap.appendChild(cb);
  wrap.appendChild(slider);
  row.appendChild(wrap);
  row.appendChild(stateLabel);
  g.appendChild(lb);
  g.appendChild(row);
  return g;
}

function getOtherPhoneNumber(targetName) {
  const others = PHONES.filter(p => p !== targetName);
  if (!others.length) return '';
  return PHONE_NUMBERS[others[0] + ' SIM1'] || PHONE_NUMBERS[others[0] + ' SIM2'] || '';
}

function getNetworkOptions(targetPhone) {
  return NETWORK_OPTIONS[targetPhone] || ['2G', '3G', '4G', '5G', '4G5G', 'AUTO'];
}

async function fetchNetworkOptions() {
  networkOptionsLoading = true;
  render();
  try {
    const res = await fetch('/api/network_options');
    if (res.ok) {
      const data = await res.json();
      if (Object.keys(data).length > 0) NETWORK_OPTIONS = data;
    }
  } catch (e) {}
  networkOptionsLoading = false;
  render();
}

function makeSelectPhone(idx) {
  const g = document.createElement('div');
  g.className = 'field-group';
  const lb = document.createElement('label');
  lb.textContent = 'Target Phone';
  const sel = document.createElement('select');
  PHONES.forEach(o => {
    const opt = document.createElement('option');
    opt.value = o;
    const num = PHONE_NUMBERS[o + ' SIM1'] || PHONE_NUMBERS[o + ' SIM2'] || '';
    opt.textContent = num ? `${o}  (${num})` : o;
    if (steps[idx].target === o) opt.selected = true;
    sel.appendChild(opt);
  });
  sel.addEventListener('change', e => {
    steps[idx].target = e.target.value;
    // Auto-select first available SIM of other phone as default number
    const otherSlots = Object.keys(PHONE_NUMBERS)
      .filter(k => !k.startsWith(e.target.value) && PHONE_NUMBERS[k]);
    if (otherSlots.length) steps[idx].number = otherSlots[0];
    render();
  });
  g.appendChild(lb); g.appendChild(sel);
  return g;
}

function makeInput(idx, field, label, placeholder, wide = false) {
  const g = document.createElement('div');
  g.className = 'field-group' + (wide ? ' wide' : '');
  const lb = document.createElement('label');
  lb.textContent = label;
  const inp = document.createElement('input');
  inp.type = 'text'; inp.placeholder = placeholder; inp.value = steps[idx][field] || '';
  inp.dataset.field = field;
  inp.addEventListener('input', e => { steps[idx][field] = e.target.value; });
  g.appendChild(lb); g.appendChild(inp);
  return g;
}

// ── Step ops ──────────────────────────────────────────────────────────────────
function removeStep(idx) { steps.splice(idx, 1); if (selectedIdx === idx) selectedIdx = null; render(); }
function moveStep(idx, dir) {
  const to = idx + dir;
  if (to < 0 || to >= steps.length) return;
  [steps[idx], steps[to]] = [steps[to], steps[idx]];
  selectedIdx = to; render();
}
function dupeStep(idx) {
  const copy = JSON.parse(JSON.stringify(steps[idx]));
  copy.id = stepCounter++;
  steps.splice(idx + 1, 0, copy);
  render();
}
function dupeSection(idx) {
  // Collect the section header + all child steps until the next section
  const block = [steps[idx]];
  for (let i = idx + 1; i < steps.length; i++) {
    if (steps[i]._isSection) break;
    block.push(steps[i]);
  }
  const copies = block.map(s => { const c = JSON.parse(JSON.stringify(s)); c.id = stepCounter++; return c; });
  steps.splice(idx + block.length, 0, ...copies);
  render();
}
function moveSelected(dir) { if (selectedIdx !== null) moveStep(selectedIdx, dir === 'up' ? -1 : 1); }
function deleteSelected() { if (selectedIdx !== null) removeStep(selectedIdx); }

// ── Drag & drop ───────────────────────────────────────────────────────────────
function onCanvasDragOver(e) {
  e.preventDefault();
  document.getElementById('drop-hint').classList.add('over');
}
function onCanvasDrop(e) {
  e.preventDefault();
  document.getElementById('drop-hint').classList.remove('over');
  if (dragSrc) { addStep(dragSrc); dragSrc = null; }
}
function onStepDrop(toIdx) {
  if (dragSrc) { steps.splice(toIdx, 0, newStep(dragSrc)); dragSrc = null; render(); return; }
  if (dragStepIdx !== null && dragStepIdx !== toIdx) {
    const [moved] = steps.splice(dragStepIdx, 1);
    const dest = toIdx > dragStepIdx ? toIdx - 1 : toIdx;
    steps.splice(dest, 0, moved);
    dragStepIdx = null; render();
  }
}

// ── Export ────────────────────────────────────────────────────────────────────
async function exportExcel() {
  if (steps.length === 0) { toast('Add some steps first.', 'error'); return; }
  try {
    const res = await fetch('/export', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ steps }) });
    if (!res.ok) { const t = await res.text(); toast('Export failed: ' + t, 'error'); return; }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url; a.download = 'test_cases.xlsx'; a.click();
    URL.revokeObjectURL(url);
    toast('Excel exported!', 'success');
  } catch(e) { toast('Export error: ' + e.message, 'error'); }
}

// ── Template manager ─────────────────────────────────────────────────────────
async function openTemplateManager() {
  document.getElementById('tpl-name-input').value = '';
  document.getElementById('tpl-modal').classList.add('open');
  document.getElementById('tpl-list').innerHTML = '<div class="tpl-empty">Loading…</div>';
  try {
    const res = await fetch('/api/templates');
    const data = await res.json();
    renderTplList(data.templates || []);
  } catch(e) {
    document.getElementById('tpl-list').innerHTML = '<div class="tpl-empty">Failed to load templates.</div>';
  }
}

function closeTplModal() {
  document.getElementById('tpl-modal').classList.remove('open');
}

function renderTplList(tpls) {
  const list = document.getElementById('tpl-list');
  list.innerHTML = '';

  // Built-in default
  const defBtn = document.createElement('div');
  defBtn.className = 'tpl-item';
  defBtn.innerHTML = `<span class="tpl-name">⭐ Default Template</span><span class="tpl-meta">built-in</span>`;
  const loadDef = document.createElement('button');
  loadDef.className = 'btn btn-ghost'; loadDef.style.cssText = 'padding:4px 10px;font-size:.75rem';
  loadDef.textContent = 'Load';
  loadDef.addEventListener('click', async () => {
    if (steps.length && !confirm('Replace current workflow?')) return;
    const res = await fetch('/template');
    const data = await res.json();
    steps = data.steps.map(s => ({ ...s, id: stepCounter++ }));
    render(); closeTplModal(); toast('Default template loaded.', 'success');
  });
  defBtn.appendChild(loadDef);
  list.appendChild(defBtn);

  if (tpls.length === 0) {
    list.insertAdjacentHTML('beforeend', '<div class="tpl-empty">No saved templates yet.<br>Build a workflow and save it below.</div>');
  }

  tpls.forEach(tpl => {
    const item = document.createElement('div');
    item.className = 'tpl-item';
    const stepCount = (tpl.steps || []).filter(s => !s._isSection).length;
    item.innerHTML = `<span class="tpl-name">${esc(tpl.name)}</span><span class="tpl-meta">${stepCount} steps · ${tpl.date || ''}</span>`;

    const loadBtn = document.createElement('button');
    loadBtn.className = 'btn btn-ghost'; loadBtn.style.cssText = 'padding:4px 10px;font-size:.75rem';
    loadBtn.textContent = 'Load';
    loadBtn.addEventListener('click', () => {
      if (steps.length && !confirm('Replace current workflow?')) return;
      steps = (tpl.steps || []).map(s => ({ ...s, id: stepCounter++ }));
      render(); closeTplModal(); toast(`"${tpl.name}" loaded.`, 'success');
    });

    const appendBtn = document.createElement('button');
    appendBtn.className = 'btn btn-ghost'; appendBtn.style.cssText = 'padding:4px 10px;font-size:.75rem;color:#4ade80;border-color:#4ade80';
    appendBtn.textContent = '+ Append';
    appendBtn.addEventListener('click', () => {
      steps.push(...(tpl.steps || []).map(s => ({ ...s, id: stepCounter++ })));
      render(); closeTplModal(); toast(`"${tpl.name}" appended (${(tpl.steps||[]).length} steps).`, 'success');
    });

    const delBtn = document.createElement('button');
    delBtn.className = 'btn btn-danger'; delBtn.style.cssText = 'padding:4px 8px;font-size:.75rem';
    delBtn.textContent = '✕';
    delBtn.addEventListener('click', async () => {
      if (!confirm(`Delete template "${tpl.name}"?`)) return;
      await fetch('/api/templates/' + encodeURIComponent(tpl.name), { method: 'DELETE' });
      toast(`"${tpl.name}" deleted.`, 'success');
      openTemplateManager();
    });

    item.appendChild(loadBtn);
    item.appendChild(appendBtn);
    item.appendChild(delBtn);
    list.appendChild(item);
  });
}

async function saveTpl() {
  const name = document.getElementById('tpl-name-input').value.trim();
  if (!name) { toast('Enter a template name.', 'error'); return; }
  if (steps.length === 0) { toast('No steps to save.', 'error'); return; }
  const res = await fetch('/api/templates', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, steps: JSON.parse(JSON.stringify(steps)) }),
  });
  if (res.ok) {
    document.getElementById('tpl-name-input').value = '';
    toast(`Template "${name}" saved.`, 'success');
    openTemplateManager();
  } else {
    toast('Failed to save template.', 'error');
  }
}

// ── Run Test ──────────────────────────────────────────────────────────────────
let _runActive = false;
let _runAbort  = null;
let _pass = 0, _fail = 0, _skip = 0, _total = 0;
let _historyRuns = [];
let _runLog    = [];   // {type, step, action, target, result, output, label}
let _runTs     = '';

function toggleRun() {
  if (_runActive) { stopRun(); } else { startRun(); }
}

function openRunPanel() {
  document.getElementById('run-panel').classList.add('open');
}
function closeRun() {
  stopRun();
  document.getElementById('run-panel').classList.remove('open');
}
function clearRun() {
  document.getElementById('run-log').innerHTML = '';
  document.getElementById('run-summary').innerHTML = '';
  _pass = 0; _fail = 0; _skip = 0; _total = 0;
  _runLog = [];
  // hide report button if present
  const rb = document.getElementById('report-btn');
  if (rb) rb.remove();
}

function stopRun() {
  if (_runAbort) { _runAbort.abort(); _runAbort = null; }
  _runActive = false;
  const btn = document.getElementById('run-btn');
  btn.textContent = '▶ Run Test'; btn.classList.remove('running');
  stopDeviceMonitoring();
}

async function startRun() {
  if (steps.length === 0) { toast('No steps to run.', 'error'); return; }
  clearRun(); openRunPanel();
  _runActive = true; _pass = 0; _fail = 0; _skip = 0; _total = 0; _runLog = [];
  _runTs = new Date().toLocaleString();
  const btn = document.getElementById('run-btn');
  btn.textContent = '■ Stop'; btn.classList.add('running');
  startDeviceMonitoring();

  _runAbort = new AbortController();
  const log = document.getElementById('run-log');

  try {
    const res = await fetch('/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ steps }),
      signal: _runAbort.signal,
    });
    if (!res.ok) { toast('Run failed: ' + await res.text(), 'error'); stopRun(); return; }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split('\n\n');
      buf = parts.pop();
      for (const part of parts) {
        const line = part.replace(/^data: /, '').trim();
        if (!line) continue;
        appendLogRow(JSON.parse(line), log);
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') toast('Connection error: ' + e.message, 'error');
  }
  stopRun();
}

function appendLogRow(ev, log) {
  _runLog.push(ev);
  const row = document.createElement('div');

  if (ev.type === 'section') {
    row.className = 'log-section';
    row.dataset.sectionLabel = ev.label || '';
    row.textContent = '▸ ' + (ev.label || '');
    log.appendChild(row); log.scrollTop = log.scrollHeight; return;
  }

  if (ev.type === 'section_result') {
    const secRow = log.querySelector(`.log-section[data-section-label="${CSS.escape(ev.label || '')}"]`);
    if (secRow) {
      const badge = document.createElement('span');
      badge.className = `log-section-result ${ev.result}`;
      badge.textContent = ev.result === 'pass' ? 'PASS' : 'FAIL';
      secRow.appendChild(badge);
    }
    return;
  }

  if (ev.type === 'done') {
    _total = ev.total;
    // Update header summary
    document.getElementById('run-summary').innerHTML =
      `<span style="color:#48bb78">✓ ${ev.passed} passed</span>
       <span style="color:#fc8181">✗ ${ev.failed} failed</span>
       <span style="color:#f6e05e">— ${ev.skipped} skipped</span>
       <span style="color:var(--muted)">of ${ev.total}</span>`;
    // Done banner + report button
    row.className = 'log-done-banner';
    row.innerHTML = `
      Test run complete &nbsp;·&nbsp;
      <strong style="color:${ev.failed>0?'#fc8181':'#48bb78'}">${ev.failed>0?ev.failed+' failure(s)':'All passed'}</strong>
      &nbsp;&nbsp;
      <button class="btn btn-primary" id="report-btn" style="padding:5px 14px;font-size:.78rem" onclick="openReport()">📊 View Report</button>`;
    log.appendChild(row); log.scrollTop = log.scrollHeight; return;
  }

  const result = (ev.result || 'RUN').toUpperCase();
  if (result === 'PASS') _pass++;
  else if (result === 'FAIL') _fail++;
  else if (result === 'SKIP') _skip++;
  const badge = { PASS:'badge-pass', FAIL:'badge-fail', SKIP:'badge-skip', RUN:'badge-run' }[result] || 'badge-run';
  const a = ACTIONS.find(x => x.id === ev.action) || { color:'#555', icon:'' };

  // Timing pills
  let timingHtml = '';
  if (ev.call_setup_ms != null)
    timingHtml += `<span class="timing-pill">📞 ${(ev.call_setup_ms/1000).toFixed(1)}s setup</span>`;
  if (ev.sms_rtt_ms != null)
    timingHtml += `<span class="timing-pill">✉ ${(ev.sms_rtt_ms/1000).toFixed(1)}s RTT</span>`;
  if (ev.duration_ms != null)
    timingHtml += `<span class="timing-pill">⏱ ${(ev.duration_ms/1000).toFixed(1)}s</span>`;

  // Screenshot link
  const ssHtml = ev.screenshot
    ? `<br><a class="screenshot-link" href="/screenshots/${ev.screenshot}" target="_blank">📷 View screenshot</a>`
    : '';

  row.className = 'log-row';
  row.innerHTML = `
    <span class="log-step">${ev.step ?? ''}</span>
    <span class="log-action"><span class="action-pill" style="background:${a.color}">${a.icon} ${esc(ev.action||'')}</span></span>
    <span class="log-target">${esc(ev.target || '')}</span>
    <span class="log-badge ${badge}">${result}</span>
    <span class="log-output">${esc((ev.output || '').slice(0,300))}${timingHtml}${ssHtml}</span>`;
  log.appendChild(row);
  log.scrollTop = log.scrollHeight;

  // live summary
  document.getElementById('run-summary').innerHTML =
    `<span style="color:#48bb78">✓ ${_pass}</span>
     <span style="color:#fc8181">✗ ${_fail}</span>
     <span style="color:#f6e05e">— ${_skip}</span>`;
}

// ── Report modal ──────────────────────────────────────────────────────────────
function openReport() {
  const evs    = _runLog.filter(e => e.type === 'step');
  const done   = _runLog.find(e => e.type === 'done') || {};
  const total  = done.total  || evs.filter(e => e.result !== 'run').length;
  const passed = done.passed || evs.filter(e => e.result === 'pass').length;
  const failed = done.failed || evs.filter(e => e.result === 'fail').length;
  const skipped= done.skipped|| evs.filter(e => e.result === 'skip').length;
  const pct    = total ? Math.round(passed/total*100) : 0;

  document.getElementById('report-ts').textContent = _runTs;

  const passW  = total ? (passed /total*100).toFixed(1) : 0;
  const failW  = total ? (failed /total*100).toFixed(1) : 0;
  const skipW  = total ? (skipped/total*100).toFixed(1) : 0;

  // Build rows — re-walk _runLog to keep sections
  let rows = '';
  let stepNum = 0;
  let currentSection = '';
  for (const ev of _runLog) {
    if (ev.type === 'section') {
      currentSection = ev.label || '';
      rows += `<tr class="sec-row"><td colspan="5">▸ ${esc(currentSection)}</td></tr>`;
      continue;
    }
    if (ev.type !== 'step' || ev.result === 'run') continue;
    stepNum++;
    const r = (ev.result || 'skip').toUpperCase();
    const badge = { PASS:'badge-pass', FAIL:'badge-fail', SKIP:'badge-skip' }[r] || 'badge-skip';
    const a = ACTIONS.find(x => x.id === ev.action) || { color:'#555', icon:'' };
    rows += `<tr>
      <td style="color:var(--muted);text-align:center">${ev.step ?? stepNum}</td>
      <td><span class="action-pill" style="background:${a.color}">${a.icon} ${esc(ev.action||'')}</span></td>
      <td style="color:var(--muted)">${esc(ev.target||'')}</td>
      <td><span class="log-badge ${badge}">${r}</span></td>
      <td style="color:#a0aec0;font-size:.73rem">${esc((ev.output||'').slice(0,300))}</td>
    </tr>`;
  }

  document.getElementById('report-body').innerHTML = `
    <div class="report-stats">
      <div class="stat-card stat-total"><div class="stat-num">${total}</div><div class="stat-label">Total</div></div>
      <div class="stat-card stat-pass" ><div class="stat-num">${passed}</div><div class="stat-label">Passed</div></div>
      <div class="stat-card stat-fail" ><div class="stat-num">${failed}</div><div class="stat-label">Failed</div></div>
      <div class="stat-card stat-skip" ><div class="stat-num">${skipped}</div><div class="stat-label">Skipped</div></div>
    </div>
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
      <div class="report-bar" style="flex:1">
        <div class="bar-pass" style="width:${passW}%"></div>
        <div class="bar-fail" style="width:${failW}%"></div>
        <div class="bar-skip" style="width:${skipW}%"></div>
      </div>
      <span style="font-size:.8rem;color:${pct===100?'#48bb78':pct>60?'#f6e05e':'#fc8181'};font-weight:700;min-width:38px">${pct}%</span>
    </div>
    <table class="report-table">
      <thead><tr><th>#</th><th>Action</th><th>Target</th><th>Result</th><th>Output</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;

  document.getElementById('report-modal').classList.add('open');
}

function closeReport() {
  document.getElementById('report-modal').classList.remove('open');
}

async function exportResults() {
  if (!_runLog.length) { toast('No results to export.', 'error'); return; }
  toast('Exporting…', '');
  const res = await fetch('/export_results', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ log: _runLog, ts: _runTs }),
  });
  if (!res.ok) { toast('Export failed', 'error'); return; }
  const blob = await res.blob();
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  const ts   = new Date().toISOString().slice(0,19).replace(/[:T]/g, '-');
  a.href = url; a.download = `test_results_${ts}.xlsx`;
  a.click();
  URL.revokeObjectURL(url);
  toast('Downloaded ✓', 'success');
}

// ── Saved sections dropdown ───────────────────────────────────────────────────
function toggleSectionsMenu(e) {
  e.stopPropagation();
  const menu = document.getElementById('sec-menu');
  const isOpen = menu.classList.toggle('open');
  if (isOpen) loadSavedSections();
}
document.addEventListener('click', e => {
  const dd = document.getElementById('sec-dropdown');
  if (dd && !dd.contains(e.target)) {
    document.getElementById('sec-menu').classList.remove('open');
  }
});

async function saveSection(idx) {
  const block = [steps[idx]];
  for (let i = idx + 1; i < steps.length; i++) {
    if (steps[i]._isSection) break;
    block.push(steps[i]);
  }
  const label = steps[idx].label || 'Unnamed Section';
  const name = prompt('Save section as:', label);
  if (!name) return;

  // Strip IDs before saving — they'll be regenerated on insert
  const cleanSteps = block.map(s => {
    const c = JSON.parse(JSON.stringify(s));
    delete c.id;
    return c;
  });

  const res = await fetch('/api/sections', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, label, steps: cleanSteps }),
  });
  if (res.ok) {
    toast(`Section "${name}" saved to library.`, 'success');
    loadSavedSections();
  } else {
    toast('Failed to save section.', 'error');
  }
}

async function loadSavedSections() {
  const el = document.getElementById('saved-sections-list');
  try {
    const res = await fetch('/api/sections');
    const data = await res.json();
    const list = data.sections || [];
    if (!list.length) {
      el.innerHTML = '<div style="font-size:.72rem;color:var(--muted)">No saved sections yet.</div>';
      return;
    }
    el.innerHTML = list.map((sec, i) => `
      <div class="saved-sec-card">
        <span class="saved-sec-name" title="${esc(sec.name)}">${esc(sec.name)}</span>
        <button class="saved-sec-btn" onclick="insertSavedSection(${i})">+ Insert</button>
        <button class="saved-sec-btn" style="color:#fc8181" onclick="deleteSavedSection('${esc(sec.name)}')">✕</button>
      </div>`).join('');
    el._sections = list;
  } catch(e) {
    el.innerHTML = '<div style="font-size:.72rem;color:var(--muted)">Load failed.</div>';
  }
}

function insertSavedSection(listIdx) {
  const el = document.getElementById('saved-sections-list');
  const list = el._sections || [];
  const sec = list[listIdx];
  if (!sec) return;

  const copies = (sec.steps || []).map(s => {
    const c = JSON.parse(JSON.stringify(s));
    c.id = stepCounter++;
    return c;
  });
  steps.push(...copies);
  render();
  document.getElementById('sec-menu').classList.remove('open');
  toast(`Section "${sec.name}" inserted.`, 'success');
}

async function deleteSavedSection(name) {
  if (!confirm(`Delete saved section "${name}"?`)) return;
  const res = await fetch('/api/sections/' + encodeURIComponent(name), { method: 'DELETE' });
  if (res.ok) { toast('Deleted.', 'success'); loadSavedSections(); }
}

// ── History ────────────────────────────────────────────────────────────────────
async function openHistory() {
  document.getElementById('history-modal').classList.add('open');
  const body = document.getElementById('history-body');
  body.innerHTML = '<div class="tpl-empty">Loading…</div>';
  try {
    const res = await fetch('/api/history');
    const data = await res.json();
    renderHistoryModal(data.runs || []);
  } catch(e) {
    body.innerHTML = '<div class="tpl-empty">Failed to load history.</div>';
  }
}

function closeHistory() {
  document.getElementById('history-modal').classList.remove('open');
}

async function clearHistory() {
  if (!confirm('Clear all run history?')) return;
  await fetch('/api/history', { method: 'DELETE' });
  renderHistoryModal([]);
  toast('History cleared.', 'success');
}

function renderHistoryModal(runs) {
  const body = document.getElementById('history-body');
  if (!runs.length) {
    body.innerHTML = '<div class="tpl-empty">No run history yet. Run a test to start tracking.</div>';
    return;
  }

  _historyRuns = runs;

  // Trend sparkline — newest on the right, max 20 runs
  const recent = runs.slice(0, 20).reverse();
  const bars = recent.map(r => {
    const pct = r.total ? Math.round(r.passed / r.total * 100) : 0;
    const col  = pct === 100 ? '#48bb78' : pct >= 60 ? '#f6e05e' : '#fc8181';
    const h    = Math.max(4, pct * 0.36);
    const dt   = (r.ts || '').slice(0, 16).replace('T', ' ');
    return `<div title="${pct}% — ${dt}" style="flex:1;height:${h}px;background:${col};border-radius:2px;align-self:flex-end"></div>`;
  }).join('');

  let html = `
    <div style="margin-bottom:16px">
      <div style="font-size:.72rem;color:var(--muted);margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px">Pass rate — last ${recent.length} runs</div>
      <div class="sparkline-bar">${bars}</div>
    </div>
    <div style="display:flex;flex-direction:column;gap:8px">`;

  runs.forEach((r, i) => {
    const pct    = r.total ? Math.round(r.passed / r.total * 100) : 0;
    const pctCol = pct === 100 ? '#48bb78' : pct >= 60 ? '#f6e05e' : '#fc8181';
    const dt     = (r.ts || '').slice(0, 16).replace('T', ' ');
    html += `
      <div class="history-card" onclick="toggleHistoryDetail(this)">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <span style="font-size:.75rem;color:var(--muted);flex-shrink:0">${dt}</span>
          <span style="font-size:.75rem">${r.total} steps</span>
          <span style="margin-left:auto;font-size:.82rem;font-weight:700;color:${pctCol}">${pct}%</span>
          <span style="font-size:.72rem;color:#48bb78">✓ ${r.passed}</span>
          <span style="font-size:.72rem;color:#fc8181">✗ ${r.failed}</span>
          <button onclick="exportHistoryRun(event,${i})" style="background:none;border:1px solid var(--border);border-radius:5px;color:var(--muted);cursor:pointer;font-size:.68rem;padding:2px 7px" title="Export this run to Excel">⬇ Excel</button>
          <span style="font-size:.65rem;color:var(--muted)">▾</span>
        </div>
        <div class="history-detail" style="display:none;margin-top:10px">
          ${renderHistorySteps(r.events || [])}
        </div>
      </div>`;
  });
  html += '</div>';
  body.innerHTML = html;
}

function renderHistorySteps(events) {
  let html = '<table style="width:100%;border-collapse:collapse;font-size:.72rem">';
  let stepNum = 0;
  for (const ev of events) {
    if (ev.type === 'section') {
      html += `<tr><td colspan="3" style="padding:5px 0 2px;color:var(--accent);font-weight:600">▸ ${esc(ev.label||'')}</td></tr>`;
      continue;
    }
    if (ev.type !== 'step' || ev.result === 'run') continue;
    stepNum++;
    const r     = (ev.result || 'skip').toUpperCase();
    const badge = { PASS:'badge-pass', FAIL:'badge-fail', SKIP:'badge-skip' }[r] || 'badge-skip';
    let extra = '';
    if (ev.call_setup_ms != null) extra += ` <span class="timing-pill">📞 ${(ev.call_setup_ms/1000).toFixed(1)}s</span>`;
    if (ev.sms_rtt_ms  != null) extra += ` <span class="timing-pill">✉ ${(ev.sms_rtt_ms/1000).toFixed(1)}s</span>`;
    if (ev.screenshot) extra += ` <a class="screenshot-link" href="/screenshots/${ev.screenshot}" target="_blank">📷</a>`;
    html += `<tr>
      <td style="color:var(--muted);width:24px;text-align:center;padding:2px">${ev.step ?? stepNum}</td>
      <td style="padding:2px 4px">${esc(ev.action||'')} <span style="color:var(--muted)">${esc(ev.target||'')}</span>${extra}</td>
      <td style="text-align:right;padding:2px"><span class="log-badge ${badge}" style="font-size:.62rem">${r}</span></td>
    </tr>`;
  }
  html += '</table>';
  return html;
}

function toggleHistoryDetail(card) {
  const detail = card.querySelector('.history-detail');
  if (detail) detail.style.display = detail.style.display === 'none' ? 'block' : 'none';
}

async function exportHistoryRun(e, idx) {
  e.stopPropagation();
  const r = _historyRuns[idx];
  if (!r) return;
  toast('Exporting…', '');
  try {
    const res = await fetch('/export_results', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ log: r.events || [], ts: r.ts || '' }),
    });
    if (!res.ok) { toast('Export failed', 'error'); return; }
    const blob = await res.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    const ts   = (r.ts || '').slice(0, 19).replace(/[T:]/g, '-');
    a.href = url; a.download = `history_${ts}.xlsx`;
    a.click();
    URL.revokeObjectURL(url);
    toast('Downloaded ✓', 'success');
  } catch(err) {
    toast('Export error: ' + err.message, 'error');
  }
}

// ── Toast ─────────────────────────────────────────────────────────────────────
function toast(msg, type = '') {
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = 'toast show ' + type;
  setTimeout(() => t.className = 'toast', 2800);
}

function esc(s) { return String(s).replace(/"/g,'&quot;'); }

// ── Device float panel ────────────────────────────────────────────────────────
function toggleDeviceFloat() {
  document.getElementById('device-float').classList.toggle('collapsed');
}

// ── Device monitoring during test ─────────────────────────────────────────────
let _devicePollTimer  = null;
let _monitorPollTimer = null;

function startDeviceMonitoring() {
  document.getElementById('device-status-bar').style.display = 'flex';
  const mc = document.getElementById('monitor-col');
  if (mc) mc.classList.add('visible');
  updateDeviceStatusBar();
  updateMonitorCol();
  _devicePollTimer  = setInterval(updateDeviceStatusBar, 2500);
  _monitorPollTimer = setInterval(updateMonitorCol, 3000);
}

function stopDeviceMonitoring() {
  if (_devicePollTimer)  { clearInterval(_devicePollTimer);  _devicePollTimer  = null; }
  if (_monitorPollTimer) { clearInterval(_monitorPollTimer); _monitorPollTimer = null; }
  document.getElementById('device-status-bar').style.display = 'none';
  const mc = document.getElementById('monitor-col');
  if (mc) mc.classList.remove('visible');
}

async function updateMonitorCol() {
  try {
    const res = await fetch('/api/phone_status');
    if (!res.ok) return;
    const data = await res.json();
    const col = document.getElementById('monitor-col');
    if (!col) return;
    col.innerHTML = '';
    PHONES.forEach(name => {
      const d = data[name] || {};
      const online     = !!d.online;
      const callState  = d.call_state ?? 0;
      const callLabel  = ['Idle', 'Ringing…', 'In Call'][callState] || '—';
      const callCls    = ['', 'ringing', 'active'][callState] || '';
      const net        = d.network || '—';
      const bat        = d.battery != null ? d.battery + '%' + (d.charging ? ' ⚡' : '') : '—';
      const sim1       = PHONE_NUMBERS[name + ' SIM1'] || '';
      const sim2       = PHONE_NUMBERS[name + ' SIM2'] || '';
      const nums       = [sim1, sim2].filter(Boolean).join(' / ');
      const div = document.createElement('div');
      div.className = 'mon-phone';
      div.innerHTML = `
        <div class="mon-phone-name">
          <span class="dot ${online ? 'online' : ''}"></span>
          <span>${esc(name)}</span>
          ${nums ? `<span style="color:var(--muted);font-size:.63rem;font-weight:400">${esc(nums)}</span>` : ''}
        </div>
        ${online ? `
          <div class="mon-row"><span class="mon-label">Network</span><span class="mon-val">${esc(net)}</span></div>
          <div class="mon-row"><span class="mon-label">VoLTE</span><span class="mon-val ${d.volte ? 'on' : 'off'}">${d.volte ? 'ON' : 'OFF'}</span></div>
          <div class="mon-row"><span class="mon-label">VoWiFi</span><span class="mon-val ${d.wifi_calling ? 'on' : 'off'}">${d.wifi_calling ? 'ON' : 'OFF'}</span></div>
          <div class="mon-row"><span class="mon-label">Battery</span><span class="mon-val">${esc(bat)}</span></div>
          <div class="mon-row"><span class="mon-label">Call</span><span class="mon-val ${callCls}">${esc(callLabel)}</span></div>
        ` : '<div style="color:var(--muted);font-size:.68rem;padding:4px 0">Offline</div>'}
      `;
      col.appendChild(div);
    });
  } catch(e) {}
}

async function updateDeviceStatusBar() {
  try {
    const res = await fetch('/config');
    const d = await res.json();
    const bar = document.getElementById('device-status-bar');
    bar.innerHTML = '<span style="color:var(--muted);margin-right:4px">Devices:</span>';
    PHONES.forEach(name => {
      const serial = d.devices[name] || '';
      const online = (d.online || []).includes(serial);
      const sim1   = d.phone_numbers[name + ' SIM1'] || '';
      const sim2   = d.phone_numbers[name + ' SIM2'] || '';
      const nums   = [sim1, sim2].filter(Boolean).join(' / ');
      const chip   = document.createElement('span');
      chip.className = 'dsb-chip';
      chip.innerHTML = `<span class="dot ${online ? 'online' : ''}"></span>${name}${nums ? ' · ' + nums : ''}`;
      bar.appendChild(chip);
    });
    // Update float header online count
    const onlineCount = PHONES.filter(n => (d.online||[]).includes(d.devices[n]||'')).length;
    const onlineEl = document.getElementById('device-float-online');
    if (onlineEl) onlineEl.textContent = onlineCount ? `${onlineCount}/${PHONES.length} online` : '';
  } catch(e) {}
}

// ── Silent auto-detect (startup) ──────────────────────────────────────────────
async function applyDetectSilent(devices) {
  const newPhones = {}, newSerials = {};
  devices.forEach((dev, i) => {
    const slot = `Phone${i + 1}`;
    newSerials[slot]          = dev.serial;
    newPhones[slot + ' SIM1'] = dev.number1 || PHONE_NUMBERS[slot + ' SIM1'] || '';
    newPhones[slot + ' SIM2'] = dev.number2 || PHONE_NUMBERS[slot + ' SIM2'] || '';
  });
  PHONES        = Object.keys(newSerials);
  DEVICES_MAP   = newSerials;
  PHONE_NUMBERS = newPhones;
  buildDeviceCards({ devices: DEVICES_MAP, phone_numbers: PHONE_NUMBERS, online: devices.map(d => d.serial) });
  render();
  await saveConfig();
}

// ── Init ──────────────────────────────────────────────────────────────────────
buildPalette();
render();
loadSavedSections();
fetchNetworkOptions();

(async () => {
  const msg = document.getElementById('startup-msg');
  const setMsg = t => { if (msg) msg.textContent = t; };

  // Step 1: detect phones
  setMsg('Detecting phones…');
  try {
    const res = await fetch('/detect');
    const data = await res.json();
    if (data.devices && data.devices.length > 0) {
      setMsg(`Found ${data.devices.length} phone(s) — applying…`);
      await applyDetectSilent(data.devices);
    } else {
      setMsg('No phones detected — loading saved config…');
      await refreshDevices();
    }
  } catch(e) {
    setMsg('Detection failed — loading saved config…');
    await refreshDevices();
  }

  // Step 2: fetch network options from connected phone(s)
  setMsg('Loading network options from phone…');
  await fetchNetworkOptions();

  const overlay = document.getElementById('startup-overlay');
  if (overlay) overlay.style.display = 'none';
  updateDeviceStatusBar();
})();
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(
        HTML,
        actions=ACTIONS,
        phones=DEVICE_NAMES,
        phone_numbers=PHONE_NUMBERS,
        devices_map=DEVICES_MAP,
    )


@app.route("/config", methods=["GET"])
def get_config():
    """Return current devices + phone numbers + online status."""
    # Try to get connected serials via adb
    online = []
    try:
        import adb_controller as adb
        online = adb.get_connected_devices()
    except Exception:
        pass
    return jsonify({
        "devices": DEVICES_MAP,
        "phone_numbers": PHONE_NUMBERS,
        "online": online,
    })


@app.route("/config", methods=["POST"])
def post_config():
    """Save updated devices + phone numbers to config.py."""
    data = request.get_json(force=True)
    new_phones  = data.get("phone_numbers", {})
    new_serials = data.get("devices", {})
    if not new_phones or not new_serials:
        return "Missing phone_numbers or devices", 400
    try:
        _save_config(new_phones, new_serials)
    except Exception as exc:
        return str(exc), 500
    return jsonify({"ok": True})


@app.route("/template")
def template():
    """Return the default template steps as JSON using current config numbers."""
    names = list(PHONE_NUMBERS.keys())
    n1 = names[0] if len(names) > 0 else "Phone1"
    n2 = names[1] if len(names) > 1 else "Phone2"
    p1 = PHONE_NUMBERS.get(n1, "+97699001111")
    p2 = PHONE_NUMBERS.get(n2, "+97699002222")
    steps = [
        {"_isSection": True, "label": f"CALL TEST — {n1} calls {n2}, {n2} answers, verify via call log"},
        {"action": "CALL",         "target": n1, "number": p2,               "value": "",                  "expected": ""},
        {"action": "ANSWER_CALL",  "target": n2, "number": "",               "value": "20",                "expected": "Call answered"},
        {"action": "WAIT",         "target": n1, "number": "10",             "value": "",                  "expected": ""},
        {"action": "END_CALL",     "target": n1, "number": "",               "value": "",                  "expected": ""},
        {"action": "CHECK_CALL",   "target": n1, "number": p2,               "value": "OUTGOING",          "expected": "Call verified"},
        {"action": "CHECK_CALL",   "target": n2, "number": p1,               "value": "INCOMING",          "expected": "Call verified"},
        {"_isSection": True, "label": f"SMS TEST — {n1} sends SMS, verify it arrives on {n2}"},
        {"action": "SMS",          "target": n1, "number": p2,               "value": f"Hello from {n1}",  "expected": ""},
        {"action": "WAIT",         "target": n1, "number": "5",              "value": "",                  "expected": ""},
        {"action": "CHECK_SMS",    "target": n2, "number": p1,               "value": f"Hello from {n1}",  "expected": f"Hello from {n1}"},
        {"_isSection": True, "label": "USSD TEST — Dial USSD on each phone, capture network response"},
        {"action": "USSD",         "target": n1, "number": "*100#",          "value": "",                  "expected": ""},
        {"action": "USSD",         "target": n2, "number": "*100#",          "value": "",                  "expected": ""},
    ]
    return jsonify({"steps": steps})


@app.route("/export", methods=["POST"])
def export_excel():
    """Convert the workflow JSON to an xlsx file and return it."""
    from excel_client import COLUMNS, PASS_FILL, FAIL_FILL, SKIP_FILL
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    data = request.get_json(force=True)
    steps = data.get("steps", [])

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "TestCases"

    header_font  = Font(bold=True, color="FFFFFF")
    header_fill  = PatternFill("solid", fgColor="4472C4")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for col_idx, name in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=name)
        cell.font = header_font; cell.fill = header_fill; cell.alignment = header_align
    ws.row_dimensions[1].height = 28

    widths = [5, 14, 12, 22, 24, 28, 28, 10, 32, 20]
    for col_idx, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    section_font = Font(bold=True, color="FFFFFF")
    section_fills = {
        "CALL": PatternFill("solid", fgColor="375623"),
        "SMS":  PatternFill("solid", fgColor="1F4E79"),
        "USSD": PatternFill("solid", fgColor="7B2C2C"),
    }
    row_fills = {
        "CALL":        PatternFill("solid", fgColor="EBF3E8"),
        "ANSWER_CALL": PatternFill("solid", fgColor="EBF3E8"),
        "END_CALL":    PatternFill("solid", fgColor="EBF3E8"),
        "CHECK_CALL":  PatternFill("solid", fgColor="EBF3E8"),
        "SMS":         PatternFill("solid", fgColor="DDEEFF"),
        "CHECK_SMS":   PatternFill("solid", fgColor="DDEEFF"),
        "USSD":        PatternFill("solid", fgColor="FFF2CC"),
        "SET_NETWORK": PatternFill("solid", fgColor="FFF2CC"),
    }
    default_fill = PatternFill("solid", fgColor="F5F5F5")
    center = Alignment(horizontal="center", vertical="center")
    left   = Alignment(horizontal="left",   vertical="center", wrap_text=True)

    data_row = 2
    step_num = 0

    for s in steps:
        if s.get("_isSection"):
            label   = s.get("label", "Section")
            section = label.split()[0] if label else "CALL"
            ws.merge_cells(start_row=data_row, start_column=1, end_row=data_row, end_column=len(COLUMNS))
            cell = ws.cell(row=data_row, column=1, value=f"  {label}")
            cell.font = section_font
            cell.fill = section_fills.get(section, header_fill)
            cell.alignment = Alignment(vertical="center")
            ws.row_dimensions[data_row].height = 22
            data_row += 1
            continue

        step_num += 1
        action  = s.get("action", "")
        target  = s.get("target", "")
        number  = s.get("number", "")
        value   = s.get("value", "")
        expected = s.get("expected", "")
        fill    = row_fills.get(action, default_fill)

        row_vals = [str(step_num), action, target, number, value, expected, "", "", "", ""]
        for col_idx, val in enumerate(row_vals, start=1):
            cell = ws.cell(row=data_row, column=col_idx, value=val)
            cell.fill = fill
            cell.alignment = center if col_idx in (1, 2, 3, 8) else left
        ws.row_dimensions[data_row].height = 18
        data_row += 1

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}1"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="test_cases.xlsx",
    )


@app.route("/export_results", methods=["POST"])
def export_results():
    """Turn _runLog (sent from browser) into a styled xlsx and return it."""
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter

    data   = request.get_json(force=True)
    log    = data.get("log", [])
    run_ts = data.get("ts", "")

    wb = openpyxl.Workbook()

    # ── Sheet 1: Summary ──────────────────────────────────────────────────────
    ws_sum = wb.active
    ws_sum.title = "Summary"

    steps   = [e for e in log if e.get("type") == "step" and e.get("result") != "run"]
    passed  = sum(1 for e in steps if e.get("result") == "pass")
    failed  = sum(1 for e in steps if e.get("result") == "fail")
    skipped = sum(1 for e in steps if e.get("result") == "skip")
    total   = len(steps)
    pct     = round(passed / total * 100, 1) if total else 0

    H = Font(bold=True, color="FFFFFF", size=11)
    accent_fill  = PatternFill("solid", fgColor="4472C4")
    green_fill   = PatternFill("solid", fgColor="375623")
    red_fill     = PatternFill("solid", fgColor="7B2C2C")
    gray_fill    = PatternFill("solid", fgColor="44475A")
    pass_fill    = PatternFill("solid", fgColor="C6EFCE")
    fail_fill    = PatternFill("solid", fgColor="FFC7CE")
    skip_fill    = PatternFill("solid", fgColor="FFEB9C")
    section_fill = PatternFill("solid", fgColor="2D3250")

    center = Alignment(horizontal="center", vertical="center")
    left   = Alignment(horizontal="left",   vertical="center", wrap_text=True)

    thin = Side(style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    ws_sum.column_dimensions["A"].width = 22
    ws_sum.column_dimensions["B"].width = 18

    summary_rows = [
        ("Run time",    run_ts),
        ("Total steps", total),
        ("Passed",      passed),
        ("Failed",      failed),
        ("Skipped",     skipped),
        ("Pass rate",   f"{pct}%"),
    ]
    fills_sum = [accent_fill, None, green_fill, red_fill, gray_fill, accent_fill]

    for i, (label, val) in enumerate(summary_rows, start=1):
        ca = ws_sum.cell(row=i, column=1, value=label)
        cb = ws_sum.cell(row=i, column=2, value=val)
        f  = fills_sum[i - 1]
        if f:
            ca.fill = f; cb.fill = f
            ca.font = H; cb.font = H
        else:
            ca.font = Font(bold=True)
        ca.alignment = left; cb.alignment = center
        ca.border = border;  cb.border = border
        ws_sum.row_dimensions[i].height = 20

    # ── Sheet 2: Results ─────────────────────────────────────────────────────
    ws = wb.create_sheet("Results")

    COLS = ["#", "Section", "Action", "Target", "Result", "Output"]
    col_widths = [5, 28, 16, 12, 10, 60]

    for ci, (name, w) in enumerate(zip(COLS, col_widths), start=1):
        cell = ws.cell(row=1, column=ci, value=name)
        cell.font  = H
        cell.fill  = accent_fill
        cell.alignment = center
        cell.border = border
        ws.column_dimensions[get_column_letter(ci)].width = w
    ws.row_dimensions[1].height = 24

    ACTION_COLORS = {
        "CALL": "27AE60", "ANSWER_CALL": "2ECC71", "END_CALL": "E74C3C",
        "SMS": "2980B9", "CHECK_SMS": "3498DB", "CHECK_CALL": "16A085",
        "CHECK_VOLTE": "0891B2", "USSD": "8E44AD", "SET_NETWORK": "D35400",
        "WAKE": "F39C12", "WAIT": "95A5A6", "SET_CONFIG": "7F8C8D",
        "GET_CONFIG": "7F8C8D", "AIRPLANE_MODE": "0369A1",
        "OPEN_BROWSER": "0284C7", "SPEEDTEST": "7C3AED", "SET_APN": "B45309",
    }

    data_row = 2
    step_num = 0
    current_section = ""

    for ev in log:
        t = ev.get("type")

        if t == "section":
            current_section = ev.get("label", "")
            ws.merge_cells(start_row=data_row, start_column=1,
                           end_row=data_row, end_column=len(COLS))
            cell = ws.cell(row=data_row, column=1, value=f"  ▸ {current_section}")
            cell.font  = Font(bold=True, color="FFFFFF", italic=True)
            cell.fill  = section_fill
            cell.alignment = left
            cell.border = border
            ws.row_dimensions[data_row].height = 20
            data_row += 1
            continue

        if t != "step" or ev.get("result") == "run":
            continue

        step_num += 1
        action = ev.get("action", "")
        target = ev.get("target", "")
        result = (ev.get("result") or "skip").upper()
        output = ev.get("output", "")

        rf = {"PASS": pass_fill, "FAIL": fail_fill}.get(result, skip_fill)

        action_col = ACTION_COLORS.get(action, "555555")
        action_font = Font(bold=True, color=action_col)

        vals = [step_num, current_section, action, target, result, output]
        for ci, val in enumerate(vals, start=1):
            cell = ws.cell(row=data_row, column=ci, value=val)
            cell.fill   = rf
            cell.border = border
            if ci == 3:
                cell.font = action_font
                cell.alignment = center
            elif ci == 5:
                result_font_color = {"PASS": "375623", "FAIL": "7B2C2C"}.get(result, "7D6608")
                cell.font = Font(bold=True, color=result_font_color)
                cell.alignment = center
            elif ci in (1, 4):
                cell.alignment = center
            else:
                cell.alignment = left
        ws.row_dimensions[data_row].height = 18
        data_row += 1

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLS))}1"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="test_results.xlsx",
    )


@app.route("/run", methods=["POST"])
def run_test():
    """Execute test steps and stream results as SSE."""
    import adb_controller as adb
    import config as cfg
    importlib.reload(cfg)

    data  = request.get_json(force=True)
    steps = data.get("steps", [])

    SUPPORTED = {
        "CALL", "END_CALL", "ANSWER_CALL",
        "SMS", "CHECK_SMS", "CHECK_CALL", "CHECK_VOLTE",
        "USSD", "SET_NETWORK", "SET_CONFIG", "GET_CONFIG",
        "WAKE", "WAIT",
        "AIRPLANE_MODE", "OPEN_BROWSER", "SPEEDTEST", "SET_APN", "DOWNLOAD_FILE",
        "SET_VOLTE", "CHECK_WIFI_CALLING", "SET_WIFI_CALLING", "CHECK_NETWORK",
        "DP_CREATE", "DP_DELETE", "DP_MODIFY", "DP_CHECK", "DP_SELECT",
    }

    def _resolve(target):
        return cfg.DEVICES.get(target)

    def _run_step(s, step_num):
        action     = s.get("action", "").upper()
        target     = s.get("target", "")
        number     = cfg.PHONE_NUMBERS.get(s.get("number", ""), s.get("number", ""))
        value      = s.get("value", "")
        value2     = s.get("value2", "")
        expected   = s.get("expected", "")
        serial     = _resolve(target)

        base = {"type": "step", "step": step_num, "action": action, "target": target}

        if action not in SUPPORTED:
            return {**base, "result": "skip", "output": f"Unsupported action '{action}'"}

        if action == "WAIT":
            secs = float(number) if number else 3.0
            time.sleep(secs)
            return {**base, "result": "pass", "output": f"Waited {secs}s"}

        if action in ("DP_CREATE", "DP_DELETE", "DP_MODIFY", "DP_CHECK", "DP_SELECT"):
            try:
                import data_pkg_api as dp
                msisdn = cfg.PHONE_NUMBERS.get(number, number)
                if action == "DP_CREATE":
                    ok, out = dp.create_package(msisdn=msisdn, params=value)
                elif action == "DP_DELETE":
                    ok, out = dp.delete_package(msisdn=msisdn, package_code=value)
                elif action == "DP_MODIFY":
                    ok, out = dp.modify_package(msisdn=msisdn, package_code=value, new_value=value2)
                elif action == "DP_CHECK":
                    ok, out = dp.check_package(msisdn=msisdn, package_code="")
                else:
                    ok, out = dp.select_package(msisdn=msisdn)
            except Exception as exc:
                ok, out = False, str(exc)
            if ok and expected and expected.lower() not in out.lower():
                result = "fail"
            else:
                result = "pass" if ok else "fail"
            return {**base, "result": result, "output": out[:400]}

        if not serial:
            return {**base, "result": "fail", "output": f"No serial for '{target}'"}

        try:
            if action == "CALL":
                ok, out = adb.make_call(serial, number)
            elif action == "END_CALL":
                ok, out = adb.end_call(serial)
            elif action == "ANSWER_CALL":
                timeout = int(value) if str(value).isdigit() else 30
                ok, out = adb.wait_for_incoming_call(serial, timeout=timeout)
                if ok:
                    ok, out = adb.answer_call(serial)
            elif action == "SMS":
                ok, out = adb.send_sms(serial, number, value)
            elif action == "CHECK_SMS":
                ok, out = adb.check_sms_received(serial, number, expected_text=value)
            elif action == "CHECK_CALL":
                ok, out = adb.check_call_log(serial, number, call_type=value)
            elif action == "CHECK_VOLTE":
                ok, out = adb.check_volte(serial)
            elif action == "USSD":
                ok, out = adb.dial_ussd(serial, number)
            elif action == "SET_NETWORK":
                ok, out = adb.set_network_type(serial, number)
            elif action == "SET_CONFIG":
                ns, _, key = number.partition("/")
                ok, out = adb.set_config(serial, ns, key, value) if key else (False, "Bad namespace/key")
            elif action == "GET_CONFIG":
                ns, _, key = number.partition("/")
                ok, out = adb.get_config(serial, ns, key) if key else (False, "Bad namespace/key")
            elif action == "WAKE":
                ok, out = adb.wake_and_unlock(serial)
            elif action == "AIRPLANE_MODE":
                # Value can be "on", "off", "on:15", "off:10" (optional wait seconds)
                ap_parts = (value or "on").split(":")
                ap_state = ap_parts[0].strip() or "on"
                ap_wait  = int(ap_parts[1]) if len(ap_parts) > 1 and ap_parts[1].isdigit() else 8
                ok, out = adb.set_airplane_mode(serial, ap_state, wait_secs=ap_wait)
            elif action == "OPEN_BROWSER":
                ok, out = adb.open_browser(serial, value or "https://google.com")
            elif action == "SPEEDTEST":
                wait = int(value) if str(value).isdigit() else 60
                ok, out = adb.run_speedtest(serial, wait_secs=wait)
            elif action == "DOWNLOAD_FILE":
                # Value: "https://url/file.bin"  or  "https://url/file.bin:50"
                import re as _re
                dl_url    = (value or "").strip()
                dl_exp_mb = 0.0
                # Only treat trailing :<number> as expected MB, not part of URL
                m_mb = _re.search(r"^(https?://.+):(\d+(?:\.\d+)?)$", dl_url)
                if m_mb:
                    dl_url    = m_mb.group(1)
                    dl_exp_mb = float(m_mb.group(2))
                if not dl_url:
                    ok, out = False, "DOWNLOAD_FILE needs a URL in Value field"
                else:
                    ok, out = adb.download_file(serial, dl_url, expected_mb=dl_exp_mb, timeout=120)
            elif action == "SET_VOLTE":
                ok, out = adb.set_volte(serial, value or "on")
            elif action == "CHECK_WIFI_CALLING":
                ok, out = adb.check_wifi_calling(serial)
            elif action == "SET_WIFI_CALLING":
                ok, out = adb.set_wifi_calling(serial, value or "on")
            elif action == "CHECK_NETWORK":
                ok, out = adb.check_network(serial)
            elif action == "SET_APN":
                ok, out = adb.set_apn(serial, number, value)
            else:
                ok, out = False, "Unhandled"
        except Exception as exc:
            ok, out = False, str(exc)

        # evaluate
        if ok and expected and expected.lower() not in out.lower():
            result = "fail"
        else:
            result = "pass" if ok else "fail"

        ev = {**base, "result": result, "output": out[:400]}

        if result == "fail" and serial:
            try:
                adb.wake_and_unlock(serial)
                time.sleep(0.4)
                ts_s = int(time.time())
                fname = f"fail_step{step_num}_{action}_{ts_s}.png"
                ok_sc, _ = adb.screenshot(serial, os.path.join(SCREENSHOTS_DIR, fname))
                if ok_sc:
                    ev["screenshot"] = fname
                # SET_NETWORK leaves the dialog open on failure so we can screenshot it;
                # close it now that the screenshot is taken
                if action == "SET_NETWORK":
                    adb.press_back(serial)
            except Exception:
                pass

        return ev

    def generate():
        total = passed = failed = skipped = 0
        step_num = 0
        _call_start = None
        _sms_start  = None
        all_events: list = []
        run_ts = datetime.now().isoformat()

        cur_sec_label: str | None = None
        cur_sec_results: list     = []

        def _flush_section():
            nonlocal cur_sec_label, cur_sec_results
            if cur_sec_label is None:
                return None
            sec_pass = bool(cur_sec_results) and all(r == "pass" for r in cur_sec_results)
            ev = {"type": "section_result", "label": cur_sec_label,
                  "result": "pass" if sec_pass else "fail"}
            cur_sec_label   = None
            cur_sec_results = []
            return ev

        for s in steps:
            if s.get("_isSection"):
                flush_ev = _flush_section()
                if flush_ev:
                    all_events.append(flush_ev)
                    yield f"data: {json.dumps(flush_ev)}\n\n"
                cur_sec_label   = s.get("label", "")
                cur_sec_results = []
                ev = {"type": "section", "label": cur_sec_label}
                all_events.append(ev)
                yield f"data: {json.dumps(ev)}\n\n"
                continue

            step_num += 1
            total += 1
            action = s.get("action", "").upper()

            if action == "CALL":
                _call_start = time.time()
            elif action == "SMS":
                _sms_start = time.time()

            yield f"data: {json.dumps({'type':'step','step':step_num,'action':action,'target':s.get('target',''),'result':'run','output':'running…'})}\n\n"

            t0 = time.time()
            ev = _run_step(s, step_num)
            ev["duration_ms"] = int((time.time() - t0) * 1000)

            if action == "ANSWER_CALL" and ev.get("result") == "pass" and _call_start is not None:
                ev["call_setup_ms"] = int((time.time() - _call_start) * 1000)
                _call_start = None
            elif action == "CHECK_SMS" and ev.get("result") == "pass" and _sms_start is not None:
                ev["sms_rtt_ms"] = int((time.time() - _sms_start) * 1000)
                _sms_start = None

            r = ev.get("result", "skip")
            if r == "pass":   passed  += 1
            elif r == "fail": failed  += 1
            else:             skipped += 1
            cur_sec_results.append(r)
            all_events.append(ev)
            yield f"data: {json.dumps(ev)}\n\n"

        # Flush the last section
        flush_ev = _flush_section()
        if flush_ev:
            all_events.append(flush_ev)
            yield f"data: {json.dumps(flush_ev)}\n\n"

        for serial in cfg.DEVICES.values():
            try:
                adb.go_home(serial)
            except Exception:
                pass

        done_ev = {"type": "done", "total": total, "passed": passed, "failed": failed, "skipped": skipped}
        all_events.append(done_ev)
        try:
            _save_run({
                "ts": run_ts,
                "total": total, "passed": passed, "failed": failed, "skipped": skipped,
                "steps": steps,
                "events": all_events,
            })
        except Exception:
            pass

        yield f"data: {json.dumps(done_ev)}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/pair", methods=["POST"])
def wireless_pair():
    """adb pair <ip>:<port> <code>"""
    data = request.get_json()
    ip   = data.get("ip", "").strip()
    port = data.get("port", "").strip()
    code = data.get("code", "").strip()
    if not ip or not port or not code:
        return jsonify({"ok": False, "output": "ip, port, code шаардлагатай."})
    try:
        result = subprocess.run(
            [ADB_PATH, "pair", f"{ip}:{port}", code],
            capture_output=True, text=True, timeout=20
        )
        output = (result.stdout + result.stderr).strip()
        ok = "Successfully paired" in output or "already paired" in output.lower()
        return jsonify({"ok": ok, "output": output})
    except Exception as e:
        return jsonify({"ok": False, "output": str(e)})


@app.route("/connect", methods=["POST"])
def wireless_connect():
    """adb connect <ip>:<port>"""
    data = request.get_json()
    ip   = data.get("ip", "").strip()
    port = data.get("port", "").strip()
    if not ip or not port:
        return jsonify({"ok": False, "output": "ip, port шаардлагатай."})
    try:
        result = subprocess.run(
            [ADB_PATH, "connect", f"{ip}:{port}"],
            capture_output=True, text=True, timeout=15
        )
        output = (result.stdout + result.stderr).strip()
        ok = "connected" in output.lower()
        return jsonify({"ok": ok, "output": output})
    except Exception as e:
        return jsonify({"ok": False, "output": str(e)})


@app.route("/detect")
def detect_devices():
    """Auto-detect connected ADB devices and try to read their phone numbers."""
    import adb_controller as adb
    # Reload config so latest saved values are used
    cfg = _load_config()
    devices_map   = cfg["DEVICES"]        # {"Phone1": "serial1", ...}
    phone_numbers = cfg["PHONE_NUMBERS"]  # {"Phone1": "+97699001111", ...}
    # Reverse map: serial → slot name  (exact match)
    serial_to_slot = {v: k for k, v in devices_map.items()}

    def _ip_of(s):
        """Extract IP from wireless serial '192.168.x.x:port', else None."""
        if re.match(r"\d+\.\d+\.\d+\.\d+:\d+", s):
            return s.split(":")[0]
        return None

    # Also build IP → slot map for wireless fallback
    ip_to_slot = {}
    for slot, ser in devices_map.items():
        ip = _ip_of(ser)
        if ip:
            ip_to_slot[ip] = slot

    serials = adb.get_connected_devices()
    result = []
    for serial in serials:
        # 1. Try reading both SIM slots from device via ADB
        num1, num2 = adb.get_device_phone_numbers(serial)
        # 2. Fallback to saved config if ADB couldn't read them
        if not num1 or not num2:
            slot = serial_to_slot.get(serial)
            if not slot:
                ip = _ip_of(serial)
                if ip:
                    slot = ip_to_slot.get(ip)
            if slot:
                num1 = num1 or phone_numbers.get(slot + " SIM1", "")
                num2 = num2 or phone_numbers.get(slot + " SIM2", "")
        model = adb.get_device_model(serial)
        result.append({"serial": serial, "number1": num1, "number2": num2, "model": model})
    return jsonify({"devices": result})


@app.route("/api/templates", methods=["GET"])
def api_templates_get():
    return jsonify({"templates": _load_templates()})


@app.route("/api/templates", methods=["POST"])
def api_templates_post():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return "name required", 400
    templates = _load_templates()
    templates = [t for t in templates if t.get("name") != name]
    templates.append({
        "name":  name,
        "date":  datetime.now().strftime("%Y-%m-%d"),
        "steps": data.get("steps", []),
    })
    _write_templates(templates)
    return jsonify({"ok": True})


@app.route("/api/templates/<path:name>", methods=["DELETE"])
def api_templates_delete(name):
    templates = _load_templates()
    templates = [t for t in templates if t.get("name") != name]
    _write_templates(templates)
    return jsonify({"ok": True})


@app.route("/api/sections", methods=["GET"])
def api_sections_get():
    return jsonify({"sections": _load_sections()})


@app.route("/api/sections", methods=["POST"])
def api_sections_post():
    data  = request.get_json(force=True)
    name  = (data.get("name") or "").strip()
    if not name:
        return "name required", 400
    sections = _load_sections()
    # Overwrite if same name exists
    sections = [s for s in sections if s.get("name") != name]
    sections.append({
        "name":  name,
        "label": data.get("label", name),
        "steps": data.get("steps", []),
    })
    _write_sections(sections)
    return jsonify({"ok": True})


@app.route("/api/sections/<path:name>", methods=["DELETE"])
def api_sections_delete(name):
    sections = _load_sections()
    sections = [s for s in sections if s.get("name") != name]
    _write_sections(sections)
    return jsonify({"ok": True})


@app.route("/api/history", methods=["GET"])
def api_history_get():
    return jsonify({"runs": _load_history()})


@app.route("/api/history", methods=["DELETE"])
def api_history_delete():
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)
    except Exception:
        pass
    return jsonify({"ok": True})


@app.route("/screenshots/<path:filename>")
def serve_screenshot(filename):
    """Serve a screenshot captured during a failed step."""
    from flask import abort
    safe = os.path.basename(filename)
    path = os.path.join(SCREENSHOTS_DIR, safe)
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype="image/png")


@app.route("/api/network_options")
def api_network_options():
    """Fetch available network-type options from each connected phone (runs in parallel)."""
    import adb_controller as adb
    result: dict = {}
    lock = threading.Lock()

    def _fetch(name: str, serial: str) -> None:
        try:
            opts = adb.get_network_options(serial)
            if opts:
                with lock:
                    result[name] = opts
        except Exception:
            pass

    threads = [threading.Thread(target=_fetch, args=(n, s), daemon=True)
               for n, s in DEVICES_MAP.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    return jsonify(result)


@app.route("/api/package_list")
def api_package_list():
    """Proxy POST request to package list API and return the list."""
    import urllib.request
    url = "http://10.10.55.84:8000/package_list"
    try:
        body = b""  # POST with empty body; add JSON payload here if the API requires it
        req = urllib.request.Request(
            url, data=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read().decode())
        # Accept plain list or {"packages": [...]} / {"data": [...]} / {"list": [...]}
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            items = (raw.get("packages") or raw.get("data") or
                     raw.get("list") or (list(raw.values())[0] if raw else []))
        else:
            items = []
        # If list contains dicts, extract ServiceName (or first string value found)
        packages = []
        for item in items:
            if isinstance(item, str):
                packages.append(item)
            elif isinstance(item, dict):
                name = (item.get("ServiceName") or item.get("serviceName") or
                        item.get("name") or item.get("code") or
                        next((v for v in item.values() if isinstance(v, str)), None))
                if name:
                    packages.append(name)
        return jsonify({"packages": packages})
    except Exception as exc:
        return jsonify({"packages": [], "error": str(exc)}), 200


@app.route("/api/phone_status")
def api_phone_status():
    """Return lightweight phone status (network, VoLTE, VoWiFi, call state, battery) for all devices."""
    import threading
    import adb_controller as adb
    cfg_data = _load_config()
    devices  = cfg_data.get("DEVICES", {})
    results  = {}

    def fetch(name, serial):
        status = adb.get_device_status(serial)
        if status.get("online"):
            try:
                status["call_state"] = adb._get_call_state(serial)
            except Exception:
                status["call_state"] = 0
        else:
            status["call_state"] = 0
        results[name] = status

    threads = [threading.Thread(target=fetch, args=(n, s), daemon=True) for n, s in devices.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    return jsonify(results)


@app.route("/api/monitor")
def api_monitor():
    """Return live status for all configured devices."""
    import adb_controller as adb
    cfg_data = _load_config()
    devices  = cfg_data.get("DEVICES", {})
    results  = []
    for name, serial in devices.items():
        status = adb.get_device_status(serial)
        status["name"] = name
        results.append(status)
    return jsonify({"devices": results})


MONITOR_HTML = """<!DOCTYPE html>
<html>
<head>
<title>Phone Monitor</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1117;--surface:#1a1d27;--surface2:#222636;--border:#2d3147;--text:#e2e8f0;--muted:#64748b;--accent:#818cf8}
@media(prefers-color-scheme:light){:root{--bg:#f0f2f8;--surface:#fff;--surface2:#f4f6fb;--border:#d1d5e8;--text:#1e2035;--muted:#6b7280;--accent:#4f46e5}}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh}
header{background:var(--surface);border-bottom:1px solid var(--border);padding:14px 24px;display:flex;align-items:center;gap:12px}
header h1{font-size:1rem;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px;padding:20px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:18px;transition:border-color .2s}
.card.online{border-color:#2d6a4f}
.card-hdr{display:flex;align-items:center;gap:8px;margin-bottom:14px}
.card-title{font-size:.95rem;font-weight:600}
.dot{width:8px;height:8px;border-radius:50%;background:#4b5563;flex-shrink:0}
.dot.on{background:#48bb78;box-shadow:0 0 6px #48bb78}
.row{display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid var(--border);font-size:.8rem}
.row:last-child{border-bottom:none}
.lbl{color:var(--muted)}
.badge{display:inline-block;padding:2px 8px;border-radius:20px;font-size:.68rem;font-weight:600}
.b4g{background:#1d4ed8;color:#fff}.b5g{background:#7c3aed;color:#fff}
.b3g{background:#0369a1;color:#fff}.b2g{background:#4b5563;color:#fff}
.bunk{background:#374151;color:#9ca3af}
.bat{display:inline-flex;align-items:center;gap:5px}
.bat-bar{width:50px;height:7px;background:var(--surface2);border-radius:4px;overflow:hidden}
.bat-fill{height:100%;border-radius:4px}
.offline{color:var(--muted);font-size:.8rem;padding:6px 0}
.footer{text-align:center;color:var(--muted);font-size:.7rem;padding:12px}
</style>
</head>
<body>
<header>
  <a href="/" style="color:var(--accent);text-decoration:none;font-size:.8rem">&#8592; Builder</a>
  <h1>&#128241; Phone Monitor</h1>
  <span id="status" style="margin-left:auto;font-size:.72rem;color:var(--muted)">Connecting…</span>
</header>
<div class="grid" id="grid"><p style="padding:20px;color:var(--muted)">Loading…</p></div>
<div class="footer" id="footer"></div>
<script>
function netBadge(net){
  if(!net||net==='Unknown')return'<span class="badge bunk">Unknown</span>';
  const n=net.toLowerCase();
  const c=n.includes('5g')?'b5g':n.includes('lte')||n.includes('4g')?'b4g':n.includes('3g')?'b3g':'b2g';
  return`<span class="badge ${c}">${net}</span>`;
}
function batBar(pct,ch){
  const col=pct<=20?'#fc8181':pct<=50?'#f6e05e':'#48bb78';
  return`<span class="bat"><span class="bat-bar"><span class="bat-fill" style="width:${pct}%;background:${col}"></span></span>${pct}%${ch?' &#9889;':''}</span>`;
}
function bool2(v){
  if(v===undefined||v===null)return'<span style="color:var(--muted)">—</span>';
  return v?'<span style="color:#48bb78;font-weight:600">ON</span>':'<span style="color:#fc8181">OFF</span>';
}
async function refresh(){
  try{
    const r=await fetch('/api/monitor');
    const d=await r.json();
    renderGrid(d.devices||[]);
    document.getElementById('status').textContent='Auto-refresh 5s · '+new Date().toLocaleTimeString();
    document.getElementById('footer').textContent='Last updated: '+new Date().toLocaleString();
  }catch(e){
    document.getElementById('status').textContent='Error: '+e.message;
  }
}
function renderGrid(devs){
  const g=document.getElementById('grid');
  if(!devs.length){g.innerHTML='<p style="padding:20px;color:var(--muted)">No devices configured.</p>';return;}
  g.innerHTML=devs.map(d=>{
    const body=d.online?`
      <div class="row"><span class="lbl">Network</span><span>${netBadge(d.network)}</span></div>
      <div class="row"><span class="lbl">Operator</span><span>${d.operator||'—'}</span></div>
      <div class="row"><span class="lbl">Signal</span><span>${d.signal_dbm!=null?d.signal_dbm+' dBm':'—'}</span></div>
      <div class="row"><span class="lbl">Battery</span><span>${d.battery!=null?batBar(d.battery,d.charging):'—'}</span></div>
      <div class="row"><span class="lbl">VoLTE</span><span>${bool2(d.volte)}</span></div>
      <div class="row"><span class="lbl">WiFi Calling</span><span>${bool2(d.wifi_calling)}</span></div>
      <div class="row"><span class="lbl">Model</span><span style="color:var(--muted);font-size:.72rem">${d.model||'—'}</span></div>
    `:'<div class="offline">Device offline — not connected via ADB</div>';
    return`<div class="card ${d.online?'online':''}">
      <div class="card-hdr">
        <div class="dot ${d.online?'on':''}"></div>
        <div class="card-title">${d.name}</div>
        <div style="margin-left:auto;font-size:.65rem;color:var(--muted)">${d.serial||''}</div>
      </div>${body}</div>`;
  }).join('');
}
refresh();
setInterval(refresh,5000);
</script>
</body>
</html>"""


@app.route("/monitor")
def monitor_page():
    return MONITOR_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Test Builder running at http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=True)
