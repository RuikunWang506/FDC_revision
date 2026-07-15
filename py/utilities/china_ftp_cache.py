"""China FTP: pooled SSH/SFTP, folder listing cache, and local search index."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from contextlib import contextmanager
from datetime import datetime
from typing import Callable, List, Optional, Tuple

import paramiko

CHINA_FTP_HOST = '219.135.198.130'
CHINA_FTP_USER = 'fdc'
CHINA_FTP_PASSWORD = 'Bst@93109310'
CHINA_FTP_REMOTE_BASE = r'D:\BST\BST_PIC\BSTpic2\梁佳怡'
CHINA_FTP_IMAGE_EXTS = frozenset({'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'})
CHINA_DIR_SEARCH_MAX = 300
CHINA_FTP_CONNECT_TIMEOUT = 25
CHINA_FTP_BANNER_TIMEOUT = 30
CHINA_FTP_TRANSFER_TIMEOUT = 45
CHINA_FTP_CONNECT_RETRIES = 2
CHINA_FTP_CONNECT_RETRY_DELAY = 1.5
CHINA_FTP_DOWNLOAD_WORKERS = 4
CHINA_FTP_CONNECT_FAILURE_ABORT = 3
LISTING_TTL_SECONDS = 900
SESSION_IDLE_SECONDS = 120

# Fetch/sync: only photoshopped white-background outputs (not raw SKU-root or pre-production).
CHINA_FTP_SYNC_WHITE_FOLDER_KEYWORDS = (
    '新建文件夹',
    '白底文件夹',
    '白底图',
    '白背景',
    '扣白底',
    '绿白底',
    '白底',
)
CHINA_FTP_SYNC_EXCLUDE_PATH_KEYWORDS = (
    '产前',
    '产前版',
)
CHINA_FTP_SYNC_SKIP_EXTS = frozenset({
    '.psd',
    '.psb',
    '.ai',
    '.zip',
    '.rar',
    '.7z',
})

_download_semaphore = threading.Semaphore(CHINA_FTP_DOWNLOAD_WORKERS)

CACHE_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'output', 'china_ftp_cache')
)
LISTINGS_DIR = os.path.join(CACHE_ROOT, 'listings')
INDEX_FILE = os.path.join(CACHE_ROOT, 'full_index.json')

os.makedirs(CACHE_ROOT, exist_ok=True)
os.makedirs(LISTINGS_DIR, exist_ok=True)


def _utc_now_iso() -> str:
    return datetime.utcnow().isoformat() + 'Z'


def current_year_label() -> str:
    """Top-level year folder name on China FTP, e.g. 2026年."""
    return f'{datetime.utcnow().year}年'


def rel_in_current_year(rel_path: str) -> bool:
    rel = _norm_rel(rel_path)
    year = current_year_label()
    return rel == year or rel.startswith(year + '/')


def is_sync_fetch_path(rel_path: str) -> bool:
    """True when path is under an allowed white-background / retouch folder."""
    path = _norm_rel(rel_path)
    if not path:
        return False
    if any(k in path for k in CHINA_FTP_SYNC_EXCLUDE_PATH_KEYWORDS):
        return False
    return any(k in path for k in CHINA_FTP_SYNC_WHITE_FOLDER_KEYWORDS)


def is_sync_fetch_file(name: str) -> bool:
    """True for downloadable image types; excludes PSD and other non-image assets."""
    ext = os.path.splitext(name or '')[1].lower()
    if not ext or ext in CHINA_FTP_SYNC_SKIP_EXTS:
        return False
    return ext in CHINA_FTP_IMAGE_EXTS


def is_sync_fetch_entry(entry: dict) -> bool:
    """Path + file-type gate for China FTP fetch/sync."""
    if entry.get('type') and entry.get('type') != 'file':
        return False
    name = entry.get('name') or os.path.basename(entry.get('full_path') or '')
    if not is_sync_fetch_file(name):
        return False
    return is_sync_fetch_path(entry.get('rel_path') or '')


def sync_fetch_filter_info() -> dict:
    return {
        'year': current_year_label(),
        'path_include': list(CHINA_FTP_SYNC_WHITE_FOLDER_KEYWORDS),
        'path_exclude': list(CHINA_FTP_SYNC_EXCLUDE_PATH_KEYWORDS),
        'allowed_extensions': sorted(CHINA_FTP_IMAGE_EXTS),
        'skip_extensions': sorted(CHINA_FTP_SYNC_SKIP_EXTS),
    }


def filter_entries_for_sync(entries: List[dict]) -> List[dict]:
    return [e for e in entries if is_sync_fetch_entry(e)]


_SKU_TOKEN_RE = re.compile(r'^([A-Z]+\d+)', re.I)


def _path_segment_matches_sku(segment: str, sku_upper: str) -> bool:
    """True when a single path segment is the SKU or starts with SKU + separator (not T36960 for T3696)."""
    seg_upper = (segment or '').strip().upper()
    if not seg_upper or not sku_upper:
        return False
    if seg_upper == sku_upper:
        return True
    if not seg_upper.startswith(sku_upper):
        return False
    suffix = seg_upper[len(sku_upper):]
    if not suffix:
        return True
    return suffix[0] in ' _-.' or not suffix[0].isalnum()


def path_contains_sku(rel_path: str, sku_upper: str) -> bool:
    """True when any folder segment in rel_path matches the SKU (trace-by-folder fallback)."""
    rel = _norm_rel(rel_path)
    if not rel:
        return False
    return any(_path_segment_matches_sku(part, sku_upper) for part in rel.split('/'))


def guess_sku_from_path(rel_path: str) -> Optional[str]:
    """Best-effort SKU token from path segments (deepest folder first)."""
    rel = _norm_rel(rel_path)
    if not rel:
        return None
    for segment in reversed(rel.split('/')):
        match = _SKU_TOKEN_RE.match((segment or '').strip())
        if match:
            return match.group(1).upper()
    return None


def _slug_path_token(text: str, *, max_len: int = 40) -> str:
    slug = re.sub(r'[^\w\-]+', '_', (text or '').strip())
    slug = re.sub(r'_+', '_', slug).strip('_')
    if not slug:
        return 'img'
    return slug[:max_len]


def is_generic_sync_filename(name: str, sku_upper: str) -> bool:
    """True when remote filename is not a reliable SKU-based name (unnamed / numeric only)."""
    original = (name or '').strip()
    if not original or original in ('.', '..'):
        return True
    stem = os.path.splitext(original)[0].strip()
    if not stem:
        return True
    stem_upper = stem.upper()
    if stem_upper.startswith(sku_upper):
        suffix = stem_upper[len(sku_upper):]
        if not suffix or suffix[0] in ' _-.':
            return False
    if stem.isdigit() or len(stem) <= 2:
        return True
    if sku_upper in stem_upper:
        return False
    if _SKU_TOKEN_RE.match(stem_upper):
        return False
    return True


def derive_sync_filename(entry: dict, sku: str) -> str:
    """Local big_picture filename; derives from folder path when remote file has no SKU name."""
    sku_upper = (sku or '').strip().upper()
    full_path = entry.get('full_path') or ''
    original = (entry.get('name') or os.path.basename(full_path) or '').strip()
    rel = entry.get('rel_path') or china_ftp_abs_to_rel(full_path)
    ext = os.path.splitext(original)[1].lower()
    if ext not in CHINA_FTP_IMAGE_EXTS:
        ext = '.jpg'

    if sku_upper and not is_generic_sync_filename(original, sku_upper):
        return original

    folder_seg = ''
    for segment in reversed(_norm_rel(rel).split('/')):
        if sku_upper and _path_segment_matches_sku(segment, sku_upper):
            folder_seg = segment
            break
    if not folder_seg:
        folder_seg = guess_sku_from_path(rel) or sku_upper or 'folder'

    folder_slug = _slug_path_token(folder_seg)
    orig_stem = os.path.splitext(original)[0].strip() or 'img'
    orig_slug = _slug_path_token(orig_stem, max_len=20)
    path_hash = hashlib.sha256(full_path.encode('utf-8')).hexdigest()[:8]
    prefix = sku_upper or guess_sku_from_path(rel) or 'SKU'
    return f'{prefix}_{folder_slug}_{orig_slug}_{path_hash}{ext}'


def _entry_matches_sku(entry: dict, sku_upper: str, *, match_mode: str) -> bool:
    name = entry.get('name') or os.path.basename(entry.get('full_path') or '')
    rel = entry.get('rel_path') or ''
    if match_mode == 'folder':
        return path_contains_sku(rel, sku_upper)
    return name.upper().startswith(sku_upper)


def _entry_passes_sku_file_filters(
    entry: dict,
    sku_upper: str,
    *,
    year_only: bool,
    sync_only: bool,
    match_mode: str,
) -> bool:
    if entry.get('type') != 'file':
        return False
    name = entry.get('name') or os.path.basename(entry.get('full_path') or '')
    rel = entry.get('rel_path') or ''
    if not _entry_matches_sku(entry, sku_upper, match_mode=match_mode):
        return False
    if year_only:
        year_label = current_year_label()
        year_prefix = f'{year_label}/'
        if not (rel == year_label or rel.startswith(year_prefix)):
            return False
    if sync_only:
        return is_sync_fetch_entry(entry)
    return bool(entry.get('is_image')) or is_sync_fetch_file(name)


def is_connection_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    exc_name = type(exc).__name__.lower()
    markers = (
        'unable to connect',
        'unreachable host',
        'connection refused',
        'connection reset',
        'timed out',
        'timeout',
        'errno none',
        'winerror 100',
        'no route to host',
        'broken pipe',
        'socket is closed',
    )
    return exc_name == 'timeout' or exc_name == 'timeouterror' or any(m in msg for m in markers)


def _safe_write_json(file_path: str, payload) -> None:
    tmp_path = f"{file_path}.tmp"
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, file_path)


def _norm_rel(rel_path: str = '') -> str:
    return (rel_path or '').replace('\\', '/').strip('/')


def _china_ftp_base_norm() -> str:
    return os.path.normpath(CHINA_FTP_REMOTE_BASE)


def china_ftp_rel_to_abs(rel_path: str = '') -> str:
    rel = (rel_path or '').replace('/', '\\').strip('\\')
    base = _china_ftp_base_norm()
    abs_path = os.path.normpath(base if not rel else os.path.join(base, rel))
    if abs_path.lower() != base.lower() and not abs_path.lower().startswith(base.lower() + '\\'):
        raise ValueError('Invalid path')
    return abs_path


def china_ftp_abs_to_rel(abs_path: str) -> str:
    base = _china_ftp_base_norm()
    norm = os.path.normpath(abs_path)
    if norm.lower() == base.lower():
        return ''
    prefix = base + '\\'
    if not norm.lower().startswith(prefix.lower()):
        return norm
    return norm[len(prefix):].replace('\\', '/')


def _china_ftp_path_tags(path_text: str) -> list:
    tags = []
    if any(k in path_text for k in ('白底', '白背景', '白底图', '新建文件夹')):
        tags.append({'id': 'white_folder', 'label': 'White BG folder', 'label_zh': '白底文件夹'})
    if any(k in path_text for k in ('场景', '实景', '模特', '外景')):
        tags.append({'id': 'scene_folder', 'label': 'Scene / lifestyle', 'label_zh': '场景图文件夹'})
    if '产前' in path_text:
        tags.append({'id': 'prenatal', 'label': 'Pre-production', 'label_zh': '产前'})
    if 'P色预售' in path_text or '预售' in path_text:
        tags.append({'id': 'presale', 'label': 'Presale', 'label_zh': '预售'})
    return tags


def _china_ftp_is_image(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in CHINA_FTP_IMAGE_EXTS


def _listing_cache_path(rel_path: str) -> str:
    key = _norm_rel(rel_path) or '_root_'
    digest = hashlib.sha256(key.encode('utf-8')).hexdigest()[:20]
    return os.path.join(LISTINGS_DIR, f'{digest}.json')


def _close_quietly(*objs) -> None:
    for obj in objs:
        if obj is None:
            continue
        try:
            obj.close()
        except Exception:
            pass


class ChinaFtpConnectionPool:
    """Reuse one SFTP session across browse requests (idle timeout + health check)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._transport: Optional[paramiko.Transport] = None
        self._sftp: Optional[paramiko.SFTPClient] = None
        self._last_used = 0.0

    def _idle_expired(self) -> bool:
        return (time.time() - self._last_used) > SESSION_IDLE_SECONDS

    def _disconnect(self) -> None:
        _close_quietly(self._sftp, self._transport)
        self._sftp = None
        self._transport = None

    def _connect_sftp(self) -> None:
        last_exc = None
        for attempt in range(1, CHINA_FTP_CONNECT_RETRIES + 1):
            transport = sftp = None
            try:
                sock = socket.create_connection(
                    (CHINA_FTP_HOST, 22),
                    timeout=CHINA_FTP_CONNECT_TIMEOUT,
                )
                transport = paramiko.Transport(sock)
                transport.banner_timeout = CHINA_FTP_BANNER_TIMEOUT
                transport.connect(username=CHINA_FTP_USER, password=CHINA_FTP_PASSWORD)
                sftp = paramiko.SFTPClient.from_transport(transport)
                self._transport = transport
                self._sftp = sftp
                self._last_used = time.time()
                return
            except Exception as exc:
                last_exc = exc
                _close_quietly(sftp, transport)
                if attempt < CHINA_FTP_CONNECT_RETRIES:
                    time.sleep(CHINA_FTP_CONNECT_RETRY_DELAY * attempt)
        raise last_exc or RuntimeError('China FTP SFTP connection failed')

    def _ping_sftp(self) -> bool:
        try:
            if self._sftp is None:
                return False
            self._sftp.listdir(_china_ftp_base_norm())
            return True
        except Exception:
            return False

    @contextmanager
    def sftp(self):
        with self._lock:
            if self._sftp is None or self._idle_expired() or not self._ping_sftp():
                self._disconnect()
                self._connect_sftp()
            try:
                yield self._sftp
            finally:
                self._last_used = time.time()


