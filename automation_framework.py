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

            with self._lock:
                session = self.sessions.get(session_id)
                if session is not None and session.get("service") == name:
                    # every status poll counts as a "hit" for this session
                    session["hits"] = session.get("hits", 0) + 1
                session = dict(session) if session else None
            if session is None or session.get("service") != name:
                return jsonify({"error": "Session not found"}), 404

            # A job is "finished" once it has a result or an error set by
            # _run_in_background()'s worker. Until then, surface a plain
            # waiting message instead of just raw progress/status fields —
            # once it IS finished, the result (or error) is returned as-is.
            finished = session.get("result") is not None or session.get("error") is not None
            if not finished:
                session["message"] = "Service in processing, please check again in a few minutes."

            return jsonify(session), 200

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

framework = AutomationFramework(__name__, log_path="logs/sessions.log", port=3333)

_ALPHA_ONLY_RE      = re.compile(r"^[A-Za-z\s]+$")            # letters and spaces only (names)
_SINGLE_DIGIT_RE    = re.compile(r"^\d$")                     # exactly one numeric digit
_NUMERIC_ONLY_RE    = re.compile(r"^\d+$")                    # digits only, any length
_MOBILE_RE          = re.compile(r"^\d{10}$")                 # exactly 10 digits
_AADHAAR_RE         = re.compile(r"^\d{12}$")                 # exactly 12 digits
_PIN_6_RE           = re.compile(r"^\d{6}$")                  # exactly 6 digits (generic PIN code)
_DATE_DDMMYYYY_RE   = re.compile(r"^\d{2}/\d{2}/\d{4}$")      # DD/MM/YYYY
_PAN_RE             = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")  # e.g. ABCDE1234F
_EMAIL_RE           = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")  # local@domain.tld
_OPTIONAL_MOBILE_RE = re.compile(r"^(\d{10})?$")              # blank OR exactly 10 digits

# -------- EPFO-specific --------
_EPFO_USERNAME_RE   = re.compile(r"^[A-Z]{5}[0-9]{10}$")      # 5 uppercase letters + 10 digits
_UAN_RE             = re.compile(r"^\d{12}$")                 # UAN — 12 digits (same shape as Aadhaar, kept separate for its own message)
_SINGLE_ALPHA_RE    = re.compile(r"^[A-Za-z]$")                # exactly one alphabetic character (marital status code)

# -------- udyam_certificate-specific --------
_UDYAM_RE           = re.compile(r"^UDYAM-[A-Z]{2}-\d{2}-\d{7}$")  # e.g. UDYAM-KR-06-0043268

# -------- per-OTP-type validation, keyed by the "otp_type" field on /<service>/otp --------
_SIX_DIGIT_OTP_RE = re.compile(r"^\d{6}$")
_OTP_TYPE_PATTERNS = {
    "otp":    (_SIX_DIGIT_OTP_RE, "exactly 6 digits"),   # default/legacy single-OTP slot
    "login":  (_SIX_DIGIT_OTP_RE, "exactly 6 digits"),   # startup_india OTP #1 — login
    "mobile": (_SIX_DIGIT_OTP_RE, "exactly 6 digits"),   # startup_india OTP #2 — mobile verification
    "email":  (_SIX_DIGIT_OTP_RE, "exactly 6 digits"),   # startup_india OTP #3 — email verification
}

_ADDRESS_SCHEMA = {
    "flat":     {"type": str, "required": True},
    "building": {"type": str, "required": False},
    "village":  {"type": str, "required": False},
    "block":    {"type": str, "required": False},
    "road":     {"type": str, "required": False},
    "city":     {"type": str, "required": True},
    "pin": {
        "type": str, "required": True,
        "pattern": _PIN_6_RE,
        "pattern_message": "'pin' must be exactly 6 digits",
    },
    "state": {
        "type": str, "required": True,
        "pattern": _NUMERIC_ONLY_RE,
        "pattern_message": "'state' must be numeric only",
    },
    "district": {
        "type": str, "required": True,
        "pattern": _NUMERIC_ONLY_RE,
        "pattern_message": "'district' must be numeric only",
    },
}

