from PIL import Image
from io import BytesIO

import base64
import hashlib
import json
import re
import time
import webbrowser as w
from urllib.parse import urljoin, urlparse, parse_qs

import requests
import urllib3
from bs4 import BeautifulSoup
from Cryptodome.Cipher import AES
from Cryptodome.Util.Padding import pad

urllib3.disable_warnings()


class TimeoutSession(requests.Session):
    """requests.Session with a default timeout applied to every call."""

    def __init__(self, timeout=60):
        super().__init__()
        self.default_timeout = timeout

    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", self.default_timeout)
        return super().request(*args, **kwargs)


class EpfoAutomation:

    BASE_URL = "https://unifiedportal-emp.epfindia.gov.in/epfo/"
    HOST = "https://unifiedportal-emp.epfindia.gov.in"

    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        )
    }

    def __init__(self, timeout=60):
        self.session = TimeoutSession(timeout=timeout)

        # ---- login-page state ----
        self.html = ""
        self.challenge = ""
        self.encrChallenge = ""
        self.encrEstType = ""
        self.randomData = ""

        # ---- post-login navigation state ----
        self.hdiv_state = ""      # userinfo HDIV
        self.hdiv = ""            # current page HDIV_STATE
        self.modify_hdiv = ""     # current page MODIFY_HDIV_STATE
        self.member_challenge = ""  # AES key for field-level encryption

        # ---- aadhaar verification result state ----
        self.aadhaar_fields = {}

        self.last_captcha_bytes = None

        self.last_captcha_debug_path = None

        self.last_member_details = {}

        self.registration_fields = []

    # ------------------------------------------------------------------
    # Crypto helpers
    # ------------------------------------------------------------------
    @staticmethod
    def generate_hid_password(password, challenge):
        password_md5 = hashlib.md5(password.encode()).hexdigest()
        fhash = hashlib.sha512(f"kr9rk{password_md5}kr9rk".encode()).hexdigest()
        return hashlib.sha512(f"{challenge}{fhash}".encode()).hexdigest()

    @staticmethod
    def _aes_encrypt(value: str, random_data: str) -> str:
        key = base64.b64decode(random_data)
        cipher = AES.new(key, AES.MODE_ECB)
        encrypted = cipher.encrypt(pad(value.encode("utf-8"), AES.block_size))
        return base64.b64encode(encrypted).decode("utf-8")

    def encrypt_username(self, username, randomData=None):
        return self._aes_encrypt(username, randomData or self.randomData)

    def encrypt_field(self, value, random_data=None):
        """Encrypt a field value with the memberChallenge AES key."""
        return self._aes_encrypt(value, random_data or self.member_challenge)

    # ------------------------------------------------------------------
    # Step 1: Load login page -> challenge / randomData / captcha
    # ------------------------------------------------------------------
    def load_login_page(self, save_debug=True):
        response = self.session.get(self.BASE_URL, headers=self.DEFAULT_HEADERS, verify=False)
        print("[load_login_page] Status:", response.status_code)

        self.html = response.text
        soup = BeautifulSoup(self.html, "html.parser")

        self.encrChallenge = soup.find("input", {"id": "encrChallenge"})["value"]
        self.encrEstType = soup.find("input", {"id": "encrEstType"})["value"]

        challenge_tag = soup.find(id="lblChallange")
        self.challenge = challenge_tag.text.strip() if challenge_tag else ""

        random_match = re.search(r"randomData\s*=\s*['\"]([^'\"]+)['\"]", self.html)
        self.randomData = random_match.group(1) if random_match else ""

        print("[load_login_page] challenge:", self.challenge)
        print("[load_login_page] randomData:", self.randomData)

        # Captcha image
        captcha_src = None
        for img in soup.find_all("img"):
            src = img.get("src")
            if src and "captcha" in src.lower():
                captcha_src = src
                break

        if not captcha_src:
            print("[load_login_page] WARNING: captcha image not found")
            return

        captcha_url = urljoin(self.BASE_URL, captcha_src)
        captcha_response = self.session.get(captcha_url, headers=self.DEFAULT_HEADERS, verify=False)

        self.last_captcha_bytes = captcha_response.content

        self.last_captcha_debug_path = None
        if save_debug:
            try:
                debug_name = f"captcha_file_{id(self)}.png"
                with open(debug_name, "wb") as f:
                    f.write(captcha_response.content)
                self.last_captcha_debug_path = debug_name
            except OSError as e:
                print(f"[load_login_page] WARNING: could not write captcha debug file: {e}")

        if "image" in str(captcha_response.headers.get("Content-Type")):
            pass  # display()/show() removed -- not usable in a headless API worker anyway
        else:
            print("[load_login_page] Captcha response was not an image")

    # ------------------------------------------------------------------
    # Step 2: Login
    # ------------------------------------------------------------------
    def login(self, username, password, captcha, save_debug=True):
        enc_username = self.encrypt_username(username)
        hid_password = self.generate_hid_password(password, self.challenge)
        fake_password = "r" * len(password)

        payload = {
            "userName": enc_username,
            "password": fake_password,
            "hidPassword": hid_password,
            "captcha": captcha,
            "challenge": self.challenge,
            "encrChallenge": self.encrChallenge,
            "encrEstType": self.encrEstType,
        }

        response = self.session.post(
            self.BASE_URL,
            headers=self.DEFAULT_HEADERS,
            data=payload,
            verify=False,
            allow_redirects=True,
        )
        self.html = response.text
        print("[login] Status:", response.status_code)

        soup = BeautifulSoup(self.html, "html.parser")

        if soup.find(string=lambda x: x and "Welcome:" in x):
            print("[login] Dashboard opened")
            return True
        elif soup.find("form", {"id": "AuthenticationForm"}):
            print("[login] Login page returned again (bad captcha/credentials)")
            return False
        else:
            print("[login] Unknown page returned")
            return False
        

    def login_with_auto_captcha(self, username, password, captcha_solver, max_attempts=5):
        self.load_login_page()

        for attempt in range(1, max_attempts + 1):
            try:
                if not self.last_captcha_bytes:
                    print(f"[login_with_auto_captcha] attempt {attempt}: no captcha image available, retrying...")
                    self.load_login_page()
                    continue
                
                captcha_b64 = base64.b64encode(self.last_captcha_bytes).decode("utf-8")
                captcha_text = captcha_solver.solve(captcha_base64=captcha_b64)
                # captcha_text=self.get_captcha_text(self.last_captcha_bytes)
                if not captcha_text:
                    print(f"[login_with_auto_captcha] attempt {attempt}: OCR could not read the captcha, retrying...")
                    self.load_login_page()
                    continue

                captcha_text = "".join(
                    c.upper() if i < 2 else c.lower() if i < 4 else c
                    for i, c in enumerate(captcha_text)
                )

                print(f"[login_with_auto_captcha] attempt {attempt}: OCR guess = {captcha_text!r}")


                if self.last_captcha_debug_path:
                    try:
                        import os
                        os.remove(self.last_captcha_debug_path)
                    except OSError:
                        pass
                    finally:
                        self.last_captcha_debug_path = None

                if self.login(username, password, captcha_text):
                    return True

            except Exception as e:
                print(f"[login_with_auto_captcha] attempt {attempt}: error ({e}), retrying...")

            self.load_login_page()  # refresh challenge/captcha before the next attempt

        print(f"[login_with_auto_captcha] gave up after {max_attempts} attempts")
        return False

    # ------------------------------------------------------------------
    # Step 3: Post-login userinfo (establishment / company details)
    # ------------------------------------------------------------------
    def get_user_info(self):
        m = re.search(r"/epfo/userController/userinfo\?_HDIV_STATE_=([^\"']+)", self.html)
        if not m:
            raise RuntimeError("Could not locate userinfo HDIV_STATE in login response")
        self.hdiv_state = m.group(1)

        userinfo_url = f"{self.HOST}/epfo/userController/userinfo?_HDIV_STATE_={self.hdiv_state}"
        headers = {
            **self.DEFAULT_HEADERS,
            "Accept": "*/*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.HOST}/",
            "Origin": self.HOST,
        }

        r = self.session.post(
            userinfo_url,
            headers=headers,
            data={"_HDIV_STATE_": self.hdiv_state},
            verify=False,
            allow_redirects=True,
        )
        print("[get_user_info] Status:", r.status_code)

        data = r.json()
        company_name = data.get("establishment", {}).get("name")
        print("[get_user_info] Company:", company_name)
        return data

    # ------------------------------------------------------------------
    # Step 4: View member registration page -> first HDIV/MODIFY_HDIV pair
    # ------------------------------------------------------------------
    def _select_previous_employment_yes(self, html=None):
        html = html or self.html
        soup = BeautifulSoup(html, "html.parser")
        tag = soup.find("input", {"id": "previousEmployementYes"})
        if tag is None:
            raise RuntimeError("previousEmployementYes radio not found in this page")

        onclick = tag.get("onclick", "")
        url = onclick.split("'")[1].replace("&amp;", "&")
        params = parse_qs(urlparse(url).query)

        self.hdiv = params.get("_HDIV_STATE_", [""])[0]
        self.modify_hdiv = params.get("_MODIFY_HDIV_STATE_", [""])[0]

        print("[_select_previous_employment_yes] HDIV_STATE      =", self.hdiv)
        print("[_select_previous_employment_yes] MODIFY_HDIV_STATE =", self.modify_hdiv)

    def view_registration(self, save_debug=True):
        m = re.search(r'/epfo/uanmember/viewRegistraton\?_HDIV_STATE_=([^"\']+)', self.html)
        if not m:
            raise RuntimeError("Could not locate viewRegistraton HDIV in previous page")
        registration_hdiv = m.group(1)

        registration_url = f"{self.HOST}/epfo/uanmember/viewRegistraton?_HDIV_STATE_={registration_hdiv}"
        r = self.session.get(registration_url, verify=False, allow_redirects=False)
        print("[view_registration] Status:", r.status_code)

        self.html = r.text

        self._select_previous_employment_yes()

    # ------------------------------------------------------------------
    # Step 5: Refresh the "previous employment" form -> new HDIV pair +
    # fresh memberChallenge (THIS is the step your manual class skipped).
    # ------------------------------------------------------------------
    def refresh_previous_employment_form(self, save_debug=True):
        refresh_form_url = (
            f"{self.HOST}/epfo/uanmember/viewPreviousEmployment"
            f"?_MODIFY_HDIV_STATE_={self.modify_hdiv}"
            f"&_HDIV_STATE_={self.hdiv}"
        )

        headers = {
            **self.DEFAULT_HEADERS,
            "Accept": "*/*",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.HOST}/",
            "Origin": self.HOST,
        }

        form_payload = {"isPreviousEmployee": "Y"}

        response_form = self.session.post(
            refresh_form_url,
            headers=headers,
            json=form_payload,
            verify=False,
        )
        print("[refresh_previous_employment_form] Status:", response_form.status_code)


        soup = BeautifulSoup(response_form.text, "html.parser")

        verify_btn = soup.find("input", {"class": "btn-verify"})
        if verify_btn is None:
            raise RuntimeError(
                "Could not find the Verify button (btn-verify). "
                "Session may have expired or the payload was rejected:\n"
                + response_form.text[:500]
            )

        data_url = verify_btn.get("data-url")
        params = parse_qs(urlparse(data_url).query)
        self.modify_hdiv = params["_MODIFY_HDIV_STATE_"][0]
        self.hdiv = params["_HDIV_STATE_"][0]

        challenge_tag = soup.find(id="memberChallenge")
        if challenge_tag is None:
            raise RuntimeError("memberChallenge not found after form refresh")
        self.member_challenge = challenge_tag.get_text(strip=True)

        key = base64.b64decode(self.member_challenge)
        if len(key) not in (16, 24, 32):
            raise RuntimeError("Invalid AES key length in memberChallenge")

        print("[refresh_previous_employment_form] new HDIV_STATE      =", self.hdiv)
        print("[refresh_previous_employment_form] new MODIFY_HDIV_STATE =", self.modify_hdiv)
        print("[refresh_previous_employment_form] memberChallenge      =", self.member_challenge)

    # ------------------------------------------------------------------
    # Step 6: Submit UAN/Aadhaar/DOB/Name for verification
    # ------------------------------------------------------------------
    def verify_aadhaar(self, member_uan, member_name, member_dob, member_aadhar, save_debug=True):
        url = (
            f"{self.HOST}/epfo/uanmember/getAadhaarInfo"
            f"?_MODIFY_HDIV_STATE_={self.modify_hdiv}"
            f"&_HDIV_STATE_={self.hdiv}"
        )

        headers = {
            **self.DEFAULT_HEADERS,
            "Accept": "*/*",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.HOST}/",
            "Origin": self.HOST,
        }

        self.last_member_details = {
            "uan": member_uan,
            "name": member_name,
            "dob": member_dob,
            "aadhaar": member_aadhar,
        }

        payload = {
            "isPreviousEmployee": "Y",
            "previousDetails": {
                "uan": self.encrypt_field(member_uan),
                "name": member_name,
                "dob": self.encrypt_field(member_dob),
                "aadhaar": self.encrypt_field(member_aadhar),
            },
            "isNorthEastMember": None,
            "aadhaarConsent": {"consentStatus": "Y"},
        }

        response = self.session.post(url, headers=headers, json=payload, verify=False)
        print("[verify_aadhaar] Status:", response.status_code)

        self.html = response.text

        return self.html

    def resubmit_aadhaar(self, payload, save_debug=True):
        url = (
            f"{self.HOST}/epfo/uanmember/getAadhaarInfo"
            f"?_MODIFY_HDIV_STATE_={self.modify_hdiv}"
            f"&_HDIV_STATE_={self.hdiv}"
        )

        headers = {
            **self.DEFAULT_HEADERS,
            "Accept": "*/*",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.HOST}/",
            "Origin": self.HOST,
        }

        response = self.session.post(url, headers=headers, json=payload, verify=False)
        print("[resubmit_aadhaar] Status:", response.status_code)

        self.html = response.text

        return self.html

    def resolve_aadhaar_mismatch(self, max_retries=3):
        result = self.parse_aadhaar_response()
        attempt = 0

        while result["status"] in ("NAME_MISMATCH", "DOB_MISMATCH") and attempt < max_retries:
            attempt += 1
            print(f"[resolve_aadhaar_mismatch] Attempt {attempt}: resubmitting after {result['status']}")
            print(json.dumps(result["next_payload"], indent=2))

            self.resubmit_aadhaar(result["next_payload"])
            result = self.parse_aadhaar_response()

        print(f"[resolve_aadhaar_mismatch] Final status: {result['status']} | {result['message']}")
        if result["next_payload"] is not None:
            print(json.dumps(result["next_payload"], indent=2))

        return result

    ALERT_PATTERNS = [
        ("name mismatch", "NAME_MISMATCH"),
        ("dob mismatch", "DOB_MISMATCH"),
        ("uan details not found", "UAN_NOT_FOUND"),
        ("aadhaar mismatch", "AADHAAR_MISMATCH"),
        ("member details matched", "MATCHED"),
        ("error while verifying uan details", "TRANSIENT_ERROR"),
    ]


    TERMINAL_ERROR_CODES = {"UAN_NOT_FOUND", "AADHAAR_MISMATCH"}

    TRANSIENT_ERROR_CODES = {"TRANSIENT_ERROR"}

    def classify_aadhaar_alert(self, soup=None):
        """
        Read the alert-danger / alert-success div and return
        (error_code, raw_message). error_code is one of:
        NAME_MISMATCH, DOB_MISMATCH, UAN_NOT_FOUND, AADHAAR_MISMATCH,
        MATCHED, TRANSIENT_ERROR, or UNKNOWN (if no recognizable alert
        text was found).
        """
        soup = soup or BeautifulSoup(self.html, "html.parser")

        alert_div = soup.find("div", class_="alert-danger") or soup.find("div", class_="alert-success")
        raw_message = alert_div.get_text(strip=True) if alert_div else ""
        lowered = raw_message.lower()

        for needle, code in self.ALERT_PATTERNS:
            if needle in lowered:
                return code, raw_message

        return "UNKNOWN", raw_message

    def parse_aadhaar_response(self):
        soup = BeautifulSoup(self.html, "html.parser")

        status, message = self.classify_aadhaar_alert(soup)
        print(f"[parse_aadhaar_response] status: {status} | message: {message!r}")

        if status in self.TERMINAL_ERROR_CODES:
            print(f"[parse_aadhaar_response] Terminal error ({status}). Stopping this member's flow.")
            return {"status": status, "message": message, "next_payload": None}

        if status in self.TRANSIENT_ERROR_CODES:
            print(f"[parse_aadhaar_response] Transient error ({status}). Caller should retry after a delay.")
            return {"status": status, "message": message, "next_payload": None}

        form = soup.find("form", {"id": "memberRegistration"})
        if form is None:
            print("[parse_aadhaar_response] No memberRegistration form found; nothing to resubmit.")
            return {"status": status, "message": message, "next_payload": None}

        def get_input_value(input_id):
            tag = soup.find("input", {"id": input_id})
            return tag.get("value", "") if tag else ""

        fields = {
            "aadhaarReferenceNo": get_input_value("aadhaarReferenceNo"),
            "uidToken": get_input_value("uidToken"),
            "uidaiResponseTransId": get_input_value("uidaiResponseTransId"),
            "aadhaarVerificationStatus": get_input_value("aadhaarVerificationStatus"),
            "verificationTimestamp": get_input_value("verificationTimestamp"),
            "currentDob": get_input_value("currentDob"),
            "prevAadhaarDemoVerified": get_input_value("prevAadhaarDemoVerified"),
            "alertMessage": get_input_value("alertMsg"),
            "name": "",
            "dob": "",
            "gender": "",
            "aadhaar": "",
        }

        table = soup.find("table")
        if table:
            for row in table.find_all("tr"):
                cols = row.find_all("td")
                if len(cols) != 2:
                    continue
                key = cols[0].get_text(strip=True)
                value = cols[1].get_text(" ", strip=True)

                if key == "Name":
                    pre = cols[1].find("pre")
                    fields["name"] = pre.get_text(strip=True) if pre else value
                elif key == "Date of Birth":
                    fields["dob"] = value
                elif key == "Gender":
                    fields["gender"] = value
                elif key == "AADHAAR":
                    fields["aadhaar"] = value

        self.aadhaar_fields = fields

        for k, v in fields.items():
            print(f"[parse_aadhaar_response] {k}: {v}")

        next_payload = {
            "isPreviousEmployee": "Y",
            "previousDetails": {"name": fields["name"]},
            "currentDetails": {"dob": fields["currentDob"]},
            "alertMessage": fields["alertMessage"],
            "aadhaarVerificationStatus": fields["aadhaarVerificationStatus"],
            "aadhaarReferenceNo": fields["aadhaarReferenceNo"],
            "verificationTimestamp": fields["verificationTimestamp"],
            "uidToken": fields["uidToken"],
            "uidaiResponseTransId": fields["uidaiResponseTransId"],
            "prevAadhaarDemoVerified": fields["prevAadhaarDemoVerified"],
        }

        if status == "MATCHED":
            ok_button = soup.find("button", class_="btn-modal-ok")
            data_url = ok_button.get("data-url") if ok_button else None
            if data_url:
                params = parse_qs(urlparse(data_url).query)
                self.modify_hdiv = params.get("_MODIFY_HDIV_STATE_", [self.modify_hdiv])[0]
                self.hdiv = params.get("_HDIV_STATE_", [self.hdiv])[0]
            else:
                print("[parse_aadhaar_response] WARNING: btn-modal-ok/data-url not found on "
                      "MATCHED page; falling back to form action HDIV pair (may not work for "
                      "validateMemberDetails)")
                action = form.get("action", "")
                params = parse_qs(urlparse(action).query)
                self.modify_hdiv = params.get("_MODIFY_HDIV_STATE_", [self.modify_hdiv])[0]
                self.hdiv = params.get("_HDIV_STATE_", [self.hdiv])[0]
        else:
            action = form.get("action", "")
            params = parse_qs(urlparse(action).query)
            self.modify_hdiv = params.get("_MODIFY_HDIV_STATE_", [self.modify_hdiv])[0]
            self.hdiv = params.get("_HDIV_STATE_", [self.hdiv])[0]

        print("[parse_aadhaar_response] next MODIFY_HDIV_STATE =", self.modify_hdiv)
        print("[parse_aadhaar_response] next HDIV_STATE        =", self.hdiv)

        return {"status": status, "message": message, "next_payload": next_payload}

    def build_validate_payload(self):
        if not self.last_member_details:
            raise RuntimeError("No previous verify_aadhaar() call found -- nothing to confirm")

        fields = self.aadhaar_fields or {}
        details = self.last_member_details

        return {
            "isPreviousEmployee": "Y",
            "previousDetails": {
                "uan": self.encrypt_field(details["uan"]),
                "name": details["name"],
                "dob": self.encrypt_field(details["dob"]),
                "aadhaar": self.encrypt_field(details["aadhaar"]),
            },
            "currentDetails": {"dob": fields.get("currentDob", details["dob"])},
            "alertMessage": fields.get("alertMessage", ""),
            "aadhaarVerificationStatus": fields.get("aadhaarVerificationStatus", "S"),
            "aadhaarReferenceNo": fields.get("aadhaarReferenceNo", ""),
            "verificationTimestamp": fields.get("verificationTimestamp", ""),
            "isNorthEastMember": None,
            "uidToken": fields.get("uidToken", ""),
            "uidaiResponseTransId": fields.get("uidaiResponseTransId", ""),
            "prevAadhaarDemoVerified": fields.get("prevAadhaarDemoVerified", ""),
            "aadhaarConsent": {"consentStatus": "Y"},
        }

    def validate_member_details(self, payload=None, save_debug=True):
        """
        POST /epfo/uanmember/validateMemberDetails using the current
        self.hdiv/self.modify_hdiv (already correct from the MATCHED
        getAadhaarInfo response). Saves the returned HTML to disk and
        opens it, same pattern as every other step.
        """
        if payload is None:
            payload = self.build_validate_payload()

        url = (
            f"{self.HOST}/epfo/uanmember/validateMemberDetails"
            f"?_MODIFY_HDIV_STATE_={self.modify_hdiv}"
            f"&_HDIV_STATE_={self.hdiv}"
        )

        headers = {
            **self.DEFAULT_HEADERS,
            "Accept": "*/*",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.HOST}/",
            "Origin": self.HOST,
        }

        print("=" * 80)
        print("[validate_member_details] URL:", url)
        print("[validate_member_details] Payload:")
        print(json.dumps(payload, indent=2))

        response = self.session.post(url, headers=headers, json=payload, verify=False)
        print("[validate_member_details] Status:", response.status_code)

        self.html = response.text
        if save_debug:
            # with open("validate_member_details.html", "w", encoding="utf8") as f:
            #     f.write(self.html)
            # w.open("validate_member_details.html")
            print("[validate_member_details] Saved and opened validate_member_details.html")

        return self.html

    @staticmethod
    def _dedupe_duplicate_name_attrs(html):

        def fix_tag(match):
            tag = match.group(0)
            occurrences = list(re.finditer(r'\sname="[^"]*"', tag))
            if len(occurrences) <= 1:
                return tag
            first_end = occurrences[0].end()
            cleaned = tag[:first_end]
            cursor = first_end
            for occ in occurrences[1:]:
                cleaned += tag[cursor:occ.start()]
                cursor = occ.end()
            cleaned += tag[cursor:]
            return cleaned

        return re.sub(r"<input\b[^>]*>", fix_tag, html)

    def parse_member_registration_form(self):

        soup = BeautifulSoup(self._dedupe_duplicate_name_attrs(self.html), "html.parser")

        challenge_tag = soup.find(id="memberChallenge")
        if challenge_tag:
            self.member_challenge = challenge_tag.get_text(strip=True)
            print("[parse_member_registration_form] fresh memberChallenge:", self.member_challenge)

        form = soup.find("form", {"id": "memRegDetails"})
        if form is None:
            raise RuntimeError("memRegDetails form not found -- was this really the validateMemberDetails response?")

        fields = [("isPreviousEmployee", "Y")]
        for tag in form.find_all(["input", "select"]):
            if tag.get("disabled") is not None:
                continue

            if tag.get("id") in ("previousEmployementNo", "previousEmployementYes"):
                continue

            name = tag.get("name")
            if not name:
                continue

            if tag.name == "select":
                selected = tag.find("option", selected=True) or tag.find("option")
                value = selected.get("value", "") if selected else ""
                fields.append((name, value))
                continue

            input_type = (tag.get("type") or "text").lower()
            if input_type in ("submit", "button", "reset", "image"):

                continue
            if input_type in ("checkbox", "radio"):
                if tag.get("checked") is not None:
                    fields.append((name, tag.get("value", "on")))
                continue

            fields.append((name, tag.get("value", "")))

        self.registration_fields = fields

        for name, value in reversed(fields):
            if name == "_HDIV_STATE_":
                self.hdiv = value
                break

        print(f"[parse_member_registration_form] captured {len(fields)} fields; "
              f"_HDIV_STATE_ = {self.hdiv}")
        return fields

    def build_save_payload(self, doj, wages, gender, member_pan=None, member_email=None,
                            marital_status_code=None, qualification_code=None,
                            aadhaar_consent=True):
        if not self.registration_fields:
            raise RuntimeError("Call parse_member_registration_form() first")

        def get_scraped(target_name):
            for n, v in self.registration_fields:
                if n == target_name:
                    return v
            return ""

        effective_gender = (
            get_scraped("currentDetails.genderCode")
            or gender
            or self.aadhaar_fields.get("gender")
        )
        if not effective_gender:
            raise ValueError(
                "gender is required (e.g. 'M', 'F', or 'T') -- neither the "
                "site, the given input, nor the Aadhaar response provided "
                "one, and there is no safe default for this, unlike "
                "marital status or qualification"
            )
        gender_code = effective_gender.strip().upper()[0]  # "Male"/"M" -> "M", etc.
        if gender_code not in ("M", "F", "T"):
            raise ValueError(f"Unrecognized gender value: {effective_gender!r} (expected M/F/T)")

        marital_status = get_scraped("currentDetails.maritalStatusCode") or marital_status_code or "U"
        qualification = get_scraped("currentDetails.qualificationCode") or qualification_code or "6"
        pan_plain = get_scraped("currentDetails.kycDocumentList[0].number") or member_pan
        email_plain = get_scraped("currentDetails.emailId") or member_email

        overrides = {
            "currentDetails.doj": doj,
            "currentDetails.wages": self.encrypt_field(str(wages)) if wages else "",
            "currentDetails.genderCode": gender_code,
            "currentDetails.gender": gender_code,
            "currentDetails.maritalStatusCode": marital_status,
            "currentDetails.qualificationCode": qualification,
            "currentDetails.kycDocumentList[0].number": self.encrypt_field(pan_plain) if pan_plain else "",
            "currentDetails.emailId": self.encrypt_field(email_plain) if email_plain else "",
        }

        result = []
        consumed = set()
        consent_inserted = False
        for name, value in self.registration_fields:
            if name in overrides:
                result.append((name, overrides[name]))
                consumed.add(name)
                continue
            if name == "aadhaarConsent.consentStatus" and aadhaar_consent and not consent_inserted:
                result.append((name, "Y"))
                consent_inserted = True
            result.append((name, value))

        for name, value in overrides.items():
            if name not in consumed:
                result.append((name, value))

        return result

    def save_member_details(self, doj, wages, gender, member_pan=None, member_email=None,
                             marital_status_code=None, qualification_code=None,
                             aadhaar_consent=True, save_debug=True):
        """
        POST /epfo/uanmember/saveMemberDetails (form-urlencoded, NOT
        JSON -- unlike every earlier step). No query params; the HDIV
        state travels entirely in the body as _HDIV_STATE_, already set
        by parse_member_registration_form().
        """
        payload = self.build_save_payload(
            doj=doj, wages=wages, gender=gender, member_pan=member_pan,
            member_email=member_email, marital_status_code=marital_status_code,
            qualification_code=qualification_code, aadhaar_consent=aadhaar_consent,
        )

        url = f"{self.HOST}/epfo/uanmember/saveMemberDetails"

        headers = {
            **self.DEFAULT_HEADERS,
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.HOST}/",
            "Origin": self.HOST,
        }

        print("[save_member_details] Submitting", len(payload), "fields",payload)

        response = self.session.post(url, headers=headers, data=payload, verify=False)
        print("[save_member_details] Status:", response.status_code)

        self.html = response.text
        if save_debug:
            print("[save_member_details] Saved and opened save_member_details.html")
        return self.html

    def check_save_result(self, html=None):
        html = html or self.html
        soup = BeautifulSoup(html, "html.parser")

        success_div = soup.find(id="success")
        if success_div is not None:
            message = success_div.get_text(" ", strip=True)
            return {"status": "SUCCESS", "message": message, "uan": None}

        error_div = soup.find(id="error")
        if error_div is not None:
            message = ""
            script = error_div.find("script")
            if script and script.string:
                m = re.search(r"var\s+err\s*=\s*`([^`]*)`", script.string)
                if m:
                    message = m.group(1).replace("\\n", "\n").strip()
            uan_match = re.search(r"UAN\s+(\d+)", message)
            uan = uan_match.group(1) if uan_match else None
            return {"status": "DUPLICATE", "message": message, "uan": uan}

        return {"status": "UNKNOWN", "message": "", "uan": None}

    def process_member(self, member, max_mismatch_retries=3,
                        max_transient_retries=3, transient_wait_seconds=30):
        member_uan = member["uan"]
        member_name = member["name"]
        member_dob = member["dob"]
        member_aadhar = member["aadhar"]

        def outcome(status, message=""):
            print(f"[process_member] {member_uan}: {status} -- {message}")
            return {"uan": member_uan, "status": status, "message": message}
        self.refresh_previous_employment_form()
        mismatch_attempts = 0
        transient_attempts = 0
        matched = False

        while True:
            self.verify_aadhaar(member_uan, member_name, member_dob, member_aadhar)
            result = self.parse_aadhaar_response()
            status = result["status"]

            if status == "MATCHED":
                matched = True
                break

            if status == "UAN_NOT_FOUND":
                return outcome("UAN_MISMATCH", result["message"])

            if status == "AADHAAR_MISMATCH":
                return outcome("AADHAR_MISMATCH", result["message"])

            if status in self.TRANSIENT_ERROR_CODES:
                transient_attempts += 1
                if transient_attempts > max_transient_retries:
                    return outcome(
                        "VERIFICATION_TIMEOUT",
                        f"gave up after {max_transient_retries} transient-error retries"
                    )
                print(f"[process_member] {member_uan}: {result['message']} -- "
                      f"waiting {transient_wait_seconds}s and retrying "
                      f"({transient_attempts}/{max_transient_retries})...")
                time.sleep(transient_wait_seconds)
                continue

            if status in ("NAME_MISMATCH", "DOB_MISMATCH"):
                mismatch_attempts += 1
                if mismatch_attempts > max_mismatch_retries:
                    return outcome(
                        "MISMATCH_UNRESOLVED",
                        f"gave up after {max_mismatch_retries} {status} retries"
                    )
                scraped = self.aadhaar_fields
                if scraped.get("name"):
                    member_name = scraped["name"]
                if scraped.get("dob"):
                    member_dob = scraped["dob"]
                print(f"[process_member] {member_uan}: {status}, retrying with "
                      f"corrected name={member_name!r} dob={member_dob!r} "
                      f"({mismatch_attempts}/{max_mismatch_retries})")
                continue

            return outcome("UNKNOWN_ERROR", f"unrecognized Aadhaar status {status}: {result['message']}")

        if not matched:
            return outcome("UNKNOWN_ERROR", "Aadhaar verification loop exited without a match")

        # Label 6: pull the fully pre-filled registration form.
        self.validate_member_details()
        self.parse_member_registration_form()

        self.save_member_details(
            doj=member["doj"],
            wages=member["wages"],
            gender=member.get("gender") or self.aadhaar_fields.get("gender"),
            member_pan=member.get("pan") or None,
            member_email=member.get("email") or None,
            marital_status_code=member.get("marital_status") or None,
            qualification_code=member.get("qualification") or None,
        )

        save_result = self.check_save_result()
        if save_result["status"] == "SUCCESS":
            return outcome("ADDED", save_result["message"])
        elif save_result["status"] == "DUPLICATE":
            return outcome("ALREADY_ADDED", save_result["message"])
        else:
            debug_path = f"save_member_details_unknown_{member_uan}_{int(time.time())}.html"
            try:
                with open(debug_path, "w", encoding="utf-8") as f:
                    f.write(self.html)
                print(f"[process_member] {member_uan}: unrecognized response saved to {debug_path}")
            except OSError as e:
                print(f"[process_member] WARNING: could not write debug response file: {e}")
                debug_path = None

            try:
                if debug_path:
                    with open(debug_path, "r", encoding="utf-8") as f:
                        captured_html = f.read()
                    print(f"[process_member] {member_uan}: captured {len(captured_html)} chars for inspection")
            finally:
                if debug_path:
                    try:
                        import os
                        os.remove(debug_path)
                    except OSError:
                        pass

            return outcome("UNKNOWN_ERROR", "unrecognized saveMemberDetails response")

    def run_batch(self, config, captcha_prompt=input):
        self.load_login_page()

        logged_in, attempts = False, 1
        while not logged_in and attempts <= 5:
            captcha = captcha_prompt("Captcha : ")
            logged_in = self.login(config["user_name"], config["password"], captcha)
            if not logged_in:
                attempts += 1
                self.load_login_page()

        if not logged_in:
            raise SystemExit("Login failed after 5 attempts")

        self.get_user_info()
        self.view_registration()  # label 3, once -- first member's HDIV pair

        results = {}
        for i, member in enumerate(config["member"]):
            print("=" * 80)
            print(f"[run_batch] Processing member {i + 1}/{len(config['member'])}: {member['uan']}")

            result = self.process_member(member)
            results[member["uan"]] = result
            saved = result["status"] == "ADDED"

            is_last = i == len(config["member"]) - 1
            if not is_last:
                if saved or result["status"] == "ALREADY_ADDED":
                    self._select_previous_employment_yes()
                else:
                    self.view_registration()

        print("=" * 80)
        print("[run_batch] Summary:")
        for uan, result in results.items():
            print(f"  {uan} - {result['status']}")

        return results

