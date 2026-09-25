"""4Seller automated login: fetch captcha, OCR, POST /api/user/login, cache session cookies."""
from __future__ import annotations

import base64
import email
import html as html_lib
import imaplib
import io
import json
import os
import re
import time
from email.header import decode_header, make_header
from email.message import Message
from typing import Any

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

LOGIN_URL = "https://www.4seller.com/api/user/login"
VALIDATION_URL = "https://www.4seller.com/api/user/validation"
LOGIN_PAGE = "https://www.4seller.com/en-US/login.html"
USER_INIT_URL = "https://www.4seller.com/api/user/user-init"
SEND_2FA_URL = "https://www.4seller.com/api/user/send-2fa-code"
VERIFY_2FA_URL = "https://www.4seller.com/api/user/verify-2fa"

# From 4Seller frontend (CiME1aIq.js) — AES key wrapper for password encryption.
_ENCRYPTED_KEY_B64 = "EBgQzQB0hVxI+RhxY3OFDBn460YGh5AarqgEZecwBy4kVyHyRhuieabMBlCz11GU"
_KEY_WRAPPER = b"kqmBRPoW6tn0rpDfijlzGVTZQuMcIebO"
_PASSWORD_JOINER = "\u00b1\u00b1"  # ±± between password and timestamp (from CiME1aIq.js)


def _decrypt_aes_ecb(ciphertext_b64: str, key: bytes) -> bytes:
    cipher = AES.new(key, AES.MODE_ECB)
    raw = base64.b64decode(ciphertext_b64)
    decrypted = cipher.decrypt(raw)
    pad_len = decrypted[-1]
    return decrypted[:-pad_len]


def _encrypt_password(plain_password: str, timestamp_ms: int | None = None) -> str:
    """Match frontend: AES-ECB encrypt '{password}—{timestamp}'."""
    ts = int(timestamp_ms if timestamp_ms is not None else time.time() * 1000)
    inner_key = _decrypt_aes_ecb(_ENCRYPTED_KEY_B64, _KEY_WRAPPER)
    payload = f"{plain_password}{_PASSWORD_JOINER}{ts}".encode("utf-8")
    cipher = AES.new(inner_key, AES.MODE_ECB)
    encrypted = cipher.encrypt(pad(payload, AES.block_size))
    return base64.b64encode(encrypted).decode("ascii")


def _new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Referer": LOGIN_PAGE,
            "Origin": "https://www.4seller.com",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return session


def _device_id() -> str:
    """Stable pseudo device id (frontend uses fingerprint lib; server accepts arbitrary string)."""
    env_id = os.getenv("FOURSELLER_DEVICE_ID", "").strip()
    if env_id:
        return env_id
    return "fdc-flask-app-4seller-sync"


def _ocr_captcha(image_bytes: bytes) -> str:
    """Read captcha image using ddddocr (best for 4Seller), with pytesseract fallback."""
    try:
        import ddddocr

        ocr = ddddocr.DdddOcr(show_ad=False)
        text = ocr.classification(image_bytes)
        cleaned = re.sub(r"[^0-9A-Za-z]", "", text or "")
        if cleaned:
            return cleaned
    except Exception:
        pass

    try:
        import pytesseract
        from PIL import Image, ImageFilter, ImageOps

        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        img = ImageOps.autocontrast(img)
        img = img.filter(ImageFilter.SHARPEN)
        w, h = img.size
        img = img.resize((w * 3, h * 3), Image.Resampling.LANCZOS)
        text = pytesseract.image_to_string(
            img,
            config="--psm 7 -c tessedit_char_whitelist=0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
        )
        cleaned = re.sub(r"[^0-9A-Za-z]", "", text)
        if cleaned:
            return cleaned
    except Exception:
        pass

    raise RuntimeError("Captcha OCR failed. Install ddddocr (pip install ddddocr) or paste a cookie manually.")


