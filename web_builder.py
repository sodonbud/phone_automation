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

from flask import Flask, Response, jsonify, render_template_string, request, send_file, stream_with_context

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")

# ── Load config safely ────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def _load_config():
    try:
        import config as _cfg
        importlib.reload(_cfg)
        return {
            "PHONE_NUMBERS": dict(getattr(_cfg, "PHONE_NUMBERS", {})),
            "DEVICES": dict(getattr(_cfg, "DEVICES", {})),
            "ADB_PATH": getattr(_cfg, "ADB_PATH", "adb"),
        }
    except ImportError:
        return {
            "PHONE_NUMBERS": {"Phone1": "+97699001111", "Phone2": "+97699002222"},
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
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; min-height: 100vh; display: flex; flex-direction: column; }
  header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 12px 20px; display: flex; align-items: center; gap: 12px; }
  header h1 { font-size: 1.1rem; font-weight: 700; color: var(--text); flex: 1; }
  .badge { background: var(--accent); color: #fff; font-size: 0.65rem; padding: 2px 8px; border-radius: 99px; font-weight: 600; letter-spacing: .5px; }
  .toolbar { display: flex; gap: 8px; }
  .btn { border: none; border-radius: 8px; padding: 7px 16px; font-size: 0.82rem; font-weight: 600; cursor: pointer; transition: opacity .15s, transform .1s; }
  .btn:active { transform: scale(.97); }
  .btn-primary { background: var(--accent); color: #fff; }
  .btn-primary:hover { opacity: .88; }
  .btn-ghost { background: transparent; color: var(--muted); border: 1px solid var(--border); }
  .btn-ghost:hover { background: var(--surface2); color: var(--text); }
  .btn-danger { background: var(--danger); color: #fff; }
  .btn-success { background: var(--success); color: #fff; }

  .main { display: flex; flex: 1; overflow: hidden; }

  /* ── Palette ── */
  .palette { width: 260px; min-width: 240px; background: var(--surface); border-right: 1px solid var(--border); overflow-y: auto; padding: 12px 10px; }
  .palette h2 { font-size: .7rem; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); margin-bottom: 8px; padding: 0 4px; }
  .palette-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 5px; margin-bottom: 4px; }
  .action-card {
    display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px;
    padding: 8px 6px; border-radius: 8px;
    cursor: grab; user-select: none;
    border: 1px solid transparent;
    transition: background .15s, transform .1s;
    font-size: .72rem; font-weight: 600; color: #fff;
    text-align: center; min-height: 54px;
  }
  .action-card:active { cursor: grabbing; transform: scale(.97); }
  .action-card .icon { font-size: 1.2rem; flex-shrink: 0; }
  .action-card .info { flex: 1; line-height: 1.2; }
  .action-card .desc { display: none; }

  /* ── Canvas split ── */
  .canvas-wrap { flex: 1; display: flex; flex-direction: column; overflow: hidden; position: relative; }
  .canvas-toolbar { background: var(--surface); border-bottom: 1px solid var(--border); padding: 8px 16px; display: flex; gap: 8px; align-items: center; }
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
  .run-log { flex: 1; overflow-y: auto; padding: 10px 20px; font-family: 'Cascadia Code','Consolas',monospace; font-size: .75rem; }
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
  .device-panel { border-top: 1px solid var(--border); padding: 10px; margin-top: 4px; }
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
</style>
</head>
<body>
<header>
  <h1>📱 Phone Test Builder</h1>
  <span class="badge">ADB Automation</span>
  <div class="toolbar">
    <button class="btn btn-ghost" onclick="addSection()">+ Section</button>
    <button class="btn btn-ghost" onclick="clearAll()">Clear</button>
    <button class="btn btn-ghost" onclick="openTemplateManager()">📁 Templates</button>
    <button class="btn btn-success" onclick="exportExcel()">⬇ Export Excel</button>
    <button class="btn btn-run" id="run-btn" onclick="toggleRun()">▶ Run Test</button>
  </div>
</header>

<div class="main">
  <!-- Palette -->
  <aside class="palette">
    <h2>Actions</h2>
    <div id="palette"></div>
    <!-- Device config panel -->
    <div class="device-panel">
      <h2>Devices <span style="font-size:.6rem;color:var(--accent);cursor:pointer" onclick="refreshDevices()">↻ refresh</span></h2>
      <div id="device-cards"></div>
      <button class="save-config-btn" style="background:var(--surface2);color:var(--accent);border:1px solid var(--accent);margin-bottom:6px" onclick="autoDetect()">🔍 Auto Detect Devices</button>
      <button class="save-config-btn" style="background:var(--surface2);color:#a78bfa;border:1px solid #a78bfa;margin-bottom:6px" onclick="openWirelessModal()">📡 Wireless Pair</button>
      <button class="save-config-btn" onclick="saveConfig()">💾 Save to config.py</button>
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
    <div class="run-log" id="run-log"></div>
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

<div class="toast" id="toast"></div>

<script>
const ACTIONS = {{ actions|tojson }};
let PHONES = {{ phones|tojson }};
let PHONE_NUMBERS = {{ phone_numbers|tojson }};
let DEVICES_MAP = {{ devices_map|tojson }};

let steps = [];
let dragSrc = null;      // palette card action id
let dragStepIdx = null;  // step reorder index
let selectedIdx = null;
let stepCounter = 0;

// ── Device panel ──────────────────────────────────────────────────────────────
async function refreshDevices() {
  const res = await fetch('/config');
  const d = await res.json();
  PHONES = Object.keys(d.devices);
  PHONE_NUMBERS = d.phone_numbers;
  DEVICES_MAP = d.devices;
  buildDeviceCards(d);
}

function buildDeviceCards(d) {
  const el = document.getElementById('device-cards');
  el.innerHTML = '';
  Object.keys(d.devices).forEach(name => {
    const serial  = d.devices[name] || '';
    const number  = d.phone_numbers[name] || '';
    const online  = (d.online || []).includes(serial);
    el.innerHTML += `
      <div class="device-card">
        <div class="device-title">
          <span class="dot ${online ? 'online' : ''}"></span>
          <span>${name}</span>
          <span style="font-size:.6rem;color:var(--muted);margin-left:auto">${online ? '🟢 connected' : '⚫ offline'}</span>
        </div>
        <div class="device-label">Serial</div>
        <input class="device-input" id="serial_${name}" value="${serial}" placeholder="device serial">
        <div class="device-label">Phone Number</div>
        <input class="device-input" id="number_${name}" value="${number}" placeholder="+976...">
      </div>`;
  });
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

    // Assign dropdown + phone number input
    const grid = document.createElement('div');
    grid.style.cssText = 'display:grid;grid-template-columns:1fr 1fr;gap:8px';

    // Slot selector
    const slotWrap = document.createElement('div');
    slotWrap.innerHTML = '<div style="font-size:.65rem;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-bottom:3px">Assign to</div>';
    const sel = document.createElement('select');
    sel.id = `detect-slot-${i}`;
    sel.style.cssText = 'width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:5px 8px;font-size:.8rem;outline:none';
    const optNone = document.createElement('option'); optNone.value = ''; optNone.textContent = '— skip —'; sel.appendChild(optNone);
    PHONES.forEach((p, pi) => {
      const opt = document.createElement('option');
      opt.value = p; opt.textContent = p;
      if (pi === i) opt.selected = true;
      sel.appendChild(opt);
    });
    slotWrap.appendChild(sel);

    // Phone number
    const numWrap = document.createElement('div');
    numWrap.innerHTML = '<div style="font-size:.65rem;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-bottom:3px">Phone Number</div>';
    const numInp = document.createElement('input');
    numInp.id = `detect-num-${i}`;
    numInp.type = 'text';
    numInp.placeholder = '+976…';
    numInp.value = dev.number || '';
    numInp.style.cssText = 'width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:5px 8px;font-size:.8rem;outline:none';
    numWrap.appendChild(numInp);

    grid.appendChild(slotWrap);
    grid.appendChild(numWrap);
    row.appendChild(grid);
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
  let assigned = 0;
  devices.forEach((dev, i) => {
    const slot   = document.getElementById(`detect-slot-${i}`)?.value;
    const number = document.getElementById(`detect-num-${i}`)?.value.trim();
    if (!slot) return;

    // Update sidebar input fields
    const sEl = document.getElementById('serial_' + slot);
    const nEl = document.getElementById('number_' + slot);
    if (sEl) sEl.value = dev.serial;
    if (nEl && number) nEl.value = number;

    // Update in-memory
    DEVICES_MAP[slot]    = dev.serial;
    if (number) PHONE_NUMBERS[slot] = number;
    assigned++;
  });

  closeDetectModal();
  toast(`${assigned} device(s) assigned. Click 💾 Save to config.py to persist.`, 'success');
}

async function saveConfig() {
  const phones = {}, serials = {};
  PHONES.forEach(name => {
    serials[name] = document.getElementById('serial_' + name)?.value || DEVICES_MAP[name] || '';
    phones[name]  = document.getElementById('number_' + name)?.value || PHONE_NUMBERS[name] || '';
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
}

// ── Step data ─────────────────────────────────────────────────────────────────
function newStep(actionId) {
  const a = ACTIONS.find(x => x.id === actionId);
  const def = a.default || {};
  return {
    id: stepCounter++,
    action: actionId,
    target: PHONES[0] || 'Phone1',
    number: def.number ?? (a.id === 'WAIT' ? '5' : ''),
    value: def.value ?? '',
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
      const freeTargetActions = ['CALL', 'ANSWER_CALL'];
      if (freeTargetActions.includes(s.action)) {
        fields.appendChild(makeInput(idx, 'target', 'Target Phone', 'e.g. Phone1'));
      } else {
        fields.appendChild(makeSelect(idx, 'target', 'Target Phone', PHONES));
      }
    }
    if (a.fields.includes('number')) {
      fields.appendChild(makeInput(idx, 'number', 'Number / Code', a.hints.number || ''));
    }
    if (a.fields.includes('value')) {
      fields.appendChild(makeInput(idx, 'value', 'Value', a.hints.value || '', a.id === 'SMS'));
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
    const num  = s.number  ? `<code style="font-size:.7rem;opacity:.9">${esc(s.number)}</code>`  : '<span style="color:var(--muted)">—</span>';
    const val  = s.value   ? esc(s.value)   : '<span style="color:var(--muted)">—</span>';
    const exp  = s.expected ? esc(s.expected) : '<span style="color:var(--muted)">—</span>';
    html += `<tr>
      <td class="step-num-cell">${stepNum}</td>
      <td>${pill}</td>
      <td>${esc(s.target || '')}</td>
      <td>${num}</td>
      <td>${val}</td>
      <td>${exp}</td>
    </tr>`;
  });

  html += '</tbody></table>';
  el.innerHTML = html;
}

function makeSelect(idx, field, label, options) {
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
  sel.addEventListener('change', e => { steps[idx][field] = e.target.value; });
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

// ── Load template ─────────────────────────────────────────────────────────────
// ── Template manager ─────────────────────────────────────────────────────────
const TPL_KEY = 'phone_test_templates';

function _loadTpls() {
  try { return JSON.parse(localStorage.getItem(TPL_KEY) || '[]'); } catch { return []; }
}
function _saveTpls(list) {
  localStorage.setItem(TPL_KEY, JSON.stringify(list));
}

function openTemplateManager() {
  renderTplList();
  document.getElementById('tpl-name-input').value = '';
  document.getElementById('tpl-modal').classList.add('open');
}
function closeTplModal() {
  document.getElementById('tpl-modal').classList.remove('open');
}

function renderTplList() {
  const list = document.getElementById('tpl-list');
  const tpls = _loadTpls();
  list.innerHTML = '';

  if (tpls.length === 0) {
    list.innerHTML = '<div class="tpl-empty">No saved templates yet.<br>Build a workflow and save it below.</div>';
  }

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

  tpls.forEach((tpl, i) => {
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

    const delBtn = document.createElement('button');
    delBtn.className = 'btn btn-danger'; delBtn.style.cssText = 'padding:4px 8px;font-size:.75rem';
    delBtn.textContent = '✕';
    delBtn.addEventListener('click', () => {
      const updated = _loadTpls(); updated.splice(i, 1); _saveTpls(updated); renderTplList();
    });

    item.appendChild(loadBtn);
    item.appendChild(delBtn);
    list.appendChild(item);
  });
}

function saveTpl() {
  const name = document.getElementById('tpl-name-input').value.trim();
  if (!name) { toast('Enter a template name.', 'error'); return; }
  if (steps.length === 0) { toast('No steps to save.', 'error'); return; }
  const tpls = _loadTpls();
  const existing = tpls.findIndex(t => t.name === name);
  const entry = { name, date: new Date().toLocaleDateString(), steps: JSON.parse(JSON.stringify(steps)) };
  if (existing >= 0) { tpls[existing] = entry; } else { tpls.push(entry); }
  _saveTpls(tpls);
  renderTplList();
  document.getElementById('tpl-name-input').value = '';
  toast(`Template "${name}" saved.`, 'success');
}

// ── Run Test ──────────────────────────────────────────────────────────────────
let _runActive = false;
let _runAbort  = null;
let _pass = 0, _fail = 0, _skip = 0, _total = 0;
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
}

async function startRun() {
  if (steps.length === 0) { toast('No steps to run.', 'error'); return; }
  clearRun(); openRunPanel();
  _runActive = true; _pass = 0; _fail = 0; _skip = 0; _total = 0; _runLog = [];
  _runTs = new Date().toLocaleString();
  const btn = document.getElementById('run-btn');
  btn.textContent = '■ Stop'; btn.classList.add('running');

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
    row.textContent = '▸ ' + (ev.label || '');
    log.appendChild(row); log.scrollTop = log.scrollHeight; return;
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
  row.className = 'log-row';
  row.innerHTML = `
    <span class="log-step">${ev.step ?? ''}</span>
    <span class="log-action"><span class="action-pill" style="background:${a.color}">${a.icon} ${esc(ev.action||'')}</span></span>
    <span class="log-target">${esc(ev.target || '')}</span>
    <span class="log-badge ${badge}">${result}</span>
    <span class="log-output">${esc((ev.output || '').slice(0,300))}</span>`;
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

// ── Toast ─────────────────────────────────────────────────────────────────────
function toast(msg, type = '') {
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = 'toast show ' + type;
  setTimeout(() => t.className = 'toast', 2800);
}

function esc(s) { return String(s).replace(/"/g,'&quot;'); }

// ── Init ──────────────────────────────────────────────────────────────────────
buildPalette();
buildDeviceCards({ devices: DEVICES_MAP, phone_numbers: PHONE_NUMBERS, online: [] });
render();
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
    }

    def _resolve(target):
        return cfg.DEVICES.get(target)

    def _run_step(s, step_num):
        action     = s.get("action", "").upper()
        target     = s.get("target", "")
        number     = s.get("number", "")
        value      = s.get("value", "")
        expected   = s.get("expected", "")
        serial     = _resolve(target)

        base = {"type": "step", "step": step_num, "action": action, "target": target}

        if action not in SUPPORTED:
            return {**base, "result": "skip", "output": f"Unsupported action '{action}'"}

        if action == "WAIT":
            secs = float(number) if number else 3.0
            time.sleep(secs)
            return {**base, "result": "pass", "output": f"Waited {secs}s"}

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

        return {**base, "result": result, "output": out[:400]}

    def generate():
        total = passed = failed = skipped = 0
        step_num = 0
        for s in steps:
            if s.get("_isSection"):
                yield f"data: {json.dumps({'type':'section','label':s.get('label','')})}\n\n"
                continue
            step_num += 1
            total += 1
            # emit "running" indicator first
            yield f"data: {json.dumps({'type':'step','step':step_num,'action':s.get('action',''),'target':s.get('target',''),'result':'run','output':'running…'})}\n\n"
            ev = _run_step(s, step_num)
            r = ev.get("result", "skip")
            if r == "pass":   passed  += 1
            elif r == "fail": failed  += 1
            else:             skipped += 1
            yield f"data: {json.dumps(ev)}\n\n"

        # Send all devices back to home screen
        for name, serial in cfg.DEVICES.items():
            try:
                adb.go_home(serial)
            except Exception:
                pass

        yield f"data: {json.dumps({'type':'done','total':total,'passed':passed,'failed':failed,'skipped':skipped})}\n\n"

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
        # 1. Try reading from device via ADB
        number = adb.get_device_phone_number(serial)
        # 2. Exact serial match in config
        if not number:
            slot = serial_to_slot.get(serial)
            if slot:
                number = phone_numbers.get(slot, "")
        # 3. IP-only match (port changes on every wireless reconnect)
        if not number:
            ip = _ip_of(serial)
            if ip:
                slot = ip_to_slot.get(ip)
                if slot:
                    number = phone_numbers.get(slot, "")
        model = adb.get_device_model(serial)
        result.append({"serial": serial, "number": number, "model": model})
    return jsonify({"devices": result})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Test Builder running at http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
