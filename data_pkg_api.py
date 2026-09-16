"""Data Package API client."""

from __future__ import annotations
import json
import urllib.request
import urllib.parse
import urllib.error

API_BASE_URL = "http://10.10.55.84:8000"

_JSON_HDR = {"Accept": "application/json", "Content-Type": "application/json"}
_FORM_HDR = {"Accept": "application/json"}


def _normalize(msisdn: str) -> str:
    return msisdn.lstrip("+")


def _extract_error(data: dict | list) -> str | None:
    """Return an error string if the JSON body signals failure, else None."""
    if not isinstance(data, dict):
        return None
    # Common error indicators across different API conventions
    status = data.get("status") or data.get("code") or data.get("resultCode") or data.get("result_code")
    if status is not None:
        try:
            s = int(status)
            if s not in (0, 200):
                msg = (data.get("message") or data.get("msg") or data.get("error")
                       or data.get("detail") or data.get("description") or "")
                return f"status={s}" + (f": {msg}" if msg else "")
        except (ValueError, TypeError):
            if str(status).lower() not in ("ok", "success", "0", "200"):
                msg = data.get("message") or data.get("error") or ""
                return f"status={status}" + (f": {msg}" if msg else "")
    # Explicit error/success flags
    if data.get("success") is False:
        msg = data.get("message") or data.get("error") or data.get("detail") or ""
        return f"success=false" + (f": {msg}" if msg else "")
    err = data.get("error") or data.get("errors")
    if err and str(err).lower() not in ("null", "none", "false", "0", ""):
        return str(err)
    return None


def _request(url: str, body: bytes = b"", headers: dict | None = None,
             method: str = "POST", timeout: int = 15) -> tuple[bool, str]:
    """Make an HTTP request and return (ok, text).

    On HTTP error: reads the response body and extracts detail.
    On success: checks JSON body for embedded error status.
    """
    req = urllib.request.Request(url, data=body,
                                 headers=headers or _FORM_HDR,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        # Read the error body — it often contains the real error message
        try:
            err_body = e.read().decode()
        except Exception:
            err_body = ""
        try:
            err_data = json.loads(err_body)
            detail = (err_data.get("message") or err_data.get("msg") or err_data.get("error")
                      or err_data.get("detail") or err_data.get("description")
                      or json.dumps(err_data, ensure_ascii=False))
        except Exception:
            detail = err_body or str(e)
        return False, f"HTTP {e.code} {e.reason}: {detail}"
    except urllib.error.URLError as e:
        return False, f"Connection error: {e.reason}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

    # Parse response JSON
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return True, raw

    # Check if JSON body itself signals an error
    err = _extract_error(data)
    if err:
        return False, f"{err}\n{json.dumps(data, ensure_ascii=False, indent=2)}"

    return True, json.dumps(data, ensure_ascii=False, indent=2)


# ── Public API functions ──────────────────────────────────────────────────────

def create_package(msisdn: str, params: str) -> tuple[bool, str]:
    """Create a single data package for a subscriber."""
    qs = urllib.parse.urlencode({"number": _normalize(msisdn), "package": params})
    return _request(f"{API_BASE_URL}/create_package?{qs}", headers=_FORM_HDR)


def create_packages_batch(msisdn: str, packages: list,
                          clear_existing: bool = True,
                          mode: str = "sequential") -> tuple[bool, str]:
    """Create multiple data packages in one batch/sequential call."""
    body = json.dumps({
        "number": _normalize(msisdn),
        "packages": packages,
        "clear_existing": clear_existing,
        "mode": mode,
    }).encode()
    return _request(f"{API_BASE_URL}/create_package", body=body, headers=_JSON_HDR)


def delete_package(msisdn: str, package_code: str) -> tuple[bool, str]:
    """Delete a data package for a subscriber."""
    qs = urllib.parse.urlencode({"number": _normalize(msisdn), "package": package_code})
    ok, out = _request(f"{API_BASE_URL}/delete_package?{qs}", headers=_FORM_HDR)
    if not ok and "no packages to delete" in out.lower():
        return True, f"Already absent — {package_code}"
    return ok, out


def modify_package(msisdn: str, _package_code: str,
                   new_value: str, date: str = "") -> tuple[bool, str]:
    """Modify threshold and/or end date for a subscriber's data package."""
    params: dict = {"number": _normalize(msisdn)}
    if new_value:
        params["treshhold"] = new_value
    if date:
        params["date"] = date
    qs = urllib.parse.urlencode(params)
    return _request(f"{API_BASE_URL}/modify_package?{qs}", headers=_FORM_HDR)


def check_package(msisdn: str, package_code: str) -> tuple[bool, str]:
    """Check/query data packages for a subscriber."""
    qs = urllib.parse.urlencode({"number": _normalize(msisdn)})
    return _request(f"{API_BASE_URL}/package_check?{qs}", headers=_FORM_HDR)


def select_package(msisdn: str) -> tuple[bool, str]:
    """Get all active data packages for a subscriber."""
    return check_package(msisdn, "")


def parse_qtastats(raw: str) -> dict:
    """Parse check_package response → {SRVNAME: full_pkg_dict}.
    Returns empty dict on any parse error.
    """
    try:
        data = json.loads(raw)
        pkgs = data if isinstance(data, list) else (
            data.get("packages") or data.get("data") or data.get("items") or []
        )
        result = {}
        for pkg in (pkgs if isinstance(pkgs, list) else []):
            name = pkg.get("SRVNAME") or pkg.get("srvname") or pkg.get("name", "")
            if name:
                result[name] = pkg
        return result
    except Exception:
        return {}
