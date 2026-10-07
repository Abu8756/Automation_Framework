"""
automation_framework.py
------------------------
Generic, service-agnostic automation framework: the Flask app builder
(AutomationFramework), the session/log/OTP engine behind /start /status
/otp /delete /logs, and the base class every automation service extends
(AutomationService).

This file has NO knowledge of any specific portal (Udyam, EPFO, Startup
India, ...). All of that lives in application.py, which imports the
`AutomationFramework` class from here, registers its own services on it,
and owns the Flask/Waitress run configuration.
"""

import json
import os
import re
import threading
import traceback
import uuid
import datetime

import base64
import hmac
import queue
import sqlite3
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from flask import Flask, Response, g, request, jsonify, send_from_directory
from flask_cors import CORS

# Socket.IO is only needed for services registered with status_comm_type="Socket"
# (pip install flask-socketio simple-websocket). Everything else works without it.
try:
    from flask_socketio import SocketIO, join_room, leave_room, emit as sio_emit
except ImportError:                                  # pragma: no cover
    SocketIO = None

def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# DATE RANGE HELPERS
# ============================================================
# Lets a schema field require a date to fall in some range relative to
# "today", instead of just matching a regex format. Used via the
# `date_range` rule in Validator.validate(), e.g.:
#
#   "dob": {"pattern": DATE_RE, "date_format": "%d/%m/%Y", "date_range": "past"}
#   "doj": {"pattern": DATE_RE, "date_format": "%d/%m/%Y",
#            "date_range": {"min": {"days": -3}, "max": "today"}}
#   "incorporation_date": {"pattern": DATE_RE,
#            "date_range": {"min": {"months": -4}}}
#
# Shorthand presets (pass the string directly as `date_range`):
#   "past"    -> value must be <= today
#   "future"  -> value must be >= today
#   "current" -> value must be == today
#
# Custom bounds (`date_range` as a dict with "min"/"max", either optional):
#   each bound is one of:
#     "today"                      -> today
#     "DD/MM/YYYY" (or date_format) -> a fixed date
#     {"days": -3}                 -> today shifted by N days (+/-)
#     {"months": -4}               -> today shifted by N months (+/-)
#     {"years": -1}                -> today shifted by N years (+/-)

DATE_RANGE_PRESETS = {
    "past": {"max": "today"},
    "future": {"min": "today"},
    "current": {"min": "today", "max": "today"},
}


def _shift_date(base: datetime.date, days=None, months=None, years=None) -> datetime.date:
    d = base
    if years:
        try:
            d = d.replace(year=d.year + years)
        except ValueError:
            d = d.replace(year=d.year + years, day=28)
    if months:
        month_index = d.month - 1 + months
        year = d.year + month_index // 12
        month = month_index % 12 + 1
        last_day = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                    31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
        d = d.replace(year=year, month=month, day=min(d.day, last_day))
    if days:
        d = d + datetime.timedelta(days=days)
    return d


def _resolve_date_bound(bound, today: datetime.date, date_format: str):
    """Turn a date_range min/max entry into a concrete date, or None."""
    if bound is None:
        return None
    if bound == "today":
        return today
    if isinstance(bound, str):
        return datetime.datetime.strptime(bound, date_format).date()
    if isinstance(bound, dict):
        return _shift_date(today, days=bound.get("days"), months=bound.get("months"), years=bound.get("years"))
    raise ValueError(f"Unsupported date_range bound: {bound!r}")


def check_date_range(value_str: str, rules: dict, full_name: str):
    """Returns an error message string if value_str violates rules['date_range'], else None."""
    date_range = rules.get("date_range")
    if not date_range:
        return None

    date_format = rules.get("date_format", "%d/%m/%Y")
    try:
        value_date = datetime.datetime.strptime(value_str, date_format).date()
    except ValueError:
        return rules.get("date_range_message", f"'{full_name}' is not a valid date")

    spec = DATE_RANGE_PRESETS[date_range] if isinstance(date_range, str) else date_range
    today = datetime.date.today()
    min_bound = _resolve_date_bound(spec.get("min"), today, date_format)
    max_bound = _resolve_date_bound(spec.get("max"), today, date_format)

    if min_bound and value_date < min_bound:
        return rules.get(
            "date_range_message",
            f"'{full_name}' must be on or after {min_bound.strftime(date_format)}",
        )
    if max_bound and value_date > max_bound:
        return rules.get(
            "date_range_message",
            f"'{full_name}' must be on or before {max_bound.strftime(date_format)}",
        )
    return None


# ============================================================
# CENTRALIZED / REUSABLE JSON OPTIONS HELPERS
# ============================================================

# Matches a value shaped like "2-Hindu Undivided Family", "51-Air Transport",
# "1-Yes", etc: leading digits, then a "-", then a human-readable label.
# Used by the `extract_number` schema rule below so a field can accept
# EITHER a bare code ("2") OR a "code-label" string and still be validated
# (and stored) as just the code.
_LEADING_NUMBER_RE = re.compile(r"^(\d+)\s*-\s*.+$")


def extract_leading_number(value):
    """If value looks like '<digits>-<label>', return just the '<digits>'
    part. Otherwise return value unchanged. Safe to call on any value —
    non-strings and plain numeric strings pass through untouched."""
    if isinstance(value, str):
        match = _LEADING_NUMBER_RE.match(value.strip())
        if match:
            return match.group(1)
    return value


# ============================================================
# DATE FORMAT AUTO-DETECTION / NORMALIZATION
# ============================================================
# Some upstream callers send dates as DD-MM-YYYY, YYYY-MM-DD (ISO),
# DD.MM.YYYY, etc. instead of the DD/MM/YYYY every date field here expects.
# Rather than reject those payloads, a field can opt in to auto-detecting
# whichever of the formats below it matches and rewriting it to the target
# format (DD/MM/YYYY by default) before the regular `pattern` check runs.
#
# Tried in this order — most specific/unambiguous first. "%m/%d/%Y" (US
# month-first) is deliberately last and only ever matches when nothing
# more specific does, since "05/06/2024" is inherently ambiguous between
# DD/MM and MM/DD; every portal here is DD/MM, so that's favored.
_DATE_INPUT_FORMATS = [
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%Y-%m-%d",   # ISO 8601, e.g. "2026-09-03"
    "%Y/%m/%d",
    "%d %b %Y",   # "24 Jul 1986"
    "%d %B %Y",   # "24 July 1986"
    "%m/%d/%Y",   # last resort, US-style month-first
]


def normalize_date(value, output_format: str = "%d/%m/%Y", input_formats=None):
    """Try each format in `input_formats` (default _DATE_INPUT_FORMATS)
    against `value` and, on the first match, return it re-formatted as
    `output_format`. If nothing matches (or value isn't a string), `value`
    is returned unchanged — so an unrecognized/garbage date still reaches
    the normal `pattern` check and produces its usual clear error instead
    of failing silently here."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    for fmt in (input_formats or _DATE_INPUT_FORMATS):
        try:
            parsed = datetime.datetime.strptime(stripped, fmt)
        except ValueError:
            continue
        return parsed.strftime(output_format)
    return value


def json_top_level_keys(options: dict) -> list:
    """Return the top-level keys of an options dict, e.g. {"1": {...}, "2": {...}} -> ["1", "2"]."""
    return list(options.keys()) if isinstance(options, dict) else []


def json_extract_values(data, key: str = "value") -> list:
    results = []
    if isinstance(data, dict):
        for k, v in data.items():
            if k == key and v not in results:
                results.append(v)
            results.extend(v_item for v_item in json_extract_values(v, key) if v_item not in results)
    elif isinstance(data, list):
        for item in data:
            results.extend(v_item for v_item in json_extract_values(item, key) if v_item not in results)
    return results


def json_build_choice_map(options: dict, group_key: str = "sub", value_key: str = "value") -> dict:
    return {
        group_id: [item[value_key] for item in info.get(group_key, [])]
        for group_id, info in (options or {}).items()
    }


# ============================================================
# EXTRA PER-SERVICE ENDPOINTS -- the @action decorator
# ============================================================
# Every service already gets /<name>/start /status /delete /logs (and
# /otp, unless needs_otp=False) for free. @action lets a service declare
# ADDITIONAL endpoints beyond that default set, e.g. a stateful multi-step
# flow like EPFO's: /epfo/start logs in once, then /epfo/member is called
# once per member reusing that same login (see set_resource/get_resource
# above), any number of times, independently of /epfo/start.
#
# Usage, inside an AutomationService subclass body:
#
#   @framework.service("epfo", needs_otp=False, schema={...})
#   class EPFOService(AutomationService):
#       def run(self, data):
#           obj = EPFOOnboarding(data=data, service=self)
#           result = obj.login()
#           self.set_resource(obj)      # keep it alive for /epfo/member
#           return result
#
#       @action("member", schema={"member": {"type": dict, "required": True,
#                                             "schema": _EPFO_MEMBER_SCHEMA}})
#       def member(self, data):
#           obj = self.get_resource()
#           if obj is None:
#               return {"error": "Call /epfo/start first"}, 409
#           return obj.add_member(data["member"])
#
# This registers POST /epfo/member. The framework builds a fresh service
# instance (session_id, framework, payload) for the call -- exactly like
# /start does for run() -- then invokes the decorated method as
# method(instance, data), where `data` is the request's JSON body
# (defaults-applied and schema-validated first, if `schema` was given).
# The method's return value becomes the JSON response:
#   - a dict            -> jsonify(dict), 200
#   - (dict, status_int) -> jsonify(dict), status_int
# Any exception raised inside is caught, logged to the session, and
# turned into a {"error": ...}, 500 response -- an action never needs its
# own try/except just to keep the server alive.
#
# -------- creates_session=True: a second "/start" for the same service --------
# By default an @action reuses an EXISTING session -- the caller must send
# a session_id from an earlier /<name>/start, and the action 404s if it's
# missing. Passing creates_session=True flips that: the action gets its
# OWN fresh session instead (no session_id in the request body), runs the
# decorated method in a background thread exactly like /start runs run(),
# and immediately responds 202 with a new session_id for the caller to
# poll via /<name>/status -- the same async contract as /start.
#
# This is for a service that needs more than one distinct "create a new
# session" entry point, e.g. EPFO:
#   /epfo/start  -> run(): logs in, and (optionally) processes an initial
#                   batch of members passed right alongside the login
#   /epfo/login  -> an action with creates_session=True: logs in only
#   /epfo/member -> a normal action: adds one member to whichever of the
#                   two sessions above is still live, reusing its login
#
#   @action("login", creates_session=True, schema={"user_name": {...}, ...})
#   def login(self, data):
#       obj = EPFOOnboarding(data=data, service=self)
#       result = obj.login()
#       self.set_resource(obj)      # keep it alive for /epfo/member
#       return result
#
# `schema` (if given) validates the request body itself, the same as the
# service's own PAYLOAD_SCHEMA does for /start -- there's no session yet
# to inherit a payload from. A service registered with unique_key=... gets
# the same "already running" 409 dedup /start gives, keyed the same way.
def action(name: str = None, methods=None, schema: dict = None, creates_session: bool = False):
    def decorator(func):
        func._is_action = True
        func._action_name = name or func.__name__
        func._action_methods = methods or ["POST"]
        func._action_schema = schema
        func._action_creates_session = creates_session
        return func
    return decorator


# ======================================================================
# API request log  (feature: every call + payload + response is stored)
#
#   * ONE OpenSearch index (_REQUEST_LOG_INDEX) holds every call as ONE
#     document: url + payload + response written together, once, after
#     the response is known.
#   * If OpenSearch is unreachable the same document goes to SQLite.
#     OpenSearch is re-checked every few seconds and used again as soon
#     as it is back.
#   * Every document carries `timestamp` (UTC ISO-8601) and `ts_ms`
#     (epoch millis) so lists are always sorted newest-first.
#   * Writes happen on a background thread -- a slow/down OpenSearch
#     never slows an API call.
# ======================================================================
_REQUEST_LOG_INDEX = "automation_api_requests"
_MAX_STORED_CHARS = 200_000
_DEFAULT_MASK_KEYS = ("password", "passwd", "pwd", "otp", "token", "access_token",
                      "refresh_token", "secret", "authorization")
_SERVICE_ROUTE_TYPES = ("start", "status", "otp", "delete", "logs")
_REQUEST_TYPES = _SERVICE_ROUTE_TYPES + ("action", "other")
_FILTER_FIELDS = ("session_id", "service", "request_type", "method")
_LIST_COLUMNS = ("request_id", "timestamp", "ts_ms", "session_id", "service", "request_type",
                 "method", "path", "url", "status_code", "duration_ms", "client_ip", "size")
_ALLOWED_ORIGIN = "https://indiafilings-tau.vercel.app"


def _utc_now_parts():
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z"), int(now.timestamp() * 1000)


def _to_ms(value, end_of_day: bool = False):
    """ISO string / epoch-millis -> epoch millis (None if empty/invalid).
    A bare 'YYYY-MM-DD' used as an upper bound means 'end of that day'.
    A timestamp without a timezone is read as server-local time."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or str(value).strip().isdigit():
        return int(value)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if len(text) == 10 and end_of_day:
        dt = dt + datetime.timedelta(days=1) - datetime.timedelta(milliseconds=1)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return int(dt.timestamp() * 1000)