# -------- allowed values for udyamreg dropdown/radio fields --------
ORG_TYPE_CHOICES = [str(n) for n in range(1, 12)]              # 1..11
SOCIAL_CATEGORY_CHOICES = ["1", "2", "3", "4"]
GENDER_CHOICES = ["1", "2", "3"]
DIVYANG_CHOICES = ["0", "1"]
MAJOR_ACTIVITY_CHOICES = ["1", "2"]
MAJOR_ACTIVITY_UNDER_SERVICES_CHOICES = ["1", "2"]
PREVIOUS_EM_CHOICES = ["0", "2", "4"]
NIC_ACTIVITY_CHOICES = ["1", "2", "3"]
COMMENCED_CHOICES = ["0", "1"]
GST_CHOICES = ["0", "1", "2"]
CATEGORY_CHOICES = ["0", "1", "2", "3"]

@framework.service("udyamreg", schema={
    # -------- Aadhaar Verification --------
    "aadhaar_name": {
        "type": str, "required": True,
        "pattern": _ALPHA_ONLY_RE,
        "pattern_message": "'aadhaar_name' must contain alphabetic characters only",
    },
    "aadhaar_number": {
        "type": str, "required": True,
        "pattern": _AADHAAR_RE,
        "pattern_message": "'aadhaar_number' must be exactly 12 digits",
    },

    # -------- PAN Verification --------
    "org_type": {
        "type": str, "required": True,
        "pattern": _SINGLE_DIGIT_RE,
        "pattern_message": "'org_type' must be a single numeric digit",
        "choices": ORG_TYPE_CHOICES,
        "choices_message": f"'org_type' must be one of: {', '.join(ORG_TYPE_CHOICES)}",
    },
    "pan_number": {
        "type": str, "required": True,
        "pattern": _PAN_RE,
        "pattern_message": "'pan_number' must be a valid PAN (e.g. ABCDE1234F)",
    },
    "pan_name": {
        "type": str, "required": True,
        "pattern": _ALPHA_ONLY_RE,
        "pattern_message": "'pan_name' must contain alphabetic characters only",
    },
    "dob": {
        "type": str, "required": True,
        "pattern": _DATE_DDMMYYYY_RE,
        "pattern_message": "'dob' must be in DD/MM/YYYY format",
    },

    # -------- Investment / Turnover --------
    "wdv": {
        "type": str, "required": True,
        "pattern": _NUMERIC_ONLY_RE,
        "pattern_message": "'wdv' must be numeric only",
    },
    "exclusion_cost": {
        "type": str, "required": True,
        "pattern": _NUMERIC_ONLY_RE,
        "pattern_message": "'exclusion_cost' must be numeric only",
    },
    "total_turnover": {
        "type": str, "required": True,
        "pattern": _NUMERIC_ONLY_RE,
        "pattern_message": "'total_turnover' must be numeric only",
    },

    # -------- Basic Details --------
    "company_name": {"type": str, "required": True},
    "mobile": {
        "type": str, "required": True,
        "pattern": _MOBILE_RE,
        "pattern_message": "'mobile' must be exactly 10 digits",
    },
    "email": {
        "type": str, "required": True,
        "pattern": _EMAIL_RE,
        "pattern_message": "'email' must be a valid email address (e.g. name@domain.com)",
    },
    "social_category": {
        "type": str, "required": True,
        "pattern": _SINGLE_DIGIT_RE,
        "pattern_message": "'social_category' must be a single numeric digit",
        "choices": SOCIAL_CATEGORY_CHOICES,   # 1=General 2=SC 3=ST 4=OBC
        "choices_message": f"'social_category' must be one of: {', '.join(SOCIAL_CATEGORY_CHOICES)}",
    },
    "gender": {
        "type": str, "required": True,
        "pattern": _SINGLE_DIGIT_RE,
        "pattern_message": "'gender' must be a single numeric digit",
        "choices": GENDER_CHOICES,
        "choices_message": f"'gender' must be one of: {', '.join(GENDER_CHOICES)}",
    },
    "divyang": {
        "type": str, "required": True,
        "pattern": _SINGLE_DIGIT_RE,
        "pattern_message": "'divyang' must be a single numeric digit",
        "choices": DIVYANG_CHOICES,   # 0=No 1=Yes
        "choices_message": f"'divyang' must be one of: {', '.join(DIVYANG_CHOICES)}",
    },

    # -------- Addresses (nested — validated field by field) --------
    "official_address": {"type": dict, "required": True, "schema": _ADDRESS_SCHEMA},
    # units is a dict of UNKNOWN keys (unit names), each shaped like an address
    "units": {"type": dict, "required": True, "each": _ADDRESS_SCHEMA},

    # -------- Status of Enterprise --------
    "previous_em": {
        "type": str, "required": True,
        "pattern": _SINGLE_DIGIT_RE,
        "pattern_message": "'previous_em' must be a single numeric digit",
        "choices": PREVIOUS_EM_CHOICES,   # 0=N/A 2=EM-II 4=Previous UAM
        "choices_message": f"'previous_em' must be one of: {', '.join(PREVIOUS_EM_CHOICES)}",
    },
    "incorporation_date": {
        "type": str, "required": True,
        "pattern": _DATE_DDMMYYYY_RE,
        "pattern_message": "'incorporation_date' must be in DD/MM/YYYY format",
    },
    "commenced": {
        "type": str, "required": True,
        "pattern": _SINGLE_DIGIT_RE,
        "pattern_message": "'commenced' must be a single numeric digit",
        "choices": COMMENCED_CHOICES,   # 1=Yes 0=No
        "choices_message": f"'commenced' must be one of: {', '.join(COMMENCED_CHOICES)}",
    },
    # only required when commenced == "1"
    "commencement_date": {
        "type": str, "required_if": {"field": "commenced", "equals": "1"},
        "pattern": _DATE_DDMMYYYY_RE,
        "pattern_message": "'commencement_date' must be in DD/MM/YYYY format",
    },

    # -------- Bank Details --------
    "bank_name":      {"type": str, "required": True},
    "ifsc":           {"type": str, "required": True},
    "account_number": {"type": str, "required": True},

    # -------- Major Activity --------
    "major_activity": {
        "type": str, "required": True,
        "pattern": _SINGLE_DIGIT_RE,
        "pattern_message": "'major_activity' must be a single numeric digit",
        "choices": MAJOR_ACTIVITY_CHOICES,   # 1=Manufacturing 2=Services
        "choices_message": f"'major_activity' must be one of: {', '.join(MAJOR_ACTIVITY_CHOICES)}",
    },
    "major_activity_under_services": {
        "type": str, "required_if": {"field": "major_activity", "equals": "2"},
        "pattern": _SINGLE_DIGIT_RE,
        "pattern_message": "'major_activity_under_services' must be a single numeric digit",
        "choices": MAJOR_ACTIVITY_UNDER_SERVICES_CHOICES,
        "choices_message": f"'major_activity_under_services' must be one of: {', '.join(MAJOR_ACTIVITY_UNDER_SERVICES_CHOICES)}",
    },
    "nic_activity": {
        "type": str, "required": True,
        "pattern": _NUMERIC_ONLY_RE,
        "pattern_message": "'nic_activity' must be numeric only",
        "choices": NIC_ACTIVITY_CHOICES,
        "choices_message": f"'nic_activity' must be one of: {', '.join(NIC_ACTIVITY_CHOICES)}",
    },
    "nic": {"type": dict, "required": True, "schema": {
        "nic2": {
            "type": str, "required": True,
            "pattern": _NUMERIC_ONLY_RE,
            "pattern_message": "'nic2' must be numeric only",
        },
        "nic4": {
            "type": str, "required": True,
            "pattern": _NUMERIC_ONLY_RE,
            "pattern_message": "'nic4' must be numeric only",
        },
        "nic5": {
            "type": str, "required": True,
            "pattern": _NUMERIC_ONLY_RE,
            "pattern_message": "'nic5' must be numeric only",
        },
    }},

    # -------- Employees --------
    "employees": {"type": dict, "required": True, "schema": {
        "male": {
            "type": str, "required": True,
            "pattern": _NUMERIC_ONLY_RE,
            "pattern_message": "'male' must be numeric only",
        },
        "female": {
            "type": str, "required": True,
            "pattern": _NUMERIC_ONLY_RE,
            "pattern_message": "'female' must be numeric only",
        },
        "others": {
            "type": str, "required": True,
            "pattern": _NUMERIC_ONLY_RE,
            "pattern_message": "'others' must be numeric only",
        },
    }},
})
class UdyamRegService(AutomationService):
    def run(self, data):
        from udyam_reg_v2 import UdyamRegistration

        self.add_log("Launching Udyam automation")
        # Pass this AutomationService instance itself (not a raw session_id/
        # sessions dict) — UdyamRegistration has no session, logging, or OTP
        # storage of its own; it calls back into this service for all of
        # that, so the framework is the single source of truth (same pattern
        # as StartupIndiaService above).
        obj = UdyamRegistration(data=data, service=self)
        return obj.run()

