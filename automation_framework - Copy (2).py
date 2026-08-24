import json
import os
import threading
import traceback
import uuid
import datetime

from flask import Flask, request, jsonify


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

class AutomationFramework:

    def __init__(self, name: str = __name__, log_path: str = "logs/sessions.log", port: int = 3333):
        self.app = Flask(name)
        self.port = port

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
    def service(self, name: str, schema: dict = None):
        """
        @framework.service("udyamreg", schema={
            "pan": {"type": str, "required": True},
            "mobile": {"type": str, "required": True},
        })
        class UdyamRegService(AutomationService):
            def run(self, data):
                ...
        """
        schema = schema or {}

        def decorator(cls):
            cls.SERVICE_NAME = name
            cls.PAYLOAD_SCHEMA = schema
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
        """
        Every session-facing log line goes through here. `kind` buckets
        the entry for the hit-count breakdown used by the /logs summary:
          - "otp"    -> counted into session["otp_hits"]
          - "status" -> counted into session["status_hits"] (default)
        Auto-detected from the message when not given explicitly, so
        existing call sites ("Waiting for OTP", "OTP submitted", ...)
        don't need to be touched to get bucketed correctly.
        """
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
        """One session's entry for the /logs summary: total_hits =
        status_hits (automation log/status updates) + otp_hits (OTP
        wait/submit/receive events) + hits (times /status was
        polled)."""
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

    def set_otp(self, session_id: str, otp: str):
        with self._lock:
            if session_id in self.sessions:
                self.sessions[session_id]["otp"] = otp
                self.sessions[session_id]["otp_received"] = True
                self.sessions[session_id]["updated_at"] = _now()

    def get_otp(self, session_id: str):
        with self._lock:
            session = self.sessions.get(session_id)
            return session.get("otp") if session else None

    def otp_received(self, session_id: str) -> bool:
        with self._lock:
            session = self.sessions.get(session_id)
            return bool(session and session.get("otp_received"))


    @classmethod
    def validate(cls, data, schema: dict, _path: str = ""):
        if not isinstance(data, dict):
            return [f"'{_path or 'payload'}' must be a JSON object"]

        errors = []
        for field, rules in schema.items():
            full_name = f"{_path}.{field}" if _path else field
            required = rules.get("required", False)
            required_if = rules.get("required_if")
            expected_type = rules.get("type")
            allow_blank = rules.get("allow_blank", False)

            if required_if:
                sibling_val = str(data.get(required_if["field"]))
                if sibling_val == str(required_if.get("equals")):
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

            if "schema" in rules and isinstance(value, dict):
                errors.extend(cls.validate(value, rules["schema"], _path=full_name))

            if "each" in rules and isinstance(value, dict):
                for key, sub_value in value.items():
                    errors.extend(cls.validate(sub_value, rules["each"], _path=f"{full_name}.{key}"))

            if "items" in rules and isinstance(value, list):
                for idx, item in enumerate(value):
                    errors.extend(cls.validate(item, rules["items"], _path=f"{full_name}[{idx}]"))

        return errors


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
            self.set_otp(session_id, otp_value)
            self.add_log(session_id, "OTP submitted")
            return jsonify({"status": "success", "message": "OTP stored", "session_id": session_id}), 200

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

    def wait_for_otp(self, poll_interval: float = 2.0, timeout: float = 180.0):
        import time
        self.add_log("Waiting for OTP")
        waited = 0.0
        while waited < timeout:
            if self.framework.otp_received(self.session_id):
                return self.framework.get_otp(self.session_id)
            time.sleep(poll_interval)
            waited += poll_interval
        raise TimeoutError("OTP not received within timeout")

    def run(self, data):
        raise NotImplementedError("Each service must implement run(self, data)")

framework = AutomationFramework(__name__, log_path="logs/sessions.log", port=3333)

_ADDRESS_SCHEMA = {
    "flat":     {"type": str, "required": True},
    "building": {"type": str, "required": False},
    "village":  {"type": str, "required": False},
    "block":    {"type": str, "required": False},
    "road":     {"type": str, "required": False},
    "city":     {"type": str, "required": True},
    "pin":      {"type": str, "required": True},
    "state":    {"type": str, "required": True},
    "district": {"type": str, "required": True},
}

