"""Browser-side glue for the Pyodide version of the Kulku CLI.

This module runs inside Pyodide. It reuses kulku.py for all API and slot
logic; only the HTTP transport and the auth storage are different.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime

import kulku

try:
    from pyodide.http import pyxhr
except ImportError:  # normal Python, used by the unit tests
    pyxhr = None

_ORIGINAL_REQUEST = kulku.request
AUTH: dict | None = None

_CACHE = None
_CACHE_AT = 0.0
_CACHE_TTL = 10.0


class AppError(Exception):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


def _ok(**extra) -> str:
    return json.dumps({"ok": True, **extra}, ensure_ascii=False)


def _error(message: str, code: str | None = None) -> str:
    return json.dumps({"ok": False, "error": message, "code": code}, ensure_ascii=False)


def _request(path: str, *, auth: dict | None = None, body=None, method: str | None = None):
    """Drop-in replacement for kulku.request that works in the browser."""
    if pyxhr is None:
        return _ORIGINAL_REQUEST(path, auth=auth, body=body, method=method)

    url = kulku.API + path
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if auth:
        headers["x-Auth-Token"] = str(auth["authToken"])
        headers["x-User-Id"] = str(auth["userId"])
    verb = (method or ("POST" if body is not None else "GET")).upper()
    kwargs = {"headers": headers}
    if body is not None:
        kwargs["data"] = json.dumps(body)
    try:
        response = getattr(pyxhr, verb.lower())(url, **kwargs)
    except Exception as exc:  # network / CORS
        return 0, {"message": str(exc)}
    raw = response.text or "null"
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = raw
    return response.status_code, payload


# All existing kulku functions (fetch_data, build_slots, do_book, ...) call
# this module-level name, so replacing it is enough.
kulku.request = _request


def _require_auth() -> dict:
    if not AUTH:
        raise AppError("Not logged in", "auth")
    return AUTH


def _invalidate() -> None:
    global _CACHE, _CACHE_AT
    _CACHE = None
    _CACHE_AT = 0.0


def _data(auth: dict, *, fresh: bool = False) -> dict:
    global _CACHE, _CACHE_AT
    if not fresh and _CACHE is not None and time.time() - _CACHE_AT < _CACHE_TTL:
        return _CACHE
    data = kulku.fetch_data(auth)
    _CACHE = data
    _CACHE_AT = time.time()
    return data


def _display_name(space: dict) -> str:
    translations = space.get("translations") or {}
    english = (translations.get("en") or {}).get("name")
    if isinstance(english, str) and english.strip():
        return english.strip()
    return kulku.space_name(space)


def _space_dict(space: dict) -> dict:
    return {
        "id": str(space.get("_id") or ""),
        "name": _display_name(space),
        "address": kulku.space_address(space),
        "icon": space.get("spaceIcon") or "other",
        "container": bool(space.get("isContainer")),
    }


def _slot_dict(slot: dict) -> dict:
    return {
        "start": slot["start"].isoformat(),
        "end": slot["end"].isoformat(),
        "label": slot["label"],
        "state": slot["state"],
        "detail": slot.get("detail") or "",
        "free": slot.get("free", 0),
        "bookingId": slot.get("booking_id"),
    }


def _attach_booking_ids(data: dict, slots: list[dict]) -> None:
    own = [booking for booking in data["bookings"] if not booking["cancelled"]]
    for slot in slots:
        slot["booking_id"] = None
        if slot["state"] != "mine":
            continue
        for booking in own:
            if booking["start"] < slot["end"] and slot["start"] < booking["end"]:
                slot["booking_id"] = booking["id"]
                break


def _groups_for(space: dict) -> list[str]:
    icon = space.get("spaceIcon") or "other"
    if icon in ("washroom", "dryroom"):
        return ["laundry"]
    return [icon]


# ---------------------------------------------------------------------------
# public API called from JavaScript. Every function returns a JSON string.
# ---------------------------------------------------------------------------


def set_auth(auth_json: str) -> str:
    global AUTH
    try:
        data = json.loads(auth_json) if auth_json else None
    except ValueError:
        return _error("Invalid auth data")
    if not isinstance(data, dict) or not data.get("userId") or not data.get("authToken"):
        AUTH = None
        return _error("Auth data needs userId and authToken")
    AUTH = {
        "userId": str(data["userId"]),
        "authToken": str(data["authToken"]),
        "username": data.get("username"),
    }
    _invalidate()
    return _ok(username=AUTH.get("username"))


def login(phone: str, password: str) -> str:
    username = kulku.normalize_phone(phone)
    try:
        status, payload = kulku.request(
            "login", body={"username": username, "password": password}
        )
    except Exception as exc:
        return _error(str(exc))
    data = payload.get("data") if isinstance(payload, dict) else None
    if status != 200 or not data or not data.get("authToken"):
        message = payload.get("message") if isinstance(payload, dict) else f"HTTP {status}"
        return _error(str(message or "Login failed"))
    auth = {
        "userId": str(data["userId"]),
        "authToken": str(data["authToken"]),
        "username": username,
    }
    set_auth(json.dumps(auth))
    return _ok(auth=auth)


def get_spaces() -> str:
    try:
        auth = _require_auth()
        data = _data(auth)
    except AppError as exc:
        return _error(str(exc), exc.code)
    except kulku.ApiError as exc:
        return _error(str(exc), "auth" if exc.status == 401 else "api")
    except Exception as exc:
        return _error(str(exc))

    spaces = []
    for space in data["spaces"].values():
        if space.get("isContainer"):
            continue
        item = _space_dict(space)
        item["groups"] = _groups_for(space)
        spaces.append(item)
    spaces.sort(key=lambda item: item["name"].lower())
    return _ok(spaces=spaces)


def _grid_from_parts(
    spaces: list[dict],
    bookings: list[dict],
    days: dict[str, dict],
    day: date,
    group: str,
) -> dict:
    now = datetime.now(kulku.TZ)
    own_ids = kulku.own_booking_ids({"bookings": bookings})

    rows = []
    columns: dict[str, str] = {}
    for space in spaces:
        row = _space_dict(space)
        row["slots"] = []
        payload = days.get(str(space.get("_id")))
        if not payload:
            row["error"] = "No data for this space"
            rows.append(row)
            continue
        if payload.get("__error"):
            row["error"] = str(payload["__error"])
            rows.append(row)
            continue
        try:
            slots = kulku.build_slots(space, payload, day, now, own_ids)
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            rows.append(row)
            continue
        _attach_booking_ids({"bookings": bookings}, slots)
        row["slots"] = [_slot_dict(slot) for slot in slots]
        row["allowReservation"] = payload.get("allowReservation") is not False
        row["paymentRequired"] = bool(payload.get("paymentRequired"))
        row["monthUsage"] = payload.get("monthUsage")
        for slot in slots:
            columns.setdefault(slot["start"].isoformat(), slot["start"].strftime("%H:%M"))
        rows.append(row)

    column_list = [{"start": start, "label": columns[start]} for start in sorted(columns)]
    return {"date": day.isoformat(), "group": group, "columns": column_list, "rows": rows}


def build_grid(payload_json: str) -> str:
    """Build the grid from payloads fetched in parallel by JavaScript."""
    try:
        _require_auth()
        payload = json.loads(payload_json)
        day = date.fromisoformat(payload["date"])
        group = payload.get("group") or ""
        spaces = [space for space in payload.get("spaces") or [] if isinstance(space, dict)]
        raw_bookings = payload.get("bookings") or []
        if isinstance(raw_bookings, dict):
            raw_bookings = raw_bookings.get("data") or []
        days = payload.get("days") or {}
        spaces_map = {
            str(space.get("_id")): space for space in spaces if space.get("_id")
        }
        now = datetime.now(kulku.TZ)
        bookings = [
            booking
            for booking in (
                kulku.normalize_booking(raw, spaces_map, now)
                for raw in raw_bookings
                if isinstance(raw, dict)
            )
            if booking
        ]
        result = _grid_from_parts(spaces, bookings, days, day, group)
        return _ok(**result)
    except AppError as exc:
        return _error(str(exc), exc.code)
    except kulku.ApiError as exc:
        return _error(str(exc), "auth" if exc.status == 401 else "api")
    except Exception as exc:
        return _error(f"{type(exc).__name__}: {exc}")


def get_grid(day_iso: str, group: str = "laundry") -> str:
    """Slow fallback: fetch every space sequentially in Python."""
    try:
        auth = _require_auth()
        day = date.fromisoformat(day_iso)
        data = _data(auth)
        spaces = [
            space
            for space in data["spaces"].values()
            if not space.get("isContainer")
        ]
        if group and group != "all":
            spaces = [space for space in spaces if group in _groups_for(space)]
        days = {}
        for space in spaces:
            try:
                days[str(space["_id"])] = kulku.fetch_space_day(auth, space["_id"], day)
            except kulku.ApiError as exc:
                days[str(space["_id"])] = {"__error": str(exc)}
        result = _grid_from_parts(spaces, data["bookings"], days, day, group)
        return _ok(**result)
    except AppError as exc:
        return _error(str(exc), exc.code)
    except kulku.ApiError as exc:
        return _error(str(exc), "auth" if exc.status == 401 else "api")
    except Exception as exc:
        return _error(f"{type(exc).__name__}: {exc}")


def book(space_id: str, day_iso: str, start_iso: str, end_iso: str) -> str:
    try:
        auth = _require_auth()
        day = date.fromisoformat(day_iso)
        start = datetime.fromisoformat(start_iso)
        end = datetime.fromisoformat(end_iso)
        start = start.replace(tzinfo=kulku.TZ) if start.tzinfo is None else start.astimezone(kulku.TZ)
        end = end.replace(tzinfo=kulku.TZ) if end.tzinfo is None else end.astimezone(kulku.TZ)

        data = _data(auth, fresh=True)
        space = data["spaces"].get(space_id)
        if not space:
            return _error("Unknown space")
        payload = kulku.fetch_space_day(auth, space_id, day)
        if payload.get("allowReservation") is False:
            return _error("This space is not bookable right now", "closed")
        weekly = kulku.day_schedule(space, day)
        if not weekly:
            return _error("This space is closed on that day", "closed")

        slots = kulku.build_slots(
            space, payload, day, datetime.now(kulku.TZ), kulku.own_booking_ids(data)
        )
        kulku.select_slots(
            slots, start, end, int(weekly.get("slotLength") or 60), weekly
        )

        status, response = kulku.request(
            "new/space/book",
            auth=auth,
            body={
                "spaceId": space.get("_id"),
                "bookingStart": start.strftime("%Y-%m-%dT%H:%M:00"),
                "bookingEnd": end.strftime("%Y-%m-%dT%H:%M:00"),
                "checkoutData": "",
            },
        )
        if not 200 <= status < 300:
            message = response.get("message") if isinstance(response, dict) else response
            code = response.get("code") if isinstance(response, dict) else None
            return _error(str(message or f"HTTP {status}"), code)

        booking_id = response
        if isinstance(response, dict):
            booking_id = response.get("_id") or response.get("id")
        if not booking_id:
            return _error(f"Unexpected booking response: {response!r}")
        _invalidate()
        return _ok(
            bookingId=str(booking_id).strip(),
            space=_space_dict(space),
            start=start.isoformat(),
            end=end.isoformat(),
        )
    except AppError as exc:
        return _error(str(exc), exc.code)
    except ValueError as exc:
        return _error(str(exc), "invalid")
    except kulku.ApiError as exc:
        return _error(str(exc), "auth" if exc.status == 401 else "api")
    except Exception as exc:
        return _error(f"{type(exc).__name__}: {exc}")


def book_direct(space_id: str, day_iso: str, start_iso: str, end_iso: str) -> str:
    """One-request booking for the UI, which already validated a free grid cell."""
    try:
        auth = _require_auth()
        start = datetime.fromisoformat(start_iso)
        end = datetime.fromisoformat(end_iso)
        start = start.replace(tzinfo=kulku.TZ) if start.tzinfo is None else start.astimezone(kulku.TZ)
        end = end.replace(tzinfo=kulku.TZ) if end.tzinfo is None else end.astimezone(kulku.TZ)
        status, response = kulku.request(
            "new/space/book",
            auth=auth,
            body={
                "spaceId": space_id,
                "bookingStart": start.strftime("%Y-%m-%dT%H:%M:00"),
                "bookingEnd": end.strftime("%Y-%m-%dT%H:%M:00"),
                "checkoutData": "",
            },
        )
        if not 200 <= status < 300:
            message = response.get("message") if isinstance(response, dict) else response
            code = response.get("code") if isinstance(response, dict) else None
            return _error(str(message or f"HTTP {status}"), code)
        booking_id = response
        if isinstance(response, dict):
            booking_id = response.get("_id") or response.get("id")
        if not booking_id:
            return _error(f"Unexpected booking response: {response!r}")
        _invalidate()
        return _ok(
            bookingId=str(booking_id).strip(),
            start=start.isoformat(),
            end=end.isoformat(),
        )
    except AppError as exc:
        return _error(str(exc), exc.code)
    except kulku.ApiError as exc:
        return _error(str(exc), "auth" if exc.status == 401 else "api")
    except Exception as exc:
        return _error(f"{type(exc).__name__}: {exc}")


def cancel(booking_id: str) -> str:
    try:
        auth = _require_auth()
        data = _data(auth, fresh=True)
        booking = next(
            (item for item in data["bookings"] if item["id"] == booking_id), None
        )
        if not booking:
            return _error("Reservation not found", "not_found")
        allowed, reason = kulku.cancellation_state(booking, data["now"])
        if not allowed:
            return _error(f"Cannot cancel: {reason}", "not_allowed")
        status, response = kulku.request(
            f"bookings/cancel/{booking_id}", auth=auth, method="POST"
        )
        if not 200 <= status < 300:
            message = response.get("message") if isinstance(response, dict) else response
            return _error(str(message or f"HTTP {status}"), "api")
        _invalidate()
        return _ok(bookingId=booking_id)
    except AppError as exc:
        return _error(str(exc), exc.code)
    except kulku.ApiError as exc:
        return _error(str(exc), "auth" if exc.status == 401 else "api")
    except Exception as exc:
        return _error(f"{type(exc).__name__}: {exc}")


def cancel_direct(booking_id: str) -> str:
    """One-request cancellation for the UI."""
    try:
        auth = _require_auth()
        status, response = kulku.request(
            f"bookings/cancel/{booking_id}", auth=auth, method="POST"
        )
        if not 200 <= status < 300:
            message = response.get("message") if isinstance(response, dict) else response
            return _error(str(message or f"HTTP {status}"), "api")
        _invalidate()
        return _ok(bookingId=booking_id)
    except AppError as exc:
        return _error(str(exc), exc.code)
    except kulku.ApiError as exc:
        return _error(str(exc), "auth" if exc.status == 401 else "api")
    except Exception as exc:
        return _error(f"{type(exc).__name__}: {exc}")