_DIR_EMP_ROW_SCHEMA = {
    "directors": {"type": int, "required": True},
    "employees": {"type": int, "required": True},
}

# -------- Chennai-specific regex (city-scoped pincode; shared patterns live near the top of the file) --------
_PINCODE_RE = re.compile(r"^600\d{3}$")                 # must start with 600, 6 digits total
_PROFESSIONAL_TAX_NUMBER = re.compile(r"^\d{2}-\d{3}-\d{5}-\d{3}$")
CATEGORY_OPTIONS = {
    "1": {
        "option_name": "Central Government",
        "sub": [
            {"option_name": "BANKS", "value": "72"},
            {"option_name": "CENTRAL GOVERNMENT EMPLOYEES", "value": "2"},
            {"option_name": "CENTRAL PUBLIC SECTOR EMPLOYEES", "value": "4"},
        ],
    },
    "2": {
        "option_name": "Private Establishment",
        "sub": [
            {"option_name": "TRADERS", "value": "6"},
            {"option_name": "PUBLIC LIMITED COMPANIES", "value": "7"},
            {"option_name": "PRIVATE LIMITED COMPANIES", "value": "8"},
            {"option_name": "SOCIETIES", "value": "9"},
            {"option_name": "TRUSTS", "value": "10"},
        ],
    },
    "3": {
        "option_name": "Individual",
        "sub": [
            {"option_name": "CONSULTANTS", "value": "55"},
            {"option_name": "ENGINEERS", "value": "54"},
            {"option_name": "ADVOCATES", "value": "52"},
            {"option_name": "DOCTORS", "value": "53"},
            {"option_name": "PROFESSIONALS", "value": "5"},
            {"option_name": "OTHERS", "value": "11"},
        ],
    },
    "5": {
        "option_name": "State Government",
        "sub": [
            {"option_name": "TAMILNADU GOVERNMENT EMPLOYEES", "value": "1"},
            {"option_name": "TAMILNADU PUBLIC SECTOR EMPLOYEE", "value": "3"},
        ],
    },
}