def _fetch_validation_image(session: requests.Session) -> tuple[bytes, str]:
    """GET /api/user/validation — returns image bytes and display form (often data URL)."""
    resp = session.get(VALIDATION_URL, timeout=30)
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected validation response: {resp.text[:200]}")

    # API may return {code:0, data:"data:image/png;base64,..."} or raw base64 string.
    payload = data.get("data") if data.get("code") == 0 else data
    if payload is None:
        # Some responses embed image directly in body field.
        payload = data.get("msg") or data.get("image") or resp.text

    text = str(payload or "")
    if text.startswith("data:"):
        _, b64part = text.split(",", 1)
        return base64.b64decode(b64part), text
    if re.fullmatch(r"[A-Za-z0-9+/=]+", text.strip()) and len(text) > 40:
        return base64.b64decode(text), text

    if resp.headers.get("content-type", "").startswith("image/"):
        return resp.content, ""

    raise RuntimeError(f"Could not parse captcha image: {data}")


def _session_cookies_to_header(session: requests.Session) -> str:
    pairs = [f"{k}={v}" for k, v in session.cookies.get_dict().items()]
    return "; ".join(pairs)


def _is_api_session_token(token: str) -> bool:
    """4Seller API auth cookie is a 32-char hex id from Set-Cookie, not the long JSON body token."""
    return bool(re.fullmatch(r"[a-f0-9]{32}", (token or "").strip(), flags=re.IGNORECASE))


def _verify_logged_in(session: requests.Session) -> bool:
    """Verify session via API (dashboard HTML alone is not a reliable auth check)."""
    try:
        resp = session.post(USER_INIT_URL, json={}, timeout=30)
        data = resp.json()
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    if data.get("code") == 4001:
        return False
    msg = str(data.get("msg") or data.get("messages") or "")
    if "Validation_Failed" in msg:
        return False
    return True


def _mfa_mail_config() -> dict[str, Any]:
    return {
        "email": (os.getenv("4seller_MFA_email") or os.getenv("FOURSELLER_MFA_EMAIL") or "").strip(),
        "password": (os.getenv("4seller_MFA_password") or os.getenv("FOURSELLER_MFA_PASSWORD") or "").strip(),
        "server": os.getenv("FOURSELLER_MFA_IMAP_SERVER", "imap.gmail.com").strip() or "imap.gmail.com",
        "port": int(os.getenv("FOURSELLER_MFA_IMAP_PORT", "993") or 993),
        "timeout": int(os.getenv("FOURSELLER_MFA_TIMEOUT", "180") or 180),
    }


def _imap_connect(cfg: dict[str, Any]) -> imaplib.IMAP4_SSL:
    mail = imaplib.IMAP4_SSL(cfg["server"], cfg["port"])
    mail.login(cfg["email"], cfg["password"])
    mail.select("INBOX")
    return mail


def _imap_next_uid(cfg: dict[str, Any]) -> int:
    """UIDNEXT of INBOX — any 2FA email that arrives after this call has uid >= the returned value."""
    mail = _imap_connect(cfg)
    try:
        typ, data = mail.status("INBOX", "(UIDNEXT)")
        if typ == "OK" and data and data[0]:
            match = re.search(rb"UIDNEXT\s+(\d+)", data[0])
            if match:
                return int(match.group(1))
        return 1
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def _message_text(msg: Message) -> str:
    parts: list[str] = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if ctype == "text/html":
            text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
            text = html_lib.unescape(re.sub(r"<[^>]+>", " ", text))
        parts.append(text)
    return re.sub(r"\s+", " ", " ".join(parts))


def _extract_2fa_code(text: str) -> str:
    near = re.search(r"(?i)(?:code|验证码)[^0-9A-Za-z]{0,40}(\d{4,8})\b", text)
    if near:
        return near.group(1)
    six = re.search(r"\b(\d{6})\b", text)
    return six.group(1) if six else ""


def _scan_inbox_for_code(cfg: dict[str, Any], min_uid: int, seen: set[int]) -> str:
    """One pass over new INBOX messages (uid >= min_uid); return a 4Seller verification code or ''."""
    mail = _imap_connect(cfg)
    try:
        typ, data = mail.uid("search", None, f"UID {min_uid}:*")
        uids = [int(u) for u in (data[0].split() if typ == "OK" and data and data[0] else [])]
        for uid in sorted((u for u in uids if u >= min_uid and u not in seen), reverse=True):
            seen.add(uid)
            typ, msg_data = mail.uid("fetch", str(uid), "(RFC822)")
            if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            sender = str(make_header(decode_header(msg.get("From", ""))))
            subject = str(make_header(decode_header(msg.get("Subject", ""))))
            body = _message_text(msg)
            if "4seller" not in f"{sender} {subject} {body[:500]}".lower():
                continue
            code = _extract_2fa_code(f"{subject} {body}")
            if code:
                return code
        return ""
    finally:
        try:
            mail.logout()
        except Exception:
            pass


