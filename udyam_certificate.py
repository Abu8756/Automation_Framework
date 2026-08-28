import base64
import os
import time

import cv2
import easyocr
import numpy as np
from selenium import webdriver

# ---------------------------------------------------------------------------
# This module implements the actual browser automation for the
# "udyam_certificate" service registered in automation_framework.py. The
# framework only ever touches this file through UdyamCertificate(data,
# service).run() - no Flask routes, sessions, or OTP storage live here;
# all of that is delegated back to `service` (an AutomationService
# instance), same pattern as startup_india.py / epfo.py.
# ---------------------------------------------------------------------------

BASE = "https://udyamregistration.gov.in"
LOGIN_URL = f"{BASE}/Udyam_Login.aspx"
DASHBOARD_URL = f"{BASE}/Udyam_User/Udyam_Dashboard.aspx"
PRINT_APPLICATION_URL = f"{BASE}/Udyam_User/Udyam_PrintApplication.aspx"
CAPTCHA_URL = f"{BASE}/CaptchaControl.aspx"

DEBUG_DIR = "debug_html"
os.makedirs(DEBUG_DIR, exist_ok=True)

print("Initializing EasyOCR reader (udyam_certificate)...")
reader = easyocr.Reader(["en"], gpu=False)

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


def dump_html(sid, step, content):
    try:
        fname = os.path.join(DEBUG_DIR, f"{sid}_{step}_{int(time.time() * 1000)}.html")
        with open(fname, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        print(f"dump_html error: {e}")


def solve_captcha(img_bytes):
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


def js_fetch_captcha_bytes(driver):
    result = driver.execute_async_script(JS_FETCH_IMAGE_AS_BASE64, CAPTCHA_URL)
    if result.startswith("ERROR:"):
        raise RuntimeError(f"Captcha fetch failed: {result}")
    return base64.b64decode(result.split(",", 1)[1])


def js_set_value(driver, elem_id, value):
    driver.execute_script(JS_SET_VALUE, elem_id, value)


def js_click(driver, elem_id):
    driver.execute_script(JS_CLICK, elem_id)


def js_get_message(driver):
    return driver.execute_script(JS_GET_MESSAGE) or ""


class UdyamCertificate:
    """One-shot Udyam certificate automation: login -> OTP -> dashboard ->
    print page -> PDF. Driven entirely by `service` for logging, progress,
    and blocking on the OTP the user submits via POST /udyam_certificate/otp."""

    def __init__(self, data: dict, service):
        self.udyam_no = data["udyam_no"]
        self.mobile = data["phone"]
        self.service = service
        self.session_id = service.session_id
        self.driver = None

    def run(self):
        options = webdriver.ChromeOptions()
        options.add_argument("--headless=new")
        self.driver = webdriver.Chrome(options=options)
        driver = self.driver

        try:
            self.service.add_log("Opening Udyam login page")
            driver.get(LOGIN_URL)
            time.sleep(2)
            dump_html(self.session_id, "1_login_page", driver.page_source)
            self.service.set_progress(10)

            self._login_and_request_otp(driver)
            self.service.set_progress(30)

            otp = self.service.wait_for_otp(timeout=180.0, driver=driver)
            self.service.set_progress(40)

            self._verify_otp(driver, otp)
            self.service.set_progress(70)

            pdf_base64 = self._download_certificate_pdf(driver)
            self.service.set_progress(95)

            self.service.add_log("Udyam certificate PDF captured")
            return {"udyam_no": self.udyam_no, "pdf_base64": pdf_base64}

        finally:
            try:
                driver.quit()
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _login_and_request_otp(self, driver):
        self.service.add_log("Solving login captcha")
        captcha_val = solve_captcha(js_fetch_captcha_bytes(driver))

        js_set_value(driver, "ctl00_ContentPlaceHolder1_txtUamNo", self.udyam_no)
        js_set_value(driver, "ctl00_ContentPlaceHolder1_txtMob", self.mobile)
        js_set_value(driver, "ctl00_ContentPlaceHolder1_txtCaptcha", captcha_val)
        js_click(driver, "ctl00_ContentPlaceHolder1_rblUamOtp_0")
        js_click(driver, "ctl00_ContentPlaceHolder1_btnUamGetOtp")
        time.sleep(3)
        dump_html(self.session_id, "2_get_otp_response", driver.page_source)

        msg = js_get_message(driver)
        if "otp" not in msg.lower() or "sent" not in msg.lower():
            raise RuntimeError(f"OTP not triggered: {msg or 'no message from server'}")

        self.service.add_log(f"OTP requested: {msg}")

    def _verify_otp(self, driver, otp):
        logged_in = False
        for attempt in range(1, 5):
            self.service.add_log(f"Verifying OTP, attempt {attempt}")
            captcha_val2 = solve_captcha(js_fetch_captcha_bytes(driver))
            js_set_value(driver, "ctl00_ContentPlaceHolder1_txtUamOtp", otp)
            js_set_value(driver, "ctl00_ContentPlaceHolder1_txtCaptcha1", captcha_val2)
            js_click(driver, "ctl00_ContentPlaceHolder1_btnValidateUamOtp")
            time.sleep(3)
            dump_html(self.session_id, f"3_verify_otp_try{attempt}", driver.page_source)

            if "Udyam_Dashboard" in driver.current_url or "lblownername" in driver.page_source:
                logged_in = True
                break

        if not logged_in:
            raise RuntimeError("OTP or captcha incorrect after all attempts")

        self.service.add_log("Logged in to Udyam dashboard")

    def _download_certificate_pdf(self, driver):
        if DASHBOARD_URL not in driver.current_url:
            driver.get(DASHBOARD_URL)
            time.sleep(2)
        dump_html(self.session_id, "4_dashboard_page", driver.page_source)

        # Step 1: click "Print Certificate" on the dashboard - an ASP.NET
        # postback/navigation (not page-JS) that lands on, or opens a new
        # window for, Udyam_PrintApplication.aspx.
        before_handles = driver.window_handles
        js_click(driver, "ctl00_ContentPlaceHolder1_btnPrintC")
        time.sleep(3)

        after_handles = driver.window_handles
        if len(after_handles) > len(before_handles):
            driver.switch_to.window(after_handles[-1])
            time.sleep(2)

        dump_html(self.session_id, "5_print_application_page", driver.page_source)

        if PRINT_APPLICATION_URL not in driver.current_url:
            raise RuntimeError(
                f"Expected to land on {PRINT_APPLICATION_URL}, "
                f"but current URL is {driver.current_url}"
            )

        # Step 2: on that page, click its own Print button (id="btnPrint")
        # - its onclick runs printDiv('divPrint'), which swaps
        # document.body.innerHTML down to just the divPrint content (and,
        # in a real browser, calls window.print()). We only need that
        # body-swap side effect, so we click it via JS like a user would.
        js_click(driver, "btnPrint")
        time.sleep(2)
        dump_html(self.session_id, "6_after_print_div_swap", driver.page_source)

        # window.print() (called inside printDiv) only opens the OS print
        # dialog and can't return bytes on its own, so Page.printToPDF via
        # CDP (what Selenium's print_page() wraps) is what actually
        # captures the now-print-only-content page as a PDF.
        pdf_result = driver.execute_cdp_cmd("Page.printToPDF", {"printBackground": True})
        return pdf_result["data"]
