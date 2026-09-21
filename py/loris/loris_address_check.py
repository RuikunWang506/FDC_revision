"""Compare a Loris ship-to address with the internal store address using local Ollama."""
from __future__ import annotations

import json
import logging
import queue
import re
import socket
import threading
import time
from datetime import datetime
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_CHECK_QUEUE: queue.Queue[str] = queue.Queue()
_QUEUED: set[str] = set()
_QUEUE_LOCK = threading.Lock()
_PO_LOCKS: dict[str, threading.Lock] = {}
_PO_LOCKS_GUARD = threading.Lock()
_WORKER_STARTED = False

# Confirm only when the model is this sure the street, city, and state are the same place.
# A matching ZIP alone cannot reach this score.
CONFIRM_CONFIDENCE = 80

_STREET_TYPES = {
    "avenue": "ave", "ave": "ave", "av": "ave",
    "street": "st", "st": "st",
    "road": "rd", "rd": "rd",
    "drive": "dr", "dr": "dr",
    "boulevard": "blvd", "blvd": "blvd",
    "lane": "ln", "ln": "ln",
    "court": "ct", "ct": "ct",
    "place": "pl", "pl": "pl",
    "parkway": "pkwy", "pkwy": "pkwy",
    "highway": "hwy", "hwy": "hwy",
    "circle": "cir", "cir": "cir",
    "terrace": "ter", "ter": "ter",
    "trail": "trl", "trl": "trl",
    "north": "n", "n": "n",
    "south": "s", "s": "s",
    "east": "e", "e": "e",
    "west": "w", "w": "w",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
    "nw": "nw", "ne": "ne", "sw": "sw", "se": "se",
}
_STREET_SUFFIXES = {"ave", "st", "rd", "dr", "blvd", "ln", "ct", "pl", "pkwy", "hwy", "cir", "ter", "trl", "way"}

_SYSTEM_PROMPT = (
    "You compare two US shipping addresses for one store order. "
    "INTERNAL is the address we will actually ship to. It is often more detailed. "
    "SHIP TO is the purchase-order address and is often shorter. "
    "Judge the street number, street name, city, and state. A matching ZIP is not enough. "
    "Abbreviations match (Rd/Road, St/Street, Ave/Avenue, N/North). "
    "A longer company name, an extra suite, a gift-shop line, or a phone number on INTERNAL still matches. "
    "A missing ZIP on SHIP TO does not lower the score when the street, city, and state match. "
    "ZIP+4 matches the same 5-digit ZIP. "
    "If the street number, street name, city, state, or 5-digit ZIP is a different place, match is false. "
    "confidence is an integer from 0 to 100 for how sure the street number, street name, city, and state are the same place. "
    "Use 90 to 100 only when those four all agree. "
    "Use 40 or below when only the ZIP agrees, or when any address line disagrees. "
    "Example high score: SHIP TO '4700 Waters Avenue, Savannah, GA 31404' and "
    "INTERNAL '4700 WATERS AVE, Lori's Gift Shop, SAVANNAH GA 31404-6220'. "
    "Example mismatch: Bristol, CT 06011 versus Bristol, CT 06010. "
    "Example mismatch: Fort Worth TX 76104 versus Houston TX 77024. "
    'Reply with JSON only: {"match": true or false, "confidence": 0, "message": "one short sentence"}.'
)


def zip_on_city_line(text: str) -> str:
    """5-digit ZIP from the city line. A leading street number is ignored."""
    lines = [
        line.strip()
        for line in str(text or "").splitlines()
        if line.strip() and not line.strip().lower().startswith("phone")
    ]
    for line in reversed(lines):
        if re.match(r"^\d+\s", line):
            continue
        found = re.search(r"\b(\d{5})(?:-\d{4})?\b", line)
        if found:
            return found.group(1)
        if re.search(r"[A-Za-z]", line):
            return ""
    return ""


def apply_zip_guard(status: str, message: str, ship_to: str, internal: str) -> tuple[str, str]:
    """Do not confirm two addresses when both ZIPs are present and different."""
    guarded = apply_address_guard(status, message, 100, ship_to, internal)
    return guarded[0], guarded[1]


def _address_lines(text: str) -> list[str]:
    return [
        line.strip()
        for line in str(text or "").splitlines()
        if line.strip() and not line.strip().lower().startswith("phone")
    ]