# Shared between the login worker subprocess and the Flask app so the /4seller page can
# show a code prompt while login waits, and hand a manually typed code back to the worker.
MFA_STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "4seller_mfa_state.json"
)


def _read_mfa_state() -> dict[str, Any]:
    try:
        with open(MFA_STATE_FILE, encoding="utf-8") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_mfa_state(state: dict[str, Any] | None) -> None:
    if state is None:
        try:
            os.remove(MFA_STATE_FILE)
        except OSError:
            pass
        return
    os.makedirs(os.path.dirname(MFA_STATE_FILE), exist_ok=True)
    tmp = MFA_STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    os.replace(tmp, MFA_STATE_FILE)


def get_mfa_status() -> dict[str, Any]:
    """For the web UI: is a login currently waiting for a 2FA code?"""
    state = _read_mfa_state()
    expires_at = float(state.get("expires_at") or 0)
    if not state or expires_at < time.time():
        return {"pending": False}
    return {
        "pending": True,
        "email_hint": state.get("email_hint", ""),
        "seconds_left": int(expires_at - time.time()),
        "error": state.get("error", ""),
        "code_submitted": bool(state.get("manual_code")),
        "imap_ok": bool(state.get("imap_ok")),
        "imap_status": state.get("imap_status", ""),
    }


def submit_manual_mfa_code(code: str) -> bool:
    code = re.sub(r"\s+", "", code or "")
    state = _read_mfa_state()
    if not code or not state or float(state.get("expires_at") or 0) < time.time():
        return False
    state["manual_code"] = code
    state["error"] = ""
    _write_mfa_state(state)
    return True


def _mask_email(value: str) -> str:
    name, _, domain = (value or "").partition("@")
    if not domain:
        return ""
    return f"{name[:2]}***@{domain}"


def _complete_2fa(session: requests.Session, temp_token: str, account_email: str = "") -> dict[str, Any]:
    """Request an email code, then accept whichever arrives first — the MFA mailbox (IMAP)
    or a code typed on the /4seller page — and POST /api/user/verify-2fa."""
    if not temp_token:
        raise RuntimeError("4Seller asked for 2FA but returned no tempToken.")
    cfg = _mfa_mail_config()
    imap_enabled = bool(cfg["email"] and cfg["password"])

    min_uid = 0
    imap_status = "Automatic email reading is off (4seller_MFA_email / 4seller_MFA_password not set)."
    if imap_enabled:
        try:
            min_uid = _imap_next_uid(cfg)
            imap_status = f"Checking {_mask_email(cfg['email'])} for the code every 5s."
        except Exception as exc:
            imap_enabled = False
            imap_status = f"Can't read {_mask_email(cfg['email'])}: {exc}"
            if "AUTHENTICATIONFAILED" in str(exc).upper() or "Invalid credentials" in str(exc):
                imap_status += " — Gmail needs a 16-character app password in 4seller_MFA_password."

    resp = session.post(SEND_2FA_URL, data={"tempToken": temp_token}, timeout=30)
    try:
        sent = resp.json()
    except ValueError:
        sent = {}
    if isinstance(sent, dict) and sent.get("code") not in (0, None) and sent.get("success") is not True:
        raise RuntimeError(f"4Seller send-2fa-code failed: {sent.get('messages') or sent.get('msg') or sent}")

    deadline = time.time() + cfg["timeout"]
    _write_mfa_state(
        {
            "email_hint": _mask_email(account_email),
            "expires_at": deadline,
            "manual_code": "",
            "error": "",
            "imap_ok": imap_enabled,
            "imap_status": imap_status,
        }
    )
    seen: set[int] = set()
    rejected: set[str] = set()
    last_error = ""
    next_imap = time.time() + 5
    try:
        while time.time() < deadline:
            time.sleep(1)
            code = str(_read_mfa_state().get("manual_code") or "")
            if code in rejected:
                code = ""
            if not code and imap_enabled and time.time() >= next_imap:
                next_imap = time.time() + 5
                try:
                    code = _scan_inbox_for_code(cfg, min_uid, seen)
                except Exception:
                    code = ""
            if not code or code in rejected:
                continue

            resp = session.post(
                VERIFY_2FA_URL,
                json={"deviceId": _device_id(), "tempToken": temp_token, "emailCode": code},
                timeout=30,
            )
            try:
                data = resp.json()
            except ValueError:
                data = {"msg": resp.text[:300]}
            if isinstance(data, dict) and (data.get("success") is True or data.get("code") == 0) and data.get("data"):
                return data
            rejected.add(code)
            last_error = str(data.get("messages") or data.get("msg") or data) if isinstance(data, dict) else str(data)
            state = _read_mfa_state()
            state.update({"manual_code": "", "error": f"Code {code} was rejected: {last_error}"})
            _write_mfa_state(state)
    finally:
        _write_mfa_state(None)

    detail = f" Last error: {last_error}" if last_error else ""
    raise RuntimeError(f"4Seller 2FA code not received within {cfg['timeout']}s.{detail}")