CATEGORY_TYPE_CHOICES = json_top_level_keys(CATEGORY_OPTIONS)                  # ["1", "2", "3", "5"]
CATEGORY_SUB_TYPE_CHOICES = json_extract_values(CATEGORY_OPTIONS, "value")     # flat list, all sub-values
CATEGORY_SUB_TYPE_MAP = json_build_choice_map(CATEGORY_OPTIONS)                # {"1": ["72","2","4"], "2": [...], ...}

@framework.service("chennaipt", schema={
    "username": {"type": str, "required": True, "default": "ABDUR RAHIM"},
    "password": {"type": str, "required": True, "default": "ABDURAHIM@123"},
    "comp_name": {"type": str, "required": True},
    "category_type": {
        "type": str, "required": True,
        "pattern": _SINGLE_DIGIT_RE,
        "pattern_message": "'category_type' must be a single numeric digit",
        "choices": CATEGORY_TYPE_CHOICES,
        "choices_message": f"'category_type' must be one of: {', '.join(CATEGORY_TYPE_CHOICES)}",
    },
    "category_sub_type": {
        "type": str, "required": True,
        "pattern": _NUMERIC_ONLY_RE,
        "pattern_message": "'category_sub_type' must be numeric only",
        "depends_on": "category_type",
        "choices_map": CATEGORY_SUB_TYPE_MAP,
        "depends_on_message": "'category_sub_type' cannot be validated: 'category_type' is missing or invalid",
        "choices_message": "'category_sub_type' is not a valid sub-category for the given 'category_type'",
    },

    "property_tax_number": {
        "type": str,
        "required_if_empty": {"field": "zone"},
        "pattern":_PROFESSIONAL_TAX_NUMBER,
    },
    "authorized_person": {"type": str, "required": True},
    "authorized_mobile_number": {
        "type": str, "required": True,
        "pattern": _MOBILE_RE,
        "pattern_message": "'authorized_mobile_number' must be exactly 10 digits",
    },

    "details_dir_emp": {"type": dict, "required": True, "schema": {
        "rows": {"type": list, "required": True, "items": _DIR_EMP_ROW_SCHEMA},
    }},

    "date_of_commencement": {
        "type": str, "required": True,
        "pattern": _DATE_DDMMYYYY_RE,
        "pattern_message": "'Date_of_commencement' must be in DD/MM/YYYY format",
    },
    "half_yearly_gross_income": {
        "type": str, "required": False,   # defaults to "0" in fill_financial_details
        "pattern": _NUMERIC_ONLY_RE,
        "pattern_message": "'half_yearly_gross_income' must be numeric only",
    },
    "remarks": {"type": str, "required": False},                 # defaults to "New Registration" if blank/missing

    # -------- only required when property_tax_number == "0" (manual address) --------
    "building_name": {"type": str, "required_if": {"field": "property_tax_number", "equals": "0"}, "allow_blank": True},
    "door_no":    {"type": str, "required_if": {"field": "property_tax_number", "equals": "0"}},
    "zone":       {"type": str, "required_if": {"field": "property_tax_number", "equals": "0"}},
    "ward_or_division": {"type": str, "required_if": {"field": "property_tax_number", "equals": "0"}},
    "area":       {"type": str, "required_if": {"field": "property_tax_number", "equals": "0"}},
    "locality":   {"type": str, "required_if": {"field": "property_tax_number", "equals": "0"}},
    "street":     {"type": str, "required_if": {"field": "property_tax_number", "equals": "0"}},
    "pincode": {
        "type": str, "required_if": {"field": "property_tax_number", "equals": "0"},
        "pattern": _PINCODE_RE,
        "pattern_message": "'pincode' must start with 600 and be exactly 6 digits",
    },
})
class ChennaiProfessionalTaxService(AutomationService):
    def run(self, data):
        from Chennai_Professional_Tax import Chennai_Professional_Tax as CPT

        if not data.get("remarks"):
            data["remarks"] = "New Registration"
            self.add_log("No remarks supplied — defaulting to 'New Registration'")

        self.add_log("Launching Chennai Professional Tax filing")

        obj = CPT(data=data, session=self.framework.sessions.get(self.session_id))
        obj.logger = self
        return obj.run()

