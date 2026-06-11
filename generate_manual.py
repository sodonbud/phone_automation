"""Generate user manual as .docx and .pptx for the Phone Test Builder web UI."""

from __future__ import annotations

# ── DOCX ──────────────────────────────────────────────────────────────────────
from docx import Document
from docx.shared import Pt, RGBColor, Inches, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

# ── PPTX ──────────────────────────────────────────────────────────────────────
from pptx import Presentation
from pptx.util import Inches as PInches, Pt as PPt, Emu
from pptx.dml.color import RGBColor as PRGBColor
from pptx.enum.text import PP_ALIGN

# ── Colors ────────────────────────────────────────────────────────────────────
DARK   = RGBColor(0x0F, 0x11, 0x17)
ACCENT = RGBColor(0x5C, 0x6E, 0xF8)
GREEN  = RGBColor(0x27, 0xAE, 0x60)
RED    = RGBColor(0xE7, 0x4C, 0x3C)
YELLOW = RGBColor(0xF3, 0x9C, 0x12)
GREY   = RGBColor(0x71, 0x80, 0x96)
WHITE  = RGBColor(0xFF, 0xFF, 0xFF)
LIGHT  = RGBColor(0xE2, 0xE8, 0xF0)

ACTIONS = [
    ("📞", "CALL",         "Make Call",       "Dial a phone number from the target device.",                         "Target Phone, Number/Code (destination number)"),
    ("📲", "ANSWER_CALL",  "Answer Call",     "Wait for incoming ring then answer automatically.",                   "Target Phone, Value (timeout seconds, e.g. 20)"),
    ("📵", "END_CALL",     "End Call",        "Hang up the active call.",                                            "Target Phone"),
    ("⏳", "WAIT",         "Wait",            "Pause execution for N seconds between steps.",                        "Number/Code (seconds, e.g. 5)"),
    ("💬", "SMS",          "Send SMS",        "Send a text message from the target device.",                         "Target Phone, Number/Code (recipient), Value (message text)"),
    ("🔍", "CHECK_SMS",    "Check SMS",       "Verify an SMS was received from a given sender.",                     "Target Phone, Number/Code (sender), Value (expected text)"),
    ("📋", "CHECK_CALL",   "Check Call Log",  "Verify a call entry exists in the call log.",                         "Target Phone, Number/Code (other party), Value (INCOMING / OUTGOING)"),
    ("📶", "CHECK_VOLTE",  "Check VoLTE",     "Verify VoLTE is active via IMS registration and network type.",       "Target Phone, Expected Result (e.g. VoLTE ACTIVE)"),
    ("📡", "USSD",         "Dial USSD",       "Dial a USSD code and capture the network response.",                  "Target Phone, Number/Code (e.g. *100#)"),
    ("📶", "SET_NETWORK",  "Set Network",     "Change the preferred network mode.",                                  "Target Phone, Number/Code (2G / 3G / 4G / 5G / AUTO)"),
    ("💡", "WAKE",         "Wake Screen",     "Wake and unlock the device screen.",                                  "Target Phone"),
    ("⚙️", "SET_CONFIG",   "Set Config",      "Set an Android settings key via ADB.",                               "Target Phone, Number/Code (namespace/key), Value"),
    ("🔧", "GET_CONFIG",   "Get Config",      "Read an Android settings key via ADB.",                               "Target Phone, Number/Code (namespace/key)"),
]

# ═══════════════════════════════════════════════════════════════════════════════
# DOCX
# ═══════════════════════════════════════════════════════════════════════════════

def set_cell_bg(cell, hex_color: str):
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    tcPr.append(shd)

def heading(doc, text, level=1, color=None):
    p = doc.add_heading(text, level=level)
    if color:
        for run in p.runs:
            run.font.color.rgb = color
    return p

def para(doc, text, bold=False, italic=False, size=11, color=None, indent=0):
    p = doc.add_paragraph()
    if indent:
        p.paragraph_format.left_indent = Cm(indent)
    run = p.add_run(text)
    run.bold = bold
    run.italic = italic
    run.font.size = Pt(size)
    if color:
        run.font.color.rgb = color
    return p

def bullet(doc, text, level=0):
    p = doc.add_paragraph(text, style="List Bullet")
    p.paragraph_format.left_indent = Cm(level * 0.5 + 0.5)
    return p

