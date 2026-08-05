"""Data Package API client — stub functions.

Replace the body of each function with your actual API call.
All functions must return (bool, str): (success, output_message).
"""

from __future__ import annotations
import json
import urllib.request
import urllib.parse

API_BASE_URL = "http://10.10.55.84:8000"


def _normalize(msisdn: str) -> str:
    """Strip leading + so API receives plain digits."""
    return msisdn.lstrip("+")


def create_package(msisdn: str, params: str) -> tuple[bool, str]:
    """Create a data package for a subscriber."""
    try:
        qs = urllib.parse.urlencode({"number": _normalize(msisdn), "package": params})
        url = f"{API_BASE_URL}/create_package?{qs}"
        req = urllib.request.Request(url, data=b"", headers={"Accept": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
        try:
            data = json.loads(raw)
            return True, json.dumps(data, ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            return True, raw
    except Exception as e:
        return False, f"DP_CREATE error: {e}"


def delete_package(msisdn: str, package_code: str) -> tuple[bool, str]:
    """Delete a data package for a subscriber.

    Args:
        msisdn:       Subscriber phone number
        package_code: Package code/ID to delete
    """
    # TODO: implement API call
    # resp = _session.delete(f"{API_BASE_URL}/packages/{package_code}", params={"msisdn": msisdn})
    # resp.raise_for_status()
    # return True, f"Deleted package {package_code} for {msisdn}"
    return False, "DP_DELETE: not implemented yet"


def modify_package(msisdn: str, package_code: str, new_value: str) -> tuple[bool, str]:
    """Modify threshold for a subscriber's data package."""
    try:
        qs = urllib.parse.urlencode({"number": _normalize(msisdn), "treshhold": new_value})
        url = f"{API_BASE_URL}/modify_package?{qs}"
        req = urllib.request.Request(url, data=b"", headers={"Accept": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
        try:
            data = json.loads(raw)
            return True, json.dumps(data, ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            return True, raw
    except Exception as e:
        return False, f"DP_MODIFY error: {e}"


def check_package(msisdn: str, package_code: str) -> tuple[bool, str]:
    """Check/query data packages for a subscriber."""
    try:
        qs = urllib.parse.urlencode({"number": _normalize(msisdn)})
        url = f"{API_BASE_URL}/package_check?{qs}"
        req = urllib.request.Request(url, data=b"", headers={"Accept": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
        try:
            data = json.loads(raw)
            return True, json.dumps(data, ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            return True, raw
    except Exception as e:
        return False, f"DP_CHECK error: {e}"


def select_package(msisdn: str) -> tuple[bool, str]:
    """Get/select all data packages for a subscriber.

    Args:
        msisdn: Subscriber phone number
    """
    # TODO: implement API call
    # resp = _session.get(f"{API_BASE_URL}/packages", params={"msisdn": msisdn})
    # resp.raise_for_status()
    # data = resp.json()
    # return True, str(data)
    return False, "DP_SELECT: not implemented yet"
