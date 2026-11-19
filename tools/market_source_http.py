"""Bounded read-only retries and safe diagnostics for the market collectors."""
import json
import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from random import uniform

import requests

MAX_RETRY_DELAY = 60.0
TRANSIENT_STATUSES = {408, 429}
SAFE_HEADER = re.compile(r"[A-Za-z0-9._ -]{1,120}\Z")
PROXY_TOKEN = re.compile(r"[A-Za-z0-9._-]{1,80}\Z")
EDGE_CODE = re.compile(r"\b(?:error\s+code\s*[:=]?\s*|error\s+)(1\d{3}|5\d{2})\b", re.IGNORECASE)


class SupabaseSourceError(RuntimeError):
    def __init__(self, message, status_code=None, attempts=1, transient=False, diagnostic=None):
        super().__init__(message)
        self.status_code = status_code
        self.attempts = attempts
        self.transient = transient
        self.diagnostic = diagnostic


def _retry_after(value, now):
    if not isinstance(value, str):
        return None
    try:
        value = value.strip()
        if value.isascii() and value.isdecimal():
            return max(0, int(value))
        stamp = parsedate_to_datetime(value)
        return max(0, (stamp.astimezone(UTC) - now).total_seconds()) if stamp.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def _diagnostic(response, error, attempt, now):
    headers = response.headers if response is not None else {}
    server, ray = (headers.get(name) for name in ('Server', 'CF-Ray'))
    proxy = headers.get('Proxy-Status')
    segments = proxy.split(';') if isinstance(proxy, str) else []
    proxy_safe = [part.strip() for part in segments[:1] if PROXY_TOKEN.fullmatch(part.strip())]
    for part in segments[1:]:
        key, _, value = part.strip().partition('=')
        if key in {'error', 'received-status'} and PROXY_TOKEN.fullmatch(value):
            proxy_safe.append(f'{key}={value}')
    status = response.status_code if response is not None else None
    body = response.text if response is not None and status >= 400 else ''
    edge = EDGE_CODE.search(body[:4096]) if isinstance(body, str) else None
    cause = error
    dns = False
    for _ in range(8):
        if cause is None:
            break
        if type(cause).__name__ in {'gaierror', 'NameResolutionError'}:
            dns = True
            break
        cause = getattr(cause, 'reason', None) or cause.__cause__ or cause.__context__
    kind = ('DNS_RESOLUTION' if dns else type(error).__name__) if error is not None else 'HTTP_ERROR'
    return {'timestamp': now.isoformat(), 'attempt': attempt, 'http_status': status,
            'server': server if isinstance(server, str) and SAFE_HEADER.fullmatch(server) else None,
            'cf_ray': ray if isinstance(ray, str) and SAFE_HEADER.fullmatch(ray) else None,
            'proxy_status': '; '.join(proxy_safe) or None, 'error_type': kind,
            'response_detail': f'edge error code {edge[1]}' if edge else None}


def fetch_rows(url, headers, params, *, retries, base_delay, previous=None):
    previous = previous if isinstance(previous, dict) else {}
    try:
        not_before = datetime.fromisoformat(previous.get('retry_not_before', ''))
        if not_before > datetime.now(UTC):
            raise SupabaseSourceError('Supabase retry deferred by Retry-After',
                                      status_code=previous.get('http_status'), attempts=0,
                                      transient=True, diagnostic=previous)
    except (ValueError, TypeError):
        pass
    retries = max(1, min(5, retries))
    base_delay = max(0.0, min(base_delay, MAX_RETRY_DELAY))
    for attempt in range(1, retries + 1):
        response = None
        error = None
        try:
            response = requests.get(url, headers=headers, params=params, timeout=30, allow_redirects=False)
            response.raise_for_status()
            if not 200 <= response.status_code < 300:
                raise ValueError('unexpected HTTP status')
            rows = response.json()
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError('invalid transaction payload')
            return rows
        except (requests.RequestException, ValueError) as exc:
            error = exc
        status = response.status_code if response is not None else None
        transient = (isinstance(status, int) and (status in TRANSIENT_STATUSES or 500 <= status < 600)
                     or isinstance(error, (requests.Timeout, requests.ConnectionError,
                                           requests.exceptions.ChunkedEncodingError)))
        now = datetime.now(UTC)
        diagnostic = _diagnostic(response, error, attempt, now)
        header_delay = _retry_after(response.headers.get('Retry-After'), now) if response is not None else None
        if transient and header_delay is not None:
            not_before_epoch = min(now.timestamp() + min(header_delay, 253402300799), 253402300799)
            diagnostic['retry_not_before'] = datetime.fromtimestamp(not_before_epoch, UTC).isoformat()
        print('MARKET SOURCE ATTEMPT:', json.dumps(diagnostic, sort_keys=True))
        if not transient or attempt >= retries or (header_delay is not None and header_delay > MAX_RETRY_DELAY):
            label = f'HTTP {status}' if isinstance(status, int) and status >= 400 else diagnostic['error_type']
            raise SupabaseSourceError(f'Supabase request failed ({label}) after {attempt} attempt(s)',
                                      status_code=status if status and status >= 400 else None,
                                      attempts=attempt, transient=transient, diagnostic=diagnostic)
        delay = min(MAX_RETRY_DELAY, base_delay * 2 ** (attempt - 1))
        delay = min(MAX_RETRY_DELAY, delay + uniform(0, delay / 4))
        time.sleep(max(delay, header_delay or 0))
    raise AssertionError('unreachable retry loop')