_pool = ChinaFtpConnectionPool()
_pool_lock = threading.Lock()


def reset_connection_pool() -> None:
    with _pool_lock:
        _pool._disconnect()


def _open_sftp_client() -> Tuple[paramiko.Transport, paramiko.SFTPClient]:
    """Open a dedicated SFTP session with connect + transfer timeouts."""
    sock = socket.create_connection(
        (CHINA_FTP_HOST, 22),
        timeout=CHINA_FTP_CONNECT_TIMEOUT,
    )
    sock.settimeout(CHINA_FTP_TRANSFER_TIMEOUT)
    transport = paramiko.Transport(sock)
    transport.banner_timeout = CHINA_FTP_BANNER_TIMEOUT
    transport.connect(username=CHINA_FTP_USER, password=CHINA_FTP_PASSWORD)
    sftp = paramiko.SFTPClient.from_transport(transport)
    return transport, sftp


def _sftp_get_once(remote_path: str, local_path: str) -> None:
    transport = sftp = None
    try:
        transport, sftp = _open_sftp_client()
        sftp.get(remote_path, local_path)
    finally:
        _close_quietly(sftp, transport)


def download_file(remote_path: str, local_path: str) -> None:
    """Download one remote file; hard timeout per attempt (paramiko may ignore socket timeout)."""
    last_exc = None
    for attempt in range(1, CHINA_FTP_CONNECT_RETRIES + 1):
        if os.path.exists(local_path):
            try:
                os.remove(local_path)
            except OSError:
                pass
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            with _download_semaphore:
                future = executor.submit(_sftp_get_once, remote_path, local_path)
                future.result(timeout=CHINA_FTP_TRANSFER_TIMEOUT)
        except FuturesTimeoutError:
            last_exc = TimeoutError(
                f'SFTP download timed out after {CHINA_FTP_TRANSFER_TIMEOUT}s'
            )
        except Exception as exc:
            last_exc = exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
            return
        if os.path.exists(local_path):
            try:
                os.remove(local_path)
            except OSError:
                pass
        if last_exc is None:
            last_exc = RuntimeError('empty download')
        if attempt < CHINA_FTP_CONNECT_RETRIES:
            time.sleep(CHINA_FTP_CONNECT_RETRY_DELAY * attempt)
    raise last_exc or RuntimeError('download failed')


