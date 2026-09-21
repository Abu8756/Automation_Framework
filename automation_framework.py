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

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

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


class AutomationFramework:

    def __init__(self, name: str = __name__, log_path: str = "logs/sessions.log", port: int = 3333,
                 screenshot_dir: str = "logs/screenshots"):
        self.app = Flask(name)
        self.port = port
        CORS(
                self.app,
                origins="https://indiafilings-tau.vercel.app"
            )

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

        self._register_global_routes()

    # ------------------------------------------------------------------
    # Service registration — the ONE decorator every service uses
    # ------------------------------------------------------------------
    def service(self, name: str, schema: dict = None, needs_otp: bool = True, unique_key: str = None):
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
            session["status"] = message
            session["updated_at"] = _now()
            if kind == "otp":
                session["otp_hits"] = session.get("otp_hits", 0) + 1
            else:
                session["status_hits"] = session.get("status_hits", 0) + 1
            service_name = session["service"]
        self._write_log_file(service_name, session_id, level, message, kind, screenshot_filename=screenshot_filename)

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
        with self._lock:
            if session_id in self.sessions:
                self.sessions[session_id]["progress"] = max(0, min(100, progress))
                self.sessions[session_id]["updated_at"] = _now()

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

            return jsonify({"service": name, "session_id": session_id, "status": "Automation started"}), 202

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

                return jsonify({"service": name, "session_id": session_id, "status": "Automation started"}), 202
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

    def run(self, **kwargs):
        kwargs.setdefault("host", "0.0.0.0")
        kwargs.setdefault("port", self.port)
        kwargs.setdefault("debug", True)
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