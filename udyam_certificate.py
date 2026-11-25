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
OTP_TIMEOUT_SECONDS = 550.0


print("Initializing EasyOCR reader...")
reader = easyocr.Reader(["en"], gpu=False)

ANNEXURE_STYLE = (
    "text-align: center; padding-left: 0%; padding-right: 25%; "
    "page-break-before: always"
)
ANNEXURE_FONT_PX = 13  # try 11 if it spills to a 2nd page, 13 if too small



# Shrink ONLY the annexure if it is wider than the printable width.
JS_ZOOM_ANNEXURE = """
const a = document.getElementById('divAnnx');
if (!a) { return 1; }
a.style.zoom = '';
const cw = a.scrollWidth, maxw = arguments[0];
if (cw > maxw) {
    const z = Math.max(0.4, maxw / cw);
    a.style.zoom = z;
    return z;
}
return 1;
"""

# A4 in inches, and printable width in CSS px (96 dpi)
PAPER_W_IN, PAPER_H_IN, MARGIN_IN = 8.27, 11.69, 0.4
PRINTABLE_PX = int((PAPER_W_IN - 2 * MARGIN_IN) * 96)  # ~717

PRINT_FIX_CSS = f"""
body {{ margin: 0 !important; }}
#divAnnx div {{ overflow: visible !important; }}
#divAnnx, #divAnnx table {{ max-width: 100% !important; }}
#divAnnx, #divAnnx * {{ font-size: {ANNEXURE_FONT_PX}px !important; }}

/* let big/nested tables flow across pages by content */
table, tbody, td {{ break-inside: auto !important; page-break-inside: auto !important; }}

/* only keep small leaf rows (no nested table) from splitting mid-row */
tr:not(:has(table)) {{ break-inside: avoid; page-break-inside: avoid; }}
img {{ break-inside: avoid; }}
"""



# Same thing the site's printAllDiv('divallprint') does, minus window.print()
JS_PREPARE_PRINT_ALL = """
const annx = document.getElementById('divAnnx');
const all = document.getElementById('divallprint');
if (!annx || !all) { return false; }
annx.setAttribute('style', arguments[0]);
document.body.innerHTML = all.innerHTML;
const st = document.createElement('style');
st.textContent = arguments[1];
document.head.appendChild(st);
return true;
"""

JS_CONTENT_WIDTH = """
return Math.max(document.documentElement.scrollWidth, document.body.scrollWidth);
"""

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
    # def _download_pdf(self):
    #     driver = self.driver

    #     if DASHBOARD_URL not in driver.current_url:
    #         driver.get(DASHBOARD_URL)
    #         time.sleep(2)
    #     _dump_html(self.session_id, "4_dashboard_page", driver.page_source)

    #     self.service.add_log("Requesting certificate print view")
    #     self.service.set_progress(75)
    #     before_handles = driver.window_handles
    #     _js_click(driver, "ctl00_ContentPlaceHolder1_btnPrintC")
    #     time.sleep(3)

    #     after_handles = driver.window_handles
    #     if len(after_handles) > len(before_handles):
    #         driver.switch_to.window(after_handles[-1])
    #         time.sleep(2)

    #     # The one step that isn't page-JS: window.print() only opens the OS
    #     # print dialog and can't return bytes, so Page.printToPDF via CDP
    #     # (what Selenium's print_page() wraps) is what actually produces
    #     # the PDF.
    #     self.service.add_log("Rendering certificate to PDF")
    #     self.service.set_progress(90)
    #     pdf_result = driver.execute_cdp_cmd("Page.printToPDF", {"printBackground": True})
    #     return pdf_result["data"]
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

        # Show annexure + restyle + swap body to divallprint (like the site's button)
        prepared = False
        for _ in range(10):
            if driver.execute_script(JS_PREPARE_PRINT_ALL, ANNEXURE_STYLE, PRINT_FIX_CSS):
                prepared = True
                self.service.add_log("Annexure shown and restyled")
                break
            time.sleep(0.5)
        if not prepared:
            self.service.add_log(
                "#divAnnx / #divallprint not found, printing page as-is", level="WARNING"
            )
        _dump_html(self.session_id, "5_certificate_print_page", driver.page_source)

        self.service.add_log("Rendering certificate to PDF")
        self.service.set_progress(90)
        try:
            driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
                "width": PRINTABLE_PX, "height": 1100,
                "deviceScaleFactor": 1, "mobile": False,
            })
            time.sleep(0.5)
            zoom = driver.execute_script(JS_ZOOM_ANNEXURE, PRINTABLE_PX)
            self.service.add_log(f"Annexure zoom {zoom}")
        finally:
            try:
                driver.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})
            except Exception:
                pass

        pdf_result = driver.execute_cdp_cmd("Page.printToPDF", {
            "printBackground": True,
            "displayHeaderFooter": False,   # removes the date/title line at the top
            "paperWidth": PAPER_W_IN,
            "paperHeight": PAPER_H_IN,
            "marginTop": MARGIN_IN, "marginBottom": MARGIN_IN,
            "marginLeft": MARGIN_IN, "marginRight": MARGIN_IN,
            "scale": 1,
        })
        return pdf_result["data"]