def _run_ssh_command(command: str, *, exec_timeout: int = 120) -> str:
    last_exc = None
    for attempt in range(1, CHINA_FTP_CONNECT_RETRIES + 1):
        ssh = None
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(
                CHINA_FTP_HOST,
                username=CHINA_FTP_USER,
                password=CHINA_FTP_PASSWORD,
                timeout=CHINA_FTP_CONNECT_TIMEOUT,
                banner_timeout=CHINA_FTP_BANNER_TIMEOUT,
                allow_agent=False,
                look_for_keys=False,
            )
            _, stdout, stderr = ssh.exec_command(command, timeout=exec_timeout)
            raw = stdout.read().decode('gbk', errors='replace').strip()
            err = stderr.read().decode('gbk', errors='replace').strip()
            if err and not raw:
                raise RuntimeError(err)
            return raw
        except Exception as exc:
            last_exc = exc
            _close_quietly(ssh)
            if attempt < CHINA_FTP_CONNECT_RETRIES:
                time.sleep(CHINA_FTP_CONNECT_RETRY_DELAY * attempt)
    raise last_exc or RuntimeError('China FTP SSH connection failed')


def _sftp_list_items(sftp, abs_path: str, rel_path: str) -> list:
    items = []
    for attr in sftp.listdir_attr(abs_path):
        name = attr.filename
        if name in ('.', '..'):
            continue
        is_dir = bool(attr.st_mode and stat.S_ISDIR(attr.st_mode))
        entry_rel = china_ftp_abs_to_rel(abs_path + '\\' + name)
        item = {
            'name': name,
            'type': 'directory' if is_dir else 'file',
            'rel_path': entry_rel,
            'full_path': abs_path + '\\' + name,
            'size': None if is_dir else int(attr.st_size or 0),
            'modified': (
                datetime.utcfromtimestamp(attr.st_mtime).isoformat() + 'Z'
                if attr.st_mtime
                else None
            ),
            'is_image': False if is_dir else _china_ftp_is_image(name),
            'tags': _china_ftp_path_tags(entry_rel),
        }
        if not is_dir:
            sku_guess = name.split()[0].upper() if name else ''
            if sku_guess and sku_guess.isalnum():
                item['sku_guess'] = sku_guess
        items.append(item)
    items.sort(key=lambda x: (x['type'] != 'directory', x['name'].lower()))
    return items


