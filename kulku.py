#!/usr/bin/env python3
"""KulkuTIME reservations in your terminal.

Usage:
    python3 scripts/kulku.py              # upcoming reservations
    python3 scripts/kulku.py --all         # upcoming + past
    python3 scripts/kulku.py --past        # past only
    python3 scripts/kulku.py --codes       # show access codes (default: hidden)
    python3 scripts/kulku.py --json        # machine-readable dump
    python3 scripts/kulku.py --cancel ID   # cancel a reservation (prefix ok)
    python3 scripts/kulku.py --change ID   # cancel + print the rebooking URL
    python3 scripts/kulku.py --login       # force a fresh login
    python3 scripts/kulku.py --logout      # forget the saved session

The script logs in directly against https://portal.kulku.io/api/v2/.
Only the session token is stored, in .auth.json (mode 600). Your password
is read with getpass and never written anywhere.

Kulku has no "edit reservation" API: changing a time means cancelling the old
reservation and booking a new one. This script can do the cancel half.

This is an unofficial client. Use it on your own account only.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import ssl
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

API = "https://portal.kulku.io/api/v2/"
TZ = ZoneInfo("Europe/Helsinki")
ROOT = os.path.dirname(os.path.abspath(__file__))
AUTH_FILE = os.path.join(ROOT, ".auth.json")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# ---------------------------------------------------------------------------
# tiny terminal helpers
# ---------------------------------------------------------------------------

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


class C:
    RESET = "\033[0m" if _COLOR else ""
    BOLD = "\033[1m" if _COLOR else ""
    DIM = "\033[2m" if _COLOR else ""
    RED = "\033[31m" if _COLOR else ""
    GREEN = "\033[32m" if _COLOR else ""
    YELLOW = "\033[33m" if _COLOR else ""
    BLUE = "\033[34m" if _COLOR else ""
    MAGENTA = "\033[35m" if _COLOR else ""
    CYAN = "\033[36m" if _COLOR else ""
    GREY = "\033[90m" if _COLOR else ""


def width(default: int = 100) -> int:
    return min(shutil.get_terminal_size((default, 24)).columns, 120)


def trunc(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def heading(text: str) -> None:
    print(f"\n{C.BOLD}{text}{C.RESET}")


def dim(text: str) -> str:
    return f"{C.DIM}{text}{C.RESET}"


# ---------------------------------------------------------------------------
# HTTP / auth
# ---------------------------------------------------------------------------


class ApiError(Exception):
    def __init__(self, status: int, payload):
        self.status = status
        self.payload = payload
        message = payload.get("message") if isinstance(payload, dict) else None
        super().__init__(message or f"HTTP {status}")


def request(path: str, *, auth: dict | None = None, body=None, method: str | None = None):
    url = API + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method or ("POST" if data is not None else "GET"),
    )
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/json")
    if auth:
        req.add_header("x-Auth-Token", auth["authToken"])
        req.add_header("x-User-Id", auth["userId"])
    try:
        with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=60) as res:
            raw = res.read().decode() or "null"
            return res.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = raw
        return e.code, payload


def normalize_phone(value: str) -> str:
    value = value.replace(" ", "")
    value = re.sub(r"[^\d+]", "", value)
    value = re.sub(r"^00", "+", value)
    if not value:
        return value
    if re.match(r"^1", value):
        value = "+" + value
    if not value.startswith("+"):
        value = "+358" + value
    if value.startswith("+3580"):
        value = "+358" + value[5:]
    return value


def load_auth() -> dict | None:
    try:
        with open(AUTH_FILE) as f:
            auth = json.load(f)
        if auth.get("userId") and auth.get("authToken"):
            return auth
    except (OSError, ValueError):
        pass
    return None


def save_auth(auth: dict) -> None:
    with open(AUTH_FILE, "w") as f:
        json.dump(auth, f, indent=2)
    os.chmod(AUTH_FILE, 0o600)


def login_interactive() -> dict:
    if not sys.stdin.isatty():
        print("No valid session and stdin is not a terminal; cannot log in.", file=sys.stderr)
        raise SystemExit(2)
    print(f"{C.BOLD}Kulku login{C.RESET}")
    phone = input("Phone number (e.g. 040 123 4567): ").strip()
    password = getpass.getpass("Password: ")
    username = normalize_phone(phone)
    status, payload = request("login", body={"username": username, "password": password})
    data = payload.get("data") if isinstance(payload, dict) else None
    if status != 200 or not data or not data.get("authToken"):
        message = payload.get("message") if isinstance(payload, dict) else f"HTTP {status}"
        print(f"Login failed: {message}", file=sys.stderr)
        raise SystemExit(1)
    auth = {
        "userId": data["userId"],
        "authToken": data["authToken"],
        "username": username,
    }
    save_auth(auth)
    print(f"Logged in. Session saved to {AUTH_FILE}\n")
    return auth


def ensure_auth(force: bool = False) -> dict:
    if not force:
        auth = load_auth()
        if auth:
            status, _ = request("myDetails", auth=auth)
            if status == 200:
                return auth
            if status == 401:
                print("Saved session expired; please sign in again.", file=sys.stderr)
            else:
                print(f"Could not validate session (HTTP {status}); using saved token.", file=sys.stderr)
                return auth
    return login_interactive()


def get_json(path: str, auth: dict):
    status, payload = request(path, auth=auth)
    if status == 401:
        raise ApiError(401, {"message": "Session expired"})
    if status != 200:
        raise ApiError(status, payload)
    return payload


def as_list(payload) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "bookings", "items", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


# ---------------------------------------------------------------------------
# data normalization
# ---------------------------------------------------------------------------


def parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ)
    except (TypeError, ValueError):
        return None


def space_name(space: dict, fallback: str = "") -> str:
    translations = space.get("translations") or {}
    for lang in ("fi", "en", "sv"):
        name = (translations.get(lang) or {}).get("name")
        if name:
            return str(name)
    return str(space.get("spaceName") or space.get("name") or fallback or "")


def space_address(space: dict) -> str:
    sites = space.get("houses") or space.get("sites") or []
    for site in sites:
        address = (site or {}).get("address")
        if isinstance(address, str) and address.strip():
            return address.split(",")[0].strip()
        if isinstance(address, dict):
            value = address.get("street") or address.get("address")
            if value:
                return str(value)
    return ""


def access_code(raw: dict) -> str | None:
    code = (raw.get("iLoq") or {}).get("accessCode")
    if code:
        return str(code)
    pin = (raw.get("abloyPulse") or {}).get("pincode")
    if pin:
        return str(pin)
    return None


def format_code(code: str | None) -> str:
    if not code:
        return ""
    text = str(code)
    if re.fullmatch(r"\d{6}", text):
        return f"{text[:3]} {text[3:]}"
    return text


def cancel_rule(raw: dict) -> float:
    value = raw.get("allowedHoursToCancelBefore")
    if value is None:
        value = ((raw.get("space") or {}).get("reservationSettings") or {}).get(
            "allowedHoursToCancelBefore"
        )
    if value is None:
        return 24.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 24.0


def normalize_booking(raw: dict, spaces: dict, now: datetime) -> dict | None:
    start = parse_dt(raw.get("bookingStart"))
    end = parse_dt(raw.get("bookingEnd"))
    if not start or not end:
        return None
    space = raw.get("space") or spaces.get(raw.get("spaceId")) or {}
    event = raw.get("event") or {}
    name = event.get("eventName") or space_name(space) or raw.get("spaceName") or "Reservation"
    hours = cancel_rule(raw)
    payment = raw.get("paymentDetails") or {}
    amount = payment.get("amountPaid")
    if amount is None:
        checkout_amount = (raw.get("checkoutData") or {}).get("checkout-amount")
        try:
            amount = int(float(checkout_amount)) if checkout_amount else None
        except (TypeError, ValueError):
            amount = None
    booking = {
        "id": str(raw.get("_id") or ""),
        "start": start,
        "end": end,
        "name": str(name),
        "address": space_address(space),
        "code": access_code(raw),
        "cancelled": bool(raw.get("cancelled")),
        "space_id": (space.get("_id") or raw.get("spaceId") or ""),
        "allowed_hours": hours,
        "payment_status": raw.get("paymentStatus"),
        "amount": amount,
        "virtual": False,
        "recurring": None,
        "raw": raw,
    }
    allowed, reason = cancellation_state(booking, now)
    booking["cancel_allowed"] = allowed
    booking["cancel_reason"] = reason
    return booking


def cancellation_state(booking: dict, now: datetime) -> tuple[bool, str]:
    if booking["cancelled"]:
        return False, "already cancelled"
    if booking["virtual"]:
        return False, "part of a recurring series"
    if booking["end"] <= now:
        return False, "already ended"
    hours = booking["allowed_hours"]
    if hours == -1:
        return True, "can cancel until the start"
    if hours < 0:
        return False, "cancellation is not allowed"
    deadline = booking["start"] - timedelta(hours=hours)
    if now >= deadline:
        return False, f"cancellation closed {deadline:%d.%m. at %H:%M}"
    return True, f"cancel by {deadline:%d.%m. at %H:%M}"


def normalize_series(raw: dict, spaces: dict) -> dict | None:
    day = str(raw.get("day") or "").lower()
    if day not in WEEKDAYS:
        return None
    space = raw.get("space") or spaces.get(raw.get("spaceId")) or {}
    validity_start = parse_dt(raw.get("start") or raw.get("validityStart"))
    validity_end = parse_dt(raw.get("end") or raw.get("validityEnd"))
    series = {
        "id": str(raw.get("id") or raw.get("_id") or ""),
        "entry_id": raw.get("entryId"),
        "day": day,
        "start_h": int(raw.get("startAtHours") or 0),
        "start_m": int(raw.get("startAtMins") or 0),
        "end_h": int(raw.get("endAtHours") or 0),
        "end_m": int(raw.get("endAtMins") or 0),
        "validity_start": validity_start,
        "validity_end": validity_end,
        "name": space_name(space),
        "address": space_address(space),
        "code": raw.get("accessCode") or access_code(raw),
        "space_id": space.get("_id") or raw.get("spaceId") or "",
        "raw": raw,
    }
    return series


def next_occurrence(series: dict, now: datetime) -> tuple[datetime, datetime] | None:
    today = now.date()
    for offset in range(0, 366):
        d = today + timedelta(days=offset)
        if WEEKDAYS[d.weekday()] != series["day"]:
            continue
        if series["validity_start"] and d < series["validity_start"].date():
            continue
        if series["validity_end"] and d > series["validity_end"].date():
            break
        start = datetime.combine(
            d, time(series["start_h"], series["start_m"]), tzinfo=TZ
        )
        end = datetime.combine(d, time(series["end_h"], series["end_m"]), tzinfo=TZ)
        if end <= now:
            continue
        return start, end
    return None


def series_occurrence_booking(series: dict, now: datetime) -> dict | None:
    occurrence = next_occurrence(series, now)
    if not occurrence:
        return None
    start, end = occurrence
    return {
        "id": f"rr:{series['id']}:{start:%Y-%m-%d}",
        "start": start,
        "end": end,
        "name": series["name"],
        "address": series["address"],
        "code": series["code"],
        "cancelled": False,
        "space_id": series["space_id"],
        "allowed_hours": 0,
        "payment_status": None,
        "amount": None,
        "virtual": True,
        "recurring": series,
        "cancel_allowed": False,
        "cancel_reason": "part of a recurring series",
        "raw": {},
    }


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------


def fmt_day(dt: datetime, now: datetime) -> str:
    d = dt.date()
    today = now.date()
    if d == today:
        return "Today"
    if d == today + timedelta(days=1):
        return "Tomorrow"
    if d == today - timedelta(days=1):
        return "Yesterday"
    return f"{d:%a} {d.day} {d:%b}"


def fmt_duration(start: datetime, end: datetime) -> str:
    minutes = max(1, round((end - start).total_seconds() / 60))
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours} h {mins} min"
    if hours:
        return f"{hours} h"
    return f"{mins} min"


def fmt_price(amount) -> str:
    if amount is None:
        return ""
    try:
        return f"{int(amount) / 100:.2f} €"
    except (TypeError, ValueError):
        return ""


def day_groups(items: list) -> list[tuple[date, list]]:
    groups: list[tuple[date, list]] = []
    for item in items:
        d = item["start"].date()
        if groups and groups[-1][0] == d:
            groups[-1][1].append(item)
        else:
            groups.append((d, [item]))
    return groups


def print_booking(booking: dict, now: datetime, show_code: bool, indent: str = "  ") -> None:
    active = booking["start"] <= now < booking["end"] and not booking["cancelled"]
    when = f"{booking['start']:%H:%M}–{booking['end']:%H:%M}"
    if active:
        marker = f"{C.GREEN}●{C.RESET}"
        when = f"{C.GREEN}{when}{C.RESET}"
    elif booking["virtual"]:
        marker = f"{C.MAGENTA}↻{C.RESET}"
    else:
        marker = " "
    tags = []
    if booking["virtual"]:
        tags.append(f"{C.MAGENTA}recurring{C.RESET}")
    if booking["cancelled"]:
        status = (booking.get("payment_status") or "").lower()
        tags.append(f"{C.RED}{'refunded' if status == 'refunded' else 'cancelled'}{C.RESET}")
    if booking.get("amount") and not booking["cancelled"]:
        tags.append(dim(fmt_price(booking["amount"])))
    tag_text = ("  " + " · ".join(tags)) if tags else ""

    term = width()
    name_width = max(14, min(34, term - 62))
    addr_width = max(0, term - name_width - 46)
    name = trunc(booking["name"], name_width)
    address = trunc(booking["address"], addr_width) if addr_width > 3 else ""
    code = ""
    if show_code and booking.get("code"):
        code = f"  {C.CYAN}{format_code(booking['code'])}{C.RESET}"
    print(f"{indent}{marker} {when}  {C.BOLD}{name}{C.RESET}{tag_text}")
    meta = "  ".join(part for part in (address, fmt_duration(booking["start"], booking["end"])) if part)
    if meta or code:
        print(f"{indent}     {dim(meta)}{code}")
    if booking["cancelled"]:
        print(f"{indent}     {C.RED}This reservation was cancelled.{C.RESET}")


def print_upcoming(items: list, now: datetime, show_code: bool) -> None:
    heading("UPCOMING")
    if not items:
        print(f"  {dim('Nothing booked. Enjoy the free time.')}")
        return
    for d, group in day_groups(items):
        label = fmt_day(group[0]["start"], now)
        suffix = f" {dim(f'{d:%a} {d.day} {d:%b}')}" if label in ("Today", "Tomorrow") else ""
        print(f"\n  {C.BOLD}{label}{C.RESET}{suffix}")
        for booking in group:
            print_booking(booking, now, show_code)


def print_past(items: list, now: datetime, show_code: bool, limit: int = 20) -> None:
    heading("PAST")
    if not items:
        print(f"  {dim('No past reservations.')}")
        return
    shown = items[:limit] if limit > 0 else items
    for d, group in day_groups(shown):
        label = fmt_day(group[0]["start"], now)
        suffix = f" {dim(f'{d:%a} {d.day} {d:%b}')}" if label in ("Today", "Yesterday") else ""
        print(f"\n  {C.BOLD}{label}{C.RESET}{suffix}")
        for booking in group:
            print_booking(booking, now, show_code)
    if limit > 0 and len(items) > limit:
        print(f"  {dim(f'… and {len(items) - limit} more (use --limit 0 to show all)')}")


def print_series(series_list: list, now: datetime, show_code: bool) -> None:
    if not series_list:
        return
    heading("RECURRING")
    for series in series_list:
        occurrence = next_occurrence(series, now)
        schedule = f"Every {series['day'].title()} {series['start_h']:02d}:{series['start_m']:02d}–{series['end_h']:02d}:{series['end_m']:02d}"
        validity = ""
        if series["validity_start"] or series["validity_end"]:
            start = f"{series['validity_start']:%d.%m.%Y}" if series["validity_start"] else "…"
            end = f"{series['validity_end']:%d.%m.%Y}" if series["validity_end"] else "…"
            validity = f"  {dim(f'{start} – {end}')}"
        code = ""
        if show_code and series.get("code"):
            code = f"  {C.CYAN}{format_code(series['code'])}{C.RESET}"
        next_text = ""
        if occurrence:
            start, _ = occurrence
            label = fmt_day(start, now).lower()
            next_text = f"  {dim('next ' + label)}"
        print(f"  {C.MAGENTA}↻{C.RESET} {schedule}  {C.BOLD}{series['name']}{C.RESET}")
        print(f"     {dim(series['address'])}{validity}{next_text}{code}")


def print_header(count_upcoming: int, count_series: int, now: datetime) -> None:
    print(f"{C.BOLD}{C.CYAN}KULKU{C.RESET} {dim('· reservations')}")
    print(dim(f"Updated {now:%H:%M} · {count_upcoming} upcoming · {count_series} recurring"))


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def fetch_data(auth: dict):
    bookings_raw = as_list(get_json("bookings?includeCancelled=1", auth))
    series_raw = as_list(get_json("recurring-reservations", auth))
    try:
        spaces_raw = as_list(get_json("spaces", auth))
    except ApiError:
        spaces_raw = []
    spaces = {str(s.get("_id")): s for s in spaces_raw if isinstance(s, dict) and s.get("_id")}
    now = datetime.now(TZ)

    bookings = [b for b in (normalize_booking(raw, spaces, now) for raw in bookings_raw) if b]
    series = [s for s in (normalize_series(raw, spaces) for raw in series_raw) if s]

    real_upcoming = [b for b in bookings if not b["cancelled"] and b["end"] > now]
    virtual = [b for b in (series_occurrence_booking(s, now) for s in series) if b]
    upcoming = sorted(real_upcoming + virtual, key=lambda b: b["start"])
    past = sorted(
        [b for b in bookings if b["cancelled"] or b["end"] <= now],
        key=lambda b: b["start"],
        reverse=True,
    )
    return {
        "now": now,
        "bookings": bookings,
        "series": series,
        "upcoming": upcoming,
        "past": past,
        "spaces": spaces,
    }


def cmd_show(data: dict, args) -> None:
    now = data["now"]
    upcoming = data["upcoming"]
    if args.week:
        cutoff = now + timedelta(days=7)
        upcoming = [b for b in upcoming if b["start"] < cutoff]
    if args.limit > 0 and args.past:
        past_limit = args.limit
    else:
        past_limit = 20

    print_header(len(upcoming), len(data["series"]), now)
    show_past = args.all or args.past
    show_upcoming = not args.past

    if show_upcoming:
        if upcoming:
            next_booking = next((b for b in upcoming if b["start"] <= now < b["end"]), upcoming[0])
            print()
            label = "NOW" if next_booking["start"] <= now < next_booking["end"] else "NEXT UP"
            color = C.GREEN if label == "NOW" else C.CYAN
            print(f"{color}{C.BOLD}{label}{C.RESET}  {next_booking['name']}")
            print(
                f"      {C.BOLD}{next_booking['start']:%H:%M}–{next_booking['end']:%H:%M}{C.RESET}  "
                f"{fmt_day(next_booking['start'], now)} {next_booking['start']:%d.%m.}"
            )
            if next_booking["address"]:
                print(f"      {dim(next_booking['address'])}")
            if args.codes and next_booking["code"]:
                print(f"      {C.CYAN}code {format_code(next_booking['code'])}{C.RESET}")
        print_upcoming(upcoming, now, args.codes)
        if not show_past:
            print_series(data["series"], now, args.codes)
    if show_past:
        print_past(data["past"], now, args.codes, past_limit)
    if show_past and args.all:
        print_series(data["series"], now, args.codes)


def cmd_json(data: dict) -> None:
    def serialize(booking):
        return {
            "id": booking["id"],
            "start": booking["start"].isoformat(),
            "end": booking["end"].isoformat(),
            "name": booking["name"],
            "address": booking["address"],
            "accessCode": booking["code"],
            "cancelled": booking["cancelled"],
            "recurring": booking["virtual"],
            "cancelAllowed": booking["cancel_allowed"],
            "paymentStatus": booking["payment_status"],
            "amountPaid": booking["amount"],
        }

    payload = {
        "now": data["now"].isoformat(),
        "upcoming": [serialize(b) for b in data["upcoming"]],
        "past": [serialize(b) for b in data["past"]],
        "recurring": [
            {
                "id": s["id"],
                "day": s["day"],
                "start": f"{s['start_h']:02d}:{s['start_m']:02d}",
                "end": f"{s['end_h']:02d}:{s['end_m']:02d}",
                "name": s["name"],
                "address": s["address"],
                "validityStart": s["validity_start"].isoformat() if s["validity_start"] else None,
                "validityEnd": s["validity_end"].isoformat() if s["validity_end"] else None,
            }
            for s in data["series"]
        ],
    }
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
    print()


def find_booking(data: dict, needle: str) -> dict | None:
    matches = []
    for booking in data["bookings"]:
        if booking["id"] == needle or booking["id"].lower().startswith(needle.lower()):
            matches.append(booking)
    if not matches:
        print(f"No reservation found with id/prefix {needle!r}.", file=sys.stderr)
        return None
    if len(matches) > 1:
        print("That prefix matches several reservations; use more characters:", file=sys.stderr)
        for booking in matches:
            print(f"  {booking['id']}  {booking['start']:%d.%m. %H:%M}  {booking['name']}", file=sys.stderr)
        return None
    return matches[0]


def print_cancel_header(booking: dict, now: datetime) -> None:
    allowed, reason = cancellation_state(booking, now)
    print(f"{C.BOLD}{booking['name']}{C.RESET}")
    print(f"  {booking['start']:%a %d.%m.%Y %H:%M}–{booking['end']:%H:%M}  {dim(booking['address'])}")
    print(f"  id {booking['id']}")
    print(f"  {dim(reason)}")
    if booking["payment_status"] == "refunded":
        print(f"  {C.CYAN}Payment already refunded.{C.RESET}")
    elif booking.get("amount"):
        print(f"  Paid {fmt_price(booking['amount'])}; refund depends on the space's policy.")


def do_cancel(booking: dict, auth: dict) -> bool:
    status, payload = request(f"bookings/cancel/{booking['id']}", auth=auth, method="POST")
    if status != 200:
        message = payload.get("message") if isinstance(payload, dict) else payload
        print(f"{C.RED}Cancellation failed ({status}):{C.RESET} {message}", file=sys.stderr)
        return False
    print(f"{C.GREEN}Cancelled.{C.RESET} {booking['name']}, {booking['start']:%a %d.%m. %H:%M}–{booking['end']:%H:%M}")
    return True


def cmd_cancel(data: dict, args, auth: dict) -> int:
    booking = find_booking(data, args.cancel)
    if not booking:
        return 1
    allowed, reason = cancellation_state(booking, data["now"])
    print_cancel_header(booking, data["now"])
    if not allowed:
        print(f"{C.RED}Cannot cancel:{C.RESET} {reason}.", file=sys.stderr)
        return 1
    if not args.yes:
        if not sys.stdin.isatty():
            print("Refusing to cancel without --yes in a non-interactive shell.", file=sys.stderr)
            return 1
        answer = input("\nCancel this reservation? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Not cancelled.")
            return 0
    return 0 if do_cancel(booking, auth) else 1


def cmd_change(data: dict, args, auth: dict) -> int:
    booking = find_booking(data, args.change)
    if not booking:
        return 1
    allowed, reason = cancellation_state(booking, data["now"])
    print_cancel_header(booking, data["now"])
    if not allowed:
        print(f"{C.RED}Cannot change:{C.RESET} {reason}.", file=sys.stderr)
        return 1
    print(f"\n{C.YELLOW}Kulku has no edit API. Changing means cancelling this slot and booking a new one.{C.RESET}")
    if not args.yes:
        if not sys.stdin.isatty():
            print("Refusing to cancel without --yes in a non-interactive shell.", file=sys.stderr)
            return 1
        answer = input("Cancel this reservation and continue to rebooking? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Not changed.")
            return 0
    if not do_cancel(booking, auth):
        return 1
    url = (
        f"https://time.kulku.io/book-space/{booking['space_id']}"
        if booking.get("space_id")
        else "https://time.kulku.io/book"
    )
    print(f"\nBook the new time here: {C.CYAN}{url}{C.RESET}")
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Show and cancel your KulkuTIME reservations.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--all", action="store_true", help="show upcoming and past")
    parser.add_argument("--past", action="store_true", help="show past only")
    parser.add_argument("--week", action="store_true", help="only the next 7 days")
    parser.add_argument("--codes", action="store_true", help="show access codes")
    parser.add_argument("--limit", type=int, default=20, help="past entries to show (0 = all)")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--cancel", metavar="ID", help="cancel a reservation by id or prefix")
    parser.add_argument("--change", metavar="ID", help="cancel a reservation and print the rebooking URL")
    parser.add_argument("-y", "--yes", action="store_true", help="do not ask before cancelling")
    parser.add_argument("--login", action="store_true", help="force a new login")
    parser.add_argument("--logout", action="store_true", help="remove the saved session")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colours")
    args = parser.parse_args()

    if args.no_color:
        global _COLOR
        _COLOR = False
        for name in ("RESET", "BOLD", "DIM", "RED", "GREEN", "YELLOW", "BLUE", "MAGENTA", "CYAN", "GREY"):
            setattr(C, name, "")

    if args.logout:
        try:
            os.remove(AUTH_FILE)
            print("Saved session removed.")
        except FileNotFoundError:
            print("No saved session.")
        return 0

    auth = ensure_auth(force=args.login)
    try:
        data = fetch_data(auth)
    except ApiError as e:
        if e.status == 401:
            print("Session expired; please sign in again.", file=sys.stderr)
            auth = login_interactive()
            data = fetch_data(auth)
        else:
            print(f"Kulku API error: {e}", file=sys.stderr)
            return 1
    except urllib.error.URLError as e:
        print(f"Network error: {e.reason}", file=sys.stderr)
        return 1

    if args.cancel:
        return cmd_cancel(data, args, auth)
    if args.change:
        return cmd_change(data, args, auth)
    if args.json:
        cmd_json(data)
        return 0
    cmd_show(data, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