def _mask_sensitive(value, keys):
    if isinstance(value, dict):
        return {k: ("***" if str(k).lower() in keys else _mask_sensitive(v, keys)) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_sensitive(v, keys) for v in value]
    return value


def _to_json_text(value) -> str:
    if value is None:
        return ""
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > _MAX_STORED_CHARS:
        text = json.dumps({"_truncated": True, "original_chars": len(text),
                           "preview": text[:_MAX_STORED_CHARS]}, ensure_ascii=False)
    return text


def _parse_status_filter(value):
    """'404' -> (404, 404); '4xx' -> (400, 499); anything else -> (None, None)."""
    v = str(value or "").strip().lower()
    if re.fullmatch(r"[1-5]xx", v):
        return int(v[0]) * 100, int(v[0]) * 100 + 99
    if re.fullmatch(r"\d{3}", v):
        return int(v), int(v)
    return None, None


def _parse_json_text(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


class _OpenSearchStore:
    """Minimal OpenSearch client on the standard library (no extra pip package)."""

    def __init__(self, url, index, user=None, password=None, verify_ssl=True, timeout=2.0):
        self.url = url.rstrip("/")
        self.index = index
        self.timeout = timeout
        self._auth = None
        if user:
            token = base64.b64encode(f"{user}:{password or ''}".encode("utf-8")).decode("ascii")
            self._auth = f"Basic {token}"
        self._ctx = ssl._create_unverified_context() if (self.url.startswith("https") and not verify_ssl) else None

    def _call(self, method, path, body=None, timeout=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self._auth:
            req.add_header("Authorization", self._auth)
        with urllib.request.urlopen(req, timeout=timeout or self.timeout, context=self._ctx) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}

    def ping(self):
        self._call("GET", "/")

    def ensure_index(self):
        try:
            self._call("GET", f"/{self.index}")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            self._call("PUT", f"/{self.index}", {
                "settings": {"number_of_shards": 1, "number_of_replicas": 0},
                "mappings": {"dynamic": False, "properties": {
                    "request_id":   {"type": "keyword"},
                    "timestamp":    {"type": "date"},
                    "ts_ms":        {"type": "long"},
                    "session_id":   {"type": "keyword"},
                    "service":      {"type": "keyword"},
                    "request_type": {"type": "keyword"},
                    "method":       {"type": "keyword"},
                    "path":         {"type": "keyword"},
                    "url":          {"type": "keyword"},
                    "status_code":  {"type": "integer"},
                    "duration_ms":  {"type": "float"},
                    "client_ip":    {"type": "keyword"},
                    "size":         {"type": "integer"},
                    # payload/response are arbitrary JSON, so they are kept as
                    # unindexed JSON text: no mapping explosion, still in _source.
                    "payload":      {"type": "keyword", "index": False, "doc_values": False},
                    "response":     {"type": "keyword", "index": False, "doc_values": False},
                }},
            })

    @staticmethod
    def _query(f):
        clauses = [{"term": {k: f[k]}} for k in _FILTER_FIELDS if f.get(k)]
        rng = {}
        if f.get("date_from") is not None:
            rng["gte"] = f["date_from"]
        if f.get("date_to") is not None:
            rng["lte"] = f["date_to"]
        if rng:
            clauses.append({"range": {"ts_ms": rng}})
        if f.get("status_min") is not None:
            clauses.append({"range": {"status_code": {"gte": f["status_min"], "lte": f["status_max"]}}})
        return {"bool": {"filter": clauses}} if clauses else {"match_all": {}}

    def insert(self, doc):
        self._call("PUT", f"/{self.index}/_doc/{doc['request_id']}", doc)

    def search(self, f, size, offset=0):
        res = self._call("POST", f"/{self.index}/_search", {
            "query": self._query(f),
            "sort": [{"ts_ms": "desc"}],
            "from": offset, "size": size,
            "track_total_hits": True,
            "_source": {"excludes": ["payload", "response"]},
        }, timeout=10)
        hits = res.get("hits", {})
        total = hits.get("total", {})
        total = total.get("value", 0) if isinstance(total, dict) else int(total or 0)
        return [h["_source"] for h in hits.get("hits", [])], total

    def get(self, request_id):
        try:
            return self._call("GET", f"/{self.index}/_doc/{urllib.parse.quote(request_id, safe='')}").get("_source")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def delete_by_query(self, query):
        res = self._call("POST", f"/{self.index}/_delete_by_query?refresh=true&conflicts=proceed",
                         {"query": query}, timeout=30)
        return int(res.get("deleted", 0))

    def delete_ids(self, ids):
        return self.delete_by_query({"ids": {"values": list(ids)}})

    def delete_filtered(self, f):
        return self.delete_by_query(self._query(f))


class _SQLiteStore:
    """Fallback store used while OpenSearch is unavailable."""

    def __init__(self, path):
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._exec("""CREATE TABLE IF NOT EXISTS api_requests (
            request_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, ts_ms INTEGER NOT NULL,
            session_id TEXT, service TEXT, request_type TEXT, method TEXT, path TEXT, url TEXT,
            status_code INTEGER, duration_ms REAL, client_ip TEXT, size INTEGER,
            payload TEXT, response TEXT)""")
        self._exec("CREATE INDEX IF NOT EXISTS ix_api_requests_ts ON api_requests(ts_ms DESC)")
        self._exec("CREATE INDEX IF NOT EXISTS ix_api_requests_session ON api_requests(session_id)")
        self._exec("CREATE INDEX IF NOT EXISTS ix_api_requests_service ON api_requests(service)")

    def _exec(self, sql, params=(), fetch=False):
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            conn.row_factory = sqlite3.Row
            with conn:
                cur = conn.execute(sql, params)
                return [dict(r) for r in cur.fetchall()] if fetch else cur.rowcount
        finally:
            conn.close()

    @staticmethod
    def _where(f):
        clauses, params = [], []
        for k in _FILTER_FIELDS:           # fixed tuple -> safe to interpolate
            if f.get(k):
                clauses.append(f"{k} = ?")
                params.append(f[k])
        if f.get("date_from") is not None:
            clauses.append("ts_ms >= ?")
            params.append(f["date_from"])
        if f.get("date_to") is not None:
            clauses.append("ts_ms <= ?")
            params.append(f["date_to"])
        if f.get("status_min") is not None:
            clauses.append("status_code BETWEEN ? AND ?")
            params += [f["status_min"], f["status_max"]]
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def insert(self, doc):
        cols = list(_LIST_COLUMNS) + ["payload", "response"]
        self._exec(f"INSERT OR REPLACE INTO api_requests ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                   [doc.get(c) for c in cols])

    def search(self, f, size, offset=0):
        where, params = self._where(f)
        total = self._exec(f"SELECT COUNT(*) AS n FROM api_requests{where}", params, fetch=True)[0]["n"]
        rows = self._exec(f"SELECT {','.join(_LIST_COLUMNS)} FROM api_requests{where} "
                          f"ORDER BY ts_ms DESC LIMIT ? OFFSET ?", params + [size, offset], fetch=True)
        return rows, total

    def get(self, request_id):
        rows = self._exec("SELECT * FROM api_requests WHERE request_id = ?", [request_id], fetch=True)
        return rows[0] if rows else None

    def delete_ids(self, ids):
        ids, deleted = list(ids), 0
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            deleted += self._exec(f"DELETE FROM api_requests WHERE request_id IN ({','.join('?' * len(chunk))})", chunk)
        return deleted

    def delete_filtered(self, f):
        where, params = self._where(f)
        return self._exec(f"DELETE FROM api_requests{where}", params)


class RequestLogStore:
    """OpenSearch first, SQLite when OpenSearch is down. Reads merge both,
    so nothing written to the fallback while OpenSearch was down is lost
    from the DevTools view."""

    def __init__(self, sqlite_path, opensearch_url=None, index=_REQUEST_LOG_INDEX, user=None,
                 password=None, verify_ssl=True, retry_seconds=30.0):
        self.sqlite = _SQLiteStore(sqlite_path)
        self.sqlite_path = sqlite_path
        self.index = index
        self.opensearch_url = opensearch_url
        self.os = _OpenSearchStore(opensearch_url, index, user, password, verify_ssl) if opensearch_url else None
        self._retry_seconds = retry_seconds
        self._os_ok = False
        self._os_checked_at = 0.0
        self._queue = queue.Queue(maxsize=10000)
        if self.os:
            print(f"[request-log] OpenSearch {'ready' if self.opensearch_available() else 'NOT reachable'} "
                  f"at {opensearch_url}; fallback SQLite: {sqlite_path}")
        else:
            print(f"[request-log] OpenSearch disabled; using SQLite: {sqlite_path}")
        threading.Thread(target=self._worker, daemon=True, name="request-log-writer").start()

    # -- availability ---------------------------------------------------
    def opensearch_available(self) -> bool:
        if not self.os:
            return False
        if self._os_ok:
            return True
        if time.time() - self._os_checked_at < self._retry_seconds:
            return False
        self._os_checked_at = time.time()
        try:
            self.os.ping()
            self.os.ensure_index()
            self._os_ok = True
        except Exception:
            self._os_ok = False
        return self._os_ok

    def _mark_os_down(self):
        self._os_ok = False
        self._os_checked_at = time.time()

    # -- write path -----------------------------------------------------
    def add(self, doc):
        try:
            self._queue.put_nowait(doc)
        except queue.Full:
            print("[request-log] queue full, dropping one entry")

    def _worker(self):
        while True:
            doc = self._queue.get()
            try:
                if self.opensearch_available():
                    try:
                        self.os.insert(doc)
                        continue
                    except Exception:
                        self._mark_os_down()
                self.sqlite.insert(doc)
            except Exception:
                traceback.print_exc()

    # -- read / delete path --------------------------------------------
    def search(self, filters, limit=100, offset=0):
        want = min(limit + offset, 10000)
        rows, total = [], 0
        if self.opensearch_available():
            try:
                r, t = self.os.search(filters, want)
                rows += [dict(x, storage="opensearch") for x in r]
                total += t
            except Exception:
                self._mark_os_down()
        r, t = self.sqlite.search(filters, want)
        rows += [dict(x, storage="sqlite") for x in r]
        total += t
        rows.sort(key=lambda d: d.get("ts_ms") or 0, reverse=True)
        return rows[offset:offset + limit], total

    def get(self, request_id):
        if self.opensearch_available():
            try:
                doc = self.os.get(request_id)
                if doc:
                    return dict(doc, storage="opensearch")
            except Exception:
                self._mark_os_down()
        doc = self.sqlite.get(request_id)
        return dict(doc, storage="sqlite") if doc else None

    def delete_ids(self, ids):
        deleted = self.sqlite.delete_ids(ids)
        if self.opensearch_available():
            try:
                deleted += self.os.delete_ids(ids)
            except Exception:
                self._mark_os_down()
        return deleted

    def delete_filtered(self, filters):
        deleted = self.sqlite.delete_filtered(filters)
        if self.opensearch_available():
            try:
                deleted += self.os.delete_filtered(filters)
            except Exception:
                self._mark_os_down()
        return deleted

    def status(self):
        return {"opensearch": self.opensearch_available(), "opensearch_url": self.opensearch_url,
                "index": self.index, "sqlite_path": self.sqlite_path}


# ======================================================================
# DevTools-style network viewer, served at GET /devtools
# (all dynamic text is inserted with textContent -- never innerHTML --
#  because payloads/responses are untrusted data)
# ======================================================================
_DEVTOOLS_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>API Network</title>
<style>
:root{color-scheme:dark light;--bg:#0f1115;--panel:#161a22;--panel2:#1b2030;--bd:#262c3a;--tx:#e4e8f0;--mut:#8b93a7;--acc:#5b8def;--sel:#1e2b4a;--hov:#1a2030;--ok:#3fb68b;--warn:#e5a93d;--err:#ef6b6b;--info:#6aa9ff;--str:#9ccf7a;--num:#e5a93d;--kw:#c586c0}
@media (prefers-color-scheme:light){:root{--bg:#f6f7fa;--panel:#fff;--panel2:#f0f2f7;--bd:#dde1ea;--tx:#1b2030;--mut:#6a7186;--acc:#2f6bff;--sel:#e3ecff;--hov:#f1f4fa;--ok:#12805c;--warn:#a96a00;--err:#c93434;--info:#1a63d6;--str:#2e7d32;--num:#a96a00;--kw:#8e44ad}}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;display:flex;flex-direction:column;background:var(--bg);color:var(--tx);font:13px/1.45 Inter,-apple-system,"Segoe UI",Roboto,sans-serif}
body.dragging{user-select:none;cursor:col-resize}
.mono,pre,td.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.mut{color:var(--mut)}
/* header */
header{display:flex;align-items:center;gap:12px;padding:10px 14px;background:var(--panel);border-bottom:1px solid var(--bd)}
.brand{font-weight:700;font-size:15px;letter-spacing:.2px}
.live{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--ok);background:color-mix(in srgb,var(--ok) 12%,transparent);padding:2px 9px;border-radius:99px}
.live i{width:7px;height:7px;border-radius:50%;background:var(--ok);animation:pulse 1.6s infinite}
.live.off{color:var(--err);background:color-mix(in srgb,var(--err) 12%,transparent)}.live.off i{background:var(--err);animation:none}
@keyframes pulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--ok) 60%,transparent)}70%{box-shadow:0 0 0 6px transparent}100%{box-shadow:0 0 0 0 transparent}}
.grow{flex:1}
.badge{font-size:11.5px;padding:2px 9px;border-radius:99px;border:1px solid var(--bd)}
.badge.ok{color:var(--ok);border-color:color-mix(in srgb,var(--ok) 40%,transparent)}.badge.warn{color:var(--warn);border-color:color-mix(in srgb,var(--warn) 40%,transparent)}
/* filters */
.filters{display:flex;flex-wrap:wrap;gap:8px 10px;align-items:flex-end;padding:10px 14px;background:var(--panel);border-bottom:1px solid var(--bd)}
.fld{display:flex;flex-direction:column;gap:3px}
.fld>span{font-size:10.5px;text-transform:uppercase;letter-spacing:.6px;color:var(--mut)}
input,select,button{background:var(--bg);color:var(--tx);border:1px solid var(--bd);border-radius:6px;padding:5px 9px;font:inherit;outline:none}
input:focus,select:focus{border-color:var(--acc)}
button{cursor:pointer}button:hover{background:var(--hov)}
.chips{display:flex;gap:4px}
.chip{padding:4px 10px;border-radius:99px;font-size:12px}
.chip.on{background:var(--acc);border-color:var(--acc);color:#fff}
.chip[data-s="2xx"].on{background:var(--ok);border-color:var(--ok)}.chip[data-s="4xx"].on{background:var(--warn);border-color:var(--warn)}.chip[data-s="5xx"].on{background:var(--err);border-color:var(--err)}
.danger{color:var(--err);border-color:color-mix(in srgb,var(--err) 50%,transparent)}.danger:hover{background:color-mix(in srgb,var(--err) 12%,transparent)}
/* layout */
.main{flex:1;display:flex;min-height:0}
.list{flex:1;overflow:auto;min-width:0}
table{border-collapse:separate;border-spacing:0;width:100%}
th{position:sticky;top:0;z-index:2;background:var(--panel2);text-align:left;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--mut);padding:7px 10px;border-bottom:1px solid var(--bd);white-space:nowrap}
td{padding:6px 10px;border-bottom:1px solid var(--bd);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
td.name{width:100%;max-width:0;font-weight:500}
tr.row{cursor:pointer}tr.row:nth-child(even){background:color-mix(in srgb,var(--panel) 55%,transparent)}
tr.row:hover{background:var(--hov)}tr.row.sel{background:var(--sel)!important;box-shadow:inset 3px 0 0 var(--acc)}
tr.row.err td.name{color:var(--err)}
tr.row.new{animation:flash 1.6s ease-out}
@keyframes flash{from{background:color-mix(in srgb,var(--acc) 35%,transparent)}to{background:transparent}}
.m{display:inline-block;min-width:54px;text-align:center;font-size:11px;font-weight:700;padding:1px 6px;border-radius:4px;background:color-mix(in srgb,var(--mut) 18%,transparent)}
.m-GET{color:var(--ok)}.m-POST{color:var(--info)}.m-DELETE{color:var(--err)}.m-PUT,.m-PATCH{color:var(--warn)}
.pill{display:inline-block;font-weight:700;font-size:11.5px;padding:1px 8px;border-radius:99px;cursor:pointer}
.s2{color:var(--ok);background:color-mix(in srgb,var(--ok) 14%,transparent)}.s3{color:var(--info);background:color-mix(in srgb,var(--info) 14%,transparent)}
.s4{color:var(--warn);background:color-mix(in srgb,var(--warn) 16%,transparent)}.s5{color:var(--err);background:color-mix(in srgb,var(--err) 16%,transparent)}
.link{color:var(--acc);cursor:pointer}.link:hover{text-decoration:underline}
.x{color:var(--mut);border:0;background:none;font-size:15px;padding:2px 6px}.x:hover{color:var(--err);background:none}
.empty{padding:50px 20px;text-align:center;color:var(--mut)}
.foot{display:flex;align-items:center;gap:10px;padding:8px 14px;color:var(--mut)}
/* detail */
.resizer{width:5px;cursor:col-resize;background:var(--bd);flex:none}.resizer:hover{background:var(--acc)}
.detail{width:46%;min-width:320px;display:flex;flex-direction:column;background:var(--panel);flex:none}
.dhead{display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid var(--bd)}
.dhead .p{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}
.tabs{display:flex;gap:2px;padding:0 8px;border-bottom:1px solid var(--bd)}
.tab{padding:8px 12px;cursor:pointer;color:var(--mut);border-bottom:2px solid transparent}.tab:hover{color:var(--tx)}.tab.on{color:var(--acc);border-color:var(--acc)}
.pane{flex:1;overflow:auto;padding:12px;position:relative}
.tools{display:flex;gap:6px;justify-content:flex-end;margin-bottom:8px}
.tools button{font-size:12px;padding:3px 9px}
pre{margin:0;white-space:pre-wrap;word-break:break-word;font-size:12px;line-height:1.55}
.k{color:var(--info)}.str{color:var(--str)}.num{color:var(--num)}.kw{color:var(--kw)}
dl{display:grid;grid-template-columns:120px 1fr;gap:7px 12px;margin:0}dt{color:var(--mut)}dd{margin:0;word-break:break-all}
.toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--tx);color:var(--bg);padding:6px 14px;border-radius:99px;font-size:12px;opacity:0;pointer-events:none;transition:opacity .2s}.toast.on{opacity:1}
[hidden]{display:none!important}
</style></head><body>
<header>
  <span class="brand">&#9889; API Network</span>
  <span class="live" id="live"><i></i><span id="live_t">Live</span></span>
  <span class="grow"></span>
  <span class="mut" id="updated"></span>
  <span class="badge" id="storage">&hellip;</span>
