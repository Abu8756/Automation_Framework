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

from flask import Flask, request, jsonify
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


class AutomationFramework:

    def __init__(self, name: str = __name__, log_path: str = "logs/sessions.log", port: int = 3333):
        self.app = Flask(name)
        self.port = port
        CORS(
                self.app,
                origins="https://indiafilings-tau.vercel.app"
            )

        self.sessions = {}          # session_id -> session dict
        self._lock = threading.Lock()

        self.services = {}          # service_name -> service class
        self.log_path = log_path

        directory = os.path.dirname(log_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        self._register_global_routes()

    # ------------------------------------------------------------------
    # Service registration — the ONE decorator every service uses
    # ------------------------------------------------------------------
    def service(self, name: str, schema: dict = None, needs_otp: bool = True):
        schema = schema or {}

        def decorator(cls):
            cls.SERVICE_NAME = name
            cls.PAYLOAD_SCHEMA = schema
            # Some portals (e.g. EPFO) never need an OTP step. needs_otp=False
            # skips registering /<name>/otp entirely, so hitting it returns a
            # plain Flask 404 instead of a fake "not needed" response.
            cls.NEEDS_OTP = needs_otp
            self.services[name] = cls
            self._register_service_routes(name, cls)
            return cls

        return decorator

    def _write_log_file(self, service: str, session_id: str, level: str, message: str, kind: str):
        entry = {
            "time": _now(),
            "service": service,
            "session_id": session_id,
            "level": level,
            "kind": kind,
            "message": message,
        }
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

    def add_log(self, session_id: str, message: str, level: str = "INFO", kind: str = None):

        if kind is None:
            kind = "otp" if "otp" in message.lower() else "status"

        with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                return
            session["logs"].append({"time": _now(), "message": message, "level": level, "kind": kind})
            session["status"] = message
            session["updated_at"] = _now()
            if kind == "otp":
                session["otp_hits"] = session.get("otp_hits", 0) + 1
            else:
                session["status_hits"] = session.get("status_hits", 0) + 1
            service_name = session["service"]
        self._write_log_file(service_name, session_id, level, message, kind)

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

    def _run_in_background(self, service_cls, session_id: str, data: dict):
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
                self.add_log(session_id, f"Error: {str(e)}", level="ERROR")

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

            session_id = self._create_session(name, data)
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
            # whole session (logs/otp/payload/etc).
            return jsonify(session.get("result")), 200

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
            if existed:
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
    # Framework-wide routes: discovery + cross-service log monitor
    # ------------------------------------------------------------------
    def _register_global_routes(self):

        @self.app.route("/services", methods=["GET"])
        def list_services():
            return jsonify({
                name: {
                    "needs_otp": getattr(cls, "NEEDS_OTP", True),
                    "schema": {
                        field: {"type": rules["type"].__name__, "required": rules.get("required", False)}
                        for field, rules in cls.PAYLOAD_SCHEMA.items()
                    }
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

    def add_log(self, message: str, level: str = "INFO"):
        self.framework.add_log(self.session_id, message, level=level)

    def set_progress(self, progress: int):
        self.framework.set_progress(self.session_id, progress)

    def set_error(self, error: str):
        self.framework.set_error(self.session_id, error)

    def set_result(self, result):
        self.framework.set_result(self.session_id, result)

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