def _login_result(session: requests.Session, data: dict[str, Any]) -> dict[str, Any]:
    inner = data["data"]
    token = inner.get("token") if isinstance(inner, dict) else inner
    # Keep HttpOnly Set-Cookie userToken (32-char hex). JSON body token is for browser localStorage only.
    cookie_header = _session_cookies_to_header(session)
    api_token = session.cookies.get("userToken", "")
    if not _is_api_session_token(str(api_token or "")):
        raise RuntimeError("4Seller login succeeded but API session cookie was not set. Try again.")
    return {
        "success": True,
        "token": api_token,
        "plugin_token": token,
        "cookie_header": cookie_header,
        "session": session,
        "raw": data,
    }


def login_4seller(
    email: str,
    password: str,
    *,
    max_attempts: int = 5,
    skip_turnstile: bool = True,
) -> dict[str, Any]:
    """
    Log in to 4Seller and return {cookie_header, session, token, ...}.

  skip_turnstile: when True (default), omit recaptchaToken so the image captcha path is used.
    """
    email = (email or "").strip()
    password = password or ""
    if not email or not password:
        raise ValueError("4Seller email and password are required.")

    session = _new_session()
    session.get(LOGIN_PAGE, timeout=30)

    last_error = ""
    for attempt in range(1, max_attempts + 1):
        image_bytes, _ = _fetch_validation_image(session)
        validate_code = _ocr_captcha(image_bytes)

        body: dict[str, Any] = {
            "email": email,
            "password": _encrypt_password(password),
            "validateCode": validate_code,
            "deviceId": _device_id(),
        }
        if not skip_turnstile:
            token = os.getenv("FOURSELLER_TURNSTILE_TOKEN", "").strip()
            if token:
                body["recaptchaToken"] = token

        resp = session.post(LOGIN_URL, json=body, timeout=30)
        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError(f"4Seller login returned non-JSON: {resp.text[:300]}")

        if isinstance(data, dict) and (
            data.get("success") is True or data.get("code") == 0
        ) and data.get("data"):
            inner = data["data"]
            if isinstance(inner, dict) and inner.get("need2FA"):
                data = _complete_2fa(session, str(inner.get("tempToken") or ""), email)
            return _login_result(session, data)

        last_error = str(data.get("messages") or data.get("msg") or data)
        # Wrong captcha — refresh and retry.
        if attempt < max_attempts:
            time.sleep(0.5)
            continue

    raise RuntimeError(f"4Seller login failed after {max_attempts} attempts: {last_error}")


def login_from_env() -> dict[str, Any]:
    from py.utilities.nssm_env import load_nssm_env_file

    load_nssm_env_file()
    email = os.getenv("FOURSELLER_EMAIL", "").strip()
    password = os.getenv("FOURSELLER_PASSWORD", "").strip()
    return login_4seller(email, password)