</header>
<div class="filters">
  <label class="fld"><span>Session ID</span><input id="f_session" placeholder="Filter by session" size="26"></label>
  <label class="fld"><span>Service</span><select id="f_service"><option value="">All</option></select></label>
  <label class="fld"><span>Request type</span><select id="f_type"><option value="">All</option></select></label>
  <label class="fld"><span>Method</span><select id="f_method"><option value="">All</option></select></label>
  <div class="fld"><span>Status code</span>
    <div class="chips">
      <button class="chip" data-s="">All</button><button class="chip" data-s="2xx">2xx</button><button class="chip" data-s="3xx">3xx</button>
      <button class="chip" data-s="4xx">4xx</button><button class="chip" data-s="5xx">5xx</button>
      <input id="f_status" placeholder="e.g. 404" size="7" maxlength="3" title="Exact code (404) or class (4xx)">
    </div></div>
  <label class="fld"><span>Time</span><select id="f_time">
    <option value="">Any time</option><option value="15">Last 15 min</option><option value="60">Last hour</option>
    <option value="1440">Last 24 hours</option><option value="custom">Custom range&hellip;</option></select></label>
  <label class="fld" id="w_from" hidden><span>From</span><input type="datetime-local" id="f_from"></label>
  <label class="fld" id="w_to" hidden><span>To</span><input type="datetime-local" id="f_to"></label>
  <span class="grow"></span>
  <button id="b_reset">Reset filters</button>
  <button id="b_del_all" class="danger">Delete filtered</button>
</div>
<div class="main">
  <div class="list" id="list">
    <table><thead><tr>
      <th>Time</th><th>Method</th><th>Name</th><th>Status</th><th>Type</th><th>Service</th><th>Session</th><th>Duration</th><th>Size</th><th></th>
    </tr></thead><tbody id="rows"></tbody></table>
    <div id="empty" class="empty" hidden>No requests match the current filters.</div>
    <div class="foot"><span id="info"></span><button id="more" hidden>Load more</button></div>
  </div>
  <div class="resizer" id="resizer" hidden></div>
  <div class="detail" id="detail" hidden>
    <div class="dhead"><span id="d_m"></span><span class="p" id="d_p"></span><span id="d_s"></span>
      <button class="x" id="d_del" title="Delete this request (Del)">&#128465;</button>
      <button class="x" id="d_close" title="Close (Esc)">&#10005;</button></div>
    <div class="tabs">
      <div class="tab on" data-t="general">General</div><div class="tab" data-t="payload">Payload</div><div class="tab" data-t="response">Response</div>
    </div>
    <div class="pane" id="pane"></div>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
const TOKEN=new URLSearchParams(location.search).get("token")||"";
const $=id=>document.getElementById(id);
const PAGE=100,POLL_MS=2000;
let offset=0,total=0,selected=null,current=null,tab="general",busy=false,first=true,seen=new Set();

