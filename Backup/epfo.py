"""
EPFO Unified Portal Automation
------------------------------
Single class, one method per page/step. Each method reads state left behind
by the previous method (self.hdiv, self.modify_hdiv, self.member_challenge,
self.session cookies) and updates it for the next one — mirroring the
working manual flow from the notebook (cells 4 -> 6 -> 8 -> 10 -> 14 -> 15 -> 16).
"""

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

        # Raw bytes of the most recently downloaded captcha image, kept
        # per-instance (not a shared file) -- see load_login_page().
        self.last_captcha_bytes = None

        # Path of the debug captcha image written to disk by
        # load_login_page() (if any) -- kept so login_with_auto_captcha()
        # can delete it once the captcha has actually been solved/used,
        # instead of leaving it on disk permanently.
        self.last_captcha_debug_path = None

        # ---- last submitted previous-employment details, kept around so
        # the final validateMemberDetails confirm call can re-encrypt the
        # SAME uan/dob/aadhaar that were actually accepted ----
        self.last_member_details = {}

        # ---- full member-registration form fields, captured from the
        # validateMemberDetails response (list of (name, value) tuples,
        # in DOM order) -- consumed by build_save_payload() ----
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

        # if save_debug:
        #     with open("epfo_home.html", "w", encoding="utf8") as f:
        #         f.write(self.html)
        #     w.open("epfo_home.html")

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

        # Keep the raw bytes on THIS instance rather than a shared file.
        # captcha_file (bare filename, no session-unique part) used to be
        # written/read by every concurrent session -- two sessions running
        # at once could clobber each other's captcha image mid-solve,
        # which is what made captcha solving fail intermittently rather
        # than consistently. self.last_captcha_bytes has no such race
        # since each session owns its own EpfoAutomation instance.
        self.last_captcha_bytes = captcha_response.content

        # Still write a debug copy to disk (best-effort, session-tagged
        # name) purely for manual inspection -- never read back from here.
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

        # if save_debug:
        #     with open("login_response.html", "w", encoding="utf8") as f:
        #         f.write(self.html)
        #     w.open("login_response.html")

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
        
    # def get_captcha_text(self,image_bytes):
    #     root = tk.Tk()
    #     root.title("Enter CAPTCHA")

    #     # Convert bytes to PIL Image
    #     image = Image.open(BytesIO(image_bytes))

    #     # Convert PIL Image to Tkinter image
    #     photo = ImageTk.PhotoImage(image)

    #     # Display image
    #     img_label = tk.Label(root, image=photo)
    #     img_label.image = photo  # Prevent garbage collection
    #     img_label.pack(pady=10)

    #     # Text input
    #     entry = tk.Entry(root, font=("Arial", 14))
    #     entry.pack(pady=10)

    #     captcha_text = {"value": ""}

    #     def submit():
    #         captcha_text["value"] = entry.get()
    #         root.destroy()

    #     tk.Button(root, text="Submit", command=submit).pack(pady=10)

    #     root.mainloop()

    #     return captcha_text["value"]

    def login_with_auto_captcha(self, username, password, captcha_solver, max_attempts=5):
        """
        Same as load_login_page() + login(), but solves the captcha with
        OCR (captcha_solver, a PFCaptcha instance) instead of prompting a
        human -- this is what a headless API worker uses, since there's
        no one there to type a captcha in.

        Reads self.last_captcha_bytes (set by load_login_page() on THIS
        instance) rather than a shared file on disk -- avoids a race
        where two sessions running concurrently could clobber each
        other's captcha image mid-solve.

        Every attempt is wrapped in try/except: a single OCR failure, a
        transient network error, or an unexpected exception from
        anywhere in this attempt is treated as "this attempt failed,
        try again" rather than aborting the whole retry loop.
        """
        self.load_login_page()

        for attempt in range(1, max_attempts + 1):
            try:
                if not self.last_captcha_bytes:
                    print(f"[login_with_auto_captcha] attempt {attempt}: no captcha image available, retrying...")
                    self.load_login_page()
                    continue
                
                # Pass the captcha image as base64 into the OCR pipeline
                # (site expects/handles it the same as raw bytes, but this
                # keeps the image in transit as base64 rather than a file
                # path).
                captcha_b64 = base64.b64encode(self.last_captcha_bytes).decode("utf-8")
                captcha_text = captcha_solver.solve(captcha_base64=captcha_b64)
                # captcha_text=self.get_captcha_text(self.last_captcha_bytes)
                if not captcha_text:
                    print(f"[login_with_auto_captcha] attempt {attempt}: OCR could not read the captcha, retrying...")
                    self.load_login_page()
                    continue

                # Site-required case pattern before validation: first 2
                # chars upper, next 2 lower, rest unchanged.
                captcha_text = "".join(
                    c.upper() if i < 2 else c.lower() if i < 4 else c
                    for i, c in enumerate(captcha_text)
                )

                print(f"[login_with_auto_captcha] attempt {attempt}: OCR guess = {captcha_text!r}")

                # The debug captcha image on disk has now been used
                # (fed into the OCR solve above) -- delete it rather
                # than leaving it around permanently.
                if self.last_captcha_debug_path:
                    try:
                        os.remove(self.last_captcha_debug_path)
                    except OSError:
                        pass
                    finally:
                        self.last_captcha_debug_path = None

                if self.login(username, password, captcha_text):
                    return True

            except Exception as e:
                # Anything unexpected here (OCR crash, network hiccup,
                # HTML parse error on a malformed response) -- log it and
                # move on to the next attempt instead of propagating and
                # killing the whole login process.
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
        """
        Shared logic: find the previousEmployementYes radio's onclick URL
        in the given HTML (or self.html) and set self.hdiv/self.modify_hdiv
        from it. Used both by view_registration() (label 3, a fresh GET)
        and directly on the response returned by save_member_details()
        (label 7) -- that response is ALREADY a registration page with a
        working previousEmployementYes link, so processing the next
        member doesn't need a separate GET back to label 3 at all.
        """
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
        # if save_debug:
        #     with open("viewmember.html", "w", encoding="utf8") as f:
        #         f.write(self.html)
        #     w.open("viewmember.html")

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

        # This JSON body is required -- without it the server does not
        # advance the form state, which is why your manual GET/POST
        # (with no body) kept returning a stale memberChallenge.
        form_payload = {"isPreviousEmployee": "Y"}

        response_form = self.session.post(
            refresh_form_url,
            headers=headers,
            json=form_payload,
            verify=False,
        )
        print("[refresh_previous_employment_form] Status:", response_form.status_code)

        # if save_debug:
        #     with open("previous_employment_response.html", "w", encoding="utf8") as f:
        #         f.write(response_form.text)
        #     w.open("previous_employment_response.html")

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

        # Remember exactly what we submitted (plaintext) -- the final
        # confirm call (validateMemberDetails) re-encrypts these same
        # values rather than anything scraped from the response.
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
        # if save_debug:
        #     with open("aadhaar_result.html", "w", encoding="utf8") as f:
        #         f.write(self.html)
        #     w.open("aadhaar_result.html")

        return self.html

    # ------------------------------------------------------------------
    # Step 6b: Re-call getAadhaarInfo with the payload scraped from a
    # NAME_MISMATCH / DOB_MISMATCH response (from parse_aadhaar_response's
    # next_payload). Same endpoint, same headers -- only the body and the
    # HDIV pair (already refreshed by parse_aadhaar_response) change.
    # ------------------------------------------------------------------
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
        # if save_debug:
        #     with open("aadhaar_resubmit_result.html", "w", encoding="utf8") as f:
        #         f.write(self.html)
        #     w.open("aadhaar_resubmit_result.html")

        return self.html

    # ------------------------------------------------------------------
    # Step 6c: Drive the mismatch retry loop end-to-end.
    #
    # Call this right after verify_aadhaar(). It:
    #   1. classifies the current self.html via parse_aadhaar_response()
    #   2. if NAME_MISMATCH / DOB_MISMATCH -> resubmit next_payload and
    #      re-classify the new response
    #   3. repeats until MATCHED, a terminal error (UAN_NOT_FOUND /
    #      AADHAAR_MISMATCH), UNKNOWN, or max_retries is hit
    #
    # Returns the final classification dict and prints the payload at
    # each hop.
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Alert classification
    # ------------------------------------------------------------------
    # Map lowercased substrings found in the alert-danger / alert-success
    # div to a stable error code. Order matters: check more specific
    # strings before generic ones.
    ALERT_PATTERNS = [
        ("name mismatch", "NAME_MISMATCH"),
        ("dob mismatch", "DOB_MISMATCH"),
        ("uan details not found", "UAN_NOT_FOUND"),
        ("aadhaar mismatch", "AADHAAR_MISMATCH"),
        ("member details matched", "MATCHED"),
        ("error while verifying uan details", "TRANSIENT_ERROR"),
    ]

    # Error codes that come back with NO resubmittable form -- these are
    # terminal for this member and there is nothing further to POST.
    TERMINAL_ERROR_CODES = {"UAN_NOT_FOUND", "AADHAAR_MISMATCH"}

    # Transient/rate-limited errors -- no form either, but these ARE worth
    # retrying (after a delay) with the exact same payload, since the
    # portal itself asked to "try after some time".
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

    # ------------------------------------------------------------------
    # Step 7: Parse the aadhaar verification result page and build the
    # payload + HDIV pair needed for the NEXT confirmation step.
    #
    # Returns a dict:
    #   {"status": <error_code>, "message": <raw alert text>,
    #    "next_payload": <dict or None>}
    #
    # next_payload is only populated for NAME_MISMATCH / DOB_MISMATCH /
    # MATCHED, since those are the only responses that come back with a
    # resubmittable <form id="memberRegistration">. UAN_NOT_FOUND,
    # AADHAAR_MISMATCH, and TRANSIENT_ERROR have no form at all -- there
    # is nothing to re-post from the page itself.
    # ------------------------------------------------------------------
    def parse_aadhaar_response(self):
        soup = BeautifulSoup(self.html, "html.parser")

        status, message = self.classify_aadhaar_alert(soup)
        print(f"[parse_aadhaar_response] status: {status} | message: {message!r}")

        if status in self.TERMINAL_ERROR_CODES:
            # No form on this page -- nothing to extract or resubmit.
            print(f"[parse_aadhaar_response] Terminal error ({status}). Stopping this member's flow.")
            return {"status": status, "message": message, "next_payload": None}

        if status in self.TRANSIENT_ERROR_CODES:
            # No form either, but this one is retryable -- caller should
            # wait and re-call verify_aadhaar() with the SAME details.
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

        # Two different next-steps need two different HDIV pairs, and
        # they are NOT the same value:
        #
        #  - NAME_MISMATCH / DOB_MISMATCH -> the retry goes back to
        #    getAadhaarInfo, and the pair to use is the form's own
        #    `action` URL (e.g. ...getAadhaarInfo?_MODIFY_HDIV_STATE_=X
        #    &_HDIV_STATE_=Y) -- exactly what was used to render this page.
        #
        #  - MATCHED -> the next call is validateMemberDetails, and the
        #    correct pair is on the "Ok" button's data-url
        #    (btn-modal-ok), e.g. ...validateMemberDetails?
        #    _MODIFY_HDIV_STATE_=A&_HDIV_STATE_=B. This is a DIFFERENT
        #    pair from the form's action -- confirmed against a real
        #    matched response + curl capture (action was 12-82/12-87,
        #    but the working validateMemberDetails call used 12-94/12-95
        #    from the Ok button). Using the form action's pair here is
        #    what would cause validateMemberDetails to fail.
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

    # ------------------------------------------------------------------
    # Step 8: Final confirm call -- /epfo/uanmember/validateMemberDetails
    #
    # Called once getAadhaarInfo returns MATCHED. Re-encrypts the exact
    # uan/dob/aadhaar/name that were actually accepted (self.last_member_details,
    # set by verify_aadhaar) and attaches all the verification metadata
    # scraped by parse_aadhaar_response (aadhaarReferenceNo, uidToken,
    # uidaiResponseTransId, verificationTimestamp, prevAadhaarDemoVerified,
    # aadhaarVerificationStatus, alertMessage) -- matching the confirmed
    # working payload shape from the browser network capture.
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Step 9: Parse the full member-registration form returned by
    # validateMemberDetails, and finally POST /epfo/uanmember/saveMemberDetails
    #
    # This page (form id="memRegDetails") already contains almost
    # everything needed to save the member: name, DOB, gender,
    # nationality, father's name, relation, marital status, mobile,
    # PAN, Aadhaar, and a full KYC document list -- all pre-filled by
    # the portal from the verified Aadhaar/UAN data. Only 2 fields are
    # left for the caller to supply: Date of Joining and Wages (these
    # come back empty in the form; they are not derivable from Aadhaar).
    # ------------------------------------------------------------------
    @staticmethod
    def _dedupe_duplicate_name_attrs(html):
        """
        Several <input> tags in this portal's markup carry TWO `name="..."`
        attributes on the same tag -- an apparent copy/paste artifact in
        their JSP, e.g.:

            <input id="doj" name="currentDetails.doj" ... name="doj" .../>
            <input id="wages" name="currentDetails.wages" ... name="wages" .../>
            <input id="emailId" name="currentDetails.emailId" ... name="emailId" .../>

        Both real browsers and BeautifulSoup's html.parser resolve
        duplicate attributes to whichever one appears LAST, which
        silently replaces the correct Spring-bound field name
        ("currentDetails.doj") with an unbound one ("doj"). The server
        then doesn't recognize the submitted field, drops it, and
        returns a validation-error page for the save call -- which is
        exactly the bug this fixes. This strips every `name="..."`
        after the first one on a given <input> tag, so parsing resolves
        to the correct (first) field name instead.
        """
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
        """
        Serialize <form id="memRegDetails"> the way a real browser would
        on submit:
          - skip any disabled <input>/<select> (browsers never submit
            disabled fields -- several fields here are duplicated as a
            disabled <select> PLUS an enabled hidden <input> with the
            same name; only the hidden one should survive)
          - for checkbox/radio, include only if `checked` is present
          - for <select>, use the selected <option> (or the first option
            if none is marked selected, matching default browser
            behaviour)
          - preserve DOM order, since a few field names appear more than
            once (e.g. aadhaarConsent.consentStatus) and the server
            binds whichever value arrives FIRST for a given name

        Also captures:
          - the fresh <label id="memberChallenge"> on this page (a NEW
            key, different from the one used for getAadhaarInfo/
            validateMemberDetails -- confirmed against a live capture:
            it's what encrypts PAN/email/wages before the final save)
          - the trailing hidden _HDIV_STATE_ (used in the saveMemberDetails
            POST body; there is no _MODIFY_HDIV_STATE_ for this call)

        Stores the result as self.registration_fields (list of (name,
        value) tuples) and returns it.
        """
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

            # These two radios share a malformed duplicate `name` attribute
            # in the raw HTML (one radio's second `name` differs from its
            # first), which confuses generic name-lookup and both end up
            # looking "checked" even though only one really is. This flow
            # is always the previous-employment branch, so it's set once,
            # explicitly, above -- skip both tags here entirely.
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
                # Only one button is ever submitted, and the working
                # capture shows none was included -- the page posts via
                # AJAX (saveMemberRegistration()) without it.
                continue
            if input_type in ("checkbox", "radio"):
                if tag.get("checked") is not None:
                    fields.append((name, tag.get("value", "on")))
                continue

            fields.append((name, tag.get("value", "")))

        self.registration_fields = fields

        # the last _HDIV_STATE_ hidden input in the form is what
        # saveMemberDetails expects in its POST body
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
        """
        Build the final saveMemberDetails body from self.registration_fields,
        applying the overrides confirmed against a real network capture:

          - currentDetails.doj         -> PLAINTEXT, filled in from `doj`
                                           (DD/MM/YYYY). Always empty in
                                           the scraped form -- this is a
                                           manual entry, not Aadhaar data.
          - currentDetails.wages       -> ENCRYPTED with the fresh
                                           memberChallenge. Always empty
                                           in the scraped form.
          - currentDetails.genderCode/.gender, maritalStatusCode,
            qualificationCode, kycDocumentList[0].number (PAN), emailId
                                        -> SITE VALUE HAS PRIORITY. The
                                           validateMemberDetails response
                                           already carries these from the
                                           verified Aadhaar/UAN record (or,
                                           for marital status/qualification,
                                           the portal's own selected
                                           default) -- if the site already
                                           has a non-empty value, that's
                                           what gets submitted, regardless
                                           of what was passed in. The given
                                           input (`gender`, `member_pan`,
                                           etc.) is used ONLY when the
                                           site's own field came back
                                           empty. PAN/email are re-encrypted
                                           with the fresh memberChallenge
                                           either way (the site's plaintext
                                           display value, or the fallback
                                           input) -- only the SOURCE of the
                                           plaintext changes.
          - aadhaarConsent.consentStatus -> a "Y" entry is inserted
                                           immediately before the
                                           existing empty hidden
                                           duplicate. The server binds
                                           the FIRST value it sees for a
                                           repeated form field, so this
                                           ordering is what actually
                                           makes consent register as
                                           given (matches how the
                                           checkbox, when checked in the
                                           real browser, sits ahead of
                                           the hidden fallback in the DOM).

        Returns a list of (name, value) tuples -- pass this directly as
        requests' `data=` so repeated field names are preserved exactly
        like a real form submission (a plain dict would silently drop
        the duplicate).
        """
        if not self.registration_fields:
            raise RuntimeError("Call parse_member_registration_form() first")

        def get_scraped(target_name):
            for n, v in self.registration_fields:
                if n == target_name:
                    return v
            return ""

        # Priority: site's own scraped registration-form value, then
        # whatever the caller explicitly passed in, then the value
        # scraped straight off the getAadhaarInfo response earlier in
        # this member's flow (self.aadhaar_fields["gender"]) -- so a
        # caller no longer has to supply gender manually at all.
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

        # Defensive: if an override's field wasn't present in the scraped
        # form at all (e.g. a value that's normally a hidden field turned
        # out to be missing for some member), append it rather than
        # silently dropping it.
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
            # with open("save_member_details.html", "w", encoding="utf8") as f:
            #     f.write(self.html)
            # w.open("save_member_details.html")
            print("[save_member_details] Saved and opened save_member_details.html")

        return self.html

    # ------------------------------------------------------------------
    # Step 10: Classify what the saveMemberDetails response actually
    # means. Two outcomes confirmed against real captures:
    #
    #   - SUCCESS: a `#success` alert saying "Member record saved
    #     successfully." The form is reset blank (isPreviousEmployee
    #     back to "No") ready for the next member.
    #
    #   - DUPLICATE: the SAME filled-in form is re-rendered (still
    #     isPreviousEmployee="Y") plus an `#error` alert-danger whose
    #     <script> builds a JS template string naming the conflict --
    #     e.g. "Member with name: X ... already exists in pending for
    #     approval transactions.\nPrevious UAN <uan> is already pending
    #     for approval, ...". This is not a bug -- the member is
    #     already sitting in EPFO's pending-approval queue (either from
    #     an earlier run, or a duplicate within this batch).
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Orchestration: process ONE member through labels 4 -> 7.
    #
    # Assumes self.hdiv/self.modify_hdiv are already correctly set for
    # label 4 (either from view_registration() for the very first member,
    # or from _select_previous_employment_yes() run on the previous
    # member's save response for every member after that).
    #
    # Returns a dict: {"uan": ..., "status": <code>, "message": ...}
    # status is one of:
    #   ADDED                -> saved successfully
    #   ALREADY_ADDED        -> duplicate, already pending approval
    #   AADHAR_MISMATCH      -> terminal Aadhaar mismatch
    #   UAN_MISMATCH         -> UAN not found on the portal
    #   MISMATCH_UNRESOLVED  -> name/DOB mismatch, retries exhausted
    #   VERIFICATION_TIMEOUT -> transient "try after some time" errors, retries exhausted
    #   UNKNOWN_ERROR        -> anything else unrecognized
    # ------------------------------------------------------------------
    def process_member(self, member, max_mismatch_retries=3,
                        max_transient_retries=3, transient_wait_seconds=30):
        member_uan = member["uan"]
        member_name = member["name"]
        member_dob = member["dob"]
        member_aadhar = member["aadhar"]

        def outcome(status, message=""):
            print(f"[process_member] {member_uan}: {status} -- {message}")
            return {"uan": member_uan, "status": status, "message": message}

        # Label 4: refresh the previous-employment form -> fresh HDIV +
        # memberChallenge for THIS member.
        self.refresh_previous_employment_form()

        # Label 5: getAadhaarInfo, with auto-retry on mismatch/transient error.
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

        # Label 7: apply the rest of the JSON's per-member fields and save.
        # gender is no longer required in the caller's member dict -- it's
        # already been scraped off the getAadhaarInfo response into
        # self.aadhaar_fields["gender"] (e.g. "FEMALE") by
        # parse_aadhaar_response(). member.get("gender") is only an
        # optional manual override; build_save_payload() itself still
        # prefers the site's own scraped currentDetails.genderCode above
        # both of these.
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
            # Unrecognized response -- capture the raw HTML EPFO actually
            # sent back so the failure can be diagnosed, then delete the
            # file once it has been read (save-then-delete, not kept
            # permanently on disk).
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
                        os.remove(debug_path)
                    except OSError:
                        pass

            return outcome("UNKNOWN_ERROR", "unrecognized saveMemberDetails response")

    # ------------------------------------------------------------------
    # Orchestration: labels 1 -> 3 once, then process every member in
    # the batch, looping back to label 4 via the post-save response's
    # own previousEmployementYes link (NOT a fresh label-3 GET -- the
    # save response is already a registration page with a working link).
    # ------------------------------------------------------------------
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
                    # Both leave self.html as a valid registration page
                    # with a working previousEmployementYes link -- reuse
                    # it directly instead of a new label-3 GET.
                    self._select_previous_employment_yes()
                else:
                    # This member never reached a usable registration
                    # page (mismatch/terminal/transient failure before
                    # label 6-7). Recover with a clean label-3 GET.
                    self.view_registration()

        print("=" * 80)
        print("[run_batch] Summary:")
        for uan, result in results.items():
            print(f"  {uan} - {result['status']}")

        return results