INDUSTRY_SECTOR_OPTIONS = {
    "Analytics": ["Big Data", "Business Intelligence", "Data Science", "Others"],
    "Advertising": ["Adtech", "Online Classified", "Others"],
    "Architecture Interior Design": ["Others"],
    "AR VR (Augmented + Virtual Reality)": ["Others"],
    "Automotive": ["Auto & Truck Manufacturers", "Auto Truck & Motor Cycle Parts", "Electric Vehicles", "Tires And Rubber Products", "Others"],
    "Art & Photography": ["Art", "Handicraft", "Photography", "Others"],
    "Animation": ["Others"],
    "Chemicals": ["Agricultural Chemicals", "Commodity Chemicals", "Diversified Chemicals", "Specialty Chemicals", "Others"],
    "Computer Vision": ["Others"],
    "Telecommunication & Networking": ["Integrated Communication services", "Network Technology Solutions", "Wireless", "Others"],
    "Construction": ["Construction & Engineering", "Construction Materials", "Construction Suppliers & Fixtures", "Homebuilding", "New – Age Construction Technologies", "Others"],
    "Agriculture": ["Agri – Tech", "Animal Husbandry", "Dairy Farming", "Fisheries", "Food Processing", "Horticulture", "Organic Agriculture", "Others"],
    "Aeronautics Aerospace & defense": ["Aviation & Others", "Defence Equipment", "Drones", "Space Technology", "Others"],
    "AI": ["Machine Learning", "NLP", "Others"],
    "Green Technology": ["Clean Tech", "Waste Management", "Others"],
    "Events": ["Events Management", "Weddings", "Others"],
    "Fashion": ["Apparel", "Fan Merchandise", "Fashion Technology", "Jewellery", "Lifestyle", "Others"],
    "Finance Technology": ["Accounting", "Billing and Invoicing", "Bitcoin and Block chain", "Business Finance", "Crowd funding", "Foreign Exchange", "Insurance", "Micro Finance", "Mobile Wallets Payments", "P2P Lending", "Payment Platforms", "Personal Finance", "Point Of Sales", "Trading", "Others"],
    "Enterprise Software": ["CXM", "Cloud", "Collaboration", "Customer Support", "ERP", "Enterprise Mobility", "Location Based", "SCM", "Others"],
    "Food And Beverage": ["Food Processing", "Food Technology / Food Delivery", "Microbrewery", "Restaurants", "Others"],
    "Design": ["Industrial Design", "Web Design", "Others"],
    "Dating Matrimonial": ["Others"],
    "Education": ["Coaching", "E-Learning", "Education Technology", "Skill Development", "Others"],
    "Renewable Energy": ["Manufacture Of Electrical Equipment", "Manufacture Of Machinery & Equipment", "Renewable Energy Solutions", "Renewable Nuclear Energy", "Renewable Solar Energy", "Renewable Wind Energy", "Others"],
    "Technology Hardware": ["3D Printing", "Electronics", "Embedded", "Manufacturing", "Semiconductor", "Others"],
    "Healthcare And Life science": ["Assistance Technology", "Biotechnology", "Health And Wellness", "Healthcare IT", "Healthcare Services", "Healthcare Technology", "Medical Devices Biomedical", "Pharmaceutical", "Others"],
    "Internet Of Things": ["Manufacturing & Warehouse", "Smart Home", "Wearable's", "Others"],
    "IT Services": ["Application Development", "BPO", "IT Consulting", "IT Management", "KPO", "Product Development", "Project Management", "Testing", "Web Development", "Others"],
    "Human Resources": ["Internships", "Requirement Jobs", "Skills Assessments", "Talent Management", "Training", "Others"],
    "Marketing": ["Branding", "Digital Marketing (SEO Automation)", "Discovery", "Loyalty", "Market Research", "Sales", "Others"],
    "Nanotechnology": ["Others"],
    "Non-renewable Energy": ["Oil & Gas Drilling", "Oil & Gas Exploration And Production", "Oil & Gas Transportation Service", "Oil Related Services And Equipment", "Others"],
    "Pets And Animals": ["Others"],
    "Media And Entertainment": ["Digital Media", "Digital Media Blogging", "Digital Media News", "Digital Media Publishing", "Digital Media Video", "Entertainment", "Movies", "OOH Media", "Social Media", "Others"],
    "Retail": ["Comparison Shopping", "Retail Technology", "Social Commerce", "Others"],
    "House-Hold Services": ["Baby Care", "Home Care", "Laundry", "Personal Care", "Others"],
    "Professional & Commercial Services": ["Business Support Services", "Business Support Supplies", "Commercial Printing Services", "Employment Services", "Environmental Services & Equipments", "Professional Information Services", "Others"],
    "Sports": ["Fantasy Sports", "Sports Promotion And Networking", "Others"],
    "Social Impact": ["Corporate Social Responsibility", "NGO", "Others"],
    "Social Network": ["Others"],
    "Textiles and Apparels": ["Apparel & Accessories", "Leather Footwear", "Leather Textiles Goods", "Non – Leather Footwear", "Non – Leather Textiles Goods", "Others"],
    "Indic Language Startups": ["E-Commerce", "Education", "Media And Entertainment", "Natural Language Processing", "Social Media", "Utility Services"],
    "Transportation And Storage": ["Freight & Logistics Services", "Passenger Transportation Services", "Traffic Management", "Transport Infrastructure", "Others"],
    "Logistics": ["Others"],
    "Travel And Tourisms": ["Experiential Travel", "Facility Management", "Holiday Rentals", "Hospitality", "Hotel", "Ticketing", "Wayside Amenities", "Others"],
    "Security Solutions": ["Cyber Security", "Home Security Solutions", "Public Citizen Security Solutions", "Others"],
    "Airport Operations": ["Others"],
    "Real Estate": ["Co working Spaces", "Housing", "Others"],
    "Other Specialty retailers": ["Auto Vehicles Parts & Service Retailers", "Computer & Electronics Retailers", "Home Furnishings Retailers", "Home Improvement Products & Service Retailers", "Others"],
    "Safety": ["Personal Security", "Others"],
    "Robotics": ["Robotics Application", "Robotics Technology", "Others"],
    "Passenger Experience": ["Others"],
    "Biotechnology": ["Others"],
    "Waste Management": ["Others"],
    "Others": ["Others"],
    "Toys and Games": ["Physical Toys And Games", "Virtual Games"],
}

