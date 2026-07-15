"""4Seller automated login: fetch captcha, OCR, POST /api/user/login, cache session cookies."""
from __future__ import annotations

import base64
import io
import os
import re
import time
from typing import Any

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

LOGIN_URL = "https://www.4seller.com/api/user/login"
VALIDATION_URL = "https://www.4seller.com/api/user/validation"
LOGIN_PAGE = "https://www.4seller.com/en-US/login.html"
USER_INIT_URL = "https://www.4seller.com/api/user/user-init"

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
                raise RuntimeError(
                    "4Seller account requires email 2FA. Disable 2FA or paste cookie manually."
                )
            token = inner.get("token") if isinstance(inner, dict) else inner
            # Keep HttpOnly Set-Cookie userToken (32-char hex). JSON body token is for browser localStorage only.
            cookie_header = _session_cookies_to_header(session)
            api_token = session.cookies.get("userToken", "")
            if not _is_api_session_token(str(api_token or "")):
                raise RuntimeError(
                    "4Seller login succeeded but API session cookie was not set. Try again."
                )

            return {
                "success": True,
                "token": api_token,
                "plugin_token": token,
                "cookie_header": cookie_header,
                "session": session,
                "raw": data,
            }

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