async function api(path,opts={}){
  opts.headers=Object.assign({"Content-Type":"application/json"},opts.headers||{},TOKEN?{"X-Devtools-Token":TOKEN}:{});
  const r=await fetch(path,opts),j=await r.json().catch(()=>({}));
  if(!r.ok)throw new Error(j.error||("HTTP "+r.status));
  return j;
}
const iso=v=>v?new Date(v).toISOString():"";
const qs=o=>new URLSearchParams(Object.entries(o).filter(([,v])=>v!==""&&v!=null)).toString();
const pad=(n,l=2)=>String(n).padStart(l,"0");
function fmt(ms){const d=new Date(ms);return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(),3)}`;}
function fmtShort(ms){const d=new Date(ms),n=new Date(),t=`${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(),3)}`;
  return d.toDateString()===n.toDateString()?t:`${pad(d.getMonth()+1)}-${pad(d.getDate())} ${t}`;}
const fmtSize=n=>n==null?"":n<1024?n+" B":(n/1024).toFixed(1)+" kB";
const fmtDur=n=>n==null?"":n>=1000?(n/1000).toFixed(2)+" s":Math.round(n)+" ms";
function el(tag,text,cls){const e=document.createElement(tag);if(text!=null)e.textContent=text;if(cls)e.className=cls;return e;}
function toast(msg){const t=$("toast");t.textContent=msg;t.classList.add("on");clearTimeout(toast.h);toast.h=setTimeout(()=>t.classList.remove("on"),1400);}
function copy(text){
  const done=()=>toast("Copied");
  if(navigator.clipboard&&window.isSecureContext)navigator.clipboard.writeText(text).then(done,()=>{});
  else{const a=el("textarea",text);a.style.cssText="position:fixed;opacity:0";document.body.append(a);a.select();document.execCommand("copy");a.remove();done();}
}

function filters(){
  const p=$("f_time").value;let from="",to="";
  if(p==="custom"){from=iso($("f_from").value);to=iso($("f_to").value);}
  else if(p){from=new Date(Date.now()-Number(p)*60000).toISOString();}
  return{session_id:$("f_session").value.trim(),service:$("f_service").value,request_type:$("f_type").value,
    method:$("f_method").value,status_code:$("f_status").value.trim(),date_from:from,date_to:to};
}
function syncChips(){const v=$("f_status").value.trim().toLowerCase();document.querySelectorAll(".chip").forEach(c=>c.classList.toggle("on",c.dataset.s===v));}

function makeRow(it,isNew){
  const tr=el("tr",null,"row"+(it.status_code>=400?" err":"")+(it.request_id===selected?" sel":"")+(isNew?" new":""));
  tr.dataset.id=it.request_id;
  const t=el("td",fmtShort(it.ts_ms),"mono mut");t.title=fmt(it.ts_ms);
  const m=el("td");m.append(el("span",it.method,"m m-"+it.method));
  const n=el("td",it.path,"name");n.title=it.url;
  const st=el("td"),pill=el("span",it.status_code,"pill s"+String(it.status_code)[0]);
  pill.title="Filter by "+it.status_code;pill.onclick=e=>{e.stopPropagation();$("f_status").value=it.status_code;syncChips();load({force:true});};
  st.append(pill);
  const se=el("td",it.session_id?it.session_id.slice(0,8)+"\u2026":"-",it.session_id?"link mono":"mut");
  if(it.session_id){se.title=it.session_id+"\n(click to filter)";se.onclick=e=>{e.stopPropagation();$("f_session").value=it.session_id;load({force:true});};}
  const d=el("button","\u{1F5D1}","x");d.title="Delete";d.onclick=e=>{e.stopPropagation();del(it.request_id);};
  const dt=el("td");dt.append(d);
  tr.append(t,m,n,st,el("td",it.request_type),el("td",it.service),se,el("td",fmtDur(it.duration_ms),"mono"),el("td",fmtSize(it.size),"mono mut"),dt);
  tr.onclick=()=>select(it.request_id);
  return tr;
}

function setLive(ok){$("live").classList.toggle("off",!ok);$("live_t").textContent=ok?"Live":"Offline";}
async function load(o={}){
  if(busy)return;busy=true;
  try{
    const more=!!o.more;
    const limit=more?PAGE:Math.min(Math.max(PAGE,offset),500);       // keep already-loaded rows on refresh
    const j=await api("/devtools/api/requests?"+qs(Object.assign(filters(),{limit,offset:more?offset:0})));
    total=j.total;
    const frag=document.createDocumentFragment();
    j.items.forEach(it=>{frag.append(makeRow(it,!first&&!more&&!seen.has(it.request_id)));seen.add(it.request_id);});
    if(more){$("rows").append(frag);offset+=j.items.length;}else{$("rows").replaceChildren(frag);offset=j.items.length;}
    first=false;
    $("empty").hidden=total>0;$("more").hidden=offset>=total;
    const errs=[...document.querySelectorAll("tr.row.err")].length;
    $("info").textContent=`${Math.min(offset,total)} of ${total} request${total===1?"":"s"}`+(errs?` \u00b7 ${errs} with errors`:"");
    const s=j.storage||{};
    $("storage").textContent=s.opensearch?"OpenSearch":"SQLite fallback";
    $("storage").className="badge "+(s.opensearch?"ok":"warn");
    $("storage").title=s.opensearch?("Index "+s.index):("OpenSearch unavailable \u2014 "+s.sqlite_path);
    $("updated").textContent="Updated "+new Date().toLocaleTimeString([],{hour12:false});
    setLive(true);
  }catch(e){setLive(false);$("info").textContent="Error: "+e.message;}
  finally{busy=false;}
}

async function select(id){
  selected=id;document.querySelectorAll("tr.row").forEach(r=>r.classList.toggle("sel",r.dataset.id===id));
  try{current=await api("/devtools/api/requests/"+encodeURIComponent(id));openDetail();render();}
  catch(e){toast(e.message);}
}
function openDetail(){$("detail").hidden=false;$("resizer").hidden=false;}
function closeDetail(){selected=null;current=null;$("detail").hidden=true;$("resizer").hidden=true;document.querySelectorAll("tr.sel").forEach(r=>r.classList.remove("sel"));}

function pretty(v){return v===null||v===undefined||v===""?"":typeof v==="string"?v:JSON.stringify(v,null,2);}
function highlight(text){          // DOM-built (never innerHTML): payloads are untrusted
  const frag=document.createDocumentFragment();
  if(text.length>150000){frag.append(text);return frag;}
  const re=/("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g;let last=0,m;
  while((m=re.exec(text))){
    if(m.index>last)frag.append(text.slice(last,m.index));
    if(m[1]){frag.append(el("span",m[1],m[2]?"k":"str"));if(m[2])frag.append(m[2]);}
    else frag.append(el("span",m[0],m[3]?"kw":"num"));
    last=re.lastIndex;
  }
  if(last<text.length)frag.append(text.slice(last));
  return frag;
}
function curl(d){
  let s=`curl -X ${d.method} '${d.url}'`;
  if(d.method!=="GET"&&d.payload!=null&&d.payload!=="")s+=` \\\n  -H 'Content-Type: application/json' \\\n  -d '${JSON.stringify(d.payload).replace(/'/g,"'\\''")}'`;
  return s;
}
function render(){
  document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("on",t.dataset.t===tab));
  const pane=$("pane");pane.textContent="";if(!current)return;
  $("d_m").replaceChildren(el("span",current.method,"m m-"+current.method));
  $("d_p").textContent=current.path;$("d_p").title=current.url;
  $("d_s").replaceChildren(el("span",current.status_code,"pill s"+String(current.status_code)[0]));
  if(tab==="general"){
    const tools=el("div",null,"tools"),b=el("button","Copy as cURL");b.onclick=()=>copy(curl(current));
    const b2=el("button","Copy session ID");b2.onclick=()=>current.session_id&&copy(current.session_id);
    tools.append(b,b2);pane.append(tools);
    const dl=el("dl");
    [["URL",current.url],["Method",current.method],["Status",current.status_code],["Service",current.service],["Request type",current.request_type],
     ["Session ID",current.session_id||"-"],["Date / time",fmt(current.ts_ms)],["UTC",current.timestamp],["Duration",fmtDur(current.duration_ms)],
     ["Size",fmtSize(current.size)],["Client IP",current.client_ip||"-"],["Stored in",current.storage],["Request ID",current.request_id]]
      .forEach(([k,v])=>dl.append(el("dt",k),el("dd",v)));
    pane.append(dl);
  }else{
    const text=pretty(current[tab]);
    const tools=el("div",null,"tools"),b=el("button","Copy");b.onclick=()=>copy(text);tools.append(b);pane.append(tools);
    if(!text)pane.append(el("div","(empty)","mut"));
    else{const pre=el("pre",null,"mono");pre.append(highlight(text));pane.append(pre);}
  }
}
async function del(id){
  if(!confirm("Delete this request?"))return;
  try{await api("/devtools/api/requests/"+encodeURIComponent(id),{method:"DELETE"});if(selected===id)closeDetail();seen.delete(id);load({force:true});}
  catch(e){toast(e.message);}
}
$("b_del_all").onclick=async()=>{
  if(!confirm(`Delete ALL ${total} request(s) matching the current filters? This cannot be undone.`))return;
  try{const r=await api("/devtools/api/requests/delete",{method:"POST",body:JSON.stringify({all_filtered:true,filters:filters()})});
    closeDetail();await load();toast(`Deleted ${r.deleted}`);}catch(e){toast(e.message);}
};
document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>{tab=t.dataset.t;render();});
$("d_close").onclick=closeDetail;$("d_del").onclick=()=>selected&&del(selected);
$("more").onclick=()=>load({more:true});
document.querySelectorAll(".chip").forEach(c=>c.onclick=()=>{$("f_status").value=c.dataset.s;syncChips();load();});
let deb;const debounced=()=>{clearTimeout(deb);deb=setTimeout(()=>load(),300);};
$("f_session").addEventListener("input",debounced);
$("f_status").addEventListener("input",()=>{syncChips();debounced();});
["f_service","f_type","f_method","f_from","f_to"].forEach(i=>$(i).addEventListener("change",()=>load()));
$("f_time").addEventListener("change",()=>{const c=$("f_time").value==="custom";$("w_from").hidden=!c;$("w_to").hidden=!c;load();});
$("b_reset").onclick=()=>{["f_session","f_service","f_type","f_method","f_status","f_from","f_to","f_time"].forEach(i=>$(i).value="");
  $("w_from").hidden=$("w_to").hidden=true;syncChips();load();};