STARTUP_CATEGORY_CHOICES = [
    "Governments", "Hyperlocal", "Discovery", "Location Based Services", "Manufacturing", "Marketplace",
    "Mobile", "Offline", "Online Aggregator", "Peer to Peer", "Platform", "Consulting",
    "Consumer Internet", "Engineering", "E-commerce", "Others", "Rental", "Enterprise Mobility",
    "Research", "Sharing Economy", "Social Enterprise", "SaaS", "Subscription Commerce",
]

INDUSTRY_CHOICES = list(INDUSTRY_SECTOR_OPTIONS.keys())

_STARTUP_ADDRESS_SCHEMA = {
    "address1": {"type": str, "required": True},
    "address2": {"type": str, "required": False, "allow_blank": True},
    "state":    {"type": str, "required": False, "allow_blank": True},
    "district": {"type": str, "required": False, "allow_blank": True},
    "city":     {"type": str, "required": False, "allow_blank": True},
    "pincode": {
        "type": str, "required": True,
        "pattern": _PIN_6_RE,
        "pattern_message": "'pincode' must be exactly 6 digits",
    },
}

_DIRECTOR_GENDER_CHOICES = ["Male", "Female", "Other"]

_DIRECTOR_SCHEMA = {
    "Name": {
        "type": str, "required": True,
        "pattern": _ALPHA_ONLY_RE,
        "pattern_message": "director 'Name' must contain alphabetic characters only",
    },
    "Gender": {
        "type": str, "required": True,
        "choices": _DIRECTOR_GENDER_CHOICES,
        "choices_message": f"director 'Gender' must be one of: {', '.join(_DIRECTOR_GENDER_CHOICES)}",
    },
    "Address": {"type": str, "required": True},
    "Mobile_no": {
        "type": str, "required": False, "allow_blank": True,
        "pattern": _OPTIONAL_MOBILE_RE,
        "pattern_message": "director 'Mobile_no' must be blank or exactly 10 digits",
    },
    "Email": {
        "type": str, "required": True,
        "pattern": _EMAIL_RE,
        "pattern_message": "director 'Email' must be a valid email address (e.g. name@domain.com)",
    },
    "Dob": {
        "type": str, "required": True,
        "pattern": _DATE_DDMMYYYY_RE,
        "pattern_message": "director 'Dob' must be in DD/MM/YYYY format",
    },
}