def _cache_get_listing(rel_path: str) -> Optional[dict]:
    path = _listing_cache_path(rel_path)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        cached_at = data.get('cached_at')
        if not cached_at:
            return None
        ts = datetime.fromisoformat(cached_at.replace('Z', ''))
        if (datetime.utcnow() - ts).total_seconds() > LISTING_TTL_SECONDS:
            return None
        return data
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return None


def _cache_set_listing(rel_path: str, abs_path: str, items: list) -> None:
    payload = {
        'rel_path': _norm_rel(rel_path),
        'full_path': abs_path,
        'cached_at': _utc_now_iso(),
        'items': items,
    }
    _safe_write_json(_listing_cache_path(rel_path), payload)


def _breadcrumbs(rel_path: str) -> list:
    parts = [p for p in _norm_rel(rel_path).split('/') if p]
    crumbs = [{'label': '梁佳怡 (root)', 'rel_path': ''}]
    built = []
    for part in parts:
        built.append(part)
        crumbs.append({'label': part, 'rel_path': '/'.join(built)})
    return crumbs


def _listing_payload(rel_path: str, abs_path: str, items: list, *, from_cache: bool) -> dict:
    return {
        'rel_path': _norm_rel(rel_path),
        'full_path': abs_path,
        'base_path': CHINA_FTP_REMOTE_BASE,
        'breadcrumbs': _breadcrumbs(rel_path),
        'items': items,
        'directory_count': sum(1 for i in items if i['type'] == 'directory'),
        'file_count': sum(1 for i in items if i['type'] == 'file'),
        'image_count': sum(1 for i in items if i.get('is_image')),
        'from_cache': from_cache,
    }


def list_directory(rel_path: str = '', *, force_refresh: bool = False) -> dict:
    rel_norm = _norm_rel(rel_path)
    abs_path = china_ftp_rel_to_abs(rel_norm)

    if not force_refresh:
        cached = _cache_get_listing(rel_norm)
        if cached and isinstance(cached.get('items'), list):
            return _listing_payload(rel_norm, abs_path, cached['items'], from_cache=True)

    with _pool_lock:
        with _pool.sftp() as sftp:
            items = _sftp_list_items(sftp, abs_path, rel_norm)
    _cache_set_listing(rel_norm, abs_path, items)
    return _listing_payload(rel_norm, abs_path, items, from_cache=False)


def _load_index() -> Optional[dict]:
    if not os.path.isfile(INDEX_FILE):
        return None
    try:
        with open(INDEX_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get('entries'), list):
            return data
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return None