// drag-to-resize detail pane
let drag=false;
$("resizer").addEventListener("mousedown",e=>{drag=true;document.body.classList.add("dragging");e.preventDefault();});
window.addEventListener("mousemove",e=>{if(drag)$("detail").style.width=Math.min(Math.max(innerWidth-e.clientX,320),innerWidth-300)+"px";});
window.addEventListener("mouseup",()=>{drag=false;document.body.classList.remove("dragging");});
// keyboard: up/down to move through requests, Esc to close, Del to delete
document.addEventListener("keydown",e=>{
  const tag=(e.target.tagName||"").toLowerCase();
  if(["input","select","textarea"].includes(tag)){if(e.key==="Escape")e.target.blur();return;}
  if(e.key==="ArrowDown"||e.key==="ArrowUp"){
    const rows=[...document.querySelectorAll("tr.row")];if(!rows.length)return;
    let i=rows.findIndex(r=>r.dataset.id===selected);
    i=e.key==="ArrowDown"?Math.min(rows.length-1,i+1):Math.max(0,i<0?0:i-1);
    rows[i].scrollIntoView({block:"nearest"});select(rows[i].dataset.id);e.preventDefault();
  }else if(e.key==="Escape")closeDetail();
  else if(e.key==="Delete"&&selected)del(selected);
});
// auto-refresh is ALWAYS on (pauses only while the tab is hidden)
setInterval(()=>{if(!document.hidden)load();},POLL_MS);
document.addEventListener("visibilitychange",()=>{if(!document.hidden)load();});
(async()=>{
  try{const m=await api("/devtools/api/meta");
    const fill=(id,arr)=>arr.forEach(v=>{const o=el("option",v);o.value=v;$(id).append(o);});
    fill("f_service",m.services);fill("f_type",m.request_types);fill("f_method",m.methods);}catch(e){}
  syncChips();load();
})();
</script></body></html>
"""


class AutomationFramework:

    def __init__(self, name: str = __name__, log_path: str = "logs/sessions.log", port: int = 3333,
                 screenshot_dir: str = "logs/screenshots",
                 request_log: bool = True,
                 request_log_sqlite_path: str = "logs/api_requests.db",
                 opensearch_url: str = None,
                 opensearch_index: str = _REQUEST_LOG_INDEX,
                 opensearch_user: str = None,
                 opensearch_password: str = None,
                 opensearch_verify_ssl: bool = None,
                 devtools: bool = True,
                 devtools_token: str = None,
                 mask_sensitive: bool = True,
                 mask_keys=_DEFAULT_MASK_KEYS):
        """New (all optional):
        request_log          store every API call + payload + response (OpenSearch, else SQLite)
        opensearch_url       default env OPENSEARCH_URL or http://localhost:9200
        opensearch_user/_password/_verify_ssl   default env OPENSEARCH_USER / OPENSEARCH_PASSWORD /
                             OPENSEARCH_VERIFY_SSL (a local docker OpenSearch usually needs https + false)
        devtools             serve the network viewer at GET /devtools
        devtools_token       if set, /devtools needs ?token=... (header X-Devtools-Token on the API)
        mask_sensitive/mask_keys   replace values of these JSON keys with *** before storing"""
        self.app = Flask(name)
        self.port = port
        CORS(
                self.app,
                origins=_ALLOWED_ORIGIN
            )

        # Live status push for services declared with status_comm_type="Socket".
        # async_mode="threading" works with Flask's dev server and with Waitress
        # (Waitress cannot upgrade to WebSocket, so Socket.IO falls back to
        # long-polling there -- see run()).
        self.socketio = (SocketIO(self.app, cors_allowed_origins=_ALLOWED_ORIGIN, async_mode="threading")
                         if SocketIO is not None else None)

        self.sessions = {}          # session_id -> session dict
        self._lock = threading.Lock()

        # session_id -> arbitrary live Python object (e.g. a logged-in
        # portal automation instance). NEVER JSON-serialized or returned
        # from any route directly -- purely an in-process handoff so a
        # custom @action (see below) can reuse state a service's run()
        # already built, instead of redoing it (e.g. logging in again)
        # on every call. See set_resource/get_resource/clear_resource.
        self.resources = {}

        self.services = {}          # service_name -> service class
        # (service_name, unique_key_value) -> session_id, for services
        # registered with unique_key=... on @framework.service(). Entries
        # are added when a session starts and removed once it finishes
        # (result/error set) or is deleted — see _register_active_key(),
        # _release_active_key(), _active_session_for_key().
        self.active_keys = {}
        self.log_path = log_path

        directory = os.path.dirname(log_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        # Where error/step screenshots handed to add_log(..., screenshot=...)
        # get saved as PNG files, so log entries can reference them by name
        # instead of embedding a giant base64 blob in every log line.
        self.screenshot_dir = screenshot_dir
        os.makedirs(self.screenshot_dir, exist_ok=True)

        # --- API request log + DevTools view ---------------------------
        self.devtools_token = devtools_token
        self.mask_sensitive = mask_sensitive
        self.mask_keys = frozenset(str(k).lower() for k in mask_keys)
        self.request_store = None
        if request_log:
            verify = opensearch_verify_ssl
            if verify is None:
                verify = os.environ.get("OPENSEARCH_VERIFY_SSL", "true").strip().lower() not in ("0", "false", "no")
            self.request_store = RequestLogStore(
                sqlite_path=request_log_sqlite_path,
                opensearch_url=opensearch_url or os.environ.get("OPENSEARCH_URL", "http://localhost:9200"),
                index=opensearch_index,
                user=opensearch_user or os.environ.get("OPENSEARCH_USER"),
                password=opensearch_password or os.environ.get("OPENSEARCH_PASSWORD"),
                verify_ssl=verify,
            )
            self._register_request_logging()
            if devtools:
                self._register_devtools_routes()

        self._register_socket_events()
        self._register_global_routes()

    # ------------------------------------------------------------------
    # Service registration — the ONE decorator every service uses
    # ------------------------------------------------------------------
    def service(self, name: str, schema: dict = None, needs_otp: bool = True, unique_key: str = None,
                status_comm_type: str = "API"):
        # status_comm_type: "API" (default) -> the client polls POST /<name>/status.
        #                   "Socket"        -> every log line / progress / final result is ALSO
        #                                      pushed live over Socket.IO (see _register_socket_events).
        # /<name>/status keeps working in both modes.
        comm_type = str(status_comm_type or "API").strip().lower()
        if comm_type not in ("api", "socket"):
            raise ValueError(f"status_comm_type must be 'API' or 'Socket', got {status_comm_type!r}")
        if comm_type == "socket" and self.socketio is None:
            raise RuntimeError(f"Service '{name}' uses status_comm_type='Socket' but Socket.IO is not "
                               f"installed: pip install flask-socketio simple-websocket")
        schema = schema or {}

        def decorator(cls):
            cls.SERVICE_NAME = name
            cls.PAYLOAD_SCHEMA = schema
            # Some portals (e.g. EPFO) never need an OTP step. needs_otp=False
            # skips registering /<name>/otp entirely, so hitting it returns a
            # plain Flask 404 instead of a fake "not needed" response.
            cls.NEEDS_OTP = needs_otp
            # unique_key names a field in the service's own payload schema
            # (e.g. "username" holding a PAN) that identifies "the thing
            # being automated". While a session for a given value of that
            # field is still running, /<name>/start refuses to spin up a
            # second one for the same value — see _active_session_for_key().
            cls.UNIQUE_KEY = unique_key
            cls.STATUS_COMM_TYPE = comm_type
            self.services[name] = cls
            self._register_service_routes(name, cls)
            self._register_action_routes(name, cls)
            return cls

        return decorator

    def _save_screenshot(self, session_id: str, screenshot) -> str:
        """Decode a base64 (or raw bytes) screenshot and save it under
        self.screenshot_dir. Returns the saved file's name (not full path),
        or None if `screenshot` couldn't be saved. Accepts either a data
        URI ("data:image/png;base64,....") or a bare base64 string."""
        if not screenshot:
            return None
        try:
            if isinstance(screenshot, bytes):
                raw = screenshot
            else:
                b64_data = screenshot.split(",", 1)[1] if screenshot.startswith("data:") else screenshot
                raw = base64.b64decode(b64_data)
            filename = f"{session_id}_{uuid.uuid4().hex[:8]}.png"
            with open(os.path.join(self.screenshot_dir, filename), "wb") as f:
                f.write(raw)
            return filename
        except Exception:
            traceback.print_exc()
            return None

    def _write_log_file(self, service: str, session_id: str, level: str, message: str, kind: str,
                         screenshot_filename: str = None):
        entry = {
            "time": _now(),
            "service": service,
            "session_id": session_id,
            "level": level,
            "kind": kind,
            "message": message,
        }
        if screenshot_filename:
            entry["screenshot"] = f"/screenshots/{screenshot_filename}"
        with self._lock:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _read_logs(self):
        if not os.path.exists(self.log_path):
            return []
        results = []
        with self._lock:
            with open(self.log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        results.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return results

    def add_log(self, session_id: str, message: str, level: str = "INFO", kind: str = None, screenshot=None):
        """screenshot (optional): a base64-encoded PNG (bare string or a
        "data:image/png;base64,..." URI) or raw PNG bytes, e.g. from
        Selenium's driver.get_screenshot_as_base64(). When given, it's
        saved to disk under screenshot_dir and this log entry gets a
        "screenshot" field with the URL to fetch it — so it comes back
        automatically from GET-able logs and from /<service>/status when
        called with {"view": "logs", "session_id": ...}."""

        if kind is None:
            kind = "otp" if "otp" in message.lower() else "status"

        screenshot_filename = self._save_screenshot(session_id, screenshot) if screenshot else None

        with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                return
            entry = {"time": _now(), "message": message, "level": level, "kind": kind}
            if screenshot_filename:
                entry["screenshot"] = f"/screenshots/{screenshot_filename}"
            session["logs"].append(entry)
            log_index = len(session["logs"]) - 1
            session["status"] = message
            session["updated_at"] = _now()
            if kind == "otp":
                session["otp_hits"] = session.get("otp_hits", 0) + 1
            else:
                session["status_hits"] = session.get("status_hits", 0) + 1
            service_name = session["service"]
        self._write_log_file(service_name, session_id, level, message, kind, screenshot_filename=screenshot_filename)
        if self._comm_type(service_name) == "socket":
            self._emit_socket(session_id, "log", {**entry, "session_id": session_id,
                                                  "service": service_name, "index": log_index})

    def _session_hit_summary(self, session_id: str, session: dict) -> dict:

        hits = session.get("hits", 0)
        status_hits = session.get("status_hits", 0)
        otp_hits = session.get("otp_hits", 0)
        return {
            "session_id": session_id,
            "service_name": session.get("service"),
            "datetime": session.get("created_at"),
            "hits": hits,
            "status_hits": status_hits,
            "otp_hits": otp_hits,
            "total_hits": hits + status_hits + otp_hits,
        }


    def set_progress(self, session_id: str, progress: int):
        service_name, value = None, None
        with self._lock:
            if session_id in self.sessions:
                value = max(0, min(100, progress))
                self.sessions[session_id]["progress"] = value
                self.sessions[session_id]["updated_at"] = _now()
                service_name = self.sessions[session_id]["service"]
        if service_name and self._comm_type(service_name) == "socket":
            self._emit_socket(session_id, "progress", {"session_id": session_id, "progress": value})

    def set_result(self, session_id: str, result):
        with self._lock:
            if session_id in self.sessions:
                self.sessions[session_id]["result"] = result
                self.sessions[session_id]["updated_at"] = _now()

    def set_error(self, session_id: str, error: str):
        with self._lock:
            if session_id in self.sessions:
                self.sessions[session_id]["error"] = error
                self.sessions[session_id]["updated_at"] = _now()

    def get_result(self, session_id: str):
        """Read back whatever the session's result currently is (or None),
        without marking it 'finished' the way the /status route does.
        Handy for an @action that wants to append to the existing result
        (e.g. a running list of processed items) instead of overwriting it."""
        with self._lock:
            session = self.sessions.get(session_id)
            return session.get("result") if session else None

    # ------------------------------------------------------------------
    # Generic per-session resource store -- an in-memory-only handoff for
    # a live Python object (a logged-in automation client, an open
    # requests.Session, ...) that a service's run() built and a later
    # @action for the same session_id needs to reuse, instead of e.g.
    # logging in again on every call. Never JSON-serialized, never
    # returned from a route -- purely process-local state.
    # ------------------------------------------------------------------
    def set_resource(self, session_id: str, obj):
        with self._lock:
            self.resources[session_id] = obj

    def get_resource(self, session_id: str):
        with self._lock:
            return self.resources.get(session_id)

    def clear_resource(self, session_id: str):
        with self._lock:
            self.resources.pop(session_id, None)

    # otp_type lets a single session carry more than one OTP slot (e.g. the
    # Startup India flow needs a "login" OTP, then later a "mobile" and an
    # "email" OTP, submitted one by one as the automation reaches each
    # stage). otp_type="otp" (the default) keeps the original single-slot
    # behaviour used by every other existing service untouched.
    @staticmethod
    def _otp_keys(otp_type: str):
        if not otp_type or otp_type == "otp":
            return "otp", "otp_received"
        return f"{otp_type}_otp", f"{otp_type}_otp_received"

    def set_otp(self, session_id: str, otp: str, otp_type: str = "otp"):
        value_key, received_key = self._otp_keys(otp_type)
        with self._lock:
            if session_id in self.sessions:
                self.sessions[session_id][value_key] = otp
                self.sessions[session_id][received_key] = True
                self.sessions[session_id]["updated_at"] = _now()

    def get_otp(self, session_id: str, otp_type: str = "otp"):
        value_key, _ = self._otp_keys(otp_type)
        with self._lock:
            session = self.sessions.get(session_id)
            return session.get(value_key) if session else None

    def otp_received(self, session_id: str, otp_type: str = "otp") -> bool:
        _, received_key = self._otp_keys(otp_type)
        with self._lock:
            session = self.sessions.get(session_id)
            return bool(session and session.get(received_key))

    def clear_otp(self, session_id: str, otp_type: str = "otp"):
        """Wipe an OTP slot (value + received flag). Used to discard a bad/expired
        value, or to consume a good one so a later retry never resubmits it."""
        value_key, received_key = self._otp_keys(otp_type)
        with self._lock:
            if session_id in self.sessions:
                self.sessions[session_id][value_key] = None
                self.sessions[session_id][received_key] = False
                self.sessions[session_id]["updated_at"] = _now()


    @classmethod
    def validate(cls, data, schema: dict, _path: str = ""):
        if not isinstance(data, dict):
            return [f"'{_path or 'payload'}' must be a JSON object"]

        errors = []
        for field, rules in schema.items():
            full_name = f"{_path}.{field}" if _path else field
            required = rules.get("required", False)
            required_if = rules.get("required_if")
            required_if_empty = rules.get("required_if_empty")
            expected_type = rules.get("type")
            allow_blank = rules.get("allow_blank", False)

            if required_if:
                sibling_val = str(data.get(required_if["field"]))
                if sibling_val == str(required_if.get("equals")):
                    required = True

            # required only when another (sibling) field is missing/blank —
            # e.g. property_tax_number is required only if 'zone' was left empty.
            if required_if_empty:
                sibling_val = data.get(required_if_empty["field"])
                if sibling_val in (None, ""):
                    required = True

            value = data.get(field)
            is_missing = field not in data or (value in (None, "") and not (allow_blank and value == ""))
            if is_missing:
                if required:
                    errors.append(f"'{full_name}' is required")
                continue

            # -------- accept "<code>-<label>" and reduce it to "<code>" --------
            # e.g. "2-Hindu Undivided Family" -> "2", "51-Air Transport" -> "51".
            # Normalizes `data[field]` in place so every check below (type,
            # pattern, choices) — and whatever the service does with `data`
            # afterwards — only ever sees the plain code.
            if rules.get("extract_number"):
                value = extract_leading_number(value)
                data[field] = value

            # -------- auto-detect the incoming date format and rewrite it --------
            # e.g. "24-07-1986" or "2026-09-03" -> "24/07/1986" / "03/09/2026",
            # so a field can declare "pattern": _DATE_DDMMYYYY_RE and still
            # accept whichever common date format the caller actually sent.
            # Override the target with "date_format" and/or the formats tried
            # with "date_input_formats" (both already used by date_range).
            if rules.get("normalize_date"):
                value = normalize_date(
                    value,
                    output_format=rules.get("date_format", "%d/%m/%Y"),
                    input_formats=rules.get("date_input_formats"),
                )
                data[field] = value

            if expected_type and not isinstance(value, expected_type):
                errors.append(f"'{full_name}' must be of type {expected_type.__name__}")
                continue

            # -------- length checks --------
            min_length = rules.get("min_length")
            max_length = rules.get("max_length")
            if isinstance(value, str):
                if min_length is not None and len(value) < min_length:
                    errors.append(f"'{full_name}' must be at least {min_length} characters long")
                    continue
                if max_length is not None and len(value) > max_length:
                    errors.append(f"'{full_name}' must be at most {max_length} characters long")
                    continue

            # -------- regex pattern check --------
            pattern = rules.get("pattern")
            if pattern and isinstance(value, str):
                if not pattern.match(value):
                    message = rules.get("pattern_message", f"'{full_name}' has an invalid format")
                    errors.append(message)
                    continue

            # -------- date range check (past / future / current / relative offsets) --------
            if "date_range" in rules and isinstance(value, str):
                date_range_error = check_date_range(value, rules, full_name)
                if date_range_error:
                    errors.append(date_range_error)
                    continue

            choices = rules.get("choices")
            choices_map = rules.get("choices_map")
            depends_on = rules.get("depends_on")

            if choices_map is not None and depends_on:
                sibling_value = data.get(depends_on)
                choices = choices_map.get(sibling_value, [])
                if sibling_value is None or sibling_value not in choices_map:
                    errors.append(
                        rules.get(
                            "depends_on_message",
                            f"'{full_name}' cannot be validated: '{depends_on}' is missing or invalid",
                        )
                    )
                    continue

            if choices is not None and value not in choices:
                message = rules.get(
                    "choices_message",
                    f"'{full_name}' must be one of: {', '.join(map(str, choices))}",
                )
                errors.append(message)
                continue

            # -------- every element of a list must be one of `each_choice` --------
            each_choice = rules.get("each_choice")
            if each_choice is not None and isinstance(value, list):
                invalid = [v for v in value if v not in each_choice]
                if invalid:
                    message = rules.get(
                        "each_choice_message",
                        f"'{full_name}' contains invalid values: {', '.join(map(str, invalid))}",
                    )
                    errors.append(message)
                    continue

            if "schema" in rules and isinstance(value, dict):
                errors.extend(cls.validate(value, rules["schema"], _path=full_name))

            if "each" in rules and isinstance(value, dict):
                for key, sub_value in value.items():
                    errors.extend(cls.validate(sub_value, rules["each"], _path=f"{full_name}.{key}"))

            if "items" in rules and isinstance(value, list):
                for idx, item in enumerate(value):
                    errors.extend(cls.validate(item, rules["items"], _path=f"{full_name}[{idx}]"))

        return errors

    @classmethod
    def apply_defaults(cls, data, schema: dict):

        if not isinstance(data, dict):
            return data

        for field, rules in schema.items():
            default = rules.get("default")
            if default is not None:
                value = data.get(field)
                if field not in data or value in (None, ""):
                    data[field] = default

            if "schema" in rules and isinstance(data.get(field), dict):
                cls.apply_defaults(data[field], rules["schema"])

            if "each" in rules and isinstance(data.get(field), dict):
                for sub_value in data[field].values():
                    if isinstance(sub_value, dict):
                        cls.apply_defaults(sub_value, rules["each"])

            if "items" in rules and isinstance(data.get(field), list):
                for item in data[field]:
                    if isinstance(item, dict):
                        cls.apply_defaults(item, rules["items"])

        return data


    def _create_session(self, service_name: str, payload: dict) -> str:
        session_id = str(uuid.uuid4())
        with self._lock:
            self.sessions[session_id] = {
                "service": service_name,
                "status": "Starting",
                "logs": [],
                "result": None,
                "error": None,
                "progress": 0,
                "otp": None,
                "otp_received": False,
                # extra OTP slots used by multi-OTP flows (e.g. startup_india's
                # login / mobile / email OTPs) — unused slots just stay None.
                "login_otp": None,
                "login_otp_received": False,
                "mobile_otp": None,
                "mobile_otp_received": False,
                "email_otp": None,
                "email_otp_received": False,
                "payload": payload,
                "created_at": datetime.datetime.now().isoformat(),
                "updated_at": _now(),
                # hit-count breakdown, see _session_hit_summary()
                "hits": 0,          # times /<service>/status was polled (session_id in body)
                "status_hits": 0,   # add_log() calls classified as "status"
                "otp_hits": 0,      # add_log() calls classified as "otp"
            }
        return session_id

    # ------------------------------------------------------------------
    # unique_key dedup — "is this PAN/username/whatever already running?"
    # ------------------------------------------------------------------
    def _active_session_for_key(self, service_name: str, key_value):
        """Return the session_id already running for (service_name, key_value),
        or None if there isn't one. Also clears out the mapping if the
        session it points to has since finished or vanished, so a stale
        entry never blocks a new /start forever."""
        if key_value is None:
            return None
        with self._lock:
            existing_id = self.active_keys.get((service_name, key_value))
            if not existing_id:
                return None
            session = self.sessions.get(existing_id)
            finished = session is None or session.get("result") is not None or session.get("error") is not None
            if finished:
                self.active_keys.pop((service_name, key_value), None)
                return None
            return existing_id

    def _register_active_key(self, service_name: str, key_value, session_id: str):
        if key_value is None:
            return
        with self._lock:
            self.active_keys[(service_name, key_value)] = session_id

    def _release_active_key(self, service_name: str, key_value):
        if key_value is None:
            return
        with self._lock:
            self.active_keys.pop((service_name, key_value), None)

    def _run_in_background(self, service_cls, session_id: str, data: dict):
        unique_key = getattr(service_cls, "UNIQUE_KEY", None)
        key_value = data.get(unique_key) if unique_key else None

        def worker():
            try:
                self.add_log(session_id, "Automation started")
                instance = service_cls(session_id=session_id, framework=self, data=data)
                result = instance.run(data)
                self.set_result(session_id, result)
                self.set_progress(session_id, 100)
                self.add_log(session_id, "Automation completed")
            except Exception as e:
                traceback.print_exc()
                self.set_error(session_id, str(e))
                # If the service exposes a live Selenium `driver` attribute,
                # grab one last screenshot automatically so the failure shows
                # up visually in /<service>/status {"view": "logs"} even if
                # the service itself never called capture_screenshot().
                screenshot = None
                driver = getattr(locals().get("instance"), "driver", None)
                if driver is not None:
                    try:
                        screenshot = driver.get_screenshot_as_base64()
                    except Exception:
                        screenshot = None
                self.add_log(session_id, f"Error: {str(e)}", level="ERROR", screenshot=screenshot)
            finally:
                # free up the unique key regardless of success/failure so a
                # later /start for the same PAN/username isn't blocked forever
                self._release_active_key(service_cls.SERVICE_NAME, key_value)
                self._emit_finished(session_id)

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Same as _run_in_background above, but for a creates_session=True
    # @action instead of run() -- used by e.g. /epfo/login. Identical
    # worker shape (build instance, call it, set_result/set_error,
    # auto-screenshot-on-failure, release any unique_key), just calling
    # func(instance, data) instead of instance.run(data).
    # ------------------------------------------------------------------
    def _run_action_in_background(self, service_cls, func, action_name: str, session_id: str, data: dict):
        unique_key = getattr(service_cls, "UNIQUE_KEY", None)
        key_value = data.get(unique_key) if unique_key else None

        def worker():
            try:
                self.add_log(session_id, f"Automation started ('{action_name}')")
                instance = service_cls(session_id=session_id, framework=self, data=data)
                result = func(instance, data)
                # An action method may return (dict, status_int) the way a
                # normal (session-reusing) action can -- for a background
                # job there's no synchronous HTTP response to attach that
                # status code to, so just keep the dict; the caller reads
                # outcome/errors from the dict itself via /<name>/status.
                if isinstance(result, tuple):
                    result = result[0]
                self.set_result(session_id, result)
                self.set_progress(session_id, 100)
                self.add_log(session_id, "Automation completed")
            except Exception as e:
                traceback.print_exc()
                self.set_error(session_id, str(e))
                screenshot = None
                driver = getattr(locals().get("instance"), "driver", None)
                if driver is not None:
                    try:
                        screenshot = driver.get_screenshot_as_base64()
                    except Exception:
                        screenshot = None
                self.add_log(session_id, f"Error in '{action_name}': {e}", level="ERROR", screenshot=screenshot)
            finally:
                self._release_active_key(service_cls.SERVICE_NAME, key_value)
                self._emit_finished(session_id)

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Per-service routes: /<name>/start /status /otp /delete /logs
    # ------------------------------------------------------------------
    def _register_service_routes(self, name: str, cls):
        prefix = f"/{name}"

        def start():
            data = request.get_json(silent=True)
            if data is None:
                return jsonify({"error": "Missing JSON payload"}), 400

            data = self.apply_defaults(data, cls.PAYLOAD_SCHEMA)

            errors = self.validate(data, cls.PAYLOAD_SCHEMA)
            if errors:
                return jsonify({"error": "Payload validation failed", "details": errors}), 422

            unique_key = getattr(cls, "UNIQUE_KEY", None)
            key_value = data.get(unique_key) if unique_key else None

            if unique_key:
                existing_session_id = self._active_session_for_key(name, key_value)
                if existing_session_id:
                    return jsonify({
                        "service": name,
                        "status": "already_running",
                        "message": f"This {key_value} is running on automation",
                        unique_key: key_value,
                        "session_id": existing_session_id,
                    }), 409

            session_id = self._create_session(name, data)
            if unique_key:
                self._register_active_key(name, key_value, session_id)
            self._run_in_background(cls, session_id, data)

            return jsonify({"service": name, "session_id": session_id, "status": "Automation started",
                            "status_comm_type": self._comm_type(name)}), 202

        def status():
            data = request.get_json(silent=True) or {}
            session_id = data.get("session_id")
            if not session_id:
                return jsonify({"error": "'session_id' is required in the request body"}), 400

            # "view" picks what /status hands back:
            #   "status" (default) -> the short finished/message payload, unchanged
            #   "logs"              -> the full log trail for this session_id
            view = data.get("view", "status")
            if view not in ("status", "logs"):
                return jsonify({"error": "'view' must be 'status' or 'logs'"}), 400

            with self._lock:
                session = self.sessions.get(session_id)
                if session is not None and session.get("service") == name:
                    # every status poll counts as a "hit" for this session
                    session["hits"] = session.get("hits", 0) + 1
                session = dict(session) if session else None
            if session is None or session.get("service") != name:
                return jsonify({"error": "Session not found"}), 404

            if view == "logs":
                logs = session.get("logs", [])
                return jsonify({
                    "service": name,
                    "session_id": session_id,
                    "count": len(logs),
                    "logs": logs,
                }), 200

            # view == "status" -------------------------------------------------
            # A job is "finished" once it has a result or an error set by
            # _run_in_background()'s worker.
            finished = session.get("result") is not None or session.get("error") is not None

            if not finished:
                return jsonify({
                    "service": name,
                    "session_id": session_id,
                    "finished": False,
                    "message": "Service in processing, please check again in a few minutes.",
                }), 200

            if session.get("error"):
                return jsonify({
                    "service": name,
                    "session_id": session_id,
                    "finished": True,
                    "status": "failed",
                    "error": session.get("error"),
                }), 200

            # Done, no error -> hand back just the result itself, not the
            # whole session (logs/otp/payload/etc). A service can put a
            # "status_code" key in its own result dict (e.g. 409 for
            # "portal session already active", 401 for "bad password") to
            # drive the actual HTTP status of this response; anything else
            # (including a plain string/list result) just gets 200.
            result = session.get("result")
            http_status = 200
            if isinstance(result, dict) and isinstance(result.get("status_code"), int):
                http_status = result["status_code"]
            return jsonify(result), http_status

        def otp():
            data = request.get_json(silent=True) or {}
            session_id = data.get("session_id")
            if not session_id:
                return jsonify({"error": "'session_id' is required in the request body"}), 400
            if session_id not in self.sessions or self.sessions[session_id].get("service") != name:
                return jsonify({"error": "Session not found"}), 404
            otp_value = data.get("otp")
            if not otp_value:
                return jsonify({"error": "'otp' is required"}), 400
            otp_value = str(otp_value).strip()

            # optional "otp_type" lets a service submit more than one OTP
            # one by one (e.g. "login", "mobile", "email"); omit it for the
            # original single-OTP flow used by every other service.
            otp_type = data.get("otp_type", "otp")
            pattern, pattern_description = _OTP_TYPE_PATTERNS.get(otp_type, _OTP_TYPE_PATTERNS["otp"])
            if not pattern.match(otp_value):
                return jsonify({"error": f"'otp' must be {pattern_description}"}), 400

            self.set_otp(session_id, otp_value, otp_type=otp_type)
            self.add_log(session_id, f"OTP submitted ({otp_type})")
            return jsonify({"status": "success", "message": "OTP stored", "session_id": session_id, "otp_type": otp_type}), 200

        def delete():
            data = request.get_json(silent=True) or {}
            session_id = data.get("session_id")
            if not session_id:
                return jsonify({"error": "'session_id' is required in the request body"}), 400
            with self._lock:
                session = self.sessions.get(session_id)
                existed = session is not None and session.get("service") == name
                if existed:
                    self.sessions.pop(session_id, None)
                self.resources.pop(session_id, None)
            if existed:
                unique_key = getattr(cls, "UNIQUE_KEY", None)
                if unique_key:
                    key_value = (session.get("payload") or {}).get(unique_key)
                    self._release_active_key(name, key_value)
                return jsonify({"status": "Deleted", "session_id": session_id}), 200
            return jsonify({"error": "Session not found"}), 404

        def service_logs():
            limit = request.args.get("limit", default=200, type=int)
            logs = [e for e in self._read_logs() if e.get("service") == name][-limit:]
            return jsonify({"service": name, "count": len(logs), "logs": logs}), 200

        # session_id travels in the JSON body for every route below, never
        # as a URL param — start/status/otp/delete are all POST with a
        # {"session_id": ...} (plus whatever else that route needs) body.
        self.app.add_url_rule(f"{prefix}/start", f"{name}_start", start, methods=["POST"])
        self.app.add_url_rule(f"{prefix}/status", f"{name}_status", status, methods=["POST"])
        # /otp is only wired up for services that declared needs_otp=True
        # (the default). A service like epfo that sets needs_otp=False never
        # gets this route registered, so POSTing to /epfo/otp returns
        # Flask's normal 404 rather than a route that pretends to work.
        if getattr(cls, "NEEDS_OTP", True):
            self.app.add_url_rule(f"{prefix}/otp", f"{name}_otp", otp, methods=["POST"])
        self.app.add_url_rule(f"{prefix}/delete", f"{name}_delete", delete, methods=["POST"])
        self.app.add_url_rule(f"{prefix}/logs", f"{name}_logs", service_logs, methods=["GET"])

    # ------------------------------------------------------------------
    # Extra per-service routes beyond the default start/status/otp/delete/
    # logs set -- one per @action-decorated method on the service class.
    # ------------------------------------------------------------------
    def _register_action_routes(self, name: str, cls):
        prefix = f"/{name}"
        action_names = []

        # vars(cls) only -- a service's own actions are declared directly
        # on its own class body, never inherited from AutomationService.
        for attr_name, attr in vars(cls).items():
            if callable(attr) and getattr(attr, "_is_action", False):
                action_names.append(attr._action_name)
                self._register_one_action_route(prefix, name, cls, attr)

        # exposed on /services for discovery, see _register_global_routes
        cls.ACTIONS = action_names

    def _register_one_action_route(self, prefix: str, name: str, cls, func):
        action_name = func._action_name
        methods = func._action_methods
        schema = func._action_schema
        creates_session = getattr(func, "_action_creates_session", False)

        if creates_session:
            # Same shape as _register_service_routes()'s start() closure:
            # no session_id yet (this call makes one), validate the body
            # against this action's own schema, honor unique_key dedup the
            # same way /start does, then hand off to the background worker
            # and respond 202 immediately -- the caller polls
            # /<name>/status with the returned session_id, exactly like
            # after a normal /start.
            def handler():
                data = request.get_json(silent=True)
                if data is None:
                    return jsonify({"error": "Missing JSON payload"}), 400

                if schema:
                    data = self.apply_defaults(data, schema)
                    errors = self.validate(data, schema)
                    if errors:
                        return jsonify({"error": "Payload validation failed", "details": errors}), 422

                unique_key = getattr(cls, "UNIQUE_KEY", None)
                key_value = data.get(unique_key) if unique_key else None

                if unique_key:
                    existing_session_id = self._active_session_for_key(name, key_value)
                    if existing_session_id:
                        return jsonify({
                            "service": name,
                            "status": "already_running",
                            "message": f"This {key_value} is running on automation",
                            unique_key: key_value,
                            "session_id": existing_session_id,
                        }), 409

                session_id = self._create_session(name, data)
                if unique_key:
                    self._register_active_key(name, key_value, session_id)
                self._run_action_in_background(cls, func, action_name, session_id, data)

                return jsonify({"service": name, "session_id": session_id, "status": "Automation started",
                            "status_comm_type": self._comm_type(name)}), 202
        else:
            def handler():
                data = request.get_json(silent=True)
                if data is None:
                    return jsonify({"error": "Missing JSON payload"}), 400

                session_id = data.get("session_id")
                if not session_id:
                    return jsonify({"error": "'session_id' is required in the request body"}), 400

                with self._lock:
                    session = self.sessions.get(session_id)
                    session = dict(session) if session else None
                if session is None or session.get("service") != name:
                    return jsonify({"error": "Session not found"}), 404
                payload = dict(session.get("payload") or {})

                if schema:
                    data = self.apply_defaults(data, schema)
                    errors = self.validate(data, schema)
                    if errors:
                        return jsonify({"error": "Payload validation failed", "details": errors}), 422

                # Built the same way /start builds a service instance for
                # run() -- session_id/framework give it add_log, set_progress,
                # get_resource, etc.; `data` (payload) is the original /start
                # body, in case the action needs it (e.g. credentials).
                instance = cls(session_id=session_id, framework=self, data=payload)

                try:
                    result = func(instance, data)
                except Exception as e:
                    traceback.print_exc()
                    self.add_log(session_id, f"Error in '{action_name}': {e}", level="ERROR")
                    return jsonify({"error": str(e)}), 500

                if isinstance(result, tuple):
                    body, http_status = result
                    return jsonify(body), http_status
                return jsonify(result), 200

        handler.__name__ = f"{name}_{action_name}"
        self.app.add_url_rule(f"{prefix}/{action_name}", f"{name}_{action_name}", handler, methods=methods)

    # ------------------------------------------------------------------
    # Framework-wide routes: discovery + cross-service log monitor
    # ------------------------------------------------------------------
    def _register_global_routes(self):

        @self.app.route("/screenshots/<path:filename>", methods=["GET"])
        def get_screenshot(filename):
            return send_from_directory(self.screenshot_dir, filename)

        @self.app.route("/services", methods=["GET"])
        def list_services():
            return jsonify({
                name: {
                    "needs_otp": getattr(cls, "NEEDS_OTP", True),
                    "status_comm_type": getattr(cls, "STATUS_COMM_TYPE", "api"),
                    "schema": {
                        field: {"type": rules["type"].__name__, "required": rules.get("required", False)}
                        for field, rules in cls.PAYLOAD_SCHEMA.items()
                    },
                    # extra endpoints beyond the default start/status/(otp)/delete/logs,
                    # declared on the service with @action(...) -- see automation_framework.action
                    "actions": getattr(cls, "ACTIONS", []),
                }
                for name, cls in self.services.items()
            })

        @self.app.route("/logs/session", methods=["POST"])
        def session_logs():
            data = request.get_json(silent=True) or {}
            session_id = data.get("session_id")
            if not session_id:
                return jsonify({"error": "'session_id' is required in the request body"}), 400
            logs = [e for e in self._read_logs() if e.get("session_id") == session_id]
            if not logs:
                return jsonify({"error": "No logs found for this session"}), 404
            return jsonify({"session_id": session_id, "logs": logs}), 200

        @self.app.route("/logs", methods=["GET"])
        def all_logs():
            grouped = {}
            with self._lock:
                for session_id, session in self.sessions.items():
                    entry = self._session_hit_summary(session_id, session)
                    service_name = entry["service_name"]
                    bucket = grouped.setdefault(service_name, {"hits": 0, "sessions": []})
                    bucket["hits"] += entry["total_hits"]
                    bucket["sessions"].append(entry)
            return jsonify(grouped), 200

    # ------------------------------------------------------------------
    # status_comm_type  --  "api" (default, client polls /<name>/status)
    #                       or "socket" (every log is also pushed live)
    # ------------------------------------------------------------------
    def _comm_type(self, service_name: str) -> str:
        return getattr(self.services.get(service_name), "STATUS_COMM_TYPE", "api")

    def _emit_socket(self, session_id: str, event: str, payload: dict):
        if self.socketio is None:
            return
        try:
            self.socketio.emit(event, payload, to=session_id)
        except Exception:
            traceback.print_exc()

    def _finished_payload(self, session_id: str):
        with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                return None
            service_name, result, error = session["service"], session.get("result"), session.get("error")
        if result is None and error is None:
            return None
        base = {"session_id": session_id, "service": service_name, "finished": True}
        if error:
            return dict(base, status="failed", error=error)
        return dict(base, status="completed", result=result)

    def _emit_finished(self, session_id: str):
        with self._lock:
            session = self.sessions.get(session_id)
            service_name = session["service"] if session else None
        if service_name is None or self._comm_type(service_name) != "socket":
            return
        payload = self._finished_payload(session_id)
        if payload:
            self._emit_socket(session_id, "finished", payload)

    def _register_socket_events(self):
        """Socket.IO protocol for services registered with status_comm_type="Socket":

            client -> server   emit("subscribe",   {"session_id": "..."})
            server -> client   "subscribed"  {session_id, service}
                               "log"         {session_id, service, index, time, level, kind, message, screenshot?}
                               "progress"    {session_id, progress}
                               "finished"    {session_id, service, finished, status, result | error}
                               "error"       {error}
            client -> server   emit("unsubscribe", {"session_id": "..."})

        On subscribe the logs written so far are replayed first (the
        automation usually starts logging before the client connects),
        so each "log" carries an `index` the client can use to de-duplicate."""
        sio = self.socketio
        if sio is None:
            return

        @sio.on("subscribe")
        def on_subscribe(data):
            session_id = str((data or {}).get("session_id") or "") if isinstance(data, dict) else ""
            with self._lock:
                session = self.sessions.get(session_id)
                service_name = session["service"] if session else None
            if service_name is None:
                sio_emit("error", {"error": "Session not found", "session_id": session_id})
                return
            if self._comm_type(service_name) != "socket":
                sio_emit("error", {"error": f"'{service_name}' uses status_comm_type='API'; poll /{service_name}/status",
                                   "session_id": session_id})
                return
            join_room(session_id)                       # join first, then replay -> nothing is missed
            with self._lock:
                session = self.sessions.get(session_id)
                logs = list(session["logs"]) if session else []
            sio_emit("subscribed", {"session_id": session_id, "service": service_name})
            for i, entry in enumerate(logs):
                sio_emit("log", {**entry, "session_id": session_id, "service": service_name, "index": i})
            payload = self._finished_payload(session_id)
            if payload:
                sio_emit("finished", payload)

        @sio.on("unsubscribe")
        def on_unsubscribe(data):
            session_id = str((data or {}).get("session_id") or "") if isinstance(data, dict) else ""
            if session_id:
                leave_room(session_id)

    # ------------------------------------------------------------------
    # API request log: every call -> ONE document (url + payload + response)
    # ------------------------------------------------------------------
    def _register_request_logging(self):
        skip_prefixes = ("/devtools", "/screenshots", "/socket.io", "/favicon.ico")

        def skipped():
            return request.method == "OPTIONS" or request.path.startswith(skip_prefixes)

        @self.app.before_request
        def _request_log_begin():
            if skipped():
                return None
            g._rl_started = time.perf_counter()
            g._rl_ts = _utc_now_parts()
            request.get_data(cache=True)        # keep the body readable after the view ran
            return None

        @self.app.after_request
        def _request_log_end(response):
            if getattr(g, "_rl_started", None) is None:
                return response
            try:
                self.request_store.add(self._build_request_doc(response))
            except Exception:
                traceback.print_exc()           # logging must never break an API call
            return response

    def _build_request_doc(self, response) -> dict:
        parts = [p for p in request.path.split("/") if p]
        if parts and parts[0] in self.services:
            service = parts[0]
            last = parts[1] if len(parts) > 1 else ""
            request_type = last if last in _SERVICE_ROUTE_TYPES else "action"
        else:
            service, request_type = "framework", "other"

        body = request.get_json(silent=True)
        payload = body if body is not None else (request.args.to_dict() or None)

        if response.direct_passthrough:
            resp_value = f"<streamed response: {response.mimetype}>"
        elif response.is_json:
            resp_value = response.get_json(silent=True)
        else:
            resp_value = response.get_data(as_text=True)[:_MAX_STORED_CHARS]

        session_id = ""
        for source in (payload, resp_value):
            if isinstance(source, dict) and source.get("session_id"):
                session_id = str(source["session_id"])
                break

        if self.mask_sensitive:
            payload = _mask_sensitive(payload, self.mask_keys)
            resp_value = _mask_sensitive(resp_value, self.mask_keys)

        response_text = _to_json_text(resp_value)
        ts_iso, ts_ms = g._rl_ts
        return {
            "request_id": uuid.uuid4().hex,
            "timestamp": ts_iso,
            "ts_ms": ts_ms,
            "session_id": session_id,
            "service": service,
            "request_type": request_type,
            "method": request.method,
            "path": request.path,
            "url": request.url,
            "status_code": response.status_code,
            "duration_ms": round((time.perf_counter() - g._rl_started) * 1000, 2),
            "client_ip": (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip(),
            "size": len(response_text),
            "payload": _to_json_text(payload),
            "response": response_text,
        }

    # ------------------------------------------------------------------
    # DevTools network view:  GET /devtools   +   /devtools/api/...
    # ------------------------------------------------------------------
    def _devtools_denied(self):
        if not self.devtools_token:
            return None
        supplied = request.headers.get("X-Devtools-Token") or request.args.get("token") or ""
        if hmac.compare_digest(supplied.encode("utf-8"), self.devtools_token.encode("utf-8")):
            return None
        return jsonify({"error": "Unauthorized"}), 401

    def _register_devtools_routes(self):
        store = self.request_store

        def filters_from(src) -> dict:
            status_min, status_max = _parse_status_filter(src.get("status_code"))
            return {
                "status_min": status_min,
                "status_max": status_max,
                "session_id": str(src.get("session_id") or "").strip(),
                "service": str(src.get("service") or "").strip(),
                "request_type": str(src.get("request_type") or "").strip().lower(),
                "method": str(src.get("method") or "").strip().upper(),
                "date_from": _to_ms(src.get("date_from")),
                "date_to": _to_ms(src.get("date_to"), end_of_day=True),
            }

        @self.app.route("/devtools", methods=["GET"])
        def devtools_page():
            denied = self._devtools_denied()
            if denied:
                return denied
            return Response(_DEVTOOLS_HTML, mimetype="text/html")

        @self.app.route("/devtools/api/meta", methods=["GET"])
        def devtools_meta():
            denied = self._devtools_denied()
            if denied:
                return denied
            return jsonify({
                "services": sorted(self.services) + ["framework"],
                "request_types": list(_REQUEST_TYPES),
                "methods": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                "storage": store.status(),
            })

        @self.app.route("/devtools/api/requests", methods=["GET"])
        def devtools_list():
            denied = self._devtools_denied()
            if denied:
                return denied
            limit = min(max(request.args.get("limit", 100, type=int), 1), 500)
            offset = max(request.args.get("offset", 0, type=int), 0)
            items, total = store.search(filters_from(request.args), limit, offset)
            return jsonify({"total": total, "limit": limit, "offset": offset, "items": items,
                            "storage": store.status()})

        @self.app.route("/devtools/api/requests/<request_id>", methods=["GET"])
        def devtools_get(request_id):
            denied = self._devtools_denied()
            if denied:
                return denied
            doc = store.get(request_id)
            if not doc:
                return jsonify({"error": "Request not found"}), 404
            doc["payload"] = _parse_json_text(doc.get("payload"))
            doc["response"] = _parse_json_text(doc.get("response"))
            return jsonify(doc)

        @self.app.route("/devtools/api/requests/<request_id>", methods=["DELETE"])
        def devtools_delete_one(request_id):
            denied = self._devtools_denied()
            if denied:
                return denied
            deleted = store.delete_ids([request_id])
            if not deleted:
                return jsonify({"error": "Request not found"}), 404
            return jsonify({"deleted": deleted})

        @self.app.route("/devtools/api/requests/delete", methods=["POST"])
        def devtools_delete_many():
            """{"ids": ["..", ".."]}   or   {"all_filtered": true, "filters": {session_id, service,
            request_type, method, date_from, date_to}}"""
            denied = self._devtools_denied()
            if denied:
                return denied
            data = request.get_json(silent=True) or {}
            if data.get("all_filtered"):
                deleted = store.delete_filtered(filters_from(data.get("filters") or {}))
            elif isinstance(data.get("ids"), list) and data["ids"]:
                deleted = store.delete_ids([str(i) for i in data["ids"]])
            else:
                return jsonify({"error": "Send 'ids' (a non-empty list) or 'all_filtered': true"}), 400
            return jsonify({"deleted": deleted})

    def run(self, **kwargs):
        kwargs.setdefault("host", "0.0.0.0")
        kwargs.setdefault("port", self.port)
        kwargs.setdefault("debug", True)
        if self.socketio is not None:
            # Flask-SocketIO's runner already starts the dev server threaded.
            kwargs.pop("threaded", None)
            kwargs.setdefault("allow_unsafe_werkzeug", True)
            self.socketio.run(self.app, **kwargs)
        else:
            kwargs.setdefault("threaded", True)
            self.app.run(**kwargs)


class OTPTimeoutError(Exception):
    """Raised by wait_for_otp() when no valid OTP arrives within the given
    timeout. Callers should catch this specifically (not a bare Exception)
    to distinguish "OTP never arrived in time" from other automation
    failures, so they know it's safe/expected to retry the flow."""
    pass