@framework.service("startup_india", schema={
    "comp_address": {"type": dict, "required": True, "schema": _STARTUP_ADDRESS_SCHEMA},
    "directors": {"type": list, "required": True, "items": _DIRECTOR_SCHEMA},
    "business": {"type": str, "required": True},
    "designation": {"type": str, "required": True},
    "username": {"type": str, "required": True},
    "mobile": {
        "type": str, "required": True,
        "pattern": _MOBILE_RE,
        "pattern_message": "'mobile' must be exactly 10 digits",
    },
    "email_id": {
        "type": str, "required": True,
        "pattern": _EMAIL_RE,
        "pattern_message": "'email_id' must be a valid email address (e.g. name@domain.com)",
    },
    "password": {"type": str, "required": True},
    "website": {"type": str, "required": False},
    "Industry": {
        "type": str, "required": True,
        "choices": INDUSTRY_CHOICES,
        "choices_message": "'Industry' must be one of the recognised Startup India industries",
    },
    # Sector is validated against the sub-list for whichever Industry was given —
    # same depends_on/choices_map pattern used for category_sub_type in chennaipt below.
    "Sector": {
        "type": str, "required": True,
        "depends_on": "Industry",
        "choices_map": INDUSTRY_SECTOR_OPTIONS,
        "depends_on_message": "'Sector' cannot be validated: 'Industry' is missing or invalid",
        "choices_message": "'Sector' is not a valid sector for the given 'Industry'",
    },
    "Catogries": {
        "type": list, "required": True,
        "each_choice": STARTUP_CATEGORY_CHOICES,
        "each_choice_message": f"'Catogries' may only contain: {', '.join(STARTUP_CATEGORY_CHOICES)}",
    },
    "startup_deeptech": {"type": int, "required": False},
    "stage": {"type": str, "required": False},
    "startup_ipr": {"type": int, "required": False},
    "no_of_emp": {
        "type": str, "required": False,
        "pattern": _NUMERIC_ONLY_RE,
        "pattern_message": "'no_of_emp' must be numeric only",
    },
    "problem_statement": {"type": str, "required": False},
    "aboutcompany":      {"type": str, "required": False},
    "who_we_are":        {"type": str, "required": False},
    "solution":          {"type": str, "required": False},
    "uniqueness":        {"type": str, "required": False},
    "revenue_growth":    {"type": str, "required": False},

})
class StartupIndiaService(AutomationService):
    def run(self, data):
        from startup_india import Startup_india

        self.add_log("Launching Startup India DPIIT recognition automation")
        # Pass this AutomationService instance itself (not a raw session_id/
        # sessions dict) — Startup_india has no session, logging, or OTP
        # storage of its own; it calls back into this service for all of
        # that, so the framework is the single source of truth.
        obj = Startup_india(data=data, service=self)
        return obj.run()


