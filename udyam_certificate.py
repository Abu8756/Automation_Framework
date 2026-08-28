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

print("Initializing EasyOCR reader for udyam_certificate...")
_reader = easyocr.Reader(["en"], gpu=False)

# ---------------------------------------------------------------------------
# NOTE ON DOM IDS
# ids for txtUamNo/txtMob/rblUamOtp_0/txtCaptcha/btnUamGetOtp/txtUamOtp/
# txtCaptcha1/btnValidateUamOtp are inferred from the original payload field
# names; divPrint/btnPrint match the certificate page's own markup. Confirm
# with devtools if the portal changes.
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

# Reproduces the two DOM-touching lines of the page's own
#   function printDiv(divName) {
#       var printContents = document.getElementById(divName).innerHTML;
#       var originalContents = document.body.innerHTML;
#       document.body.innerHTML = printContents;
#       window.print();
#       document.body.innerHTML = originalContents;
#   }
# minus the window.print() call itself - see _print_certificate() for why.
JS_SWAP_TO_PRINT_DIV = """
const div = document.getElementById(arguments[0]);
if (!div) return false;
if (window.__origBodyHTML === undefined) { window.__origBodyHTML = document.body.innerHTML; }
document.body.innerHTML = div.innerHTML;
return true;
"""

JS_RESTORE_BODY = """
if (window.__origBodyHTML !== undefined) {
    document.body.innerHTML = window.__origBodyHTML;
    return true;
}
return false;
"""


def _dump_html(sid, step, content):
    try:
        fname = os.path.join(DEBUG_DIR, f"{sid}_{step}_{int(time.time() * 1000)}.html")
        with open(fname, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        pass


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
        results = _reader.readtext(final, detail=0)
        val = "".join(c for c in "".join(results) if c.isalnum()).upper()
        return val or "000000"
    except Exception:
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
    """Logs into the Udyam portal for an already-registered UDYAM number and
    downloads the certificate PDF. Called as UdyamCertificate(data=data,
    service=self).run() from UdyamCertificateService.run() in
    automation_framework.py - same shape as Startup_india/EPFOOnboarding.

    All page interaction goes through execute_script()/execute_async_script()
    on a single browser held open for the whole run(); the framework's own
    wait_for_otp() blocks this background thread until POST
    /udyam_certificate/otp delivers the code, so there's no input() anywhere.
    """

    def __init__(self, data: dict, service):
        self.data = data
        self.service = service  # AutomationService: logging/progress/OTP live here
        self.session_id = service.session_id
        self.driver = None

    def run(self):
        udyam_no = self.data["udyam_no"]
        phone = self.data["phone"]

        options = webdriver.ChromeOptions()
        # options.add_argument("--headless=new")
        self.driver = webdriver.Chrome(options=options)
        driver = self.driver

        try:
            self.service.add_log(f"Opening login page: {LOGIN_URL}")
            driver.get(LOGIN_URL)
            time.sleep(2)
            _dump_html(self.session_id, "1_login_page", driver.page_source)
            self.service.set_progress(10)

            self.service.add_log("Fetching captcha via JS fetch() and solving with EasyOCR")
            captcha_val = _solve_captcha(_js_fetch_captcha_bytes(driver))

            _js_set_value(driver, "ctl00_ContentPlaceHolder1_txtUamNo", udyam_no)
            _js_set_value(driver, "ctl00_ContentPlaceHolder1_txtMob", phone)
            _js_set_value(driver, "ctl00_ContentPlaceHolder1_txtCaptcha", captcha_val)
            _js_click(driver, "ctl00_ContentPlaceHolder1_rblUamOtp_0")
            _js_click(driver, "ctl00_ContentPlaceHolder1_btnUamGetOtp")
            time.sleep(3)
            _dump_html(self.session_id, "2_get_otp_response", driver.page_source)
            self.service.set_progress(30)

            msg = _js_get_message(driver)
            self.service.add_log(f"Server response: {msg}")
            if "otp" not in msg.lower() or "sent" not in msg.lower():
                raise RuntimeError(f"OTP not triggered: {msg or 'no message from server'}")

            # Blocks this background thread until a client POSTs
            # {"session_id": ..., "otp": "..."} to /udyam_certificate/otp.
            # Quits the driver itself on a 180s timeout so nothing lingers.
            otp = self.service.wait_for_otp(timeout=180.0, driver=driver)
            self.service.set_progress(40)

            logged_in = False
            for attempt in range(1, 5):
                self.service.add_log(f"Verification attempt {attempt}")
                captcha_val2 = _solve_captcha(_js_fetch_captcha_bytes(driver))
                _js_set_value(driver, "ctl00_ContentPlaceHolder1_txtUamOtp", otp)
                _js_set_value(driver, "ctl00_ContentPlaceHolder1_txtCaptcha1", captcha_val2)
                _js_click(driver, "ctl00_ContentPlaceHolder1_btnValidateUamOtp")
                time.sleep(3)
                _dump_html(self.session_id, f"3_verify_otp_try{attempt}", driver.page_source)

                if "Udyam_Dashboard" in driver.current_url or "lblownername" in driver.page_source:
                    logged_in = True
                    break
                self.service.add_log("Attempt failed, retrying with a fresh captcha", level="WARNING")

            if not logged_in:
                raise RuntimeError("OTP or captcha incorrect after all attempts")

            self.service.set_progress(70)
            self.service.add_log("Logged in - opening the certificate/dashboard page")
            if DASHBOARD_URL not in driver.current_url:
                driver.get(DASHBOARD_URL)
                time.sleep(2)
            _dump_html(self.session_id, "4_dashboard_page", driver.page_source)

            self.service.set_progress(85)
            self.service.add_log("Rendering the certificate via the page's own Print button")
            pdf_base64 = self._print_certificate()

            self.service.set_progress(100)
            self.service.add_log("Certificate captured")
            return {"pdf_base64": pdf_base64}

        except OTPTimeoutError:
            # wait_for_otp() already quit the driver and logged the timeout.
            raise
        except Exception as e:
            self.service.add_log(f"Error: {e}", level="ERROR")
            raise
        finally:
            if self.driver is not None:
                try:
                    self.driver.quit()
                except Exception:
                    pass

    def _print_certificate(self):
        """Runs the same DOM step as the certificate page's own
        onclick="printDiv('divPrint')" button - swapping document.body.innerHTML
        to the divPrint content - via execute_script, but captures the result
        with Page.printToPDF over CDP instead of letting the button's own
        window.print() run. window.print() only opens the OS print dialog and
        can't hand PDF bytes back to the script; CDP produces exactly what
        choosing "Save as PDF" in that dialog would, without a human to click
        it. The body is restored afterwards either way."""
        driver = self.driver
        driver.execute_script(JS_SWAP_TO_PRINT_DIV, "divPrint")
        time.sleep(1)
        try:
            pdf_result = driver.execute_cdp_cmd("Page.printToPDF", {"printBackground": True})
            return pdf_result["data"]
        finally:
            driver.execute_script(JS_RESTORE_BODY)
