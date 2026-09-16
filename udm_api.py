"""UDM (Unified Data Management) API client."""
from __future__ import annotations
import json
import urllib.request
import urllib.parse
import urllib.error

API_BASE_URL = "http://10.10.55.84:8000"


def _normalize(msisdn: str) -> str:
    """Strip leading + so API receives plain digits."""
    return msisdn.lstrip("+")


def _extract_error(data: dict) -> str | None:
    """Return error string if JSON body signals failure, else None."""
    if not isinstance(data, dict):
        return None
    status = (data.get("status") or data.get("code") or
              data.get("resultCode") or data.get("result_code"))
    if status is not None:
        try:
            s = int(status)
            if s not in (0, 200):
                msg = (data.get("message") or data.get("msg") or
                       data.get("error") or data.get("detail") or "")
                return f"status={s}" + (f": {msg}" if msg else "")
        except (ValueError, TypeError):
            if str(status).lower() not in ("ok", "success", "0", "200"):
                msg = data.get("message") or data.get("error") or ""
                return f"status={status}" + (f": {msg}" if msg else "")
    if data.get("success") is False:
        msg = data.get("message") or data.get("error") or data.get("detail") or ""
        return "success=false" + (f": {msg}" if msg else "")
    err = data.get("error") or data.get("errors")
    if err and str(err).lower() not in ("null", "none", "false", "0", ""):
        return str(err)
    return None


def _post(path: str, qs: dict | None = None, body: dict | None = None) -> tuple[bool, str]:
    url = f"{API_BASE_URL}{path}"
    if qs:
        url += "?" + urllib.parse.urlencode(qs)
    data = json.dumps(body).encode() if body else b""
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode()
        except Exception:
            err_body = ""
        try:
            err_data = json.loads(err_body)
            detail = (err_data.get("message") or err_data.get("msg") or
                      err_data.get("error") or err_data.get("detail") or
                      json.dumps(err_data, ensure_ascii=False))
        except Exception:
            detail = err_body or str(e)
        return False, f"HTTP {e.code} {e.reason}: {detail}"
    except urllib.error.URLError as e:
        return False, f"Connection error: {e.reason}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return True, raw

    err = _extract_error(parsed)
    if err:
        return False, f"{err}\n{json.dumps(parsed, ensure_ascii=False, indent=2)}"

    return True, json.dumps(parsed, ensure_ascii=False, indent=2)