_EPFO_MEMBER_SCHEMA = {
    "uan": {
        "type": str, "required": True,
        "pattern": _UAN_RE,
        "pattern_message": "member 'uan' must be exactly 12 digits",
    },
    "name": {
        "type": str, "required": True,
        "pattern": _ALPHA_ONLY_RE,
        "pattern_message": "member 'name' must contain alphabetic characters only",
    },
    "aadhar": {
        "type": str, "required": True,
        "pattern": _AADHAAR_RE,
        "pattern_message": "member 'aadhar' must be exactly 12 digits",
    },
    "dob": {
        "type": str, "required": True,
        "pattern": _DATE_DDMMYYYY_RE,
        "pattern_message": "member 'dob' must be in DD/MM/YYYY format",
    },
    "doj": {
        "type": str, "required": True,
        "pattern": _DATE_DDMMYYYY_RE,
        "pattern_message": "member 'doj' must be in DD/MM/YYYY format",
    },
    "wages": {
        "type": str, "required": True,
        "pattern": _NUMERIC_ONLY_RE,
        "pattern_message": "member 'wages' must be numeric only",
    },
    "marital_status": {
        "type": str, "required": True,
        "pattern": _SINGLE_ALPHA_RE,
        "pattern_message": "member 'marital_status' must be a single alphabetic character",
    },
    "qualification": {
        "type": str, "required": True,
        "pattern": _SINGLE_DIGIT_RE,
        "pattern_message": "member 'qualification' must be a single numeric digit",
    },
    "pan": {
        "type": str, "required": False,
        "pattern": _PAN_RE,
        "pattern_message": "member 'pan' must be a valid PAN (e.g. ABCDE1234F)",
    },
    "email": {
        "type": str, "required": False,
        "pattern": _EMAIL_RE,
        "pattern_message": "member 'email' must be a valid email address (e.g. name@domain.com)",
    },
}

# EPFO's UAN member portal has no OTP step in the onboarding flow itself,
# so this service is registered with needs_otp=False — /epfo/otp is never
# wired up and returns a plain 404 instead of a route that does nothing.
@framework.service("epfo", needs_otp=False, schema={
    "user_name": {
        "type": str, "required": True,
        "pattern": _EPFO_USERNAME_RE,
        "pattern_message": "'user_name' must be 5 uppercase letters followed by 10 digits",
    },
    "password": {"type": str, "required": True},
    "company_name": {"type": str, "required": True},
    "member": {"type": list, "required": True, "items": _EPFO_MEMBER_SCHEMA},
})
class EPFOService(AutomationService):
    def run(self, data):
        from epfo import EPFOOnboarding

        self.add_log("Launching EPFO PF member onboarding automation")
        obj = EPFOOnboarding(data=data, session=self.framework.sessions.get(self.session_id))
        return obj.run()


@framework.service("itr_notice", needs_otp=False, schema={
    "username": {
        "type": str, "required": True,
        "pattern": _PAN_RE,
        "pattern_message": "'username' must be a valid PAN (e.g. ABCDE1234F)",
    },
    "password": {"type": str, "required": True},
})
class ITRNoticeService(AutomationService):
    def run(self, data):
        from income_tax_notice import IncomeTaxNotice

        self.add_log("Launching Income Tax e-Proceedings notice download automation")
        client = IncomeTaxNotice()
        # income_tax_notice.IncomeTaxNotice.login() expects a PAN — the
        # public field is called "username" here since that's what the
        # e-filing portal login screen itself calls it.
        responses = client.login(pan=data["username"], password=data["password"])
        if isinstance(responses, dict):
            result = responses.get("result", [])
        else:
            result = responses

        return result


@framework.service("udyam_certificate", schema={
    "udyam_no": {
        "type": str, "required": True,
        "pattern": _UDYAM_RE,
        "pattern_message": "'udyam_no' must look like UDYAM-XX-00-0000000",
    },
    "phone": {
        "type": str, "required": True,
        "pattern": _MOBILE_RE,
        "pattern_message": "'phone' must be exactly 10 digits",
    },
})
class UdyamCertificateService(AutomationService):
    def run(self, data):
        from udyam_certificate import UdyamCertificate

        self.add_log("Launching Udyam certificate download automation")
        # Same pattern as StartupIndiaService/EPFOService: hand this
        # AutomationService instance itself to the module so it can log,
        # report progress, and block on wait_for_otp() through the
        # framework rather than keeping any session state of its own.
        obj = UdyamCertificate(data=data, service=self)
        return obj.run()


if __name__ == "__main__":
    framework.run()