import threading as _threading

import cv2
import numpy as np
import easyocr


class PFCaptcha:
    def __init__(self):
        self.reader = easyocr.Reader(["en"], gpu=False)
        self._lock = _threading.Lock()

    def _straighten_and_zoom(self, img, gap_px=12, zoom_factor=1.15):
        height, width, channels = img.shape

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        _, thresh = cv2.threshold(gray, 75, 255, cv2.THRESH_BINARY_INV)

        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        valid_contours = []
        for cnt in contours:
            if cv2.contourArea(cnt) > 5:
                x, y, w, h = cv2.boundingRect(cnt)
                valid_contours.append((x, y, w, h))
        valid_contours.sort(key=lambda item: item[0])

        extended_width = width * 2
        output_img = np.ones((height, extended_width, channels), dtype=np.uint8) * 255

        target_y = height // 2
        cursor = 20

        for x, y, w, h in valid_contours:
            character_mask = thresh[y:y + h, x:x + w]

            new_w = int(w * zoom_factor)
            new_h = int(h * zoom_factor)
            resized_mask = cv2.resize(character_mask, (new_w, new_h), interpolation=cv2.INTER_AREA)

            new_y = target_y - (new_h // 2)
            output_img[new_y:new_y + new_h, cursor:cursor + new_w][resized_mask > 127] = [0, 0, 0]

            cursor += new_w + gap_px

        final_width = min(cursor + 10, extended_width)
        return output_img[:, :final_width]

    def _preprocess(self, img):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, None, fx=8, fy=8, interpolation=cv2.INTER_CUBIC)
        gray = cv2.fastNlMeansDenoising(gray)
        return cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 2
        )

    def _extract(self, processed):
        results = self.reader.readtext(
            processed,
            detail=0,
            paragraph=False,
            allowlist="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
        )
        return "".join(results)

    def solve(self, captcha_bytes=None, captcha_base64=None):
        """
        Solve a captcha image and return the decoded alphanumeric text,
        or None if OCR couldn't read anything. Pass EXACTLY ONE of
        captcha_bytes (raw image bytes) or captcha_base64.
        """
        if captcha_base64 is not None:
            image_bytes = base64.b64decode(captcha_base64)
        elif captcha_bytes is not None:
            image_bytes = captcha_bytes
        else:
            raise ValueError("Provide either captcha_bytes or captcha_base64")

        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            return None

        with self._lock:
            clean_img = self._straighten_and_zoom(img)
            processed = self._preprocess(clean_img)
            if processed is None:
                return None
            text = self._extract(processed)

        return re.sub(r"[^A-Za-z0-9]", "", text) or None