def make_docx(path: str):
    doc = Document()

    # Page margins
    for section in doc.sections:
        section.top_margin    = Cm(2)
        section.bottom_margin = Cm(2)
        section.left_margin   = Cm(2.5)
        section.right_margin  = Cm(2.5)

    # ── Cover ─────────────────────────────────────────────────────────
    doc.add_paragraph()
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = title.add_run("📱 Phone Test Builder")
    r.font.size = Pt(28)
    r.font.bold = True
    r.font.color.rgb = ACCENT

    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r2 = sub.add_run("Web UI User Manual")
    r2.font.size = Pt(16)
    r2.font.color.rgb = GREY

    doc.add_paragraph()
    info = doc.add_paragraph()
    info.alignment = WD_ALIGN_PARAGRAPH.CENTER
    info.add_run("ADB-based Phone Automation System  ·  Version 1.0")

    doc.add_page_break()

    # ── 1. Overview ───────────────────────────────────────────────────
    heading(doc, "1. Overview", 1, ACCENT)
    para(doc, (
        "Phone Test Builder is a web-based interface that lets you design, run, and review "
        "automated phone test scenarios — without editing Excel files or running command-line scripts. "
        "It communicates with Android devices over ADB (Android Debug Bridge) and supports "
        "calls, SMS, USSD, VoLTE checks, and network type changes."
    ))

    heading(doc, "Key Features", 2)
    for f in [
        "Drag-and-drop workflow builder — compose test steps visually",
        "Live preview table — see all steps as a formatted table in real time",
        "Device configuration panel — edit phone numbers and serial numbers directly in the browser",
        "Run tests from the browser — stream live results step by step",
        "View Report — summary with pass/fail stats and a progress bar after each run",
        "Named Templates — save and reload frequently used workflows",
        "Export to Excel — generate test_cases.xlsx compatible with the CLI runner",
        "Home screen reset — all devices return to home screen after the test finishes",
    ]:
        bullet(doc, f)

    # ── 2. Getting Started ────────────────────────────────────────────
    doc.add_page_break()
    heading(doc, "2. Getting Started", 1, ACCENT)

    heading(doc, "2.1  Prerequisites", 2)
    for req in [
        "Python 3.10 or later",
        "ADB (Android Debug Bridge) installed and added to PATH",
        "USB debugging enabled on each Android device",
        "Devices connected via USB (verify with: adb devices)",
    ]:
        bullet(doc, req)

    heading(doc, "2.2  Installation", 2)
    para(doc, "Install required Python packages:")
    para(doc, "    pip install flask openpyxl", bold=True, size=10)
    doc.add_paragraph()
    para(doc, "Edit config.py to set your device serial numbers and phone numbers:")
    para(doc, (
        '    DEVICES = {"Phone1": "YOUR_SERIAL_1", "Phone2": "YOUR_SERIAL_2"}\n'
        '    PHONE_NUMBERS = {"Phone1": "+976XXXXXXXX", "Phone2": "+976XXXXXXXX"}'
    ), size=10)

    heading(doc, "2.3  Starting the Web UI", 2)
    para(doc, "Option A — Double-click start_web.bat (Windows)")
    para(doc, "Option B — Run from terminal:")
    para(doc, "    python web_builder.py", bold=True, size=10)
    para(doc, "Then open your browser and go to:  http://localhost:5000")

    # ── 3. Interface Layout ───────────────────────────────────────────
    doc.add_page_break()
    heading(doc, "3. Interface Layout", 1, ACCENT)

    tbl = doc.add_table(rows=1, cols=2)
    tbl.style = "Table Grid"
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    hdr = tbl.rows[0].cells
    for cell, txt in zip(hdr, ["Area", "Description"]):
        set_cell_bg(cell, "4472C4")
        run = cell.paragraphs[0].add_run(txt)
        run.font.bold = True
        run.font.color.rgb = WHITE

    rows_data = [
        ("Header Bar",     "Title, toolbar buttons: + Section, Clear, 📁 Templates, ⬇ Export Excel, ▶ Run Test"),
        ("Left Sidebar — Actions palette", "Draggable action cards. Double-click or drag onto the canvas to add."),
        ("Left Sidebar — Devices panel",   "Edit Phone1/Phone2 serial numbers and phone numbers. Shows online/offline status. Save to config.py."),
        ("Center — Editor pane",           "The workflow canvas. Drag actions here, reorder steps, edit fields inline."),
        ("Right — Preview pane",           "Live read-only table: step number, action, target, number/code, value, expected result."),
        ("Bottom — Run panel",             "Appears when ▶ Run Test is clicked. Streams live results with PASS/FAIL badges."),
    ]
    for area, desc in rows_data:
        row = tbl.add_row().cells
        row[0].paragraphs[0].add_run(area).bold = True
        row[1].paragraphs[0].add_run(desc)

    # ── 4. Building a Workflow ────────────────────────────────────────
    doc.add_page_break()
    heading(doc, "4. Building a Workflow", 1, ACCENT)

    heading(doc, "4.1  Adding Steps", 2)
    bullet(doc, "Drag an action card from the left palette and drop it onto the canvas.")
    bullet(doc, "Or double-click any action card to append it at the end.")
    bullet(doc, "Fill in the Target Phone, Number/Code, Value, and Expected Result fields.")

    heading(doc, "4.2  Sections (Dividers)", 2)
    bullet(doc, 'Click "+ Section" in the toolbar to insert a section header row.')
    bullet(doc, "Click on the section title text to rename it (type directly in the field).")
    bullet(doc, 'Click the "✕" button on the right of the section row to delete it.')

    heading(doc, "4.3  Reordering Steps", 2)
    bullet(doc, "Drag a step by its colored header bar to a new position.")
    bullet(doc, "Or click a step to select it, then use the ▲ Up / ▼ Down buttons in the toolbar.")

    heading(doc, "4.4  Duplicating and Deleting", 2)
    bullet(doc, "⎘ button on a step — duplicate that step.")
    bullet(doc, "✕ button on a step — delete that step.")
    bullet(doc, "Select a step then click ✕ Delete in the toolbar to delete it.")

    heading(doc, "4.5  Step Fields", 2)
    tbl2 = doc.add_table(rows=1, cols=2)
    tbl2.style = "Table Grid"
    for cell, txt in zip(tbl2.rows[0].cells, ["Field", "Meaning"]):
        set_cell_bg(cell, "4472C4")
        cell.paragraphs[0].add_run(txt).font.bold = True
        cell.paragraphs[0].runs[0].font.color.rgb = WHITE

    for field, meaning in [
        ("Target Phone",    "Which device performs the action (Phone1 or Phone2). Dropdown shows the phone number."),
        ("Number / Code",   "The phone number to call/SMS, USSD code, wait duration, or settings key."),
        ("Value",           "SMS message body, answer timeout, network type, settings value."),
        ("Expected Result", "Text that must appear in the action output for the step to PASS. Leave blank to always PASS if action succeeds."),
    ]:
        r = tbl2.add_row().cells
        r[0].paragraphs[0].add_run(field).bold = True
        r[1].paragraphs[0].add_run(meaning)

    # ── 5. Actions Reference ──────────────────────────────────────────
    doc.add_page_break()
    heading(doc, "5. Actions Reference", 1, ACCENT)

    tbl3 = doc.add_table(rows=1, cols=3)
    tbl3.style = "Table Grid"
    tbl3.columns[0].width = Cm(3)
    tbl3.columns[1].width = Cm(5)
    tbl3.columns[2].width = Cm(8)
    for cell, txt in zip(tbl3.rows[0].cells, ["Action", "Description", "Required Fields"]):
        set_cell_bg(cell, "4472C4")
        cell.paragraphs[0].add_run(txt).font.bold = True
        cell.paragraphs[0].runs[0].font.color.rgb = WHITE

    for icon, action_id, label, desc, fields in ACTIONS:
        row = tbl3.add_row().cells
        row[0].paragraphs[0].add_run(f"{icon} {action_id}").bold = True
        row[1].paragraphs[0].add_run(desc)
        row[2].paragraphs[0].add_run(fields)

    # ── 6. Device Configuration ───────────────────────────────────────
    doc.add_page_break()
    heading(doc, "6. Device Configuration", 1, ACCENT)
    para(doc, (
        "The Devices panel at the bottom of the left sidebar shows the current device settings "
        "loaded from config.py."
    ))

    heading(doc, "How to update device settings:", 2)
    bullet(doc, "Edit the Serial field — paste the ADB serial number (from adb devices).")
    bullet(doc, "Edit the Phone Number field — enter the SIM number in E.164 format (+976...).")
    bullet(doc, 'Click "💾 Save to config.py" — overwrites the DEVICES and PHONE_NUMBERS blocks in config.py.')
    bullet(doc, 'Click "↻ refresh" — re-queries ADB to update the online/offline status dot.')

    para(doc, "")
    para(doc, "🟢 Green dot = device connected via ADB    ⚫ Grey dot = device not detected", italic=True)

    # ── 7. Running Tests ──────────────────────────────────────────────
    doc.add_page_break()
    heading(doc, "7. Running Tests", 1, ACCENT)

    heading(doc, "7.1  Starting a Run", 2)
    bullet(doc, 'Click "▶ Run Test" in the header. The run panel slides up covering the canvas.')
    bullet(doc, "Each step shows a blue running… badge while executing.")
    bullet(doc, "Results update live: ✓ PASS (green), ✗ FAIL (red), — SKIP (yellow).")
    bullet(doc, "The live counter in the panel header tracks pass/fail/skip totals.")

    heading(doc, "7.2  Stopping a Run", 2)
    bullet(doc, 'Click "■ Stop" (the Run button changes label while running) to abort mid-run.')
    bullet(doc, 'Click "✕" in the run panel header to close the panel.')

    heading(doc, "7.3  After the Run", 2)
    bullet(doc, "All devices automatically return to their home screen.")
    bullet(doc, 'A "Test run complete" banner appears at the bottom of the log.')
    bullet(doc, 'Click "📊 View Report" to open the full report modal.')

    # ── 8. Viewing the Report ─────────────────────────────────────────
    heading(doc, "8. Viewing the Report", 1, ACCENT)

    heading(doc, "Report sections:", 2)
    bullet(doc, "Stat cards: Total / Passed / Failed / Skipped counts.")
    bullet(doc, "Progress bar: green (pass) + red (fail) + yellow (skip) with pass-rate %.")
    bullet(doc, "Results table: step number, action pill, target phone, PASS/FAIL/SKIP badge, output text.")
    bullet(doc, "Section dividers are shown as accent-colored header rows in the table.")
    bullet(doc, "Click anywhere outside the modal or press ✕ to close.")

    # ── 9. Templates ──────────────────────────────────────────────────
    doc.add_page_break()
    heading(doc, "9. Templates", 1, ACCENT)
    para(doc, (
        "Templates let you save and reload named workflows. They are stored in your browser's "
        "localStorage — no server or file needed."
    ))

    heading(doc, "Saving a template:", 2)
    bullet(doc, 'Click "📁 Templates" in the header.')
    bullet(doc, 'Type a name in the "Template name…" field at the bottom of the modal.')
    bullet(doc, 'Click "💾 Save current" or press Enter.')
    bullet(doc, "If a template with the same name already exists it will be overwritten.")

    heading(doc, "Loading a template:", 2)
    bullet(doc, 'Click "📁 Templates".')
    bullet(doc, "Click Load next to any saved template (or the built-in Default Template).")
    bullet(doc, "Confirm the prompt to replace your current workflow.")

    heading(doc, "Deleting a template:", 2)
    bullet(doc, "Click ✕ next to the template name in the list.")

    # ── 10. Export to Excel ───────────────────────────────────────────
    doc.add_page_break()
    heading(doc, "10. Export to Excel", 1, ACCENT)
    para(doc, (
        'Click "⬇ Export Excel" in the header. The browser downloads test_cases.xlsx. '
        "This file is directly compatible with the CLI runner (python main.py) and contains "
        "all steps with color-coded rows and section headers."
    ))

    heading(doc, "Excel columns:", 2)
    for col, meaning in [
        ("Step",            "Sequential step number"),
        ("Action",          "Action keyword (CALL, SMS, CHECK_VOLTE, etc.)"),
        ("Target Phone",    "Phone1 or Phone2"),
        ("Number/Code",     "Destination number, USSD code, wait duration, etc."),
        ("Value",           "Message body, timeout, network type, etc."),
        ("Expected Result", "Text that must appear in the output for PASS"),
        ("Actual Result",   "Filled in automatically after CLI run"),
        ("Pass/Fail",       "PASS / FAIL / SKIP — color coded"),
        ("Notes",           "Action description (auto-filled from notes map)"),
        ("Timestamp",       "Date/time the step was executed"),
    ]:
        bullet(doc, f"{col} — {meaning}")

    # ── 11. Typical Test Workflow ─────────────────────────────────────
    doc.add_page_break()
    heading(doc, "11. Typical Test Workflow Example", 1, ACCENT)

    steps_example = [
        ("Section",      "CALL TEST",   "",       "",                  ""),
        ("CALL",         "Phone1",      "Phone2 number", "",           ""),
        ("ANSWER_CALL",  "Phone2",      "",       "20",                "Call answered"),
        ("CHECK_VOLTE",  "Phone1",      "",       "",                  "VoLTE ACTIVE"),
        ("CHECK_VOLTE",  "Phone2",      "",       "",                  "VoLTE ACTIVE"),
        ("WAIT",         "Phone1",      "10",     "",                  ""),
        ("END_CALL",     "Phone1",      "",       "",                  ""),
        ("CHECK_CALL",   "Phone1",      "Phone2 number", "OUTGOING",   "Call verified"),
        ("CHECK_CALL",   "Phone2",      "Phone1 number", "INCOMING",   "Call verified"),
        ("Section",      "SMS TEST",    "",       "",                  ""),
        ("SMS",          "Phone1",      "Phone2 number", "Hello",      ""),
        ("WAIT",         "Phone1",      "5",      "",                  ""),
        ("CHECK_SMS",    "Phone2",      "Phone1 number", "Hello",      "Hello"),
    ]

    tbl4 = doc.add_table(rows=1, cols=5)
    tbl4.style = "Table Grid"
    for cell, txt in zip(tbl4.rows[0].cells, ["Action", "Target", "Number/Code", "Value", "Expected"]):
        set_cell_bg(cell, "4472C4")
        cell.paragraphs[0].add_run(txt).font.bold = True
        cell.paragraphs[0].runs[0].font.color.rgb = WHITE

    for action, target, number, value, expected in steps_example:
        row = tbl4.add_row().cells
        if action == "Section":
            set_cell_bg(row[0], "375623")
            p = row[0].paragraphs[0]
            r = p.add_run(f"▸ {target}")
            r.bold = True; r.font.color.rgb = WHITE
            # merge cells
            for i in range(1, 5):
                set_cell_bg(row[i], "375623")
        else:
            row[0].paragraphs[0].add_run(action).bold = True
            row[1].paragraphs[0].add_run(target)
            row[2].paragraphs[0].add_run(number)
            row[3].paragraphs[0].add_run(value)
            row[4].paragraphs[0].add_run(expected)

    doc.save(path)
    print(f"✓ Word document saved: {path}")