def get_apn_list() -> tuple[bool, list]:
    """GET /apn_list — returns (ok, list_of_apn_dicts).
    Each dict expected to have 'id' (matches APNTPLID) plus 'name' and 'apn' keys.
    """
    url = f"{API_BASE_URL}/apn_list"
    req = urllib.request.Request(url, data=b"", headers={"Accept": "application/json", "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read().decode())
        # Accept plain list or wrapped {"data":[…]} / {"apns":[…]}
        if isinstance(raw, list):
            return True, raw
        if isinstance(raw, dict):
            for key in ("data", "apns", "list", "items"):
                if isinstance(raw.get(key), list):
                    return True, raw[key]
            # Single-key dict — take first list value
            for v in raw.values():
                if isinstance(v, list):
                    return True, v
        return True, []
    except Exception as e:
        return False, [{"_error": f"{type(e).__name__}: {e}"}]


def _apn_for_tplid(apntplid: str, apn_list: list) -> tuple[str, str]:
    """Return (apn_name, apn_value) for a given APNTPLID. Empty strings if not found."""
    tid = str(apntplid)
    for entry in apn_list:
        entry_id = (str(entry.get("apn_id", "")) or str(entry.get("id", ""))
                    or str(entry.get("apntplid", "")))
        if entry_id == tid:
            name  = entry.get("name", "") or entry.get("apn_name", "") or entry.get("apnName", "")
            value = entry.get("apn", "") or entry.get("apn_value", "") or entry.get("apnValue", "")
            return name, value
    return "", ""


def list_gprs(isdn: str) -> tuple[bool, str]:
    """POST /udm/optgprs/list?isdn={isdn}"""
    return _post("/udm/optgprs/list", qs={"isdn": _normalize(isdn)})


def assign_gprs(isdn: str, option: str, *, pdpadd: str = "", apntplid: str = "", qostplid: str = "") -> tuple[bool, str]:
    """POST /udm/optgprs/assign — body: {"isdn":…,"pdpadd":…,"apntplid":N,"qostplid":N}"""
    body: dict = {"isdn": _normalize(isdn), "option": option}
    if pdpadd:
        body["pdpadd"] = pdpadd
    if apntplid:
        try:
            body["apntplid"] = int(apntplid)
        except ValueError:
            body["apntplid"] = apntplid
    if qostplid:
        try:
            body["qostplid"] = int(qostplid)
        except ValueError:
            body["qostplid"] = qostplid
    return _post("/udm/optgprs/assign", body=body)


def remove_gprs(isdn: str, cntxids: str) -> tuple[bool, str]:
    """POST /udm/optgprs/remove — body: {"isdn":…,"cntxids":[11,12]}"""
    ids = [int(x.strip()) for x in cntxids.split(",") if x.strip().lstrip("-").isdigit()]
    body = {"isdn": _normalize(isdn), "cntxids": ids}
    return _post("/udm/optgprs/remove", body=body)


def test_apn(isdn: str, pdpadd: str, apntplid: str, qostplid: str,
             serial: str = "", check_ip: str = "", check_port: str = "",
             scan_ports: str = "") -> tuple[bool, str]:
    """Full APN test flow: list → assign → set phone APN → remove → verify.
    Returns (overall_ok, log_string).
    """
    import adb_controller as adb
    lines: list[str] = []

    def _ok(n, label, msg=""):
        lines.append(f"[{n}]✓ {label}" + (f": {msg}" if msg else ""))

    def _fail(n, label, msg=""):
        lines.append(f"[{n}]✗ {label}" + (f": {msg}" if msg else ""))

    def _skip(n, label):
        lines.append(f"[{n}]- {label}: SKIP")

    # Fetch APN list once — used in step 1 display and step 3 lookup
    apn_list_ok, apn_list = get_apn_list()
    if not apn_list_ok:
        err = apn_list[0].get("_error", "unknown") if apn_list else "unknown"
        lines.append(f"[!] /apn_list fetch failed: {err}")

    # ── Step 1: List current contexts ────────────────────────────────────────
    ok, raw = list_gprs(isdn)
    if not ok:
        _fail(1, "List GPRS", raw[:400])
        return False, "\n".join(lines)
    try:
        data = json.loads(raw)
        before_contexts = data.get("contexts", [])
        before_ids = {c["CNTXID"] for c in before_contexts}
        ctx_lines = []
        for c in before_contexts:
            cid = c.get("CNTXID", "?")
            tplid = c.get("APNTPLID", "")
            atype = c.get("APN_TYPE", "")
            name, _ = _apn_for_tplid(tplid, apn_list) if apn_list_ok else ("", "")
            ctx_lines.append(f"  [{cid}] {atype} apn_id={tplid} → {name or '(unknown)'}")
        _ok(1, "List GPRS", f"{len(before_contexts)} contexts\n" + "\n".join(ctx_lines))
    except Exception as e:
        _fail(1, "List GPRS", f"parse error: {e}")
        return False, "\n".join(lines)

    # ── Step 2: Assign new APN ────────────────────────────────────────────────
    ok, raw = assign_gprs(isdn, "", pdpadd=pdpadd, apntplid=apntplid, qostplid=qostplid)
    if not ok:
        _fail(2, "Assign GPRS", raw[:120])
        return False, "\n".join(lines)
    try:
        data = json.loads(raw)
        after_contexts = data.get("after") or []
        # Assign modifies existing contexts (same CNTXIDs), so detect changed ones:
        # find CNTXIDs whose APNTPLID changed TO the value we just assigned.
        before_map = {c["CNTXID"]: str(c.get("APNTPLID", "")) for c in before_contexts}
        new_ids = sorted(
            [c["CNTXID"] for c in after_contexts
             if str(c.get("APNTPLID", "")) == str(apntplid)
             and before_map.get(c["CNTXID"]) != str(apntplid)],
            key=lambda x: int(x) if str(x).lstrip("-").isdigit() else 0
        )
        failed_actions = [a for a in data.get("actions", [])
                          if a.get("soap_status") is not None and a.get("soap_status") != 200]
        if failed_actions:
            code = failed_actions[0].get("soap_status")
            _fail(2, "Assign GPRS", f"action failed (soap_status={code})")
            return False, "\n".join(lines)
        if not new_ids and after_contexts:
            sample = after_contexts[:3]
            debug = "; ".join(f"CNTX={c.get('CNTXID')} APN={c.get('APNTPLID')}" for c in sample)
            lines.append(f"  [2-dbg] after[0..2]: {debug} | before had: {dict(list(before_map.items())[:4])}")
        _ok(2, "Assign GPRS", f"changed contexts: {', '.join(str(i) for i in new_ids)}")
    except Exception as e:
        _fail(2, "Assign GPRS", f"parse error: {e}")
        return False, "\n".join(lines)

    # ── Steps 3–5: Phone APN ─────────────────────────────────────────────────
    # apntplid is what we assigned — use it directly for APN lookup
    new_apntplid = str(apntplid) if apntplid else ""
    apn_name = ""
    apn_value = ""
    apn_list_sample = ""
    if new_apntplid and apn_list_ok and apn_list:
        apn_name, apn_value = _apn_for_tplid(new_apntplid, apn_list)
        if not apn_value and apn_name:
            apn_value = apn_name  # API only returns name; use it as the APN string too
        if not apn_name:
            sample = apn_list[0] if apn_list else {}
            apn_list_sample = f" | apn_list[0] keys: {list(sample.keys())}, values: {list(str(v) for v in sample.values())[:6]}"

    # Save original preferred APN before changing it
    orig_apn_id = None
    if serial:
        ok_p, pout = adb.get_preferred_apn_id(serial)
        if ok_p:
            orig_apn_id = pout

    apn_inserted = False
    if serial and apn_name:
        ok, out = adb.set_apn(serial, apn_name, apn_value)
        if ok:
            apn_inserted = "inserted" in out
            _ok(3, "Set phone APN", out)
        else:
            _fail(3, "Set phone APN", out[:80])
            overall_ok = False
    else:
        reason = "no serial" if not serial else f"APN not found for APNTPLID={new_apntplid}"
        _skip(3, f"Set phone APN ({reason}){apn_list_sample}")

    if serial:
        adb.set_airplane_mode(serial, "on",  wait_secs=2)
        adb.set_airplane_mode(serial, "off", wait_secs=15)

    if serial:
        ok, out = adb.get_mobile_ip(serial)
        if ok:
            import re as _re
            phone_ip = _re.search(r"([\d.]+)", out)
            phone_ip = phone_ip.group(1) if phone_ip else ""
            if pdpadd and phone_ip:
                match = "✓ match" if phone_ip == pdpadd else f"✗ mismatch (pdpadd={pdpadd})"
                _ok("3b", "Phone mobile IP", f"{out} — {match}")
            else:
                _ok("3b", "Phone mobile IP", out)
        else:
            lines.append(f"  [3b] Phone mobile IP: {out}")

    if serial and check_ip:
        ok, out = adb.check_connectivity(serial, check_ip, check_port)
        if ok:
            _ok(4, "Connectivity check", out)
        else:
            _fail(4, "Connectivity check", out)
            overall_ok = False
    else:
        reason = "no serial" if not serial else "no check_ip set"
        _skip(4, f"Connectivity check ({reason})")

    if serial and check_ip and scan_ports:
        ok, out = adb.port_scan(serial, check_ip, scan_ports)
        if ok:
            _ok("4b", "Port scan", out)
        else:
            _fail("4b", "Port scan", out)
    elif scan_ports:
        _skip("4b", "Port scan (no check_ip set)")

    if serial and apn_name:
        # Restore original preferred APN
        if orig_apn_id:
            adb.set_preferred_apn_by_id(serial, orig_apn_id)
        # Only delete APN from phone if we inserted it (not pre-existing)
        if apn_inserted:
            ok, out = adb.delete_apn_by_name(serial, apn_name)
            _ok(5, "Restore phone APN", f"deleted '{apn_name}', restored orig id={orig_apn_id}")
        else:
            _ok(5, "Restore phone APN", f"restored preferred to orig id={orig_apn_id}")
    else:
        _skip(5, "Restore phone APN")

    # ── Step 6: Remove the newly assigned contexts ────────────────────────────
    overall_ok = True
    ok, raw = remove_gprs(isdn, ",".join(str(i) for i in new_ids))
    if not ok:
        _fail(6, "Remove GPRS", raw[:120])
        overall_ok = False
    else:
        _ok(6, "Remove GPRS", f"removed: {', '.join(str(i) for i in new_ids)}")

    # ── Step 7: Verify contexts restored to original ──────────────────────────
    ok, raw = list_gprs(isdn)
    if not ok:
        _fail(7, "Verify config", raw[:120])
        overall_ok = False
    else:
        try:
            data = json.loads(raw)
            final_ids = {c["CNTXID"] for c in data.get("contexts", [])}
            if final_ids == before_ids:
                _ok(7, "Verify config", f"restored — {len(final_ids)} contexts match original")
            else:
                extra   = final_ids - before_ids
                missing = before_ids - final_ids
                parts = []
                if extra:   parts.append(f"extra: {sorted(extra, key=int)}")
                if missing: parts.append(f"missing: {sorted(missing, key=int)}")
                _fail(7, "Verify config", "; ".join(parts))
                overall_ok = False
        except Exception as e:
            _fail(7, "Verify config", f"parse error: {e}")
            overall_ok = False

    return overall_ok, "\n".join(lines)