def index_status() -> dict:
    data = _load_index()
    if not data:
        return {'built_at': None, 'entry_count': 0, 'folder_count': 0}
    entries = data.get('entries') or []
    return {
        'built_at': data.get('built_at'),
        'entry_count': len(entries),
        'folder_count': data.get('folder_count'),
        'file_count': data.get('file_count'),
    }


def search_index(query: str, limit: int = CHINA_DIR_SEARCH_MAX) -> Optional[dict]:
    q = (query or '').strip()
    if not q:
        return {'query': q, 'items': [], 'count': 0, 'truncated': False, 'source': 'index'}

    data = _load_index()
    if not data:
        return None

    q_lower = q.lower()
    matches = []
    for entry in data.get('entries') or []:
        name = (entry.get('name') or '').lower()
        rel = (entry.get('rel_path') or '').lower()
        if q_lower in name or q_lower in rel:
            matches.append(entry)
            if len(matches) >= limit:
                break

    matches.sort(key=lambda x: (x.get('type') != 'directory', (x.get('name') or '').lower()))
    return {
        'query': q,
        'items': matches,
        'count': len(matches),
        'truncated': len(matches) >= limit,
        'source': 'index',
        'index_built_at': data.get('built_at'),
    }


def search_live(query: str, limit: int = CHINA_DIR_SEARCH_MAX) -> dict:
    q = (query or '').strip()
    safe_q = q.replace("'", "''")
    like = safe_q.replace('*', '%')
    ps = (
        f"$base = '{CHINA_FTP_REMOTE_BASE}'; "
        f"$q = '{like}'; "
        f"Get-ChildItem -LiteralPath $base -Recurse -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.Name -like ('*' + $q + '*') -or $_.FullName -like ('*' + $q + '*') }} | "
        f"Select-Object -First {int(limit)} "
        "@{N='FullName';E={$_.FullName}}, @{N='Name';E={$_.Name}}, "
        "@{N='Length';E={$_.Length}}, @{N='IsDirectory';E={$_.PSIsContainer}}, "
        "@{N='LastWriteTime';E={$_.LastWriteTime.ToString('o')}} | "
        f"ConvertTo-Json -Compress"
    )
    with _pool_lock:
        raw = _run_ssh_command(f'powershell -command "{ps}"', exec_timeout=180)
    parsed = json.loads(raw) if raw else []
    if isinstance(parsed, dict):
        parsed = [parsed]

    items = []
    for row in parsed:
        full_path = row.get('FullName') or ''
        rel = china_ftp_abs_to_rel(full_path)
        is_dir = bool(row.get('IsDirectory'))
        name = row.get('Name') or os.path.basename(full_path)
        item = {
            'name': name,
            'type': 'directory' if is_dir else 'file',
            'rel_path': rel,
            'full_path': full_path,
            'size': None if is_dir else int(row.get('Length') or 0),
            'modified': row.get('LastWriteTime'),
            'is_image': False if is_dir else _china_ftp_is_image(name),
            'tags': _china_ftp_path_tags(rel),
        }
        if not is_dir:
            sku_guess = name.split()[0].upper() if name else ''
            if sku_guess and sku_guess.isalnum():
                item['sku_guess'] = sku_guess
        items.append(item)

    items.sort(key=lambda x: (x['type'] != 'directory', x['name'].lower()))
    return {
        'query': q,
        'items': items,
        'count': len(items),
        'truncated': len(items) >= limit,
        'source': 'live',
    }


def search(query: str, limit: int = CHINA_DIR_SEARCH_MAX) -> dict:
    indexed = search_index(query, limit=limit)
    if indexed is not None:
        return indexed
    return search_live(query, limit=limit)


def find_sku_files_from_index(
    sku: str,
    *,
    year_only: bool = True,
    sync_only: bool = False,
    match_mode: str = 'filename',
) -> List[dict]:
    sku_upper = (sku or '').strip().upper()
    if not sku_upper:
        return []

    data = _load_index()
    if not data:
        return []

    matches = []
    for entry in data.get('entries') or []:
        if _entry_passes_sku_file_filters(
            entry,
            sku_upper,
            year_only=year_only,
            sync_only=sync_only,
            match_mode=match_mode,
        ):
            matches.append(entry)
    return matches