# ═══════════════════════════════════════════════════════════════════════════════
# PPTX
# ═══════════════════════════════════════════════════════════════════════════════

PA = PRGBColor(0x5C, 0x6E, 0xF8)   # accent
PD = PRGBColor(0x0F, 0x11, 0x17)   # dark bg
PW = PRGBColor(0xFF, 0xFF, 0xFF)   # white
PM = PRGBColor(0x71, 0x80, 0x96)   # muted
PG = PRGBColor(0x27, 0xAE, 0x60)   # green
PR = PRGBColor(0xE7, 0x4C, 0x3C)   # red
PY = PRGBColor(0xF3, 0x9C, 0x12)   # yellow
PS = PRGBColor(0x1A, 0x1D, 0x27)   # surface


def _bg(slide, color: PRGBColor):
    bg = slide.background
    fill = bg.fill
    fill.solid()
    fill.fore_color.rgb = color


def _txt(tf, text, size=18, bold=False, color=PW, align=PP_ALIGN.LEFT):
    tf.text = ""
    p = tf.paragraphs[0]
    p.alignment = align
    run = p.add_run()
    run.text = text
    run.font.size = PPt(size)
    run.font.bold = bold
    run.font.color.rgb = color


def _add_para(tf, text, size=14, bold=False, color=PW, bullet_char=""):
    p = tf.add_paragraph()
    p.alignment = PP_ALIGN.LEFT
    run = p.add_run()
    run.text = (bullet_char + " " if bullet_char else "") + text
    run.font.size = PPt(size)
    run.font.bold = bold
    run.font.color.rgb = color