# ======================================================================
# Captcha OCR -- a separate class in this same file, called by
# EPFOOnboarding.run() as an automated step (no human types anything).
#
# Adapted from the original Selenium-driven PFCaptcha: that version
# pulled the captcha image via a browser screenshot. This automation
# never opens a browser at all -- EpfoAutomation.load_login_page()
# downloads the raw captcha image bytes directly via `requests`, and
# that's what gets fed into the same straighten -> zoom -> preprocess
# -> EasyOCR pipeline below.
# ======================================================================
import threading as _threading

import cv2
import numpy as np
import easyocr


class PFCaptcha:
    def __init__(self):
        # gpu=False: this runs inside a headless API worker process,
        # not a dev machine with a GPU guaranteed to be available.
        self.reader = easyocr.Reader(["en"], gpu=False)
        # EasyOCR's Reader is not guaranteed thread-safe for concurrent
        # readtext() calls -- serialize access rather than loading a
        # fresh (expensive) model per session.
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


# ======================================================================
# Orchestrator -- drives one full onboarding run for a Flask session.
# Same constructor/logging convention as your other automations
# (data, session_id, sessions), including per-step `progress` updates
# and a `process()` method that appends timestamped log entries -- with
# one addition: every log entry is also appended to its own log file
# under ./log/{session_id}.log, so logs survive even if the in-memory
# `sessions` dict is ever cleared or the process restarts.
# ======================================================================
import os
import logging as log_module