def _norm_tokens(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", str(text or "").lower())
    return [_STREET_TYPES.get(token, token) for token in tokens]


def _street_parts(text: str) -> tuple[str, list[str]] | None:
    """House number and street-name tokens from the first street line."""
    for line in _address_lines(text):
        if not re.match(r"^\d+(?:-\d+)?\s+[A-Za-z0-9]", line):
            continue
        tokens = _norm_tokens(line)
        if not tokens or not tokens[0].isdigit():
            continue
        name: list[str] = []
        for token in tokens[1:]:
            if token in ("ste", "suite", "apt", "unit", "fl", "floor", "bldg", "building"):
                break
            name.append(token)
        if name:
            return tokens[0], name
    return None


_NAME_WORDS = {
    "freeway": "fwy", "fwy": "fwy",
    "expressway": "expy", "expy": "expy", "expwy": "expy",
    "highway": "hwy", "hwy": "hwy",
    "saint": "st", "state": "st",
    "first": "1st", "second": "2nd", "third": "3rd", "fourth": "4th",
    "fifth": "5th", "sixth": "6th", "seventh": "7th", "eighth": "8th",
    "ninth": "9th", "tenth": "10th",
    "roade": "rd",
}
_STREET_IGNORE = {"frnt", "front", "rear", "lbby", "lobby"}
_DIRECTIONS = {"n", "s", "e", "w", "ne", "nw", "se", "sw"}


def _street_core(tokens: list[str]) -> list[str]:
    if tokens and tokens[-1] in _STREET_SUFFIXES:
        return tokens[:-1]
    return tokens


def _street_signature(tokens: list[str]) -> list[str]:
    """Street words with First/1st and 27th/#27 treated as the same number."""
    signature = []
    for token in tokens:
        token = _NAME_WORDS.get(token, token)
        if token in _STREET_IGNORE or token == "s":
            continue
        if token in _DIRECTIONS or token in _STREET_SUFFIXES:
            continue
        ordinal = re.fullmatch(r"(\d+)(?:st|nd|rd|th)", token)
        if ordinal:
            token = ordinal.group(1)
        if signature and signature[-1] == token:
            continue
        signature.append(token)
    return signature


def _street_names_match(left: list[str], right: list[str]) -> bool:
    if _street_core(left) == _street_core(right) and _street_core(left):
        return True
    a = _street_signature(left)
    b = _street_signature(right)
    if a == b and a:
        return True
    a_names = [token for token in a if not token.isdigit()]
    b_names = [token for token in b if not token.isdigit()]
    a_numbers = [token for token in a if token.isdigit()]
    b_numbers = [token for token in b if token.isdigit()]
    if a_names == b_names and a_numbers and a_numbers == b_numbers:
        return True
    joined_a = "".join(a_names)
    joined_b = "".join(b_names)
    if joined_a and joined_a == joined_b and a_numbers == b_numbers:
        return True
    if min(len(joined_a), len(joined_b)) >= 4 and _edit_distance(joined_a, joined_b) <= 2 and a_numbers == b_numbers:
        return True
    return False


def _has_street_line(text: str) -> bool:
    if _street_parts(text):
        return True
    for line in _address_lines(text):
        if re.search(r"\b(street|st|ave|avenue|rd|road|dr|drive|plaza|pkwy|blvd|way|ln|lane|hwy|highway)\b", line, re.I):
            return True
    return False


_CITY_WORDS = {
    "hls": "hills",
    "hts": "heights",
    "hgts": "heights",
    "vlg": "village",
    "spgs": "springs",
    "ft": "fort",
    "mt": "mount",
    "st": "saint",
    "bch": "beach",
    "ctr": "center",
    "jct": "junction",
}


def _edit_distance(left: str, right: str) -> int:
    if abs(len(left) - len(right)) > 2:
        return 3
    prev = list(range(len(right) + 1))
    for i, char_left in enumerate(left, 1):
        current = [i]
        for j, char_right in enumerate(right, 1):
            current.append(min(
                prev[j] + 1,
                current[-1] + 1,
                prev[j - 1] + (char_left != char_right),
            ))
        prev = current
    return prev[-1]


def _city_key(city: str) -> str:
    tokens = [_CITY_WORDS.get(token, token) for token in city.lower().split()]
    return " ".join(tokens)


def _cities_match(left: str, right: str) -> bool:
    """Same city, including ZIP+4 neighbors, abbreviations, and a short PDF fragment."""
    if left == right:
        return True
    a, b = _city_key(left), _city_key(right)
    if a == b:
        return True
    if min(len(a), len(b)) >= 6 and _edit_distance(a, b) <= 2:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if 3 <= len(short) < len(long) and _edit_distance(short, long[-len(short):]) <= 1:
        return True
    if len(short) == 1 and len(long) >= 6:
        return True
    return False


def _city_state(text: str) -> tuple[str, str] | None:
    for line in reversed(_address_lines(text)):
        if re.match(r"^\d+\s", line):
            continue
        found = re.search(
            r"^(.+?)[,\s]+([A-Za-z]{2})(?:\s+\d{5}(?:-\d{4})?)?\s*$",
            line.strip(),
        )
        if not found:
            continue
        city = " ".join(re.findall(r"[a-z]+", found.group(1).lower()))
        state = found.group(2).upper()
        if city and state:
            return city, state
    for line in reversed(_address_lines(text)):
        found = re.search(r"^([A-Za-z][A-Za-z .'-]*?)\s+(\d{5})(?:-\d{4})?\s*$", line.strip())
        if found:
            city = " ".join(re.findall(r"[a-z]+", found.group(1).lower()))
            if city:
                return city, ""
    return None


def _parse_confidence(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    raw = str(value).strip().rstrip("%")
    if not raw:
        return None
    try:
        number = float(raw)
    except ValueError:
        return None
    if number != number:
        return None
    return max(0, min(100, int(round(number))))


def apply_address_guard(
    status: str,
    message: str,
    confidence: int | None,
    ship_to: str,
    internal: str,
) -> tuple[str, str, int | None]:
    """Confirm only when the street, city, and state agree and confidence clears the bar."""
    score = _parse_confidence(confidence)
    reasons = []
    ship_zip = zip_on_city_line(ship_to)
    internal_zip = zip_on_city_line(internal)
    if ship_zip and internal_zip and ship_zip != internal_zip:
        reasons.append(f"ZIP {ship_zip} vs {internal_zip}")

    ship_street = _street_parts(ship_to)
    internal_street = _street_parts(internal)
    if ship_street and internal_street:
        if ship_street[0] != internal_street[0]:
            reasons.append(f"street number {ship_street[0]} vs {internal_street[0]}")
        elif not _street_names_match(ship_street[1], internal_street[1]):
            reasons.append(
                f"street {' '.join(ship_street[1])} vs {' '.join(internal_street[1])}"
            )

    ship_place = _city_state(ship_to)
    internal_place = _city_state(internal)
    if ship_place and internal_place:
        if ship_place[1] and internal_place[1] and ship_place[1] != internal_place[1]:
            reasons.append(f"state {ship_place[1]} vs {internal_place[1]}")
        elif not _cities_match(ship_place[0], internal_place[0]):
            reasons.append(f"city {ship_place[0]} vs {internal_place[0]}")

    if reasons:
        shown = min(score if score is not None else 0, 39)
        detail = "; ".join(reasons)
        return "wrong", f"The address lines do not match ({detail}).", shown

    places_ok = bool(ship_place and internal_place)
    zips_agree = bool(ship_zip and internal_zip and ship_zip == internal_zip) or not (ship_zip and internal_zip)
    same_lines = bool(ship_street and internal_street and places_ok and zips_agree)
    shorter_po = bool(
        not ship_street
        and _has_street_line(internal)
        and places_ok
        and ship_zip
        and internal_zip
        and ship_zip == internal_zip
    )
    if same_lines or shorter_po:
        # 40215-1161 and 40215 are the same ZIP. A model complaint about the
        # extra 4 digits does not override a matching street, city, and state.
        shown = max(score or 0, 92)
        if status == "confirm" and message:
            return "confirm", message, shown
        return (
            "confirm",
            "Street, city, state, and ZIP match. The extra 4 digits on the ZIP are the same ZIP.",
            shown,
        )

    if status == "confirm":
        if score is None or score < CONFIRM_CONFIDENCE:
            shown = 0 if score is None else score
            note = (
                f"Confidence {shown}% is below {CONFIRM_CONFIDENCE}%. "
                "The street, city, and state were not sure enough to confirm."
            )
            text = f"{message} {note}".strip() if message else note
            return "wrong", text, shown
        return "confirm", message, score

    if status == "wrong":
        return "wrong", message, 0 if score is None else score
    return status, message, score


def addresses_match(ship_to: str, internal: str) -> bool:
    """True when a past ship-to is the same destination as the internal address."""
    if not str(ship_to or "").strip() or not str(internal or "").strip():
        return False
    status, _message, _score = apply_address_guard("confirm", "", 100, ship_to, internal)
    return status == "confirm"


def _order_column_exists(cursor, column: str) -> bool:
    cursor.execute(
        """
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'lori_orders'
          AND COLUMN_NAME = %s
        """,
        (column,),
    )
    row = cursor.fetchone()
    if row is None:
        return False
    count = list(row.values())[0] if isinstance(row, dict) else row[0]
    return int(count or 0) > 0


def ensure_shipped_address_column(cursor) -> None:
    """Keep the internal address that was on file when the order shipped."""
    if _order_column_exists(cursor, "shipped_internal_address"):
        return
    cursor.execute(
        "ALTER TABLE lori_orders ADD COLUMN shipped_internal_address MEDIUMTEXT NULL"
    )


def _store_row(cursor, store_number: str) -> dict | None:
    cursor.execute(
        """
        SELECT company, address1, address2, address3, city, state, zip_code, phone
        FROM loris_store_info
        WHERE store_number = %s
        """,
        (store_number,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    if isinstance(row, dict):
        return row
    keys = ("company", "address1", "address2", "address3", "city", "state", "zip_code", "phone")
    return dict(zip(keys, row))


def freeze_shipped_internal_addresses(cursor, po_numbers: list[str] | None = None) -> int:
    """Save today's internal address onto shipped orders that do not have one yet.

    Later edits to the store do not change a saved copy.
    """
    ensure_shipped_address_column(cursor)
    sql = """
        SELECT DISTINCT LEFT(po_number, 4) AS store_number
        FROM lori_orders
        WHERE status IN ('shipped', 'completed')
          AND (shipped_internal_address IS NULL OR shipped_internal_address = '')
    """
    params: list[str] = []
    if po_numbers:
        placeholders = ", ".join(["%s"] * len(po_numbers))
        sql += f" AND po_number IN ({placeholders})"
        params.extend(po_numbers)
    cursor.execute(sql, params)
    stores = []
    for row in cursor.fetchall() or []:
        store_number = row.get("store_number") if isinstance(row, dict) else row[0]
        store_number = str(store_number or "").strip()
        if store_number:
            stores.append(store_number)
    saved = 0
    for store_number in stores:
        internal = format_internal_address(_store_row(cursor, store_number))
        if not internal:
            continue
        cursor.execute(
            """
            UPDATE lori_orders
            SET shipped_internal_address = %s
            WHERE po_number LIKE %s
              AND status IN ('shipped', 'completed')
              AND (shipped_internal_address IS NULL OR shipped_internal_address = '')
            """,
            (internal, store_number + "-%"),
        )
        saved += int(cursor.rowcount or 0)
    return saved


def shipment_history(cursor, po_number: str) -> list[dict]:
    """Shipped orders for this store, other than the order being checked."""
    po_number = str(po_number or "").strip()
    store = store_number_from_po(po_number)
    if not store:
        return []
    freeze_shipped_internal_addresses(cursor)
    cursor.execute(
        "SELECT * FROM loris_store_info WHERE store_number = %s",
        (store,),
    )
    store_row = cursor.fetchone()
    if store_row and not isinstance(store_row, dict):
        store_row = None
    internal = format_internal_address(store_row if isinstance(store_row, dict) else _store_row(cursor, store))
    cursor.execute(
        """
        SELECT po_number, tracking, shipped_internal_address
        FROM lori_orders
        WHERE po_number LIKE %s AND po_number <> %s
          AND status IN ('shipped', 'completed')
        ORDER BY COALESCE(batch_date, created_at) DESC, created_at DESC
        LIMIT 40
        """,
        (store + "-%", po_number),
    )
    history = []
    for row in cursor.fetchall() or []:
        if not isinstance(row, dict):
            row = {
                "po_number": row[0],
                "tracking": row[1],
                "shipped_internal_address": row[2],
            }
        saved = str(row.get("shipped_internal_address") or "").strip()
        tracking = str(row.get("tracking") or "").strip()
        history.append({
            "po_number": str(row.get("po_number") or ""),
            "address": saved,
            "tracking": tracking,
            "match": addresses_match(saved, internal) if saved else None,
        })
    return history


def build_prompt(ship_to: str, internal: str) -> str:
    """The text sent to the model, shown in the check popup."""
    return (
        f"{_SYSTEM_PROMPT}\n\n"
        f"SHIP TO:\n{ship_to or ''}\n\n"
        f"INTERNAL:\n{internal or ''}"
    )


def computer_label(base_url: str) -> str:
    host = (urlparse(base_url or "").hostname or "").strip()
    if host in ("127.0.0.1", "localhost", "::1", ""):
        return f"{socket.gethostname()} (this computer)"
    if host == "192.168.254.20":
        return "192.168.254.20 (inside computer)"
    return host


def elapsed_label(elapsed_ms: int) -> str:
    ms = max(0, int(elapsed_ms or 0))
    if ms < 1000:
        return f"{ms} ms"
    return f"{ms / 1000:.1f} s"


def interpret_model_reply(text: str) -> tuple[str, str, int | None]:
    """Map a model reply to confirm, wrong, or fail, plus a 0-100 confidence."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    data = _load_json_object(raw)
    if not isinstance(data, dict):
        return "fail", "The model did not return a result.", None
    message = str(data.get("message") or "").strip() or "No explanation from the model."
    confidence = _parse_confidence(data.get("confidence"))
    match = data.get("match")
    if match is True or str(match).strip().lower() in ("true", "yes"):
        return "confirm", message, confidence
    if match is False or str(match).strip().lower() in ("false", "no"):
        return "wrong", message, confidence
    return "fail", "The model did not say whether the addresses match.", confidence


def format_internal_address(store: dict | None) -> str:
    if not store:
        return ""
    lines = []
    for key in ("company", "address1", "address2", "address3"):
        value = str(store.get(key) or "").strip()
        if value:
            lines.append(value)
    city_bits = [
        str(store.get(key) or "").strip()
        for key in ("city", "state", "zip_code")
    ]
    city_bits = [bit for bit in city_bits if bit]
    if city_bits:
        lines.append(" ".join(city_bits))
    phone = str(store.get("phone") or "").strip()
    if phone and len(phone) == 10 and phone.isdigit():
        phone = f"{phone[:3]}-{phone[3:6]}-{phone[6:]}"
    if phone:
        lines.append("Phone: " + phone)
    return "\n".join(lines)


def store_number_from_po(po_number: str) -> str:
    return str(po_number or "")[:4]


def attach_ai_checks(cursor, orders: list[dict]) -> None:
    """Add ai_check onto each order dict. Missing checks stay None."""
    if not orders:
        return
    ensure_table(cursor)
    pos = [str(order.get("po_number") or "") for order in orders if order.get("po_number")]
    found = load_checks(cursor, pos)
    for order in orders:
        order["ai_check"] = found.get(str(order.get("po_number") or ""))


def enqueue_missing_pending(orders: list[dict]) -> None:
    """Queue a check for pending orders that do not have one yet."""
    for order in orders or []:
        if str(order.get("status") or "") != "pending":
            continue
        check = order.get("ai_check") or {}
        if check.get("status") and check.get("confidence") is not None:
            continue
        po_number = str(order.get("po_number") or "").strip()
        if po_number and po_number != "N/A":
            enqueue_address_check(po_number)


def enqueue_address_check(po_number: str) -> None:
    po_number = str(po_number or "").strip()
    if not po_number or po_number == "N/A":
        return
    _ensure_worker()
    with _QUEUE_LOCK:
        if po_number in _QUEUED:
            return
        _QUEUED.add(po_number)
        _CHECK_QUEUE.put(po_number)


def invalidate_and_enqueue_store(store_number: str) -> None:
    """Drop saved checks for a store and run them again after an address change."""
    store_number = str(store_number or "").strip()
    if not store_number:
        return
    from py.utilities.database_utils import close_mysql_connection, get_mysql_connection_unpooled

    connection = get_mysql_connection_unpooled()
    if not connection:
        return
    try:
        cursor = connection.cursor()
        ensure_table(cursor)
        cursor.execute(
            "SELECT po_number FROM lori_orders WHERE po_number LIKE %s",
            (store_number + "-%",),
        )
        pos = [row[0] for row in cursor.fetchall()]
        if pos:
            placeholders = ", ".join(["%s"] * len(pos))
            cursor.execute(
                f"DELETE FROM loris_address_ai_checks WHERE po_number IN ({placeholders})",
                pos,
            )
            connection.commit()
        for po_number in pos:
            enqueue_address_check(po_number)
    except Exception:
        logger.exception("Could not refresh address checks for store %s", store_number)
    finally:
        close_mysql_connection(connection)


def run_address_check(po_number: str) -> dict:
    """Run one check now and store it. Safe to call from the recheck button."""
    po_number = str(po_number or "").strip()
    lock = _lock_for(po_number)
    with lock:
        try:
            return _run_address_check(po_number)
        except Exception as exc:
            logger.exception("Address check failed for %s", po_number)
            result = _result(
                status="fail",
                message=str(exc) or "The address check did not finish.",
                elapsed_ms=0,
                model="",
                computer=computer_label(""),
            )
            _save_result(po_number, "", "", result)
            return result


def reconcile_saved_checks() -> int:
    """Re-score saved checks so a ZIP+4 extension is not a different ZIP."""
    from py.utilities.database_utils import close_mysql_connection, get_mysql_connection_unpooled

    connection = get_mysql_connection_unpooled()
    if not connection:
        return 0
    updated = 0
    try:
        cursor = connection.cursor(dictionary=True)
        ensure_table(cursor)
        cursor.execute(
            """
            SELECT po_number, status, message, confidence, ship_to_snapshot, internal_snapshot
            FROM loris_address_ai_checks
            WHERE ship_to_snapshot IS NOT NULL AND ship_to_snapshot <> ''
              AND internal_snapshot IS NOT NULL AND internal_snapshot <> ''
            """
        )
        rows = cursor.fetchall()
        for row in rows:
            status, message, confidence = apply_address_guard(
                row.get("status") or "fail",
                row.get("message") or "",
                row.get("confidence"),
                row.get("ship_to_snapshot") or "",
                row.get("internal_snapshot") or "",
            )
            if status == row.get("status") and message == (row.get("message") or "") and confidence == _parse_confidence(row.get("confidence")):
                continue
            cursor.execute(
                """
                UPDATE loris_address_ai_checks
                SET status = %s, message = %s, confidence = %s
                WHERE po_number = %s
                """,
                (status, message, confidence, row.get("po_number")),
            )
            updated += 1
        connection.commit()
        return updated
    except Exception:
        logger.exception("Could not reconcile saved address checks")
        return updated
    finally:
        close_mysql_connection(connection)


def load_checks(cursor, po_numbers: list[str]) -> dict[str, dict]:
    po_numbers = [po for po in po_numbers if po]
    if not po_numbers:
        return {}
    ensure_table(cursor)
    placeholders = ", ".join(["%s"] * len(po_numbers))
    cursor.execute(
        f"""
        SELECT po_number, status, message, ran_at, elapsed_ms, model, computer, prompt, confidence
        FROM loris_address_ai_checks
        WHERE po_number IN ({placeholders})
        """,
        po_numbers,
    )
    out = {}
    for row in cursor.fetchall():
        if isinstance(row, dict):
            item = row
        else:
            item = {
                "po_number": row[0],
                "status": row[1],
                "message": row[2],
                "ran_at": row[3],
                "elapsed_ms": row[4],
                "model": row[5],
                "computer": row[6],
                "prompt": row[7] if len(row) > 7 else "",
                "confidence": row[8] if len(row) > 8 else None,
            }
        out[str(item["po_number"])] = _public_check(item)
    return out


def ensure_table(cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS loris_address_ai_checks (
            po_number VARCHAR(64) NOT NULL PRIMARY KEY,
            status VARCHAR(16) NOT NULL,
            message TEXT,
            ran_at DATETIME NULL,
            elapsed_ms INT NOT NULL DEFAULT 0,
            model VARCHAR(128) NOT NULL DEFAULT '',
            computer VARCHAR(255) NOT NULL DEFAULT '',
            ship_to_snapshot MEDIUMTEXT,
            internal_snapshot MEDIUMTEXT,
            prompt MEDIUMTEXT NULL,
            confidence TINYINT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    if not _has_column(cursor, "prompt"):
        try:
            cursor.execute("ALTER TABLE loris_address_ai_checks ADD COLUMN prompt MEDIUMTEXT NULL")
        except Exception:
            logger.exception("Could not add address-check prompt column")
    if not _has_column(cursor, "confidence"):
        try:
            cursor.execute("ALTER TABLE loris_address_ai_checks ADD COLUMN confidence TINYINT NULL")
        except Exception:
            logger.exception("Could not add address-check confidence column")
    _backfill_missing_prompts(cursor)


def purge_expired_prompts() -> int:
    """Clear stored prompts for orders older than 90 days. Called on import."""
    from py.utilities.database_utils import close_mysql_connection, get_mysql_connection_unpooled

    connection = get_mysql_connection_unpooled()
    if not connection:
        return 0
    try:
        cursor = connection.cursor()
        ensure_table(cursor)
        cursor.execute(
            """
            UPDATE loris_address_ai_checks ai
            INNER JOIN lori_orders o ON o.po_number = ai.po_number
            SET ai.prompt = NULL
            WHERE ai.prompt IS NOT NULL AND ai.prompt <> ''
              AND COALESCE(o.batch_date, DATE(o.created_at)) < (CURDATE() - INTERVAL 90 DAY)
            """
        )
        connection.commit()
        return int(cursor.rowcount or 0)
    except Exception:
        logger.exception("Could not clear old address-check prompts")
        return 0
    finally:
        close_mysql_connection(connection)


def _has_column(cursor, column: str) -> bool:
    cursor.execute(
        """
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'loris_address_ai_checks'
          AND COLUMN_NAME = %s
        """,
        (column,),
    )
    row = cursor.fetchone()
    if row is None:
        return False
    count = list(row.values())[0] if isinstance(row, dict) else row[0]
    return int(count or 0) > 0


def _backfill_missing_prompts(cursor) -> None:
    cursor.execute(
        """
        UPDATE loris_address_ai_checks
        SET prompt = CONCAT(%s, %s, IFNULL(ship_to_snapshot, ''), %s, IFNULL(internal_snapshot, ''))
        WHERE (prompt IS NULL OR prompt = '')
          AND ship_to_snapshot IS NOT NULL AND ship_to_snapshot <> ''
        """,
        (_SYSTEM_PROMPT, "\n\nSHIP TO:\n", "\n\nINTERNAL:\n"),
    )


def _load_json_object(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None


def _public_check(row: dict) -> dict:
    ran_at = row.get("ran_at")
    if hasattr(ran_at, "strftime"):
        ran_at = ran_at.strftime("%Y-%m-%d %H:%M:%S")
    elapsed_ms = int(row.get("elapsed_ms") or 0)
    return {
        "status": row.get("status") or "fail",
        "message": row.get("message") or "",
        "ran_at": ran_at or "",
        "elapsed_ms": elapsed_ms,
        "elapsed_label": elapsed_label(elapsed_ms),
        "model": row.get("model") or "",
        "computer": row.get("computer") or "",
        "prompt": row.get("prompt") or "",
        "confidence": _parse_confidence(row.get("confidence")),
    }


def _result(
    *,
    status: str,
    message: str,
    elapsed_ms: int,
    model: str,
    computer: str,
    prompt: str = "",
    confidence: int | None = None,
) -> dict:
    if confidence is None:
        confidence = 0
    return _public_check({
        "status": status,
        "message": message,
        "ran_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_ms": elapsed_ms,
        "model": model,
        "computer": computer,
        "prompt": prompt,
        "confidence": confidence,
    })


def _ensure_worker() -> None:
    global _WORKER_STARTED
    with _QUEUE_LOCK:
        if _WORKER_STARTED:
            return
        thread = threading.Thread(target=_worker_loop, name="loris-address-ai", daemon=True)
        thread.start()
        _WORKER_STARTED = True


def _worker_loop() -> None:
    while True:
        po_number = _CHECK_QUEUE.get()
        try:
            run_address_check(po_number)
        except Exception:
            logger.exception("Address check worker failed for %s", po_number)
        finally:
            with _QUEUE_LOCK:
                _QUEUED.discard(po_number)
            _CHECK_QUEUE.task_done()


def _lock_for(po_number: str) -> threading.Lock:
    with _PO_LOCKS_GUARD:
        lock = _PO_LOCKS.get(po_number)
        if lock is None:
            lock = threading.Lock()
            _PO_LOCKS[po_number] = lock
        return lock


def _run_address_check(po_number: str) -> dict:
    from py.utilities.database_utils import close_mysql_connection, get_mysql_connection_unpooled

    connection = get_mysql_connection_unpooled()
    if not connection:
        raise RuntimeError("Database connection failed")
    try:
        cursor = connection.cursor(dictionary=True)
        ensure_table(cursor)
        cursor.execute(
            "SELECT po_number, ship_to_address FROM lori_orders WHERE po_number = %s",
            (po_number,),
        )
        order = cursor.fetchone()
        if not order:
            result = _result(
                status="fail",
                message="Order was not found.",
                elapsed_ms=0,
                model="",
                computer=computer_label(""),
            )
            _save_with_cursor(cursor, po_number, "", "", result)
            connection.commit()
            return result

        ship_to = str(order.get("ship_to_address") or "").strip()
        cursor.execute(
            "SELECT * FROM loris_store_info WHERE store_number = %s",
            (store_number_from_po(po_number),),
        )
        store = cursor.fetchone()
        internal = format_internal_address(store)
    finally:
        close_mysql_connection(connection)

    prompt = build_prompt(ship_to, internal)
    if not ship_to or ship_to == "N/A":
        result = _result(
            status="fail",
            message="The ship-to address is missing, so the check did not run.",
            elapsed_ms=0,
            model="",
            computer=computer_label(""),
            prompt=prompt,
        )
        _save_result(po_number, ship_to, internal, result)
        return result
    if not internal:
        result = _result(
            status="fail",
            message="No internal address is on file. Add the store before this can be checked. The internal address is the one we ship.",
            elapsed_ms=0,
            model="",
            computer=computer_label(""),
            prompt=prompt,
        )
        _save_result(po_number, ship_to, internal, result)
        return result

    started = time.perf_counter()
    model = ""
    computer = computer_label("")
    try:
        reply, model, computer = _ask_model(ship_to, internal)
        status, message, confidence = apply_address_guard(
            *interpret_model_reply(reply), ship_to, internal
        )
    except Exception as exc:
        status, message, confidence = "fail", str(exc) or "The address check did not finish.", 0
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    result = _result(
        status=status,
        message=message,
        elapsed_ms=elapsed_ms,
        model=model,
        computer=computer,
        prompt=prompt,
        confidence=confidence,
    )
    _save_result(po_number, ship_to, internal, result)
    return result


def _ask_model(ship_to: str, internal: str) -> tuple[str, str, str]:
    from py.utilities.fdc_ai_agent import _post_ollama, ai_config, check_llm_health

    health = check_llm_health()
    if not health.get("ok") or not health.get("model_ready"):
        raise RuntimeError(health.get("error") or health.get("hint") or "Ollama is not ready")
    base_url = str(health.get("base_url") or "")
    model = str(health.get("model") or "")
    cfg = ai_config()
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"SHIP TO:\n{ship_to}\n\nINTERNAL:\n{internal}",
            },
        ],
        "stream": False,
        "format": "json",
        "keep_alive": cfg.get("keep_alive") or "10m",
        "options": {"temperature": 0, "num_predict": 220},
    }
    response = _post_ollama(f"{base_url}/api/chat", payload, timeout=90)
    data = response.json() or {}
    message = str((data.get("message") or {}).get("content") or "").strip()
    if not message:
        raise RuntimeError("Ollama returned an empty response")
    return message, model, computer_label(base_url)


def _save_result(po_number: str, ship_to: str, internal: str, result: dict) -> None:
    from py.utilities.database_utils import close_mysql_connection, get_mysql_connection_unpooled

    connection = get_mysql_connection_unpooled()
    if not connection:
        logger.error("Could not save address check for %s", po_number)
        return
    try:
        cursor = connection.cursor()
        ensure_table(cursor)
        _save_with_cursor(cursor, po_number, ship_to, internal, result)
        connection.commit()
    except Exception:
        logger.exception("Could not save address check for %s", po_number)
    finally:
        close_mysql_connection(connection)


def _save_with_cursor(cursor, po_number: str, ship_to: str, internal: str, result: dict) -> None:
    cursor.execute(
        """
        INSERT INTO loris_address_ai_checks
            (po_number, status, message, ran_at, elapsed_ms, model, computer,
             ship_to_snapshot, internal_snapshot, prompt, confidence)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            status = VALUES(status),
            message = VALUES(message),
            ran_at = VALUES(ran_at),
            elapsed_ms = VALUES(elapsed_ms),
            model = VALUES(model),
            computer = VALUES(computer),
            ship_to_snapshot = VALUES(ship_to_snapshot),
            internal_snapshot = VALUES(internal_snapshot),
            prompt = VALUES(prompt),
            confidence = VALUES(confidence)
        """,
        (
            po_number,
            result.get("status") or "fail",
            result.get("message") or "",
            result.get("ran_at") or None,
            int(result.get("elapsed_ms") or 0),
            result.get("model") or "",
            result.get("computer") or "",
            ship_to,
            internal,
            result.get("prompt") or None,
            result.get("confidence"),
        ),
    )