def _find_sku_files_live(
    sku: str,
    *,
    year_only: bool = True,
    sync_only: bool = False,
    match_mode: str = 'filename',
) -> List[dict]:
    sku_upper = (sku or '').strip().upper()
    if not sku_upper:
        return []

    if year_only:
        search_abs = china_ftp_rel_to_abs(current_year_label())
    else:
        search_abs = _china_ftp_base_norm()

    skip_exts = ','.join(f"'{ext}'" for ext in sorted(CHINA_FTP_SYNC_SKIP_EXTS))
    img_exts = ','.join(f"'{ext}'" for ext in sorted(CHINA_FTP_IMAGE_EXTS))
    safe_sku = sku_upper.replace("'", "''")
    if match_mode == 'folder':
        ps = (
            f"$p = '{search_abs}'; "
            f"$sku = '{safe_sku}'; "
            f"$skip = @({skip_exts}); "
            f"$img = @({img_exts}); "
            f"Get-ChildItem -LiteralPath $p -Recurse -File -ErrorAction SilentlyContinue | "
            f"Where-Object {{ $skip -notcontains $_.Extension.ToLower() -and $img -contains $_.Extension.ToLower() }} | "
            f"Where-Object {{ $_.FullName -match ('[\\\\/]' + [regex]::Escape($sku) + '([\\\\/]|$|[^0-9A-Za-z])') }} | "
            f"Select-Object "
            "@{N='FullName';E={$_.FullName}}, @{N='Name';E={$_.Name}}, "
            "@{N='Length';E={$_.Length}}, @{N='IsDirectory';E={$_.PSIsContainer}}, "
            "@{N='LastWriteTime';E={$_.LastWriteTime.ToString('o')}} | "
            f"ConvertTo-Json -Compress"
        )
    else:
        ps = (
            f"$p = '{search_abs}'; "
            f"$skip = @({skip_exts}); "
            f"Get-ChildItem -LiteralPath $p -Recurse -File -Filter '{safe_sku}*' "
            f"-ErrorAction SilentlyContinue | "
            f"Where-Object {{ $skip -notcontains $_.Extension.ToLower() }} | "
            f"Select-Object "
            "@{N='FullName';E={$_.FullName}}, @{N='Name';E={$_.Name}}, "
            "@{N='Length';E={$_.Length}}, @{N='IsDirectory';E={$_.PSIsContainer}}, "
            "@{N='LastWriteTime';E={$_.LastWriteTime.ToString('o')}} | "
            f"ConvertTo-Json -Compress"
        )
    with _pool_lock:
        raw = _run_ssh_command(f'powershell -command "{ps}"', exec_timeout=300)

    parsed = json.loads(raw) if raw else []
    if isinstance(parsed, dict):
        parsed = [parsed]
    entries = [_powershell_row_to_entry(row) for row in parsed]
    if sync_only:
        entries = filter_entries_for_sync(entries)
    elif match_mode == 'folder':
        entries = [
            e for e in entries
            if e.get('is_image') or is_sync_fetch_file(e.get('name') or '')
        ]
    return entries


def find_sku_files(
    sku: str,
    *,
    year_only: bool = True,
    sync_only: bool = False,
) -> Tuple[List[dict], str]:
    """Return image file entries for a SKU. Uses local index first, then scoped live search."""
    sku_upper = (sku or '').strip().upper()
    if not sku_upper:
        return [], 'none'

    def _strategies_for(sync_flag: bool):
        items = [
            ('filename', sync_flag),
            ('folder', sync_flag),
        ]
        if sync_flag:
            items.append(('folder', False))
        return items

    year_scopes = [True, False] if year_only else [False]

    for use_year_only in year_scopes:
        for match_mode, use_sync_only in _strategies_for(sync_only):
            indexed = find_sku_files_from_index(
                sku_upper,
                year_only=use_year_only,
                sync_only=use_sync_only,
                match_mode=match_mode,
            )
            if indexed:
                parts = [f'index_{match_mode}']
                if not use_sync_only:
                    parts.append('relaxed')
                if not use_year_only:
                    parts.append('all_years')
                return indexed, '_'.join(parts)

            live = _find_sku_files_live(
                sku_upper,
                year_only=use_year_only,
                sync_only=use_sync_only,
                match_mode=match_mode,
            )
            if live:
                parts = [f'live_{match_mode}']
                if not use_sync_only:
                    parts.append('relaxed')
                if not use_year_only:
                    parts.append('all_years')
                return live, '_'.join(parts)

    return [], 'none'


def sku_entry_to_asset_info(entry: dict, sku: str, *, already_synced: bool = False) -> dict:
    full_path = entry.get('full_path') or ''
    original_name = entry.get('name') or os.path.basename(full_path)
    filename = derive_sync_filename(entry, sku)
    rel = entry.get('rel_path') or china_ftp_abs_to_rel(full_path)
    return {
        'id': None,
        'sku': sku.upper(),
        'version': None,
        'filename': filename,
        'remote_filename': original_name,
        'source': 'china_ftp',
        'width': None,
        'height': None,
        'is_canonical': False,
        'created_at': None,
        'directory': os.path.dirname(full_path),
        'relative_path': rel,
        'file_path': full_path,
        'file_size': entry.get('size'),
        'already_synced': already_synced,
    }


def _powershell_row_to_entry(row: dict) -> dict:
    full_path = row.get('FullName') or ''
    rel = china_ftp_abs_to_rel(full_path)
    is_dir = bool(row.get('IsDirectory'))
    name = row.get('Name') or os.path.basename(full_path)
    item = {
        'name': name,
        'type': 'directory' if is_dir else 'file',
        'rel_path': rel,
        'full_path': full_path,
        'size': None if is_dir else int(row.get('Length') or 0),
        'modified': row.get('LastWriteTime'),
        'is_image': False if is_dir else _china_ftp_is_image(name),
        'tags': _china_ftp_path_tags(rel),
    }
    if not is_dir:
        sku_guess = name.split()[0].upper() if name else ''
        if sku_guess and sku_guess.isalnum():
            item['sku_guess'] = sku_guess
    return item


def _emit_progress(progress_fn, **kwargs) -> None:
    if progress_fn:
        progress_fn(kwargs)


def estimate_walk_progress(since_iso: str) -> dict:
    """Estimate SFTP walk progress from listing cache files written during rebuild."""
    if not since_iso:
        return {}
    try:
        since_ts = datetime.fromisoformat(since_iso.replace('Z', '')).timestamp() - 1.0
    except (ValueError, TypeError):
        return {}

    folder_count = 0
    try:
        for name in os.listdir(LISTINGS_DIR):
            path = os.path.join(LISTINGS_DIR, name)
            if os.path.isfile(path) and os.path.getmtime(path) >= since_ts:
                folder_count += 1
    except OSError:
        return {}

    if not folder_count:
        return {}
    return {
        'phase': 'walking',
        'method': 'sftp',
        'folder_count': folder_count,
    }


