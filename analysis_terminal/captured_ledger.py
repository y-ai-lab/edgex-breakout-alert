"""Read-only pagination of unchanged captured research ledgers.

No snapshot tables/cache or execution integration. A signed, short-lived cursor
binds the source, read clock, full content digest and page size. Any update
requires restarting, rather than combining outcomes from different reads.
"""
import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import re
import secrets

SOURCES = ('pending', 'vwap')
EVIDENCE_SOURCES = ('pending-evidence', 'vwap-evidence')
TTL_MS = 120000
_SIGNING_KEY = secrets.token_bytes(32)  # Process-only; restart invalidates cursors.


class LedgerError(ValueError):
    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class ReadRequest:
    source: str
    limit: int
    snapshot_ms: int
    offset: int = 0
    digest: str | None = None


def _canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'),
                          allow_nan=False, ensure_ascii=False).encode()
    except (ValueError, TypeError):
        raise LedgerError(503, 'LEDGER_DATA_UNVERIFIABLE') from None


def _token(payload):
    body = base64.urlsafe_b64encode(_canonical(payload)).rstrip(b'=')
    signature = hmac.new(_SIGNING_KEY, body, hashlib.sha256).hexdigest()
    return body.decode() + '.' + signature


def read_request(*, source, limit, now_ms, cursor=None):
    if (source not in SOURCES+EVIDENCE_SOURCES or type(limit) is not int or not 1 <= limit <= 500
            or type(now_ms) is not int or now_ms <= 0):
        raise LedgerError(400, 'INVALID_LEDGER_REQUEST')
    if cursor is None:
        return ReadRequest(source, limit, now_ms)
    if not isinstance(cursor, str) or not 1 <= len(cursor) <= 1024:
        raise LedgerError(400, 'INVALID_LEDGER_CURSOR')
    parts = cursor.split('.')
    if len(parts) != 2 or not re.fullmatch(r'[A-Za-z0-9_-]+', parts[0]) or not re.fullmatch(r'[a-f0-9]{64}', parts[1]):
        raise LedgerError(400, 'INVALID_LEDGER_CURSOR')
    body = parts[0].encode()
    if not hmac.compare_digest(parts[1], hmac.new(_SIGNING_KEY, body, hashlib.sha256).hexdigest()):
        raise LedgerError(409, 'LEDGER_CURSOR_RESTART_REQUIRED')
    try:
        p = json.loads(base64.urlsafe_b64decode(body + b'=' * (-len(body) % 4)))
        valid = (type(p) is dict and set(p) == {'v','s','n','t','i','d'}
                 and type(p['v']) is int and p['v'] == 1
                 and p['s'] == source and type(p['n']) is int and p['n'] == limit
                 and type(p['t']) is int and p['t'] > 0
                 and type(p['i']) is int and p['i'] > 0
                 and isinstance(p['d'], str) and re.fullmatch(r'[a-f0-9]{64}', p['d']))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise LedgerError(400, 'INVALID_LEDGER_CURSOR')
    if not 0 <= now_ms-p['t'] < TTL_MS:
        raise LedgerError(409, 'LEDGER_CURSOR_RESTART_REQUIRED')
    return ReadRequest(source, limit, p['t'], p['i'], p['d'])


def page(request, review, records):
    if (not isinstance(review, dict) or not isinstance(records, list) or review.get('latest') != []
            or type(review.get('total_records')) is not int
            or review['total_records'] != len(records)):
        raise LedgerError(503, 'LEDGER_DATA_UNVERIFIABLE')
    if any(not isinstance(r, dict) or not isinstance(r.get('key'), str) or not r['key']
           or type(r.get('created_ms')) is not int or r['created_ms'] <= 0 for r in records):
        raise LedgerError(503, 'LEDGER_DATA_UNVERIFIABLE')
    if len({r['key'] for r in records}) != len(records):
        raise LedgerError(503, 'LEDGER_DATA_UNVERIFIABLE')
    ordered = sorted(records, key=lambda r: (r['created_ms'], r['key']))
    digest = hashlib.sha256(_canonical(dict(source=request.source, review=review, records=ordered))).hexdigest()
    if request.digest is not None and not hmac.compare_digest(request.digest, digest):
        raise LedgerError(409, 'LEDGER_CHANGED_RESTART_REQUIRED')
    if request.offset and request.offset >= len(ordered):
        raise LedgerError(400, 'INVALID_LEDGER_CURSOR')
    end = min(request.offset+request.limit, len(ordered))
    next_cursor = None if end == len(ordered) else _token(dict(
        v=1, s=request.source, n=request.limit, t=request.snapshot_ms, i=end, d=digest))
    result = dict(review, latest=ordered[request.offset:end])
    result['ledger_page'] = dict(source=request.source, snapshot_ms=request.snapshot_ms,
        expires_ms=request.snapshot_ms+TTL_MS, snapshot_sha256=digest,
        offset=request.offset, page_size=request.limit, returned_records=end-request.offset,
        total_records=len(ordered), next_cursor=next_cursor, complete=next_cursor is None,
        ordering='CREATED_MS_ASC_KEY_ASC', new_independent_sample=False,
        interpretation='SAME_CAPTURED_RESEARCH_LEDGER_NOT_ACTUAL_EXECUTION')
    return result
