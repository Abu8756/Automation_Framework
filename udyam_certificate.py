"""
Udyam certificate download automation, wired into AutomationFramework.

This used to be its own standalone Flask app (1_udyam_flask_api.py) with its
own SESSIONS dict, its own /start /verify-otp /udyam_pdf /cleanup routes, and
its own idle-session reaper thread. All of that is now provided by
AutomationFramework + AutomationService (session storage, /udyam_certificate
/start /status /otp /delete /logs routes, OTP polling via wait_for_otp(),
progress/result/error reporting) — see the @framework.service("udyam_certificate", ...)
registration in automation_framework.py, which does:

    obj = UdyamCertificate(data=data, service=self)
    return obj.run()

So this module owns only the Selenium + OCR automation itself. It has no
Flask app, no session dict, and no OTP storage of its own — everything that
needs to persist or be polled goes through `self.service` (an
AutomationService instance): self.service.add_log(...), .set_progress(...),
.wait_for_otp(...), etc.
"""

import base64
import os
import time

import cv2
import easyocr
import numpy as np
from selenium import webdriver

from automation_framework import OTPTimeoutError

BASE = "https://udyamregistration.gov.in"
LOGIN_URL = f"{BASE}/Udyam_Login.aspx"
DASHBOARD_URL = f"{BASE}/Udyam_User/Udyam_Dashboard.aspx"
CAPTCHA_URL = f"{BASE}/CaptchaControl.aspx"

DEBUG_DIR = "debug_html"
os.makedirs(DEBUG_DIR, exist_ok=True)

MAX_OTP_VERIFY_ATTEMPTS = 4
OTP_TIMEOUT_SECONDS = 180.0

# Loaded once per process, the first time this module is imported (lazily,
# from inside UdyamCertificateService.run()) — same cost/behaviour as the
# original Flask app's module-level `reader = easyocr.Reader(...)`.
print("Initializing EasyOCR reader...")
reader = easyocr.Reader(["en"], gpu=False)


# ---------------------------------------------------------------------------
# Same JS-driven helpers as the original file: captcha is fetched from
# inside the browser via fetch(), form fields are filled and buttons are
# clicked via execute_script(), and the ASP.NET postback does the actual
# navigation.
# ---------------------------------------------------------------------------

JS_FETCH_IMAGE_AS_BASE64 = """
const callback = arguments[arguments.length - 1];
const url = arguments[0];
fetch(url, { credentials: 'include' })
    .then(r => r.blob())
    .then(blob => {
        const reader = new FileReader();
        reader.onloadend = () => callback(reader.result);
        reader.onerror = (e) => callback('ERROR:' + e);
        reader.readAsDataURL(blob);
    })
    .catch(e => callback('ERROR:' + e));
"""

JS_SET_VALUE = """
const el = document.getElementById(arguments[0]);
if (el) { el.value = arguments[1]; }
return !!el;
"""

JS_CLICK = """
const el = document.getElementById(arguments[0]);
if (el) { el.click(); return true; }
return false;
"""

JS_GET_MESSAGE = """
const els = document.querySelectorAll('[id*="lblUamMsg"]');
if (els.length > 0) { return els[0].innerText || els[0].textContent || ''; }
return '';
"""