def _scan_path_powershell(
    abs_path: str,
    *,
    exec_timeout: int = 600,
    log_fn: Optional[Callable[[str], None]] = None,
    progress_fn: Optional[Callable[[dict], None]] = None,
) -> List[dict]:
    log = log_fn or (lambda msg: None)
    log(f'Scanning {abs_path}…')
    _emit_progress(progress_fn, phase='scanning', method='powershell', message=f'Scanning {abs_path}…')

    ps = (
        f"$base = '{abs_path}'; "
        f"Get-ChildItem -LiteralPath $base -Recurse -ErrorAction SilentlyContinue | "
        f"Select-Object "
        "@{N='FullName';E={$_.FullName}}, @{N='Name';E={$_.Name}}, "
        "@{N='Length';E={$_.Length}}, @{N='IsDirectory';E={$_.PSIsContainer}}, "
        "@{N='LastWriteTime';E={$_.LastWriteTime.ToString('o')}} | "
        f"ConvertTo-Json -Compress"
    )
    with _pool_lock:
        raw = _run_ssh_command(f'powershell -command "{ps}"', exec_timeout=exec_timeout)

    parsed = json.loads(raw) if raw else []
    if isinstance(parsed, dict):
        parsed = [parsed]
    return [_powershell_row_to_entry(row) for row in parsed]


def _merge_index_entries(existing_entries: List[dict], scope_rel: str, new_entries: List[dict]) -> List[dict]:
    scope_rel = _norm_rel(scope_rel)
    scope_prefix = scope_rel + '/'
    kept = [
        e for e in existing_entries
        if not (
            (e.get('rel_path') or '') == scope_rel
            or (e.get('rel_path') or '').startswith(scope_prefix)
        )
    ]
    return kept + new_entries