@framework.service("udyamreg", schema={
    # -------- Aadhaar Verification --------
    "aadhaar_name":   {"type": str, "required": True},
    "aadhaar_number": {"type": str, "required": True},

    # -------- PAN Verification --------
    "org_type":   {"type": str, "required": True},   # 1=Proprietary ... 11=Trust, see udyam_reg_v2.py header
    "pan_number": {"type": str, "required": True},
    "pan_name":   {"type": str, "required": True},
    "dob":        {"type": str, "required": True},   # DD/MM/YYYY

    # -------- Investment / Turnover --------
    "wdv":             {"type": str, "required": True},  # Written Down Value
    "exclusion_cost":  {"type": str, "required": True},
    "total_turnover":  {"type": str, "required": True},

    # -------- Basic Details --------
    "company_name":    {"type": str, "required": True},
    "mobile":          {"type": str, "required": True},
    "email":           {"type": str, "required": True},
    "social_category": {"type": str, "required": True},   # 1=General 2=SC 3=ST 4=OBC
    "gender":          {"type": str, "required": True},   # 1=Male 2=Female 3=Others
    "divyang":         {"type": str, "required": True},   # 0=No 1=Yes

    # -------- Addresses (nested — validated field by field) --------
    "official_address": {"type": dict, "required": True, "schema": _ADDRESS_SCHEMA},
    # units is a dict of UNKNOWN keys (unit names), each shaped like an address
    "units": {"type": dict, "required": True, "each": _ADDRESS_SCHEMA},

    # -------- Status of Enterprise --------
    "previous_em":         {"type": str, "required": True},   # 0=N/A 2=EM-II 4=Previous UAM
    "incorporation_date":  {"type": str, "required": True},
    "commenced":           {"type": str, "required": True},   # 1=Yes 0=No
    # only required when commenced == "1"
    "commencement_date":   {"type": str, "required_if": {"field": "commenced", "equals": "1"}},

    # -------- Bank Details --------
    "bank_name":      {"type": str, "required": True},
    "ifsc":           {"type": str, "required": True},
    "account_number": {"type": str, "required": True},

    # -------- Major Activity --------
    "major_activity":  {"type": str, "required": True},   # 1=Manufacturing 2=Services
    # only required when major_activity == "2" (Services) — else optional/default "1"
    "major_activity_under_services": {"type": str, "required_if": {"field": "major_activity", "equals": "2"}},
    "nic_activity":    {"type": str, "required": True},
    "nic": {"type": dict, "required": True, "schema": {
        "nic2": {"type": str, "required": True},
        "nic4": {"type": str, "required": True},
        "nic5": {"type": str, "required": True},
    }},

    # -------- Employees --------
    "employees": {"type": dict, "required": True, "schema": {
        "male":   {"type": str, "required": True},
        "female": {"type": str, "required": True},
        "others": {"type": str, "required": True},
    }},
})
class UdyamRegService(AutomationService):
    def run(self, data):
        from udyam_reg_v2 import UdyamRegistration

        self.add_log("Launching Udyam automation")
        obj = UdyamRegistration(data=data, session=self.framework.sessions.get(self.session_id))
        obj.logger = self  # self.add_log(message) / self.set_progress(n) both match what obj already calls
        return obj.udyam_reg_v2()

_DIR_EMP_ROW_SCHEMA = {
    "directors": {"type": int, "required": True},
    "employees": {"type": int, "required": True},
}

@framework.service("chennaipt", schema={
    "username": {"type": str, "required": True},
    "password": {"type": str, "required": True},
    "comp_name": {"type": str, "required": True},
    "category_type": {"type": str, "required": True},
    "category_sub_type": {"type": str, "required": True},
    "property_tax_number": {"type": str, "required": True},   # "0" = manual address entry, else existing PT number
    "authorized_person": {"type": str, "required": True},
    "authorized_mobile_number": {"type": str, "required": True},

    "details_dir_emp": {"type": dict, "required": True, "schema": {
        "rows": {"type": list, "required": True, "items": _DIR_EMP_ROW_SCHEMA},
    }},

    "Date_of_commencement": {"type": str, "required": True},
    "half_yearly_gross_income": {"type": str, "required": False},   # defaults to "0" in fill_financial_details
    "remarks": {"type": str, "required": False},                 # defaults to "New Registration" if blank/missing

    # -------- only required when property_tax_number == "0" (manual address) --------
    "buildingName": {"type": str, "required_if": {"field": "property_tax_number", "equals": "0"}, "allow_blank": True},
    "door_no":    {"type": str, "required_if": {"field": "property_tax_number", "equals": "0"}},
    "zone":       {"type": str, "required_if": {"field": "property_tax_number", "equals": "0"}},
    "ward_or_division": {"type": str, "required_if": {"field": "property_tax_number", "equals": "0"}},
    "area":       {"type": str, "required_if": {"field": "property_tax_number", "equals": "0"}},
    "locality":   {"type": str, "required_if": {"field": "property_tax_number", "equals": "0"}},
    "street":     {"type": str, "required_if": {"field": "property_tax_number", "equals": "0"}},
    "pincode":    {"type": str, "required_if": {"field": "property_tax_number", "equals": "0"}},
})
class ChennaiProfessionalTaxService(AutomationService):
    def run(self, data):
        from Chennai_Professional_Tax import Chennai_Professional_Tax as CPT

        # No remarks means it's a fresh filing — default it instead of
        # sending a blank value to the portal.
        if not data.get("remarks"):
            data["remarks"] = "New Registration"
            self.add_log("No remarks supplied — defaulting to 'New Registration'")

        self.add_log("Launching Chennai Professional Tax filing")

        obj = CPT(data=data, session=self.framework.sessions.get(self.session_id))
        obj.logger = self
        return obj.run()

_EPFO_MEMBER_SCHEMA = {
    "uan":    {"type": str, "required": True},
    "name":   {"type": str, "required": True},
    "aadhar": {"type": str, "required": True},
    "dob":    {"type": str, "required": True},
    "doj":    {"type": str, "required": True},
    "wages":  {"type": str, "required": True},

    "gender":         {"type": str, "required": False},
    "marital_status": {"type": str, "required": False},
    "qualification":  {"type": str, "required": False},
    "pan":            {"type": str, "required": False},
    "email":          {"type": str, "required": False},
}

@framework.service("epfo", schema={
    "user_name": {"type": str, "required": True},
    "password":  {"type": str, "required": True},
    "company_name": {"type": str, "required": False},
    "member": {"type": list, "required": True, "items": _EPFO_MEMBER_SCHEMA},
})
class EPFOService(AutomationService):
    def run(self, data):
        from epfo import EPFOOnboarding

        self.add_log("Launching EPFO onboarding")
        obj = EPFOOnboarding(data=data, session=self.framework.sessions.get(self.session_id))
        obj.logger = self
        return obj.run()



if __name__ == "__main__":
    framework.run()