log_module.basicConfig(
    level=log_module.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# Absolute path anchored to THIS file's own directory, not the process's
# current working directory. A bare relative "log" folder resolves
# differently depending on where the app is launched from (systemd unit,
# gunicorn, a different shell, etc.) -- that mismatch is why log files
# sometimes couldn't be created at all: os.makedirs(LOG_DIR) can raise
# PermissionError/OSError on a directory the process didn't expect to be
# in, and since this used to run at MODULE IMPORT time with no guard,
# that failure would crash the entire app before Flask even started.
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log")

try:
    os.makedirs(LOG_DIR, exist_ok=True)
    _LOG_DIR_AVAILABLE = True
except OSError as e:
    print(f"[epfo] WARNING: could not create log directory {LOG_DIR!r} ({e}). "
          f"Per-session log files will be skipped; in-memory session logs still work.")
    _LOG_DIR_AVAILABLE = False

# Maps EpfoAutomation.process_member()'s status codes to the exact log
# phrasing wanted: "{uan} - added", "{uan} - aadhar mismatch", etc.
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
# NOTE: "gender" intentionally excluded -- it's scraped from the
# getAadhaarInfo response (self.aadhaar_fields["gender"]) once Aadhaar
# verification matches, so the caller's member JSON no longer needs to
# supply it. It remains an OPTIONAL override: if the caller does pass
# "gender", it's still honored as a fallback in build_save_payload()
# (site-scraped value > caller-supplied value > Aadhaar-scraped value).


class EPFOOnboarding:
    def __init__(self, data, session_id, sessions):
        self.data = data
        self.session_id = session_id
        self.sessions = sessions
        self.pf = EpfoAutomation()
        self.captcha_solver = PFCaptcha()
        self.process(log="Application Started")
        self.process(log=f"Data: {data}")

    # ------------------------------------------------------------------
    def process(self, log=None, progress=None, status=None, error=None, result=None):
        if not self.session_id:
            return

        session = self.sessions[self.session_id]

        if status:
            session["status"] = status
        if progress is not None:
            session["progress"] = progress
        if error:
            session["error"] = error
        if result is not None:
            session["result"] = result

        if log:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            session["logs"].append({"time": timestamp, "message": log})
            session["status"] = log
            print(log)

            # Per-session log FILE, appended every time, independent of
            # the in-memory sessions dict. Never let a file-system issue
            # here (permissions, disk full, directory unavailable) take
            # down the automation itself -- the in-memory log above
            # already has this entry regardless.
            if _LOG_DIR_AVAILABLE:
                try:
                    log_path = os.path.join(LOG_DIR, f"{self.session_id}.log")
                    with open(log_path, "a", encoding="utf-8") as f:
                        f.write(f"{timestamp} - {log}\n")
                except OSError as e:
                    print(f"[process] WARNING: could not write to log file for "
                          f"session {self.session_id}: {e}")

        session["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

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

            self.process(log="Login Started", progress=0)
            logged_in = self.pf.login_with_auto_captcha(
                data.get("user_name"), data.get("password"), self.captcha_solver
            )
            if not logged_in:
                raise RuntimeError(
                    "Login failed (bad credentials, or captcha OCR could not solve it after retries)"
                )
            self.process(log="Login Successful")

            self.process(log="Opening Member Registration")
            self.pf.get_user_info()
            self.pf.view_registration()

            completed_steps = 1
            self.process(
                log="Member Registration Page Ready",
                progress=round(completed_steps / total_steps * 100, 1),
            )

            for member in members:
                uan = member.get("uan") or "<unknown>"

                is_valid, missing_fields = self._validate_member(member)
                if not is_valid:
                    self.process(log=f"{uan} - invalid member data: missing {', '.join(missing_fields)}")
                    completed_steps += 1
                    self.process(progress=round(completed_steps / total_steps * 100, 1))
                    continue

                try:
                    result = self.pf.process_member(member)
                except Exception as e:
                    result = {"uan": uan, "status": "UNKNOWN_ERROR", "message": str(e)}

                status_code = result.get("status", "UNKNOWN_ERROR")
                phrase = STATUS_LOG_PHRASES.get(status_code, status_code.lower())
                self.process(log=f"{uan} - {phrase}")

                completed_steps += 1
                self.process(progress=round(completed_steps / total_steps * 100, 1))

                # ADDED / ALREADY_ADDED both leave the response as a valid
                # registration page with a working previousEmployementYes
                # link -- reuse it directly for the next member instead of
                # an extra GET back to the registration page.
                if status_code in ("ADDED", "ALREADY_ADDED"):
                    try:
                        self.pf._select_previous_employment_yes()
                    except RuntimeError:
                        self.pf.view_registration()
                else:
                    self.pf.view_registration()

            final_result = {
                "total_members": len(members),
                "logs": self.sessions[self.session_id]["logs"],
            }
            self.process(log="Completed", progress=100, result=final_result)
            return final_result

        except Exception as e:
            self.process(log="Failed", error=str(e), status="failed")
            raise