def build_current_year_index(
    log_fn: Optional[Callable[[str], None]] = None,
    progress_fn: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Incremental index update: rescan current year folder and merge into existing index."""
    log = log_fn or (lambda msg: None)
    started = time.time()
    year_label = current_year_label()
    year_abs = china_ftp_rel_to_abs(year_label)
    log(f'Starting current-year index update for {year_label}…')

    existing = _load_index()
    existing_entries = list((existing or {}).get('entries') or [])

    try:
        new_entries = _scan_path_powershell(
            year_abs,
            exec_timeout=600,
            log_fn=log_fn,
            progress_fn=progress_fn,
        )
    except Exception as exc:
        log(f'PowerShell scan failed ({exc}); falling back to SFTP walk for {year_label}…')
        new_entries = _walk_scope_bfs(year_label, log_fn=log_fn, progress_fn=progress_fn)

    merged = _merge_index_entries(existing_entries, year_label, new_entries)
    folder_count = sum(1 for e in merged if e.get('type') == 'directory')
    file_count = sum(1 for e in merged if e.get('type') == 'file')

    _emit_progress(
        progress_fn,
        phase='saving',
        method='merge',
        folder_count=folder_count,
        entry_count=len(merged),
        year=year_label,
    )

    payload = {
        'built_at': _utc_now_iso(),
        'base_path': CHINA_FTP_REMOTE_BASE,
        'folder_count': folder_count,
        'file_count': file_count,
        'entry_count': len(merged),
        'entries': merged,
        'duration_seconds': round(time.time() - started, 1),
        'build_method': 'current_year_merge',
        'scope_year': year_label,
    }
    _safe_write_json(INDEX_FILE, payload)
    log(
        f"Done — merged {year_label}: {len(new_entries)} scanned, "
        f"{len(merged)} total entries in {payload['duration_seconds']}s"
    )
    return {
        'folder_count': folder_count,
        'entry_count': len(merged),
        'duration_seconds': payload['duration_seconds'],
        'built_at': payload['built_at'],
        'build_method': 'current_year_merge',
        'scope_year': year_label,
        'scanned_entries': len(new_entries),
    }


def _walk_scope_bfs(
    scope_rel: str,
    log_fn: Optional[Callable[[str], None]] = None,
    progress_fn: Optional[Callable[[dict], None]] = None,
) -> List[dict]:
    log = log_fn or (lambda msg: None)
    entries: List[dict] = []
    folder_count = 0
    queue = [_norm_rel(scope_rel)]
    seen = set()

    with _pool_lock:
        with _pool.sftp() as sftp:
            while queue:
                rel = queue.pop(0)
                if rel in seen:
                    continue
                seen.add(rel)
                abs_path = china_ftp_rel_to_abs(rel)
                try:
                    items = _sftp_list_items(sftp, abs_path, rel)
                except Exception as exc:
                    log(f'skip {rel or "(root)"}: {exc}')
                    continue
                _cache_set_listing(rel, abs_path, items)
                folder_count += 1
                for item in items:
                    entries.append(item)
                    if item['type'] == 'directory':
                        queue.append(item['rel_path'])
                if folder_count == 1 or folder_count % 10 == 0:
                    _emit_progress(
                        progress_fn,
                        phase='walking',
                        method='sftp',
                        folder_count=folder_count,
                        entry_count=len(entries),
                    )
    return entries


def _build_full_index_powershell(
    log_fn: Optional[Callable[[str], None]] = None,
    progress_fn: Optional[Callable[[dict], None]] = None,
) -> dict:
    log = log_fn or (lambda msg: None)
    started = time.time()
    log('Starting PowerShell recursive scan (fast path)…')
    _emit_progress(progress_fn, phase='scanning', method='powershell', message='Remote scan in progress…')

    ps = (
        f"$base = '{CHINA_FTP_REMOTE_BASE}'; "
        f"Get-ChildItem -LiteralPath $base -Recurse -ErrorAction SilentlyContinue | "
        f"Select-Object "
        "@{N='FullName';E={$_.FullName}}, @{N='Name';E={$_.Name}}, "
        "@{N='Length';E={$_.Length}}, @{N='IsDirectory';E={$_.PSIsContainer}}, "
        "@{N='LastWriteTime';E={$_.LastWriteTime.ToString('o')}} | "
        f"ConvertTo-Json -Compress"
    )
    with _pool_lock:
        raw = _run_ssh_command(f'powershell -command "{ps}"', exec_timeout=900)

    _emit_progress(progress_fn, phase='parsing', method='powershell', message='Parsing scan results…')
    parsed = json.loads(raw) if raw else []
    if isinstance(parsed, dict):
        parsed = [parsed]

    entries = [_powershell_row_to_entry(row) for row in parsed]
    folder_count = sum(1 for e in entries if e.get('type') == 'directory')
    _emit_progress(
        progress_fn,
        phase='saving',
        method='powershell',
        folder_count=folder_count,
        entry_count=len(entries),
    )

    payload = {
        'built_at': _utc_now_iso(),
        'base_path': CHINA_FTP_REMOTE_BASE,
        'folder_count': folder_count,
        'file_count': sum(1 for e in entries if e.get('type') == 'file'),
        'entry_count': len(entries),
        'entries': entries,
        'duration_seconds': round(time.time() - started, 1),
        'build_method': 'powershell',
    }
    _safe_write_json(INDEX_FILE, payload)
    log(
        f"Done — {folder_count} folders, {len(entries)} entries in {payload['duration_seconds']}s (PowerShell)"
    )
    return {
        'folder_count': folder_count,
        'entry_count': len(entries),
        'duration_seconds': payload['duration_seconds'],
        'built_at': payload['built_at'],
        'build_method': 'powershell',
    }


def _build_full_index_bfs(
    log_fn: Optional[Callable[[str], None]] = None,
    progress_fn: Optional[Callable[[dict], None]] = None,
) -> dict:
    log = log_fn or (lambda msg: None)
    started = time.time()
    entries: List[dict] = []
    folder_count = 0
    queue = ['']
    seen = set()

    log('Starting BFS walk from root (SFTP)…')
    _emit_progress(progress_fn, phase='walking', method='sftp', folder_count=0, entry_count=0)
    with _pool_lock:
        with _pool.sftp() as sftp:
            while queue:
                rel = queue.pop(0)
                if rel in seen:
                    continue
                seen.add(rel)
                abs_path = china_ftp_rel_to_abs(rel)
                try:
                    items = _sftp_list_items(sftp, abs_path, rel)
                except Exception as exc:
                    log(f'skip {rel or "(root)"}: {exc}')
                    continue
                _cache_set_listing(rel, abs_path, items)
                folder_count += 1
                for item in items:
                    entries.append(item)
                    if item['type'] == 'directory':
                        queue.append(item['rel_path'])
                if folder_count == 1 or folder_count % 10 == 0:
                    _emit_progress(
                        progress_fn,
                        phase='walking',
                        method='sftp',
                        folder_count=folder_count,
                        entry_count=len(entries),
                    )
                if folder_count % 50 == 0:
                    log(f'{folder_count} folders, {len(entries)} entries…')

    _emit_progress(
        progress_fn,
        phase='saving',
        method='sftp',
        folder_count=folder_count,
        entry_count=len(entries),
    )
    payload = {
        'built_at': _utc_now_iso(),
        'base_path': CHINA_FTP_REMOTE_BASE,
        'folder_count': folder_count,
        'file_count': sum(1 for e in entries if e.get('type') == 'file'),
        'entry_count': len(entries),
        'entries': entries,
        'duration_seconds': round(time.time() - started, 1),
        'build_method': 'sftp',
    }
    _safe_write_json(INDEX_FILE, payload)
    log(f"Done — {folder_count} folders, {len(entries)} entries in {payload['duration_seconds']}s (SFTP)")
    return {
        'folder_count': folder_count,
        'entry_count': len(entries),
        'duration_seconds': payload['duration_seconds'],
        'built_at': payload['built_at'],
        'build_method': 'sftp',
    }


def build_full_index(
    log_fn: Optional[Callable[[str], None]] = None,
    progress_fn: Optional[Callable[[dict], None]] = None,
    *,
    scope: str = 'current_year',
) -> dict:
    """Build or refresh search index.

    Default (current_year): merge-scan only the current year folder into the existing index.
    scope='full': rebuild entire tree (slow; use only when no index exists yet).
    """
    log = log_fn or (lambda msg: None)
    if scope != 'full' and _load_index():
        return build_current_year_index(log_fn, progress_fn)
    try:
        return _build_full_index_powershell(log_fn, progress_fn)
    except Exception as exc:
        log(f'PowerShell scan failed ({exc}); falling back to SFTP walk…')
        return _build_full_index_bfs(log_fn, progress_fn)