def _box(slide, left, top, width, height, text, size=14, bold=False,
         bg=None, fg=PW, align=PP_ALIGN.LEFT):
    txb = slide.shapes.add_textbox(
        PInches(left), PInches(top), PInches(width), PInches(height)
    )
    tf = txb.text_frame
    tf.word_wrap = True
    if bg:
        txb.fill.solid()
        txb.fill.fore_color.rgb = bg
    _txt(tf, text, size=size, bold=bold, color=fg, align=align)
    return txb


def _rect(slide, left, top, width, height, bg, text="", size=13, fg=PW, bold=False):
    from pptx.util import Inches as I
    shape = slide.shapes.add_shape(
        1,  # MSO_SHAPE_TYPE.RECTANGLE
        I(left), I(top), I(width), I(height)
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = bg
    shape.line.color.rgb = PRGBColor(0x2E, 0x33, 0x50)
    if text:
        tf = shape.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        run = p.add_run()
        run.text = text
        run.font.size = PPt(size)
        run.font.bold = bold
        run.font.color.rgb = fg
    return shape


def make_pptx(path: str):
    prs = Presentation()
    prs.slide_width  = PInches(13.33)
    prs.slide_height = PInches(7.5)
    blank = prs.slide_layouts[6]  # blank

    # ── Slide 1: Title ────────────────────────────────────────────────
    s = prs.slides.add_slide(blank)
    _bg(s, PD)
    _rect(s, 0, 0, 13.33, 7.5, PD)
    # accent bar
    _rect(s, 0, 2.8, 13.33, 0.06, PA)
    _box(s, 1, 1.2, 11.33, 1.2, "📱 Phone Test Builder", size=44, bold=True, fg=PA, align=PP_ALIGN.CENTER)
    _box(s, 1, 2.5, 11.33, 0.6, "Web UI User Manual", size=24, fg=PW, align=PP_ALIGN.CENTER)
    _box(s, 1, 3.5, 11.33, 0.5, "ADB-based Phone Automation System", size=16, fg=PM, align=PP_ALIGN.CENTER)
    _box(s, 1, 4.2, 11.33, 0.5, "Version 1.0  ·  Mobicom", size=14, fg=PM, align=PP_ALIGN.CENTER)

    # ── Slide 2: Overview ─────────────────────────────────────────────
    s = prs.slides.add_slide(blank)
    _bg(s, PD)
    _rect(s, 0, 0, 13.33, 0.7, PS)
    _box(s, 0.3, 0.1, 12, 0.55, "Overview", size=26, bold=True, fg=PA)

    features = [
        ("🖱️ Drag & Drop",    "Build test workflows visually — no code needed"),
        ("👁️ Live Preview",   "See steps as a formatted table in real time"),
        ("⚙️ Device Config",  "Edit phone numbers & serials directly in browser"),
        ("▶ Run Tests",       "Stream live PASS/FAIL results from the browser"),
        ("📊 Reports",        "Pass rate bar, stat cards, full results table"),
        ("📁 Templates",      "Save and reload named workflows (localStorage)"),
        ("⬇ Export Excel",   "Generate xlsx compatible with the CLI runner"),
        ("🏠 Auto Home",      "Devices return to home screen after test ends"),
    ]
    cols = 4
    w, h = 3.0, 1.1
    for i, (icon_title, desc) in enumerate(features):
        col = i % cols
        row = i // cols
        left = 0.2 + col * (w + 0.13)
        top  = 0.9 + row * (h + 0.15)
        _rect(s, left, top, w, h, PS, fg=PW)
        _box(s, left + 0.1, top + 0.05, w - 0.2, 0.4, icon_title, size=14, bold=True, fg=PA)
        _box(s, left + 0.1, top + 0.45, w - 0.2, 0.55, desc, size=11, fg=PW)

    # ── Slide 3: Getting Started ──────────────────────────────────────
    s = prs.slides.add_slide(blank)
    _bg(s, PD)
    _rect(s, 0, 0, 13.33, 0.7, PS)
    _box(s, 0.3, 0.1, 12, 0.55, "Getting Started", size=26, bold=True, fg=PA)

    steps_gs = [
        ("1", "Enable USB Debugging",   "Settings → Developer Options → USB Debugging ON on each phone"),
        ("2", "Connect via USB",        "Plug both phones into the PC. Run: adb devices — both serials must appear"),
        ("3", "Edit config.py",         "Set DEVICES serial numbers and PHONE_NUMBERS for each phone"),
        ("4", "Install requirements",   "pip install flask openpyxl"),
        ("5", "Launch",                 "Double-click start_web.bat  OR  run: python web_builder.py"),
        ("6", "Open browser",           "Navigate to http://localhost:5000"),
    ]
    for i, (num, title, desc) in enumerate(steps_gs):
        top = 0.85 + i * 1.0
        _rect(s, 0.2, top, 0.55, 0.55, PA, text=num, size=20, bold=True, fg=PW)
        _box(s, 0.9, top - 0.05, 5.5, 0.38, title, size=14, bold=True, fg=PW)
        _box(s, 0.9, top + 0.3,  12.0, 0.38, desc, size=12, fg=PM)

    # ── Slide 4: Interface Layout ─────────────────────────────────────
    s = prs.slides.add_slide(blank)
    _bg(s, PD)
    _rect(s, 0, 0, 13.33, 0.7, PS)
    _box(s, 0.3, 0.1, 12, 0.55, "Interface Layout", size=26, bold=True, fg=PA)

    # Mock UI diagram
    _rect(s, 0.2, 0.8,  12.93, 0.55, PS, "Header Bar — Title | + Section | Clear | 📁 Templates | ⬇ Export | ▶ Run Test", size=11, fg=PW)
    _rect(s, 0.2, 1.45, 2.5,  5.4,  PRGBColor(0x16,0x19,0x26), "Left Sidebar\n\nActions Palette\n(drag to canvas)\n\n────────\n\nDevices Panel\nPhone1 / Phone2\nSerial + Number\n💾 Save config", size=10, fg=PM)
    _rect(s, 2.8, 1.45, 6.3,  5.4,  PRGBColor(0x12,0x15,0x20), "Editor Pane\n\nDrag & drop steps here\nEdit fields inline\nReorder by dragging", size=11, fg=PM)
    _rect(s, 9.2, 1.45, 4.1,  5.4,  PRGBColor(0x0A,0x0C,0x14), "Preview Pane\n\n#  Action  Target\n1  📞CALL  Ph1\n2  📲ANSW  Ph2\n3  ⏳WAIT  Ph1\n──────────\n4  💬SMS   Ph1", size=10, fg=PM)

    # labels
    _box(s, 0.22, 6.92, 2.5, 0.3, "← Sidebar", size=10, fg=PA)
    _box(s, 4.5,  6.92, 4.0, 0.3, "← Editor Pane →", size=10, fg=PA, align=PP_ALIGN.CENTER)
    _box(s, 9.8,  6.92, 3.0, 0.3, "Preview →", size=10, fg=PA)

    # ── Slide 5: Actions Reference ────────────────────────────────────
    s = prs.slides.add_slide(blank)
    _bg(s, PD)
    _rect(s, 0, 0, 13.33, 0.7, PS)
    _box(s, 0.3, 0.1, 12, 0.55, "Actions Reference", size=26, bold=True, fg=PA)

    action_colors = {
        "CALL": PRGBColor(0x27,0xAE,0x60), "ANSWER_CALL": PRGBColor(0x2E,0xCC,0x71),
        "END_CALL": PRGBColor(0xE7,0x4C,0x3C), "WAIT": PRGBColor(0x95,0xA5,0xA6),
        "SMS": PRGBColor(0x29,0x80,0xB9), "CHECK_SMS": PRGBColor(0x34,0x98,0xDB),
        "CHECK_CALL": PRGBColor(0x16,0xA0,0x85), "CHECK_VOLTE": PRGBColor(0x08,0x91,0xB2),
        "USSD": PRGBColor(0x8E,0x44,0xAD), "SET_NETWORK": PRGBColor(0xD3,0x54,0x00),
        "WAKE": PRGBColor(0xF3,0x9C,0x12), "SET_CONFIG": PRGBColor(0x7F,0x8C,0x8D),
        "GET_CONFIG": PRGBColor(0x7F,0x8C,0x8D),
    }
    cols_n = 4
    aw, ah = 3.1, 0.95
    for i, (icon, aid, label, desc, _) in enumerate(ACTIONS):
        col = i % cols_n
        row = i // cols_n
        left = 0.2 + col * (aw + 0.1)
        top  = 0.85 + row * (ah + 0.12)
        color = action_colors.get(aid, PS)
        r_, g_, b_ = color[0], color[1], color[2]
        bg_dim = PRGBColor(max(r_-60,0), max(g_-60,0), max(b_-60,0))
        _rect(s, left, top, aw, ah, bg_dim)
        _box(s, left+0.1, top+0.05, aw-0.2, 0.38, f"{icon} {aid}", size=12, bold=True, fg=PRGBColor(r_,g_,b_))
        _box(s, left+0.1, top+0.4,  aw-0.2, 0.48, desc, size=9, fg=PW)

    # ── Slide 6: Building a Workflow ──────────────────────────────────
    s = prs.slides.add_slide(blank)
    _bg(s, PD)
    _rect(s, 0, 0, 13.33, 0.7, PS)
    _box(s, 0.3, 0.1, 12, 0.55, "Building a Workflow", size=26, bold=True, fg=PA)

    steps_wf = [
        ("1️⃣", "Add Steps",     "Drag actions from the palette OR double-click them"),
        ("2️⃣", "Add Sections",  'Click "+ Section" → type the section title → press Enter'),
        ("3️⃣", "Fill Fields",   "Set Target Phone, Number/Code, Value, Expected Result"),
        ("4️⃣", "Reorder",       "Drag the step header OR use ▲▼ buttons in toolbar"),
        ("5️⃣", "Duplicate",     "Click ⎘ on any step to copy it"),
        ("6️⃣", "Delete",        "Click ✕ on a step, or select + Delete button in toolbar"),
        ("7️⃣", "Save Template", "📁 Templates → enter a name → 💾 Save current"),
        ("8️⃣", "Export",        "⬇ Export Excel → downloads test_cases.xlsx"),
    ]

    for i, (num, title, desc) in enumerate(steps_wf):
        col = i % 2
        row = i // 2
        left = 0.3 + col * 6.5
        top  = 0.85 + row * 1.4
        _rect(s, left, top, 6.1, 1.2, PS)
        _box(s, left+0.1, top+0.05, 0.5, 0.5, num, size=20)
        _box(s, left+0.65, top+0.05, 5.3, 0.4, title, size=15, bold=True, fg=PA)
        _box(s, left+0.65, top+0.45, 5.3, 0.6, desc, size=12, fg=PW)

    # ── Slide 7: Running Tests ────────────────────────────────────────
    s = prs.slides.add_slide(blank)
    _bg(s, PD)
    _rect(s, 0, 0, 13.33, 0.7, PS)
    _box(s, 0.3, 0.1, 12, 0.55, "Running Tests", size=26, bold=True, fg=PA)

    # Left col — steps
    run_steps = [
        "Click ▶ Run Test in the header bar",
        "The run panel opens, covering the canvas",
        "Each step shows blue 'running…' badge while executing",
        "Results update live: ✓ PASS | ✗ FAIL | — SKIP",
        "Live counter tracks totals in the panel header",
        "Click ■ Stop at any time to abort",
        "Devices return to Home Screen automatically when done",
        "Click 📊 View Report to see the full summary",
    ]
    _box(s, 0.3, 0.8, 5.5, 0.4, "How to run:", size=15, bold=True, fg=PA)
    for i, step in enumerate(run_steps):
        _rect(s, 0.3, 1.2 + i*0.73, 0.4, 0.4, PA, text=str(i+1), size=12, bold=True, fg=PW)
        _box(s, 0.85, 1.2 + i*0.73, 5.8, 0.45, step, size=12, fg=PW)

    # Right col — badge legend
    _box(s, 7.3, 0.8, 5.5, 0.4, "Result badges:", size=15, bold=True, fg=PA)
    badges = [
        (PG, "PASS",    "Action succeeded (and Expected Result matched if set)"),
        (PR, "FAIL",    "Action failed or Expected Result not found in output"),
        (PY, "SKIP",    "Action not supported or dry-run mode"),
        (PRGBColor(0x29,0x80,0xB9), "running…", "Step is currently executing"),
    ]
    for i, (color, badge, desc) in enumerate(badges):
        top = 1.35 + i * 1.3
        _rect(s, 7.3, top, 1.5, 0.55, color, text=badge, size=14, bold=True, fg=PW)
        _box(s, 9.0, top, 4.2, 0.55, desc, size=12, fg=PW)

    # ── Slide 8: Report & Templates ───────────────────────────────────
    s = prs.slides.add_slide(blank)
    _bg(s, PD)
    _rect(s, 0, 0, 13.33, 0.7, PS)
    _box(s, 0.3, 0.1, 12, 0.55, "Reports & Templates", size=26, bold=True, fg=PA)

    # Report section
    _box(s, 0.3, 0.8, 6.0, 0.4, "📊 Test Report", size=16, bold=True, fg=PA)
    _rect(s, 0.3, 1.3, 1.3, 1.1, PRGBColor(0x22,0x26,0x3A), "11\nTotal",  size=12, bold=True, fg=PW)
    _rect(s, 1.7, 1.3, 1.3, 1.1, PRGBColor(0x1A,0x47,0x31), "9\nPassed", size=12, bold=True, fg=PG)
    _rect(s, 3.1, 1.3, 1.3, 1.1, PRGBColor(0x4A,0x15,0x15), "2\nFailed", size=12, bold=True, fg=PR)
    _rect(s, 4.5, 1.3, 1.3, 1.1, PRGBColor(0x3D,0x32,0x00), "0\nSkipped",size=12, bold=True, fg=PY)
    # progress bar
    _rect(s, 0.3,  2.55, 4.85, 0.25, PG)
    _rect(s, 3.96, 2.55, 1.19, 0.25, PR)
    _box(s, 5.2, 2.5, 1.0, 0.3, "82%", size=13, bold=True, fg=PG)
    # table preview
    _rect(s, 0.3,  2.95, 6.0, 0.35, PRGBColor(0x22,0x26,0x3A), "#   Action      Target   Result   Output",  size=10, fg=PM)
    _rect(s, 0.3,  3.30, 6.0, 0.32, PD,  "1   📞 CALL    Phone1   PASS     Starting: Intent...", size=9,  fg=PW)
    _rect(s, 0.3,  3.62, 6.0, 0.32, PS,  "2   📲 ANSW    Phone2   PASS     Call answered",       size=9,  fg=PW)
    _rect(s, 0.3,  3.94, 6.0, 0.32, PD,  "3   📶 VOLTE   Phone1   PASS     VoLTE ACTIVE — IMS",  size=9,  fg=PW)
    _rect(s, 0.3,  4.26, 6.0, 0.32, PS,  "4   💬 SMS     Phone1   FAIL     Send button not found",size=9, fg=PR)

    # Templates section
    _box(s, 7.2, 0.8, 5.8, 0.4, "📁 Template Manager", size=16, bold=True, fg=PA)
    _rect(s, 7.2, 1.3, 5.8, 0.45, PS, "⭐ Default Template     built-in     [Load]", size=11, fg=PW)
    _rect(s, 7.2, 1.8, 5.8, 0.45, PS, "Regression Test    8 steps · 6/10     [Load][✕]", size=11, fg=PW)
    _rect(s, 7.2, 2.3, 5.8, 0.45, PS, "VoLTE Test         4 steps · 6/10     [Load][✕]", size=11, fg=PW)
    _rect(s, 7.2, 2.8, 4.3, 0.45, PRGBColor(0x22,0x26,0x3A), "Template name…", size=12, fg=PM)
    _rect(s, 11.6,2.8, 1.4, 0.45, PA, "💾 Save", size=12, bold=True, fg=PW)

    tpl_tips = [
        "Stored in browser localStorage — survives page refresh",
        "Overwrite: save with the same name",
        "Works across test sessions on the same machine",
    ]
    for i, tip in enumerate(tpl_tips):
        _box(s, 7.2, 3.45 + i * 0.5, 5.8, 0.45, f"• {tip}", size=12, fg=PM)

    # ── Slide 9: Device Configuration ────────────────────────────────
    s = prs.slides.add_slide(blank)
    _bg(s, PD)
    _rect(s, 0, 0, 13.33, 0.7, PS)
    _box(s, 0.3, 0.1, 12, 0.55, "Device Configuration", size=26, bold=True, fg=PA)

    # Device panel mock
    _rect(s, 0.3, 0.85, 4.5, 5.9, PS)
    _box(s, 0.5, 0.9, 4.0, 0.4, "Devices", size=14, bold=True, fg=PA)

    _rect(s, 0.5, 1.35, 4.1, 2.5, PRGBColor(0x22,0x26,0x3A))
    _box(s, 0.6, 1.4, 1.5, 0.35, "🟢 Phone1", size=13, bold=True, fg=PW)
    _box(s, 2.5, 1.4, 2.0, 0.35, "connected", size=11, fg=PG)
    _box(s, 0.6, 1.8, 1.0, 0.28, "Serial", size=10, fg=PM)
    _rect(s, 0.6, 2.08, 3.9, 0.38, PD, "RF8Y606FYXX", size=11, fg=PW)
    _box(s, 0.6, 2.5, 1.5, 0.28, "Phone Number", size=10, fg=PM)
    _rect(s, 0.6, 2.78, 3.9, 0.38, PD, "+97694310546", size=11, fg=PW)

    _rect(s, 0.5, 3.95, 4.1, 2.5, PRGBColor(0x22,0x26,0x3A))
    _box(s, 0.6, 4.0,  1.5, 0.35, "🟢 Phone2", size=13, bold=True, fg=PW)
    _box(s, 2.5, 4.0,  2.0, 0.35, "connected", size=11, fg=PG)
    _box(s, 0.6, 4.4,  1.0, 0.28, "Serial", size=10, fg=PM)
    _rect(s, 0.6, 4.68, 3.9, 0.38, PD, "4B270DLAQ003DE", size=11, fg=PW)
    _box(s, 0.6, 5.1,  1.5, 0.28, "Phone Number", size=10, fg=PM)
    _rect(s, 0.6, 5.38, 3.9, 0.38, PD, "+97695091051", size=11, fg=PW)

    _rect(s, 0.5, 6.5, 4.1, 0.45, PA, "💾 Save to config.py", size=13, bold=True, fg=PW)

    # Instructions
    tips_dc = [
        ("Find your serial",  "Run:  adb devices\nin a terminal with phone connected"),
        ("Online indicator",  "🟢 Green = ADB connected\n⚫ Grey = not detected"),
        ("Refresh status",    'Click "↻ refresh" to re-check\nADB connection in real time'),
        ("Save changes",      '"💾 Save to config.py" writes\nboth serial and phone number'),
    ]
    for i, (title, desc) in enumerate(tips_dc):
        col = i % 2; row = i // 2
        left = 5.2 + col * 4.0
        top  = 1.0 + row * 3.0
        _rect(s, left, top, 3.8, 2.5, PRGBColor(0x22,0x26,0x3A))
        _box(s, left+0.15, top+0.1, 3.5, 0.45, title, size=14, bold=True, fg=PA)
        _box(s, left+0.15, top+0.6, 3.5, 1.7,  desc,  size=12, fg=PW)

    # ── Slide 10: Tips & Troubleshooting ─────────────────────────────
    s = prs.slides.add_slide(blank)
    _bg(s, PD)
    _rect(s, 0, 0, 13.33, 0.7, PS)
    _box(s, 0.3, 0.1, 12, 0.55, "Tips & Troubleshooting", size=26, bold=True, fg=PA)

    tips = [
        (PA, "💡 Best Practices",
         "• Run CHECK_VOLTE during an active call (after CALL + ANSWER_CALL) for accurate results\n"
         "• Add WAIT (5–10s) after SMS before CHECK_SMS to let the message arrive\n"
         "• Use sections to group related steps — they appear as dividers in the report\n"
         "• Name and save your workflow as a template before running"),
        (PR, "🔴 Common Issues",
         "• Device offline: check USB cable, re-enable USB debugging, run adb devices\n"
         "• ANSWER_CALL timeout: increase Value to 30s if the ring takes longer\n"
         "• CHECK_SMS fails: add a longer WAIT before it, or check sender number format\n"
         "• USSD no response: the carrier may block ADB-initiated USSD on some SIMs"),
        (PG, "✅ VoLTE Testing",
         "• Workflow: CALL → ANSWER_CALL → CHECK_VOLTE (both phones) → END_CALL\n"
         "• imsCallType=2 in telephony.registry = VoLTE confirmed\n"
         "• If result is 'no active call': run CHECK_VOLTE while call is still connected\n"
         "• Ensure 4G/LTE network and 'Enhanced 4G LTE' is ON in mobile settings"),
        (PY, "⚙️ Configuration",
         "• Edit config.py OR use the Devices panel in the web UI — both update the same file\n"
         "• ADB_PATH in config.py must point to your adb.exe on Windows\n"
         "• Restart start_web.bat after changing config.py outside the web UI\n"
         "• Templates are browser-local (localStorage) — not shared between machines"),
    ]

    for i, (color, title, body) in enumerate(tips):
        col = i % 2; row = i // 2
        left = 0.2 + col * 6.55
        top  = 0.8 + row * 3.2
        _rect(s, left, top, 6.4, 3.0, PRGBColor(0x22,0x26,0x3A))
        _rect(s, left, top, 6.4, 0.5, color, text=title, size=14, bold=True, fg=PW)
        _box(s, left+0.15, top+0.6, 6.1, 2.3, body, size=11, fg=PW)

    # ── Slide 11: Thank You ───────────────────────────────────────────
    s = prs.slides.add_slide(blank)
    _bg(s, PD)
    _rect(s, 0, 2.8, 13.33, 0.06, PA)
    _box(s, 1, 1.5, 11.33, 1.0, "📱 Phone Test Builder", size=36, bold=True, fg=PA, align=PP_ALIGN.CENTER)
    _box(s, 1, 2.6, 11.33, 0.6, "Start the web UI:  python web_builder.py  or  start_web.bat", size=16, fg=PW, align=PP_ALIGN.CENTER)
    _box(s, 1, 3.5, 11.33, 0.5, "Open browser →  http://localhost:5000", size=18, bold=True, fg=PA, align=PP_ALIGN.CENTER)
    _box(s, 1, 4.5, 11.33, 0.5, "Mobicom  ·  Phone Automation System  ·  Version 1.0", size=13, fg=PM, align=PP_ALIGN.CENTER)

    prs.save(path)
    print(f"✓ PowerPoint saved: {path}")


if __name__ == "__main__":
    make_docx("/home/user/phone_automation/Phone_Test_Builder_Manual.docx")
    make_pptx("/home/user/phone_automation/Phone_Test_Builder_Manual.pptx")
    print("\nDone! Both files created.")