def _dump_html(session_id, step, content):
    try:
        fname = os.path.join(DEBUG_DIR, f"{session_id}_{step}_{int(time.time() * 1000)}.html")
        with open(fname, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        print(f"dump_html error: {e}")


def _solve_captcha(img_bytes):
    try:
        nparr = np.frombuffer(img_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            return "000000"
        lower_blue = np.array([100, 40, 10])
        upper_blue = np.array([160, 80, 40])
        mask = cv2.inRange(img, lower_blue, upper_blue)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
        cleaned = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        final = cv2.bitwise_not(cleaned)
        results = reader.readtext(final, detail=0)
        val = "".join(c for c in "".join(results) if c.isalnum()).upper()
        return val or "000000"
    except Exception as e:
        print(f"Captcha error: {e}")
        return "000000"


def _js_fetch_captcha_bytes(driver):
    result = driver.execute_async_script(JS_FETCH_IMAGE_AS_BASE64, CAPTCHA_URL)
    if result.startswith("ERROR:"):
        raise RuntimeError(f"Captcha fetch failed: {result}")
    return base64.b64decode(result.split(",", 1)[1])


def _js_set_value(driver, elem_id, value):
    driver.execute_script(JS_SET_VALUE, elem_id, value)


def _js_click(driver, elem_id):
    driver.execute_script(JS_CLICK, elem_id)


def _js_get_message(driver):
    return driver.execute_script(JS_GET_MESSAGE) or ""


class UdyamCertificate:
    """One instance per automation run. `service` is the AutomationService
    the framework created for this session — it's the only thing this class
    talks to for logging, progress, and OTP (no Flask, no session dict, no
    reaper thread of its own; the framework already provides all of that)."""

    def __init__(self, data: dict, service):
        self.data = data
        self.service = service
        self.session_id = service.session_id
        self.udyam_no = (data.get("udyam_no") or "").strip()
        self.mobile = (data.get("phone") or "").strip()
        self.driver = None

    # ------------------------------------------------------------------
    def run(self):
        options = webdriver.ChromeOptions()
        options.add_argument("--headless=new")
        self.driver = webdriver.Chrome(options=options)

        try:
            self._login_and_request_otp()
            otp_value = self.service.wait_for_otp(
                timeout=OTP_TIMEOUT_SECONDS, driver=self.driver
            )
            self._verify_otp(otp_value)
            pdf_base64 = self._download_pdf()

            self.service.add_log("Udyam certificate PDF ready")
            self.service.set_progress(100)
            return {"status": "done", "pdf_base64": pdf_base64}

        except OTPTimeoutError:
            # wait_for_otp() already quit self.driver in this case.
            self.service.add_log("Udyam automation aborted: OTP not received in time", level="ERROR")
            raise
        finally:
            try:
                self.driver.quit()
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _login_and_request_otp(self):
        driver = self.driver
        self.service.add_log("Opening Udyam login page")
        self.service.set_progress(10)
        driver.get(LOGIN_URL)
        time.sleep(2)
        _dump_html(self.session_id, "1_login_page", driver.page_source)

        self.service.add_log("Solving login captcha")
        captcha_val = _solve_captcha(_js_fetch_captcha_bytes(driver))

        _js_set_value(driver, "ctl00_ContentPlaceHolder1_txtUamNo", self.udyam_no)
        _js_set_value(driver, "ctl00_ContentPlaceHolder1_txtMob", self.mobile)
        _js_set_value(driver, "ctl00_ContentPlaceHolder1_txtCaptcha", captcha_val)
        _js_click(driver, "ctl00_ContentPlaceHolder1_rblUamOtp_0")
        _js_click(driver, "ctl00_ContentPlaceHolder1_btnUamGetOtp")
        time.sleep(3)
        _dump_html(self.session_id, "2_get_otp_response", driver.page_source)

        msg = _js_get_message(driver)
        if "otp" not in msg.lower() or "sent" not in msg.lower():
            raise RuntimeError(f"OTP not triggered: {msg or 'no message from server'}")

        self.service.set_progress(30)
        self.service.add_log(f"OTP sent: {msg}")

    # ------------------------------------------------------------------
    def _verify_otp(self, otp_value):
        driver = self.driver
        logged_in = False

        for attempt in range(1, MAX_OTP_VERIFY_ATTEMPTS + 1):
            self.service.add_log(f"Submitting OTP (attempt {attempt}/{MAX_OTP_VERIFY_ATTEMPTS})")
            captcha_val2 = _solve_captcha(_js_fetch_captcha_bytes(driver))
            _js_set_value(driver, "ctl00_ContentPlaceHolder1_txtUamOtp", otp_value)
            _js_set_value(driver, "ctl00_ContentPlaceHolder1_txtCaptcha1", captcha_val2)
            _js_click(driver, "ctl00_ContentPlaceHolder1_btnValidateUamOtp")
            time.sleep(3)
            _dump_html(self.session_id, f"3_verify_otp_try{attempt}", driver.page_source)

            if "Udyam_Dashboard" in driver.current_url or "lblownername" in driver.page_source:
                logged_in = True
                break

        if not logged_in:
            raise RuntimeError("OTP or captcha incorrect after all attempts")

        self.service.set_progress(60)
        self.service.add_log("Logged in to Udyam dashboard")

    # ------------------------------------------------------------------
    def _download_pdf(self):
        driver = self.driver

        if DASHBOARD_URL not in driver.current_url:
            driver.get(DASHBOARD_URL)
            time.sleep(2)
        _dump_html(self.session_id, "4_dashboard_page", driver.page_source)

        self.service.add_log("Requesting certificate print view")
        self.service.set_progress(75)
        before_handles = driver.window_handles
        _js_click(driver, "ctl00_ContentPlaceHolder1_btnPrintC")
        time.sleep(3)

        after_handles = driver.window_handles
        if len(after_handles) > len(before_handles):
            driver.switch_to.window(after_handles[-1])
            time.sleep(2)

        # The one step that isn't page-JS: window.print() only opens the OS
        # print dialog and can't return bytes, so Page.printToPDF via CDP
        # (what Selenium's print_page() wraps) is what actually produces
        # the PDF.
        self.service.add_log("Rendering certificate to PDF")
        self.service.set_progress(90)
        pdf_result = driver.execute_cdp_cmd("Page.printToPDF", {"printBackground": True})
        return pdf_result["data"]