# ======================================================================
# Base class every service extends — only run() is required
# ======================================================================
class AutomationService:

    def __init__(self, session_id: str, framework: AutomationFramework, data: dict):
        self.session_id = session_id
        self.framework = framework
        self.data = data

    def add_log(self, message: str, level: str = "INFO", screenshot=None):
        self.framework.add_log(self.session_id, message, level=level, screenshot=screenshot)

    def capture_screenshot(self, driver, message: str = "Error screenshot captured", level: str = "ERROR"):
        """Convenience for services using Selenium: grab the current page as
        a screenshot and attach it to a log entry in one call, e.g. from an
        except block:
            except Exception as e:
                self.capture_screenshot(driver, f"Failed at step X: {e}")
                raise
        The screenshot will then be returned by /<service>/status when
        called with {"view": "logs", "session_id": self.session_id}."""
        try:
            b64 = driver.get_screenshot_as_base64()
        except Exception:
            self.add_log(f"{message} (screenshot capture failed)", level=level)
            return
        self.add_log(message, level=level, screenshot=b64)

    def set_progress(self, progress: int):
        self.framework.set_progress(self.session_id, progress)

    def set_error(self, error: str):
        self.framework.set_error(self.session_id, error)

    def set_result(self, result):
        self.framework.set_result(self.session_id, result)

    def get_result(self):
        """Read back this session's current result (or None) without
        marking it 'finished'. Useful in an @action that wants to append
        to the existing result instead of overwriting it."""
        return self.framework.get_result(self.session_id)

    def set_resource(self, obj):
        """Keep a live Python object (e.g. a logged-in automation client)
        alive in memory for this session_id, so a later @action call for
        the same session can reuse it via get_resource() instead of
        rebuilding/re-logging-in from scratch."""
        self.framework.set_resource(self.session_id, obj)

    def get_resource(self):
        """Fetch whatever set_resource() stored for this session_id, or
        None if nothing has been stored (e.g. /start was never called, or
        the session was deleted)."""
        return self.framework.get_resource(self.session_id)

    def clear_resource(self):
        self.framework.clear_resource(self.session_id)

    def wait_for_otp(self, otp_type: str = "otp", timeout: float = 180.0, poll_interval: float = 2.0,
                      pattern=None, driver=None, consume: bool = True):
        """Session-based OTP wait, shared by every service that needs one.

        - otp_type: which OTP slot to wait on ("otp", "login", "mobile", "email", ...)
          — matches the otp_type a caller POSTs to /<service>/otp.
        - timeout / poll_interval: how long to wait, and how often to check.
        - pattern: compiled regex the OTP value must match; defaults to the
          same pattern used to validate that otp_type on /<service>/otp. A
          value that fails the pattern is discarded and waiting continues —
          a bad value is never returned, and never counts as a timeout by
          itself.
        - driver: optional Selenium driver. If given, it is quit() the
          moment this call times out, so a stuck browser never lingers.
        - consume: clear the OTP slot once a valid value is read, so a
          retry by the caller never resubmits a stale OTP.

        Returns the validated OTP string. Raises OTPTimeoutError if nothing
        valid arrives within `timeout` seconds.
        """
        import time
        pattern = pattern or _OTP_TYPE_PATTERNS.get(otp_type, _OTP_TYPE_PATTERNS["otp"])[0]
        self.add_log(f"Waiting for OTP (type={otp_type}, timeout={timeout}s)")
        waited = 0.0
        while waited < timeout:
            if self.framework.otp_received(self.session_id, otp_type=otp_type):
                otp_value = str(self.framework.get_otp(self.session_id, otp_type=otp_type) or "").strip()
                if otp_value and pattern.match(otp_value):
                    if consume:
                        self.framework.clear_otp(self.session_id, otp_type=otp_type)
                    return otp_value
                # Wrong shape (or empty) — discard and keep waiting for a fresh one.
                self.framework.clear_otp(self.session_id, otp_type=otp_type)
                self.add_log(f"Invalid OTP received for '{otp_type}', discarding and continuing to wait")
            time.sleep(poll_interval)
            waited += poll_interval

        self.add_log(f"OTP ('{otp_type}') not received within {timeout}s timeout")
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        raise OTPTimeoutError(f"OTP ('{otp_type}') not received within {timeout} seconds")

    def wait_for_multi_otp(self, otp_types, timeout: float = 180.0, poll_interval: float = 2.0,
                            pattern=None, driver=None, consume: bool = True):
        """Same as wait_for_otp(), but waits for several OTP slots to all
        become valid together (e.g. a step needing a Mobile OTP and an
        Email OTP submitted together before it can continue).

        Returns {otp_type: otp_value} once every slot in `otp_types` is
        valid. Raises OTPTimeoutError (and quits `driver`, if given) if the
        full set isn't ready within `timeout` seconds.
        """
        import time
        self.add_log(f"Waiting for OTP ({', '.join(otp_types)}, timeout={timeout}s)")
        waited = 0.0
        values = {t: "" for t in otp_types}
        while waited < timeout:
            for t in otp_types:
                if values[t]:
                    continue  # already validated this one on an earlier pass
                if not self.framework.otp_received(self.session_id, otp_type=t):
                    continue  # nothing submitted yet for this slot
                otp_value = str(self.framework.get_otp(self.session_id, otp_type=t) or "").strip()
                t_pattern = pattern or _OTP_TYPE_PATTERNS.get(t, _OTP_TYPE_PATTERNS["otp"])[0]
                if otp_value and t_pattern.match(otp_value):
                    values[t] = otp_value
                else:
                    self.framework.clear_otp(self.session_id, otp_type=t)
                    self.add_log(f"Invalid OTP received for '{t}', discarding and continuing to wait")
            if all(values.values()):
                if consume:
                    for t in otp_types:
                        self.framework.clear_otp(self.session_id, otp_type=t)
                return values
            time.sleep(poll_interval)
            waited += poll_interval

        self.add_log(f"OTP(s) ({', '.join(otp_types)}) not received within {timeout}s timeout")
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        raise OTPTimeoutError(f"OTP(s) {otp_types} not received within {timeout} seconds")

    def run(self, data):
        raise NotImplementedError("Each service must implement run(self, data)")


# -------- per-OTP-type validation, keyed by the "otp_type" field on /<service>/otp --------
_SIX_DIGIT_OTP_RE = re.compile(r"^\d{6}$")
_OTP_TYPE_PATTERNS = {
    "otp":    (_SIX_DIGIT_OTP_RE, "exactly 6 digits"),   # default/legacy single-OTP slot
    "login":  (_SIX_DIGIT_OTP_RE, "exactly 6 digits"),   # startup_india OTP #1 — login
    "mobile": (_SIX_DIGIT_OTP_RE, "exactly 6 digits"),   # startup_india OTP #2 — mobile verification
    "email":  (_SIX_DIGIT_OTP_RE, "exactly 6 digits"),   # startup_india OTP #3 — email verification
}