STATUS_LOG_PHRASES = {
    "ADDED": "added",
    "ALREADY_ADDED": "already added in the portal",
    "AADHAR_MISMATCH": "aadhar mismatch",
    "UAN_MISMATCH": "uan mismatch",
    "MISMATCH_UNRESOLVED": "name/dob mismatch not resolved after retries",
    "VERIFICATION_TIMEOUT": "verification error, gave up after retries",
    "UNKNOWN_ERROR": "unknown error",
}

REQUIRED_MEMBER_FIELDS = ("uan", "name", "aadhar", "dob", "doj", "wages")



class EPFOOnboarding:
    def __init__(self, data, session=None):
        self.data = data
        self.session = session      # live session dict from automation_framework.py
        self.logger = None          # AutomationService instance, set by the framework
        self.pf = EpfoAutomation()
        self.captcha_solver = PFCaptcha()
        self.add_log("Application Started")
        self.add_log(f"Data: {data}")

    # ------------------------------------------------------------------
    def add_log(self, msg):
        if self.logger:
            self.logger.add_log(msg)
        print(msg)

    def set_progress(self, value):
        if self.logger and hasattr(self.logger, "set_progress"):
            self.logger.set_progress(value)

    # ------------------------------------------------------------------
    def _validate_member(self, member):
        """Check a single member dict has everything process_member() needs
        BEFORE calling any API for it -- catches bad input early instead
        of failing partway through a live Aadhaar verification call."""
        missing = [f for f in REQUIRED_MEMBER_FIELDS if not member.get(f)]
        return (len(missing) == 0, missing)

    # ------------------------------------------------------------------
    def run(self):
        try:
            data = self.data
            members = data.get("member") or []
            total_steps = len(members) + 1  # +1 for login/setup

            self.add_log("Login Started")
            self.set_progress(0)
            logged_in = self.pf.login_with_auto_captcha(
                data.get("user_name"), data.get("password"), self.captcha_solver
            )
            if not logged_in:
                raise RuntimeError(
                    "Login failed (bad credentials, or captcha OCR could not solve it after retries)"
                )
            self.add_log("Login Successful")

            self.add_log("Opening Member Registration")
            self.pf.get_user_info()
            self.pf.view_registration()

            completed_steps = 1
            self.add_log("Member Registration Page Ready")
            self.set_progress(round(completed_steps / total_steps * 100, 1))

            for member in members:
                uan = member.get("uan") or "<unknown>"

                is_valid, missing_fields = self._validate_member(member)
                if not is_valid:
                    self.add_log(f"{uan} - invalid member data: missing {', '.join(missing_fields)}")
                    completed_steps += 1
                    self.set_progress(round(completed_steps / total_steps * 100, 1))
                    continue

                try:
                    result = self.pf.process_member(member)
                except Exception as e:
                    result = {"uan": uan, "status": "UNKNOWN_ERROR", "message": str(e)}

                status_code = result.get("status", "UNKNOWN_ERROR")
                phrase = STATUS_LOG_PHRASES.get(status_code, status_code.lower())
                self.add_log(f"{uan} - {phrase}")

                completed_steps += 1
                self.set_progress(round(completed_steps / total_steps * 100, 1))

                if status_code in ("ADDED", "ALREADY_ADDED"):
                    try:
                        self.pf._select_previous_employment_yes()
                    except RuntimeError:
                        self.pf.view_registration()
                else:
                    self.pf.view_registration()
            final_result = {
                "total_members": len(members),
                "logs": self.session["logs"] if self.session else [],
            }
            self.add_log("Completed")
            self.set_progress(100)
            return final_result

        except Exception as e:
            self.add_log(f"Failed: {e}")
            raise