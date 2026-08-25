from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.chrome.options import Options
import time
import pyperclip
import os
import datetime
import json
import logging
import colorlog
import requests
import re

handler = colorlog.StreamHandler()
formatter = colorlog.ColoredFormatter(
    "%(log_color)s%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    log_colors={
        'DEBUG': 'cyan',
        'INFO': 'green',
        'WARNING': 'yellow',
        'ERROR': 'red',
        'CRITICAL': 'bold_red',
    }
)
handler.setFormatter(formatter)
log = colorlog.getLogger()
log.addHandler(handler)
log.setLevel(logging.INFO)

Startup_india_path=r"C:\mca-filing-dev\file_dir\din_pf"


class OTPTimeoutError(Exception):
    """Raised when an OTP (or set of OTPs) doesn't arrive/validate within the
    given timeout window. Caught separately from other exceptions so the
    caller knows this specific failure means "safe to retry the run",
    not "something in the form filling broke"."""
    pass


class SessionStatus:
    """Plain string labels used inside log messages — this class holds no
    session state of its own. All actual session storage, progress,
    logging, error, and result tracking lives in the AutomationFramework
    (automation_framework.py); this file never touches a session dict or
    a log file directly — everything goes through `self.service`."""
    STARTING = "STARTING"
    LOGIN_SUCCESS = "LOGIN_SUCCESS"
    WAITING_FOR_OTP = "WAITING_FOR_OTP"
    OTP_RECEIVED = "OTP_RECEIVED"
    OTP_VERIFIED = "OTP_VERIFIED"
    FORM_FILLING = "FORM_FILLING"
    SAVED_AS_DRAFT = "SAVED_AS_DRAFT"
    SUBMITTED = "SUBMITTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

class Startup_india:

    def __init__(self, data, service):
        self.data = data
        # `service` is the StartupIndiaService (AutomationService) instance
        # that created this run. It is the ONLY place session state,
        # progress, logging, error/result storage, and OTP storage live —
        # this class holds none of that itself, it just calls back into
        # the framework through this object.
        self.service = service
        self.session_id = service.session_id  # read-only, handy for log text

    def download_file(self,driver):
        print("Download File section")
        try:
            max_retry = 10
            retry = 0
            while retry < max_retry:
                try:
                    approved = WebDriverWait(driver,10).until(EC.visibility_of_element_located((By.XPATH,"//span[normalize-space()='Approved']")))
                    self.service.set_progress(93)
                    self.service.add_log(str("Approved status visible"))
                
                    download_buttons = driver.find_elements(By.XPATH,"//span[normalize-space()='Download']")
                    if download_buttons:
                        self.service.set_progress(94)
                        self.service.add_log(str("Download button visible"))
                        
                        download_buttons[0].click()
                        self.service.set_progress(95)
                        self.service.add_log(str("Download started"))
                        break
                    else:
                        self.service.set_progress(93)
                        self.service.add_log(str("Download button not visible, refreshing page"))
                except Exception as e:
                    self.service.set_progress(93)
                    self.service.add_log(str("Waiting for elements..."+str(e)))
                retry += 1
                driver.refresh()
                time.sleep(5)
            self.service.set_progress(98)
            self.service.add_log(str("Download the Certificate with base64"))
            
            return self.get_file_response(driver)
        except Exception as e:
            self.service.set_progress(100)
            self.service.add_log(str(f"{retry} times try to download but cannot Download But Application is Submitted"))
            self.service.set_error(str(e))



    def get_file_response(self,driver):
        # Enable Network logging
        driver.execute_cdp_cmd("Network.enable", {})

        def drain_logs():
            entries = []
            try:
                for entry in driver.get_log("performance"):
                    try:
                        msg = json.loads(entry["message"])["message"]
                        entries.append(msg)
                    except Exception:
                        pass
            except Exception:
                pass
            return entries

        def get_body(request_id):
            try:
                result = driver.execute_cdp_cmd(
                    "Network.getResponseBody",
                    {"requestId": request_id}
                )
                return result.get("body", "")
            except Exception:
                return ""

        TARGET_URL = "/protocol/openid-connect/token"

        requests_data = {}
        captures = []

        deadline = time.time() + 20

        while time.time() < deadline:

            for msg in drain_logs():

                method = msg.get("method", "")

                if method == "Network.requestWillBeSent":

                    request = msg["params"]["request"]

                    if TARGET_URL in request["url"]:

                        request_id = msg["params"]["requestId"]

                        requests_data[request_id] = {
                            "url": request["url"],
                            "method": request["method"],
                            "headers": request.get("headers", {}),
                            "postData": request.get("postData", "")
                        }

                elif method == "Network.responseReceived":

                    response = msg["params"]["response"]

                    if TARGET_URL in response["url"]:

                        request_id = msg["params"]["requestId"]

                        body = get_body(request_id)

                        try:
                            response_json = json.loads(body)
                        except Exception:
                            response_json = body

                        captures.append({
                            "request": requests_data.get(request_id, {}),
                            "response": {
                                "url": response["url"],
                                "status": response["status"],
                                "headers": response.get("headers", {}),
                                "body": response_json
                            }
                        })

            time.sleep(0.2)

        if not captures:
            raise Exception("No token captured.")

        access_token = captures[-1]["response"]["body"]["access_token"]

        # ---------------- Fetch Org Profile ----------------
        headers = {
            "accept": "application/json, text/plain, */*",
            "authorization": f"Bearer {access_token}",
            "content-type": "application/json",
            "referer": "https://www.nsws.gov.in/portal/caf",
            "user-agent": "Mozilla/5.0"
        }

        response = requests.get(
            "https://www.nsws.gov.in/gateway/user/investor/org/fetchOrgProfile/",
            headers=headers
        )
        response.raise_for_status()

        owner = response.json()["data"]["owner"]

        # ---------------- Fetch Document ----------------
        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "authorization": f"Bearer {access_token}",
            "access-id": "M_TTN001",
            "access-secret": "TtnTest@1234",
            "api-key": "ApiKeyGDV1@45",
            "origin": "https://www.nsws.gov.in",
            "referer": "https://www.nsws.gov.in/",
            "user-agent": "Mozilla/5.0"
        }

        payload = {
            "contentId": [
                f"{owner}-MDOC000099-#DFLT#-$ORGID$-1"
            ]
        }

        response = requests.post(
            "https://www.nodal-authority.nsws.gov.in/nsws_document/getDocumentV1",
            headers=headers,
            json=payload
        )
        response.raise_for_status()

        response_json = response.json()

        file_response = response_json["response"][0]["fileResponse"]

        return file_response

    # def base64_file(self,driver):
    #     try:
    #         print("Base64 File section")
    #         # download_buttons = driver.find_elements(By.XPATH,"//span[normalize-space()='Download']")
    #         self.session_manager.update(progress=0,log="Download the Certificate with base64")
    #         before_files = set(os.listdir(self.download_dir))
    #         # download_buttons[0].click()
    #         self.session_manager.update(progress=0,log="Download started")
    #         download_complete = False
    #         file_path = None

    #         for i in range(120):
    #             print(f"{i} - Seconds")
    #             time.sleep(1)

    #             after_files = set(os.listdir(self.download_dir))
    #             new_files = after_files - before_files

    #             if new_files:
    #                 for file_name in new_files:
    #                     if file_name.endswith(".crdownload"):
    #                         continue

    #                     file_path = os.path.join(self.download_dir, file_name)
    #                     size1 = os.path.getsize(file_path)
    #                     time.sleep(1)
    #                     size2 = os.path.getsize(file_path)

    #                     if size1 == size2:
    #                         download_complete = True
    #                         break

    #             if download_complete:
    #                 break
    #         time.sleep(2)
    #         print("Downloaded file")
    #         if download_complete and file_path:

    #             self.session_manager.update(progress=0,log=f"Downloaded file: {file_path}")
    #             with open(file_path, "rb") as f:
    #                 encoded = base64.b64encode(f.read()).decode("utf-8")

    #             # base_name = os.path.basename(file_path)
    #             # txt_file_path = os.path.join(self.download_dir, base_name + ".txt")

    #             # with open(txt_file_path, "w") as f:
    #             #     f.write(encoded)

    #             #self.session_manager.update(progress=0,log=f"Base64 saved to {txt_file_path}")
    #             # print(encoded)
    #             self.session_manager.update(log="Download File Base64",result=f"{encoded}")
    #             return encoded

    #         else:
    #             raise Exception("Download did not complete")
    #     except Exception as e:
    #         self.session_manager.update(log="Download did not complete",error=str(e))
    
    
    def clipboard(self,value):
        pyperclip.copy(value)
        time.sleep(1)
        
    def wait_loader(self, driver, timeout=120):
        end_time = time.time() + timeout

        while time.time() < end_time:
            hidden = driver.execute_script("""
                const loader = document.getElementById('loader-wrapper');
                if (!loader) return true;

                const style = window.getComputedStyle(loader);

                return (
                    style.display === 'none' ||
                    style.visibility === 'hidden' ||
                    style.opacity === '0' ||
                    loader.hidden ||
                    loader.offsetParent === null
                );
            """)

            if hidden:
                return True

            time.sleep(0.2)

        return False
    
    def wait_loader1(self,driver, timeout=120):
        try:
            WebDriverWait(driver, timeout).until(
                EC.invisibility_of_element_located(
                    (By.ID, "loader-wrapper")
                )
            )
            #print("Loader disappeared")
            return True

        except Exception:
            #print("Loader still visible after timeout")
            return False

    def startup_india(self):
        driver = None
        try:
            #print("Chrome Started")
            self.download_dir = os.path.join(Startup_india_path,str(self.session_id))
            os.makedirs(self.download_dir,exist_ok=True)
            
            self.service.set_progress(3)
            self.service.add_log(str("Program started"))
            error = None
            #print("ddta Stroing")
            data = self.data or {}
        
            options = Options()
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_argument("--start-maximized")
            options.add_argument("--disable-popup-blocking")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            # options.add_argument("--headless=new")
            options.add_argument("--window-size=1920,1080")
            options.add_argument("--start-maximized")

            # Set download path
            prefs = {
                "download.default_directory": self.download_dir,
                "download.prompt_for_download": False,
                "download.directory_upgrade": True,
                "safebrowsing.enabled": True
            }
            prefs["plugins.always_open_pdf_externally"] = True
            options.add_experimental_option("prefs", prefs)
            options.set_capability("goog:loggingPrefs", {
                "performance": "ALL"
            })
            driver = webdriver.Chrome(options=options)
            driver.maximize_window()
            
            # driver.get("https://www.nsws.gov.in/")     
            driver.get("https://www.nsws.gov.in/portal/login")      

            wait = WebDriverWait(driver, 20)
            self.service.set_progress(5)
            self.service.add_log(str("Page opened"))
            company_name = self.data.get("business", "company").replace(" ", "_").replace("/", "_")

            try:
                username_value = self.data.get("username")
                password_value = self.data.get("password")
            except Exception as e:
                pass
            

            wait.until(EC.visibility_of_element_located((By.ID, "username"))).clear()
            wait.until(EC.visibility_of_element_located((By.ID, "username"))).send_keys(username_value)
            

            wait.until(EC.visibility_of_element_located((By.ID, "userPassword"))).clear()
            wait.until(EC.visibility_of_element_located((By.ID, "userPassword"))).send_keys(password_value)
            

            wait.until(EC.element_to_be_clickable((By.ID, "kc-login"))).click()
            self.service.set_progress(8)
            self.service.add_log(str("Credentials fetched"))

            try:
                WebDriverWait(driver, 5).until(EC.visibility_of_element_located((By.ID, "input-error-email-login")))
                self.service.set_progress(100)
                self.service.add_log(str(SessionStatus.FAILED) + ": " + str("Credentials is Wrong"))
                self.service.set_error(str("Credentials is Wrong"))
                driver.quit()
                log.error("Login failed")
            except TimeoutException:
                self.service.set_progress(10)
                self.service.add_log(str("Login successful"))

                attempts_left=0
                attempts=0
                LOGIN_OTP_REGEX = re.compile(r"^\d{6}$")  # Login OTP: exactly 6 numeric digits

                while True:
                    if attempts==3:
                        self.service.set_progress(100)
                        self.service.add_log(str(SessionStatus.FAILED) + ": " + str("Three Attempts are Failed"))
                        raise Exception("Three Attempts are Incorrect OTP.")

                    # Wait for the Login OTP (OTP #1) to arrive on the session.
                    # wait_for_otp() polls the session, silently discards any
                    # malformed value it sees (so a fresh one still gets a
                    # chance), and — if 280s pass with nothing valid — quits
                    # the driver and raises OTPTimeoutError so this run fails
                    # cleanly and can be retried instead of hanging forever.
                    otp = self.service.wait_for_otp(
                        otp_type="login",
                        timeout=280,
                        poll_interval=2,
                        pattern=LOGIN_OTP_REGEX,
                        driver=driver,
                    )

                    self.service.add_log(str(SessionStatus.OTP_RECEIVED))
                    self.service.add_log(str("Login OTP received and validated"))

                    try:
                        error_block = WebDriverWait(driver, 3).until(EC.visibility_of_element_located((By.ID, "otp-error-block")))
                        if error_block.is_displayed():
                            message = error_block.text.strip()
                            if "OTP expired" in message:
                                raise Exception("OTP expired")
                    except TimeoutException:
                        pass

                    # Find all 6 OTP inputs
                    inputs = WebDriverWait(driver, 10).until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "input.otp-input")))

                    if len(inputs) != 6:
                        raise Exception(f"Expected 6 OTP inputs, found {len(inputs)}")

                    # Enter each digit
                    for i, digit in enumerate(otp):
                        inputs[i].clear()
                        inputs[i].send_keys(digit)

                    # Click Verify & Continue
                    verify_button = WebDriverWait(driver, 10).until(
                        EC.element_to_be_clickable(
                            (By.CSS_SELECTOR, "button.registerLoginBtn")
                        )
                    )

                    verify_button.click()
                    time.sleep(5)

                    # Consuming the OTP is already handled inside
                    # self.service.wait_for_otp() (consume=True by default),
                    # via the framework's clear_otp() — no local state to
                    # clear here.

                    try:
                        error = WebDriverWait(driver, 5).until(EC.visibility_of_element_located((By.ID, "loginErrorMessage")))
                        message = error.text.strip()
                        match = re.search(r"(\d+)\s+attempts?\s+left", message)

                        if match:
                            attempts_left = int(match.group(1))
                            self.service.add_log(str(f"Incorrect Login OTP, attempts left: {attempts_left}"))
                            attempts += 1
                            continue
                    except TimeoutException:
                        pass
                    break

                wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, ".content-list"))).click()
                time.sleep(20)
                WebDriverWait(driver, 300).until(EC.visibility_of_element_located((By.XPATH, "//span[text()='My Dashboard']")))
                # WebDriverWait(driver, 300).until(EC.element_to_be_clickable((By.XPATH, "//button[normalize-space()='Apply Now']")))
                if driver.find_elements(By.CSS_SELECTOR, ".button.action-button"):
                    self.service.set_progress(12)
                    self.service.add_log(str("Apply button show"))
                    btn1 = wait.until(EC.presence_of_element_located((By.XPATH, "//button[normalize-space()='Apply Now']")))
                    driver.execute_script("arguments[0].click();", btn1)

                    # WebDriverWait(driver, 20).until(EC.element_to_be_clickable((By.XPATH, ))).click()
                    # wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".button.action-button"))).click()
                    time.sleep(10)
                    self.wait_loader(driver)
                else:
                    log.warning("Apply button not show")
                    approved = driver.find_elements(By.XPATH,"//span[normalize-space()='Approved']")
                    if approved:
                        file = self.download_file(driver)
                        self.service.set_progress(100)
                        self.service.add_log(str(SessionStatus.COMPLETED) + ": " + str("Approved status visible"))
                        self.service.set_error(str("Already Application is Approved"))
                        # result_code=self.base64_file(driver) 
                        driver.quit()         
                                    
                        return {"status":200,"message":"Success completed","timestamp":datetime.datetime.now().isoformat()}
                        # raise Exception("This Already created because download button is visible")
                    else:
                        main_window = driver.current_window_handle
                        # Open DPIIT page directly in new tab
                        driver.execute_script("window.open('https://www.nsws.gov.in/portal/approval-details/ministry-of-commerce-and-industry/dpiit/startup-recognition-by-dpiit','_blank');")
                        # Switch to new tab
                        wait.until(lambda d: len(d.window_handles) > 1)
                        for window in driver.window_handles:
                            if window != main_window:
                                driver.switch_to.window(window)
                                break
                        # Click Add to Dashboard                        
                        # wait.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(.,'Add to Dashboard')]"))).click()
                        btn = wait.until(EC.presence_of_element_located((By.XPATH, "//button[normalize-space()='Add to Dashboard']")))
                        driver.execute_script("arguments[0].click();", btn)
                        print("Add After")
                        time.sleep(3)
                        # Close the tab
                        driver.close()
                        # Back to main window
                        driver.switch_to.window(main_window)
                        # Refresh dashboard
                        
                        driver.refresh()
                        # Wait for dashboard to load
                        WebDriverWait(driver, 300).until(EC.visibility_of_element_located((By.XPATH, "//span[text()='My Dashboard']")))
                        # main_window = driver.current_window_handle                        
                        # wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "div.search-icon"))).click()
                        # search_box = wait.until(EC.presence_of_element_located((By.NAME, "global_search")))
                        # search_box.clear()
                        # for ch in "StartUp Recognition by DPIIT":
                        #     search_box.send_keys(ch)
                        #     time.sleep(0.2)
                        # try:
                        #     wait.until(EC.element_to_be_clickable((By.XPATH, "//li[contains(text(),'StartUp Recognition by DPIIT')]"))).click()
                        # except:
                        #     search_box.clear()
                        #     for ch in "Registration as a Startup":
                        #         search_box.send_keys(ch)
                        #         time.sleep(0.2)
                        #     wait.until(EC.element_to_be_clickable((By.XPATH, "//li[contains(text(),'Registration as a Startup')]"))).click()
                        # wait.until(EC.number_of_windows_to_be(2))
                        # for window in driver.window_handles:
                        #     if window != main_window:
                        #         driver.switch_to.window(window)
                        #         break
                        # wait.until(EC.element_to_be_clickable((By.XPATH, "//button[normalize-space()='Add to Dashboard']"))).click()
                        # driver.close()
                        # driver.switch_to.window(main_window)
                        # driver.refresh()
                        time.sleep(10)
                        wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".button.action-button"))).click()
                        self.service.set_progress(15)
                        self.service.add_log(str("Apply button clicked"))
                print("Add Before")
                apply_buttons = driver.find_elements(By.CSS_SELECTOR, ".button.action-button")
                print("Add After")
                print(driver.get_cookies())
                self.service.set_progress(18)
                self.service.add_log(str("Start Page started"))
                selector = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "div.document-type-control .ant-select-selector")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", selector)
                selector.click()
                wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "div.ant-select-dropdown")))
                search_input = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "input.ant-select-selection-search-input")))
                search_input.send_keys(Keys.CONTROL + "a")
                search_input.send_keys("Company Logo")
                search_input.send_keys(Keys.ENTER)

                browse_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//button[.//span[text()='Browse File']]")))
                browse_btn.click()

                file_input = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@role='dialog']//input[@type='file']")))

                logo_base64 = self.data.get("lOGO_file")
                if logo_base64:
                    # self.download_dir = os.path.expanduser("~/Automation_tempfiles/Startup_india")-------------------------------------------------
                    # os.makedirs(self.download_dir, exist_ok=True)

                    file_path = os.path.join(self.download_dir, f"{company_name}_logo.png")

                    with open(file_path, "wb") as f:
                        f.write(base64.b64decode(logo_base64))

                    file_input.send_keys(file_path)
                    time.sleep(3)

                about_text = self.data.get("aboutcompany")
                self.clipboard(about_text)
                time.sleep(1)
                print("111")

                # textarea = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "textarea.ant-input.caf-textarea-control")))
                # textarea = wait.until(EC.element_to_be_clickable((By.XPATH, "//textarea[contains(@name,'about_start_up')]")))
                # textarea.click()
                textarea = wait.until(
                    EC.presence_of_element_located(
                        (By.XPATH, "//textarea[contains(@name,'about_start_up')]")
                    )
                )

                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});",
                    textarea
                )

                time.sleep(1)

                textarea.click()
                textarea.send_keys(Keys.CONTROL, "v")
                # textarea.send_keys(Keys.CONTROL, "v")
                print("222")

                website_value = self.data.get("website")
                self.clipboard(website_value)
                website_input = wait.until(EC.visibility_of_element_located((By.ID, "Website")))
                website_input.clear()
                website_input.send_keys(Keys.CONTROL, "v")
                time.sleep(1)
                print("333")

                # wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "div[name^='you_are_interested_in'] .ant-select-selector"))).click()
                # dropdown = wait.until(EC.presence_of_element_located((By.XPATH, "(//input[@role='combobox'])[2]")))
                dropdown = wait.until(EC.presence_of_element_located((By.XPATH, "//div[starts-with(@name,'you_are_interested_in')]//div[contains(@class,'ant-select-selector')]")))
                # driver.execute_script("arguments[0].click();", dropdown)
                driver.execute_script("arguments[0].click();", dropdown)
                
                print("444")
                driver.execute_script("""
                    let options = document.querySelectorAll('.ant-select-tree-title');

                    options.forEach(opt => {
                        if (['Investors','Mentors','Other Startups','Incubators','Accelerators'].includes(opt.innerText.trim())) {
                            opt.click();
                        }
                    });
                    """)

                # options = ["Investors", "Mentors", "Other Startups", "Incubators", "Accelerators"]
                # for option in options:
                #     print("44.4.4")
                #     wait.until(EC.element_to_be_clickable((By.XPATH, f"//span[normalize-space()='{option}']"))).click() 
                #     driver.execute_script("arguments[0].scrollIntoView({block:'center'});",option_click)
                #     time.sleep(1)
                #     print("44.4.4....")
                    # option_click.click()               
                
                print("555")
                wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "div.nsws-field.form-declaration")))
                print("666")
                driver.execute_script("""document.querySelector('.ant-checkbox-inner').click();""")
                # terms_label = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "label.ant-checkbox-wrapper")))
                # driver.execute_script("arguments[0].click();", terms_label)
                print("777")
                if os.path.exists(file_path):
                    os.remove(file_path)

                element = wait.until(EC.element_to_be_clickable((By.XPATH,"//span[normalize-space()='Start Up Profile']/ancestor::div[@class='ant-collapse-header']")))

                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
                time.sleep(1)
                # Click
                element.click()
                time.sleep(2)
                self.service.set_progress(20)
                self.service.add_log(str("Start Up Profile Complete"))
                self.service.set_progress(22)
                self.service.add_log(str("Entity Details Started"))
                element=wait.until(EC.element_to_be_clickable((By.XPATH, "//span[normalize-space()='Entity Details']/ancestor::div[contains(@class,'ant-collapse-header')]")))
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", element)
                time.sleep(1)
                print("8888")
                industry = self.data.get("Industry")
                element = driver.find_element(By.XPATH,"//div[starts-with(@name,'industry_')]")
                driver.execute_script("arguments[0].click();", element)
                print("9999")
                time.sleep(1)
                # driver.execute_script("""
                # let industry = arguments[0];

                # document.querySelector("div[name^='industry_'] .ant-select-selector").click();

                # setTimeout(() => {
                #     document.querySelectorAll('.ant-select-item-option-content').forEach(el => {
                #         if (el.innerText.trim() === industry) {
                #             el.click();
                #         }
                #     });
                # }, 500);
                # """, industry)
                print("1010")
                industry = self.data.get("Industry")

                driver.execute_script("""
                const value = arguments[0];

                document.querySelector("div[name^='industry_'] .ant-select-selector").click();

                setTimeout(() => {

                    const input = document.querySelector("div[name^='industry_'] input.ant-select-selection-search-input");

                    const setter = Object.getOwnPropertyDescriptor(
                        HTMLInputElement.prototype,
                        'value'
                    ).set;

                    setter.call(input, value);
                    input.dispatchEvent(new Event('input', { bubbles: true }));

                    setTimeout(() => {
                        document.querySelectorAll(".ant-select-item-option-content").forEach(e => {
                            if (e.textContent.trim() === value) {
                                e.click();
                            }
                        });
                    }, 300);

                }, 300);
                """, industry)
                print("Industry Selcted")
                print("1111")
                # wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "div[name^='industry_'] .ant-select-selector"))).click()
                # search_box = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "div[name^='industry_'] input.ant-select-selection-search-input")))
                # search_box.send_keys(self.data.get("Industry"))
                # wait.until(EC.element_to_be_clickable((By.XPATH, "//div[contains(@class,'ant-select-item-option-content') and normalize-space()='{}']".format(self.data.get('Industry'))))).click()
                time.sleep(3)
                sector = self.data.get("Sector")

                driver.execute_script("""
                const value = arguments[0];

                document.querySelector("div[name^='sector_'] .ant-select-selector").click();

                setTimeout(() => {

                    const input = document.querySelector("div[name^='sector_'] input.ant-select-selection-search-input");

                    const setter = Object.getOwnPropertyDescriptor(
                        HTMLInputElement.prototype,
                        'value'
                    ).set;

                    setter.call(input, value);
                    input.dispatchEvent(new Event('input', { bubbles: true }));

                    setTimeout(() => {
                        document.querySelectorAll(".ant-select-item-option-content").forEach(e => {
                            if (e.textContent.trim() === value) {
                                e.click();
                            }
                        });
                    }, 300);

                }, 300);
                """, sector)

                # sector = self.data.get("Sector")
                # element = driver.find_element(By.XPATH,"//div[starts-with(@name,'sector_')]")
                # driver.execute_script("arguments[0].click();", element)
                time.sleep(3)

                # driver.execute_script("""
                # let input = document.querySelector("div[name^='sector_'] input[type='search']");
                # let nativeSetter = Object.getOwnPropertyDescriptor(
                #     window.HTMLInputElement.prototype,
                #     'value'
                # ).set;

                # nativeSetter.call(input, arguments[0]);
                # input.dispatchEvent(new Event('input', { bubbles: true }));
                # """, sector)
                # driver.execute_script("""
                # let sector = arguments[0];

                # let option = [...document.querySelectorAll('.ant-select-item-option-content')]
                #     .find(el => el.textContent.trim() === sector);

                # if(option){
                #     option.click();
                # }
                # """, sector)
                
                # wait.until(EC.element_to_be_clickable((By.XPATH, "//input[@id='rc_select_6']/ancestor::div[contains(@class,'ant-select-selector')]"))).click()
                # driver.switch_to.active_element.send_keys(self.data.get("Sector"))
                # print("1111")
                # wait.until(EC.element_to_be_clickable((By.XPATH, "//div[contains(@class,'ant-select-item-option-content') and normalize-space()='{}']".format(self.data.get('Sector'))))).click()
                # time.sleep(1)
                print("1212")
                wait.until(EC.presence_of_element_located((By.XPATH,"//div[contains(@class,'ant-select') and starts-with(@name,'categories_')]//div[contains(@class,'ant-select-selector')]")))
                print("1313")
                options_to_select = self.data.get("Catogries")

                dropdown = wait.until(EC.element_to_be_clickable((By.XPATH, "//label[.//span[text()='Categories']]/following::div[contains(@class,'ant-select-selector')][1]")))
                driver.execute_script("arguments[0].click();", dropdown)

                time.sleep(1)
                search_inputs = wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "input.ant-select-selection-search-input")))

                search_input = search_inputs[5]  

                for option in options_to_select:
                    search_input.send_keys(option)
                    option_xpath = f"//span[contains(@class,'ant-select-tree-title') and normalize-space()='{option}']"
                    option_element = wait.until(EC.element_to_be_clickable((By.XPATH, option_xpath)))
                    driver.execute_script("arguments[0].click();", option_element)
                    time.sleep(0.5)
                    search_input.clear()

                driver.execute_script("document.body.click();")
                value = 0
                print("1414")
                option = "Yes" if value == 1 else "No"
                self.service.set_progress(25)
                self.service.add_log(str(f"Options:{option}"))
                if value==1:
                    self.service.set_progress(28)
                    self.service.add_log(str("Yes"))
                    radio_button = wait.until(EC.element_to_be_clickable((By.XPATH,f"(//label[.//input[@type='radio' and @value='Yes']])[1]")))
                else:
                    self.service.set_progress(30)
                    self.service.add_log(str("No"))
                    radio_button = wait.until(EC.element_to_be_clickable((By.XPATH,f"(//label[.//input[@type='radio' and @value='No']])[1]")))
                print("1515")
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", radio_button)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", radio_button)
                time.sleep(1)
                print("1616")
                elements=wait.until(EC.element_to_be_clickable((By.XPATH, "//span[normalize-space()='Entity Details']/ancestor::div[contains(@class,'ant-collapse-header')]")))
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", elements)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", elements)
                print("1717")
                time.sleep(1)
                self.service.set_progress(32)
                self.service.add_log(str("Entity Details Completed"))
                self.service.set_progress(34)
                self.service.add_log(str("Full Address(Office) Started"))
                element=wait.until(EC.element_to_be_clickable((By.XPATH, "//span[normalize-space()='Full Address(Office)']/ancestor::div[contains(@class,'ant-collapse-header')]")))
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
                print("1818")
                time.sleep(1)
                driver.execute_script("arguments[0].click();", element)
                # element.click()
                time.sleep(1)
                print("1818.1")
                element = wait.until(EC.presence_of_element_located((By.XPATH, "//div[starts-with(@name,'state_')]//div[contains(@class,'ant-select-selector')]")))
                driver.execute_script("arguments[0].click();", element)
                
                
                state = self.data.get("comp_address", {}).get("state")
                driver.execute_script("""
                const input = document.querySelector("input.ant-select-selection-search-input");
                input.value = arguments[0];
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
                """, state)
                
                
                
                # state = self.data.get("comp_address", {}).get("state")
                # # Open dropdown
                # dropdown = wait.until(
                #     EC.presence_of_element_located(
                #         (By.XPATH, "//div[starts-with(@name,'state_')]//div[contains(@class,'ant-select-selector')]")
                #     )
                # )
                # driver.execute_script("arguments[0].click();", dropdown)

                # # Type state in search box
                # search = wait.until(
                #     EC.presence_of_element_located(
                #         (By.XPATH, "//input[contains(@class,'ant-select-selection-search-input')]")
                #     )
                # )

                # driver.execute_script("""
                # arguments[0].focus();
                # arguments[0].value = arguments[1];
                # arguments[0].dispatchEvent(new Event('input', {bubbles:true}));
                # arguments[0].dispatchEvent(new Event('change', {bubbles:true}));
                # """, search, state)

                # # Wait for option and click with JavaScript
                # option = wait.until(
                #     EC.presence_of_element_located(
                #         (By.XPATH, f"//div[contains(@class,'ant-select-item-option-content') and normalize-space()='{state}']")
                #     )
                # )

                # driver.execute_script("arguments[0].click();", option)
                
                
                
                # wait.until(EC.element_to_be_clickable((By.XPATH, "//div[starts-with(@name,'state_')]//div[contains(@class,'ant-select-selector')]"))).click()
                # print("1919")
                # state=self.data.get("comp_address", {}).get("state")
                # self.clipboard(state)
                # driver.switch_to.active_element.send_keys(Keys.CONTROL, "v")
                # print("2020")
                # element=wait.until(EC.element_to_be_clickable((By.XPATH, "//div[contains(@class,'ant-select-item-option-content') and normalize-space()='{}']".format(state))))
                # driver.execute_script("arguments[0].click();", element)
                # print("2121")
                # # time.sleep(2)
                # element=wait.until(EC.element_to_be_clickable((By.XPATH, "//div[starts-with(@name,'district_')]//div[contains(@class,'ant-select-selector')]")))
                # driver.execute_script("arguments[0].click();", element)
                # print("2222")
                # district=self.data.get("comp_address", {}).get("district")
                # self.clipboard(district)
                # driver.switch_to.active_element.send_keys(Keys.CONTROL, "v")
                # print("2323")
                # element=wait.until(EC.element_to_be_clickable((By.XPATH, "//div[contains(@class,'ant-select-item-option-content') and normalize-space()='{}']".format(district)))).click()
                # driver.execute_script("arguments[0].click();", element)
                # time.sleep(2)
                # print("2424")
                print("1818.1")
                district = self.data["comp_address"]["district"]
                # Open the district dropdown
                driver.execute_script("""document.querySelector("div[name^='district_'] .ant-select-selector").click();""")

                # Set the search text
                driver.execute_script("""
                const input = document.querySelector("div[name^='district_'] input.ant-select-selection-search-input");
                input.focus();
                input.value = arguments[0];
                input.dispatchEvent(new Event('input', { bubbles: true }));
                """, district)

                # Click the matching option
                driver.execute_script("""
                const options = [...document.querySelectorAll('.ant-select-item-option-content')];
                const option = options.find(o => o.textContent.trim() === arguments[0]);
                if (option) {
                    option.parentElement.click();
                }
                """, district)
                print("1919")
                city_value = self.data.get("comp_address", {}).get("city")
                self.clipboard(city_value)
                city_input = wait.until(EC.visibility_of_element_located((By.ID, "City/Village")))
                city_input.clear()
                city_input.send_keys(Keys.CONTROL, "v")
                print("2020")
                pin_value = self.data.get("comp_address", {}).get("pincode")
                self.clipboard(pin_value)
                pin_input = wait.until(EC.visibility_of_element_located((By.ID, "Pin Code")))
                pin_input.clear()
                pin_input.send_keys(Keys.CONTROL, "v")
                print("2121")
                time.sleep(1)

                element = wait.until(EC.presence_of_element_located((By.XPATH, "//span[normalize-space()='Full Address(Office)']/ancestor::div[contains(@class,'ant-collapse-header')]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
                time.sleep(1)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", element)
                time.sleep(1)
                print("3131")
                self.service.set_progress(36)
                self.service.add_log(str("Full Address(Office) Completed"))
                self.service.set_progress(37)
                self.service.add_log(str("Authorized Representative Details Started"))

                element=wait.until(EC.element_to_be_clickable((By.XPATH, "//span[normalize-space()='Authorized Representative Details']/ancestor::div[contains(@class,'ant-collapse-header')]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
                time.sleep(1)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", element)
                time.sleep(1)
                print("3232")
                wait = WebDriverWait(driver, 20)

                name_value = self.data.get("directors", [{}])[0].get("Name")
                self.clipboard(name_value)
                wait.until(EC.visibility_of_element_located((By.ID, "Name"))).clear()
                wait.until(EC.visibility_of_element_located((By.ID, "Name"))).send_keys(name_value)#Keys.CONTROL, "v")
                print("3232.00")
                designation_value = self.data.get("designation")
                self.clipboard(designation_value)
                wait.until(EC.visibility_of_element_located((By.ID, "Designation"))).clear()
                wait.until(EC.visibility_of_element_located((By.ID, "Designation"))).send_keys(designation_value)#Keys.CONTROL, "v")
                print("3232.11")
                mobile_value = self.data.get("mobile")
                self.clipboard(mobile_value)
                wait.until(EC.visibility_of_element_located((By.ID, "Mobile Number"))).clear()
                wait.until(EC.visibility_of_element_located((By.ID, "Mobile Number"))).send_keys(mobile_value)#Keys.CONTROL, "v")
                print("3232.22")
                email_value = self.data.get("email_id") 
                self.clipboard(email_value)
                wait.until(EC.visibility_of_element_located((By.ID, "Email Address"))).clear()
                wait.until(EC.visibility_of_element_located((By.ID, "Email Address"))).send_keys(email_value)#Keys.CONTROL, "v")
                print("3232.3333")
                time.sleep(5)

                element=wait.until(EC.element_to_be_clickable((By.ID,"CheckMobileVerification")))
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", element)
                time.sleep(5)
                print("clicked THe otp")
                
                mobile_otp_input=wait.until(EC.presence_of_element_located((By.XPATH,"//label[.//span[text()='Mobile Number']]/following::input[@type='password'][1]")))
                mobile_otp_input.clear()
                mobile_otp_input.send_keys("")
                
                print("3232.333")                
                element=wait.until(EC.element_to_be_clickable((By.ID,"CheckEmailVerification")))
                element=driver.find_element(By.ID,"CheckEmailVerification")
                element.click();
                time.sleep(10)
                print("3232.444")
                print("Waiting for OTP")
                # wait_for_multi_otp() polls both slots together, discards any
                # malformed value while continuing to wait for a fresh one,
                # and — if 280s pass without both being valid — quits the
                # driver and raises OTPTimeoutError so this run fails cleanly
                # and can be retried instead of hanging forever.
                otp_values = self.service.wait_for_multi_otp(
                    ["mobile", "email"],
                    timeout=280,
                    poll_interval=1,
                    driver=driver,
                )
                mobile = otp_values["mobile"]
                email = otp_values["email"]
                self.service.add_log(str("Mobile OTP and Email OTP received and validated"))
                time.sleep(2)
                print("3434")
                mobile_otp_input=wait.until(EC.presence_of_element_located((By.XPATH,"//label[.//span[text()='Mobile Number']]/following::input[@type='password'][1]")))
                mobile_otp_input.clear()
                mobile_otp_input.send_keys(mobile)
                print("3535")
                email_otp_input=wait.until(EC.presence_of_element_located((By.XPATH,"//label[.//span[text()='Email Address']]/following::input[@type='password'][1]")))
                email_otp_input.send_keys(email)
                print("3636")
                validate_buttons = wait.until(EC.presence_of_all_elements_located((By.XPATH, "//button[normalize-space()='Validate']")))
                for btn in validate_buttons:
                    time.sleep(2)
                    driver.execute_script("arguments[0].removeAttribute('disabled')", btn)
                    btn.click()
                    time.sleep(1)
                    self.wait_loader(driver)
                self.service.add_log(str(SessionStatus.OTP_VERIFIED))
                print("3737")
                
                # log.critical("Otp will be triggered")
                # wait.until(EC.element_to_be_clickable((By.ID, "CheckMobileVerification"))).click()
                # time.sleep(2)
                # wait.until(lambda d: d.find_element(By.ID, "CheckEmailVerification").is_enabled())
                # email_btn = driver.find_element(By.ID, "CheckEmailVerification")
                # driver.execute_script("arguments[0].scrollIntoView({block:'center'});", email_btn)
                # driver.execute_script("document.body.click();")
                # wait.until(lambda d: email_btn.is_displayed())
                # email_btn.click()

                # mobile_otp = input("Enter Mobile OTP: ")
                # email_otp  = input("Enter Email OTP: ")

                # mobile_otp_input = wait.until(EC.presence_of_element_located((By.XPATH, "//label[.//span[text()='Mobile Number']]/following::input[@type='password'][1]")))
                # driver.execute_script("arguments[0].removeAttribute('disabled')", mobile_otp_input)
                # mobile_otp_input.clear()
                # mobile_otp_input.send_keys(mobile_otp)
                # log.critical(f"Mobile OTP entered: {mobile_otp}")
                # email_otp_input = wait.until(EC.presence_of_element_located((By.XPATH, "//label[.//span[text()='Email Address']]/following::input[@type='password'][1]")))
                # driver.execute_script("arguments[0].removeAttribute('disabled')", email_otp_input)
                # email_otp_input.clear()
                # email_otp_input.send_keys(email_otp)
                # log.critical(f"Email OTP entered: {email_otp}")
                # validate_buttons = wait.until(EC.presence_of_all_elements_located((By.XPATH, "//button[normalize-space()='Validate']")))
                # for btn in validate_buttons:
                #     time.sleep(2)
                #     driver.execute_script("arguments[0].removeAttribute('disabled')", btn)
                #     btn.click()
                print("3838")
                element=wait.until(EC.element_to_be_clickable((By.XPATH, "//span[normalize-space()='Authorized Representative Details']/ancestor::div[contains(@class,'ant-collapse-header')]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
                time.sleep(1)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", element)
                time.sleep(1)
                print("3939")
                time.sleep(2)
                self.service.set_progress(38)
                self.service.add_log(str("Director(s) / Partner(s) Details"))
                self.service.set_progress(40)
                self.service.add_log(str("Director(s) / Partner(s) Details Started"))

                no_of_dir = len(self.data.get("directors"))
                wait = WebDriverWait(driver, 20)
                flag=True

                for i in range(no_of_dir):
                    print("4040")

                    director_sections = wait.until(EC.presence_of_all_elements_located((By.XPATH, "//div[contains(@class,'ant-collapse-item')][.//span[contains(text(),'Director(s) / Partner(s) Details')]]")))

                    section = director_sections[i]

                    header = section.find_element(By.XPATH,".//div[contains(@class,'ant-collapse-header')]")

                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", header)
                    driver.execute_script("arguments[0].click();", header)

                    # Wait until active
                    wait.until(lambda d: "active" in section.find_element(By.XPATH,".//div[contains(@class,'ant-collapse-content')]").get_attribute("class"))

                    content = section.find_element(By.XPATH,".//div[contains(@class,'ant-collapse-content-active')]")

                    dir_name=self.data.get("directors", [{}])[i].get("Name", f"Director {i+1}")
                    self.clipboard(dir_name)
                    name_input = content.find_element(By.XPATH, ".//input[contains(@name,'name_')]")
                    name_input.clear()
                    name_input.send_keys(Keys.CONTROL, "v")
                    time.sleep(1)

                    gender_box = content.find_element(By.XPATH, ".//div[starts-with(@name,'gender_')]")
                    selector = gender_box.find_element(By.CLASS_NAME, "ant-select-selector")
                    driver.execute_script("arguments[0].click();", selector)

                    gender_input = gender_box.find_element(By.XPATH,".//input[contains(@class,'ant-select-selection-search-input')]")

                    gender_input.send_keys(self.data.get("directors", [{}])[i].get("Gender", "Male"))
                    gender_input.send_keys(Keys.RETURN)
                    time.sleep(1)

                    dir_mobile=self.data.get("directors", [{}])[i].get("Mobile_no")
                    self.clipboard(dir_mobile)
                    mobile_input = content.find_element(By.XPATH, ".//input[@name='phoneNumber']")
                    mobile_input.clear()
                    mobile_input.send_keys(Keys.CONTROL, "v")
                    time.sleep(1)

                    dir_address=self.data.get("directors", [{}])[i].get("Address")
                    self.clipboard(dir_address)
                    postal_input = content.find_element(By.XPATH, ".//input[contains(@name,'postal_address')]")
                    postal_input.clear()
                    postal_input.send_keys(Keys.CONTROL, "v")
                    time.sleep(3)

                    dir_dob=self.data.get("directors", [{}])[i].get("Dob")
                    self.service.set_progress(42)
                    self.service.add_log(str(f"Dob:{dir_dob}"))
                    dob_field = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.XPATH,f"(//input[@placeholder='DD/MM/YYYY'])[{i+2}]")))
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", dob_field)
                    time.sleep(2)
                    dob_field.click()
                    dob_field.clear()
                    dob_field.send_keys(dir_dob)
                    time.sleep(1)
                    
                    dir_email=self.data.get("directors", [{}])[i].get("Email")
                    self.clipboard(dir_email)
                    email_input = content.find_element(By.XPATH, ".//input[contains(@name,'email_address')]")
                    email_input.clear()
                    email_input.send_keys(Keys.CONTROL, "v")
                    time.sleep(1)

                    if flag==True:
                        flag=False
                        for j in range(no_of_dir-1):
                            add_button = wait.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(text(),'+ Add Section')]")))
                            driver.execute_script("arguments[0].click();", add_button)
                            time.sleep(1)

                    driver.execute_script("arguments[0].click();", header)
                print("4141")
                self.service.set_progress(45)
                self.service.add_log(str("Director(s) / Partner(s) Details Completed"))
                self.service.set_progress(47)
                self.service.add_log(str("Information required Started"))
                element=wait.until(EC.element_to_be_clickable((By.XPATH, "//span[normalize-space()='Information required']/ancestor::div[contains(@class,'ant-collapse-header')]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
                time.sleep(1)
                print("4242")
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", element)
                time.sleep(1)
                print("4343")
                employees_value = self.data.get("no_of_emp") #input("Enter Current Number of Employees (including founders): ")
                employees_input = wait.until(EC.visibility_of_element_located((By.ID, "Current Number of Employees(including founders)")))
                employees_input.clear()
                employees_input.send_keys(employees_value)
                print("4444")
                # Open dropdown (click parent selector)
                wait.until(EC.element_to_be_clickable((By.XPATH, "//input[@id='rc_select_12']/ancestor::div[contains(@class,'ant-select-selector')]"))).click()
                # Type Ideation
                stage=self.data.get("stage")
                driver.switch_to.active_element.send_keys(stage.title())
                # Select Ideation option
                wait.until(EC.element_to_be_clickable((By.XPATH, "//div[contains(@class,'ant-select-item-option-content') and normalize-space()='{}']".format(stage.title())))).click()
                value =  0
                option = "Yes" if value == 1 else "No"
                self.service.set_progress(50)
                self.service.add_log(str(f"Options:{option}"))
                print("4545")
                if value==1:
                    self.service.set_progress(52)
                    self.service.add_log(str("Yes"))
                    radio_button = wait.until(EC.element_to_be_clickable((By.XPATH,f"(//label[.//input[@type='radio' and @value='Yes']])[2]")))
                else:
                    self.service.set_progress(54)
                    self.service.add_log(str("No"))
                    radio_button = wait.until(EC.element_to_be_clickable((By.XPATH,f"(//label[.//input[@type='radio' and @value='No']])[2]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", radio_button)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", radio_button)                
                element=wait.until(EC.element_to_be_clickable((By.XPATH, "//span[normalize-space()='Information required']/ancestor::div[contains(@class,'ant-collapse-header')]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
                time.sleep(1)
                print("4646")
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", element)
                time.sleep(1)
                print("4747")
                self.service.set_progress(56)
                self.service.add_log(str("Information required Completed"))
                self.service.set_progress(58)
                self.service.add_log(str("Nature of Startup Started"))
                nature_header = wait.until(EC.presence_of_element_located((By.XPATH, "//span[normalize-space()='Nature of Startup']/ancestor::div[contains(@class,'ant-collapse-header')]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", nature_header)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", nature_header)
                print("4848")
                wait.until(EC.element_to_be_clickable((By.XPATH, "//div[starts-with(@name,'please_define_nature_of_your_startup_')]//div[contains(@class,'ant-select-selector')]"))).click()
                driver.switch_to.active_element.send_keys("Innovative and Scalable")
                wait.until(EC.element_to_be_clickable((By.XPATH, "//div[contains(@class,'ant-select-item-option-content') and normalize-space()='Innovative and Scalable']"))).click()
                nature_header = wait.until(EC.presence_of_element_located((By.XPATH, "//span[normalize-space()='Nature of Startup']/ancestor::div[contains(@class,'ant-collapse-header')]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", nature_header)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", nature_header)
                print("4949")
                self.service.set_progress(60)
                self.service.add_log(str("Nature of Startup Completed"))
                self.service.set_progress(62)
                self.service.add_log(str("Is the startup creating an innovative product Started"))
                innovation_header = wait.until(EC.presence_of_element_located((By.XPATH, "//span[contains(normalize-space(),'Is the startup creating an innovative product')]/ancestor::div[contains(@class,'ant-collapse-header')]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", innovation_header)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", innovation_header)
                print("5050")
                yes_radio_label = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@value='Yes' and contains(@name,'is_the_startup_creating')]/ancestor::label")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", yes_radio_label)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", yes_radio_label)
                time.sleep(1)         
                print("5151")       
                product_improvement = wait.until(EC.presence_of_element_located((By.XPATH, "//input[contains(@name,'product_') and @value='Improvement']/ancestor::label")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", product_improvement)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", product_improvement)
                service_improvement = wait.until(EC.presence_of_element_located((By.XPATH, "//input[contains(@name,'service_') and @value='Improvement']/ancestor::label")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", service_improvement)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", service_improvement)
                process_improvement = wait.until(EC.presence_of_element_located((By.XPATH, "//input[contains(@name,'process_') and @value='Improvement']/ancestor::label")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", process_improvement)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", process_improvement)
                innovation_header = wait.until(EC.presence_of_element_located((By.XPATH, "//span[contains(normalize-space(),'Is the startup creating an innovative product')]/ancestor::div[contains(@class,'ant-collapse-header')]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", innovation_header)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", innovation_header)
                self.service.set_progress(65)
                self.service.add_log(str("Is the startup creating an innovative product Completed"))
                self.service.set_progress(68)
                self.service.add_log(str("Is the startup creating a scalable business model Started"))
                scalable_header = wait.until(EC.presence_of_element_located((By.XPATH, "//span[contains(normalize-space(),'Is the startup creating a scalable business model')]/ancestor::div[contains(@class,'ant-collapse-header')]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", scalable_header)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", scalable_header)
                scalable_yes = wait.until(EC.presence_of_element_located((By.XPATH, "//input[contains(@name,'is_the_startup_creating_a_scalable_business_model') and @value='Yes']/ancestor::label")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", scalable_yes)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", scalable_yes)
                time.sleep(1)
                checkbox_values = ["Employment Generation", "Wealth Creation"]
                print("5252") 
                for value in checkbox_values:
                    checkbox_label = wait.until(EC.presence_of_element_located((By.XPATH, f"//input[@type='checkbox' and @value='{value}']/ancestor::label")))
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", checkbox_label)
                    driver.execute_script("document.body.click();")
                    driver.execute_script("arguments[0].click();", checkbox_label)
                    
                scalable_header = wait.until(EC.presence_of_element_located((By.XPATH, "//span[contains(normalize-space(),'Is the startup creating a scalable business model')]/ancestor::div[contains(@class,'ant-collapse-header')]")))
                
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", scalable_header)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", scalable_header)

                self.service.set_progress(69)
                self.service.add_log(str("Is the startup creating a scalable business model Completed"))

                self.service.set_progress(70)
                self.service.add_log(str("Fill brief note textarea Started"))
                print("5353") 
                note_header = wait.until(EC.presence_of_element_located((By.XPATH, "//span[contains(normalize-space(),'Please submit a brief note supporting')]/ancestor::div[contains(@class,'ant-collapse-header')]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", note_header)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", note_header)

                brief_note = self.data.get("who_we_are")
                print("5454") 
                textarea = wait.until(EC.visibility_of_element_located((By.XPATH, "//textarea[contains(@name,'please_submit_a_brief_note_supporting')]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", textarea)
                pyperclip.copy(brief_note)
                textarea.click()
                textarea.send_keys(Keys.CONTROL, "v")
                print("5555") 
                note_header = wait.until(EC.presence_of_element_located((By.XPATH, "//span[contains(normalize-space(),'Please submit a brief note supporting')]/ancestor::div[contains(@class,'ant-collapse-header')]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", note_header)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", note_header)
                print("5656") 
                funding_header = wait.until(EC.presence_of_element_located((By.XPATH, "//span[normalize-space()='Has your startup received any funding']/ancestor::div[contains(@class,'ant-collapse-header')]")))

                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", funding_header)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", funding_header)


                funding_no = wait.until(EC.presence_of_element_located((By.XPATH, "//input[contains(@name,'has_your_startup_received_any_funding') and @value='No']/ancestor::label")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", funding_no)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", funding_no)

                funding_header = wait.until(EC.presence_of_element_located((By.XPATH, "//span[normalize-space()='Has your startup received any funding']/ancestor::div[contains(@class,'ant-collapse-header')]")))

                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", funding_header)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", funding_header)

                self.service.set_progress(71)
                self.service.add_log(str("Has your startup received any funding Completed"))

                self.service.set_progress(73)
                self.service.add_log(str("Startup Activities Started"))

                activities_header = wait.until(EC.presence_of_element_located((By.XPATH, "//span[normalize-space()='Startup Activities']/ancestor::div[contains(@class,'ant-collapse-header')]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", activities_header)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", activities_header)

                recognition_no = wait.until(EC.presence_of_element_located((By.XPATH, "//input[contains(@name,'any_recognition_or_awards_received_by_the_startup') and @value='No']/ancestor::label")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", recognition_no)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", recognition_no)

                problem_text = self.data.get("problem_statement")
                problem_textarea = wait.until(EC.visibility_of_element_located((By.XPATH, "//textarea[contains(@name,'what_is_the_problem_the_startup_is_solving')]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", problem_textarea)
                self.clipboard(problem_text)
                problem_textarea.click()
                problem_textarea.send_keys(Keys.CONTROL, "v")
                print("5757") 
                solution_text = self.data.get("solution")
                solution_textarea = wait.until(EC.visibility_of_element_located((By.XPATH, "//textarea[contains(@name,'how_does_the_startup_propose_to_solve_the_problem')]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", solution_textarea)
                self.clipboard(solution_text)
                solution_textarea.click()
                solution_textarea.send_keys(Keys.CONTROL, "v")
                print("5858") 
                uniqueness_text = self.data.get("uniqueness")
                uniqueness_textarea = wait.until(EC.visibility_of_element_located((By.XPATH, "//textarea[contains(@name,'what_is_the_uniqueness_of_the_solution')]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", uniqueness_textarea)
                self.clipboard(uniqueness_text)
                uniqueness_textarea.click()
                uniqueness_textarea.send_keys(Keys.CONTROL, "v")
                print("5959") 
                revenue_text = self.data.get("revenue_growth")
                revenue_textarea = wait.until(EC.visibility_of_element_located((By.XPATH, "//textarea[contains(@name,'how_does_the_startup_generate_revenue')]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", revenue_textarea)
                self.clipboard(revenue_text)
                revenue_textarea.click()
                revenue_textarea.send_keys(Keys.CONTROL, "v")
                print("6060")         
                activities_header = wait.until(EC.presence_of_element_located((By.XPATH, "//span[normalize-space()='Startup Activities']/ancestor::div[contains(@class,'ant-collapse-header')]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", activities_header)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", activities_header)
                print("6161") 
                self.service.set_progress(74)
                self.service.add_log(str("Startup Activities Completed"))

                self.service.set_progress(76)
                self.service.add_log(str("Support Documents Started"))
                print("6262") 
                time.sleep(1)
                support_doc_header = wait.until(EC.presence_of_element_located((By.XPATH, "//span[contains(normalize-space(),'Please provide links or upload additional document')]/ancestor::div[contains(@class,'ant-collapse-header')]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", support_doc_header)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", support_doc_header)
                time.sleep(2)
                print("6363") 
                try:
                    print("6363.1") 
                    type_input = wait.until(EC.element_to_be_clickable((By.XPATH, "(//label[.//span[text()='Type']]/following::input[contains(@class,'ant-select-selection-search-input')][1])[1]")))
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", type_input)
                    type_input.click()
                    print("6363.2") 
                    type_input.send_keys(Keys.CONTROL + "a")
                    type_input.send_keys(Keys.DELETE)
                    time.sleep(1)
                    print("6363.3") 
                    type_input.send_keys("Pitch Desk")
                    time.sleep(2)
                    print("6363.4") 
                    type_input.send_keys(Keys.ENTER)
                    print("6363.5") 
                except:
                    print("6464") 
                    wait.until(EC.element_to_be_clickable((By.XPATH, "(//label[.//span[text()='Type']]/following::div[contains(@class,'ant-select-selector')][1])[1]"))).click()
                    type_input = wait.until(EC.presence_of_element_located((By.XPATH, "//label[.//span[text()='Type']]/following::input[contains(@class,'ant-select-selection-search-input')][1]")))
                    print("6464.1") 
                    type_input.send_keys(Keys.CONTROL + "a")
                    type_input.send_keys(Keys.DELETE)
                    print("6464.2")
                    for ch in "Pitch desk":
                        type_input.send_keys(ch)
                        time.sleep(0.15)
                    time.sleep(1)
                    print("6464.3") 
                    pitch_option = wait.until(EC.presence_of_element_located((By.XPATH, "//span[contains(@class,'ant-select-tree-title') and normalize-space()='Pitch desk']")))
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", pitch_option)
                    driver.execute_script("arguments[0].click();", pitch_option)
                    print("6464.4") 
                    time.sleep(2)
                    type_input.send_keys(Keys.ESCAPE)
                    time.sleep(2)
                    print("6464.5")
                print("6565") 
                # ===== SELECT SUBTYPE : Others =====
                subtype_input = wait.until(EC.element_to_be_clickable((By.XPATH, "(//input[contains(@class,'ant-select-selection-search-input')])[last()-3]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", subtype_input)
                subtype_input.click()
                subtype_input.send_keys(Keys.CONTROL + "a")
                subtype_input.send_keys(Keys.DELETE)
                subtype_input.send_keys("Others")
                print("6666") 
                time.sleep(1)
                subtype_input.send_keys(Keys.ENTER)
                time.sleep(2)
                print("6767") 
                browse_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "(//button[.//span[text()='Browse File']])[last()-3]")))
                browse_btn.click()
                wait.until(EC.visibility_of_element_located((By.XPATH, "(//div[@role='dialog'])[2]")))
                file_input = wait.until(EC.presence_of_element_located((By.XPATH, "(//div[@role='dialog']//input[@type='file'])[2]")))
                pd_base64 = self.data.get("pitchdesk_file")
                # Pitch Deck Upload
                print("6868") 
                if pd_base64:
                    if "," in pd_base64:
                        pd_base64 = pd_base64.split(",")[1]

                    # self.download_dir = os.path.expanduser("~/Automation_tempfiles/Startup_india")
                    # os.makedirs(self.download_dir, exist_ok=True)

                    file_path = os.path.join(self.download_dir, f"{company_name}_pitchdeck.pdf")

                    with open(file_path, "wb") as f:
                        f.write(base64.b64decode(pd_base64))

                    file_input.send_keys(file_path)
                    self.service.set_progress(79)
                    self.service.add_log(str("Pitch Deck Uploading"))
                    time.sleep(15)
                    print("6969") 
                    if os.path.exists(file_path):
                        os.remove(file_path)

                    self.service.set_progress(80)
                    self.service.add_log(str("Pitch Deck Uploaded"))

                time.sleep(10)  
                print("7070") 
                support_doc_header = wait.until(EC.presence_of_element_located((By.XPATH, "//span[contains(normalize-space(),'Please provide links or upload additional document')]/ancestor::div[contains(@class,'ant-collapse-header')]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", support_doc_header)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", support_doc_header)
                print("7171") 
                self.service.set_progress(82)
                self.service.add_log(str("Support Documents Completed"))

                self.service.set_progress(83)
                self.service.add_log(str("Self Certification Started"))
                print("7272") 
                self_cert_header = wait.until(EC.presence_of_element_located((By.XPATH, "//span[normalize-space()='Self Certification']/ancestor::div[contains(@class,'ant-collapse-header')]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", self_cert_header)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", self_cert_header)
                print("7373") 
                self.service.set_progress(84)
                self.service.add_log(str("Self Certification Completed"))

                coi_input = wait.until(EC.element_to_be_clickable((By.XPATH, "(//input[contains(@class,'ant-select-selection-search-input')])[last()-1]")))

                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", coi_input)
                coi_input.click()
                print("7474") 
                coi_input.send_keys(Keys.CONTROL + "a")
                coi_input.send_keys(Keys.DELETE)
                coi_input.send_keys("Certificate of Incorporation")
                time.sleep(1)
                coi_input.send_keys(Keys.ENTER)
                browse_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "(//button[contains(@class,'browse-btn')])[last()-1]")))
                browse_btn.click()
                active_modal = wait.until(EC.visibility_of_element_located((By.XPATH, "(//div[@role='dialog' and contains(@class,'ant-modal')])[last()]")))
                file_input = active_modal.find_element(By.XPATH, ".//input[@type='file']")
                coi_base64 = self.data.get("COI_file")
                print("7575") 
                # COI Upload
                if coi_base64:
                    if "," in coi_base64:
                        coi_base64 = coi_base64.split(",")[1]

                    # self.download_dir = os.path.expanduser("~/Automation_tempfiles/Startup_india")
                    # os.makedirs(self.download_dir, exist_ok=True)

                    file_path = os.path.join(self.download_dir, f"{company_name}_COI.pdf")

                    with open(file_path, "wb") as f:
                        f.write(base64.b64decode(coi_base64))

                    file_input.send_keys(file_path)
                    time.sleep(2)

                    if os.path.exists(file_path):
                        os.remove(file_path)

                time.sleep(3)
                select_input = wait.until(EC.element_to_be_clickable((By.XPATH, "(//input[contains(@class,'ant-select-selection-search-input')])[last()]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", select_input)
                select_input.click()
                print("7474") 
                select_input.send_keys(Keys.CONTROL + "a")
                select_input.send_keys(Keys.DELETE)
                select_input.send_keys("Authorization Letter")
                time.sleep(1)
                select_input.send_keys(Keys.ENTER)
                browse_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "(//button[contains(@class,'browse-btn')])[last()]")))
                browse_btn.click()
                active_modal = wait.until(EC.visibility_of_element_located((By.XPATH, "(//div[@role='dialog' and contains(@class,'ant-modal')])[last()]")))
                file_input = active_modal.find_element(By.XPATH, ".//input[@type='file']")
                loa_base64 = self.data.get("LOA_file")
                print("7575") 
                if loa_base64:
                    if "," in loa_base64:
                        loa_base64 = loa_base64.split(",")[1]

                    # self.download_dir = os.path.expanduser("~/Automation_tempfiles/Startup_india")
                    # os.makedirs(self.download_dir, exist_ok=True)

                    file_path = os.path.join(self.download_dir, f"{company_name}_LOA.pdf")

                    with open(file_path, "wb") as f:
                        f.write(base64.b64decode(loa_base64))

                    file_input.send_keys(file_path)
                    time.sleep(2)

                    if os.path.exists(file_path):
                        os.remove(file_path)
                time.sleep(2)
                print("7676") 
                cert_checkboxes = wait.until(EC.presence_of_all_elements_located((By.XPATH, "//div[contains(@class,'form-declaration')]//input[@type='checkbox']")))
                for checkbox in cert_checkboxes:
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", checkbox)            
                    is_checked = driver.execute_script("return arguments[0].checked;", checkbox)            
                    if not is_checked:
                        driver.execute_script("arguments[0].click();", checkbox)
                print("7777") 
                first_radio_label = wait.until(EC.presence_of_element_located((By.XPATH, "(//input[contains(@name,'please_select_either_of_the_below_options_applicable_for_the_entity')]/ancestor::label)[1]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", first_radio_label)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", first_radio_label)
                self_cert_header = wait.until(EC.presence_of_element_located((By.XPATH, "//span[normalize-space()='Self Certification']/ancestor::div[contains(@class,'ant-collapse-header')]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", self_cert_header)
                driver.execute_script("document.body.click();")
                driver.execute_script("arguments[0].click();", self_cert_header)
                print("7878") 
                self.service.set_progress(85)
                self.service.add_log(str("Saving as draft"))
                draft_button = wait.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(@class,'caf-save-as-draft')]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", draft_button)
                driver.execute_script("arguments[0].click();", draft_button)
                time.sleep(5)
                self.service.set_progress(86)
                self.service.add_log(str("Saved as draft"))
                print("7979") 

                submit_button = wait.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(@class,'caf-review-submit')]")))
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", submit_button)
                driver.execute_script("arguments[0].click();", submit_button)
                time.sleep(2)
                self.service.set_progress(87)
                self.service.add_log(str("Successfully Submit the Application next Review Application"))
                print("8080") 
                try:
                    status = driver.find_element(By.XPATH, "//span[contains(@class,'form-status-title')]").text
                    if "Incomplete data" in status:
                        print("8181") 
                        sections = driver.find_elements(By.XPATH,"//span[contains(text(),'Incomplete data')]/ancestor::div[contains(@class,'ant-collapse-header')]")
                        
                        self.service.set_progress(100)
                        self.service.add_log(str(SessionStatus.FAILED) + ": " + str("Form is incomplete"))
                        self.service.set_error(str(f"{sections} times incomplete alert occur in the site so contact to develop"))
                        return {"status":200,"message":"Form is incomplete","data":status,"sections":sections,"length":len(sections)}
                except Exception as e:
                    self.service.set_progress(0)
                    self.service.add_log(str("No status found"))
                print("8282") 
                time.sleep(2)
                self.service.set_progress(90)
                self.service.add_log(str("Review then Sumbit the Application"))
                checkbox = wait.until(EC.element_to_be_clickable((By.XPATH, "//span[contains(@class,'ant-checkbox-inner')]")))
                driver.execute_script("arguments[0].click();", checkbox)        
                print("8383")     
                submit_btn = driver.find_element(By.XPATH, "//button[normalize-space()='Submit Application']")
                self.service.add_log(str(submit_btn.is_enabled()))
                submit_btn.click()
                print("8484") 
                time.sleep(5)
                self.service.set_progress(91)
                self.service.add_log(str("After Submit the Application"))
                driver.find_element(By.CLASS_NAME, "sumbit-ok").click()
                time.sleep(5)
                self.service.set_progress(92)
                self.service.add_log(str("Application Submitted Successfully"))
                print("8585") 
                file=self.download_file(driver)
                print("8686")   
                error=None
                self.service.add_log(str(SessionStatus.COMPLETED))
                self.service.set_progress(100)
                self.service.add_log(str("Successfully Completed"))
                driver.quit()
                
                result = {"status":200,"message":"Success completed","timestamp":datetime.datetime.now().isoformat(),"file_reponse":file}
                self.service.set_result(result)
                return result
        except Exception as e:
            self.service.set_progress(100)
            self.service.add_log(str(SessionStatus.FAILED) + ": " + str(f"Program Error : {str(e)}"))
            self.service.set_error(str(e))

            # Grab a screenshot of whatever the browser is showing at the
            # moment of failure — this has to happen BEFORE driver.quit(),
            # since a quit driver can no longer be screenshotted. If the
            # driver never even got created (e.g. the failure happened
            # before webdriver.Chrome(...) ran), there's nothing to shoot.
            screenshot_base64 = None
            if driver is not None:
                try:
                    screenshot_base64 = driver.get_screenshot_as_base64()
                    self.service.add_log("Captured error screenshot")
                except Exception as screenshot_error:
                    self.service.add_log(f"Could not capture error screenshot: {screenshot_error}")

            # Quit only after the screenshot attempt above. Guarded because
            # the driver may already be closed (wait_for_otp /
            # wait_for_multi_otp quit it themselves on an OTP timeout) —
            # quitting again should never raise and mask the real error.
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass

            error_result = {
                "status": 500,
                "message": "Automation failed",
                "error": str(e),
                "timestamp": datetime.datetime.now().isoformat(),
                "screenshot_base64": screenshot_base64,
            }
            self.service.set_result(error_result)
            return error_result
    
    def run(self):
        try:
            #print("Startthe run")
            return self.startup_india()
        except Exception as e:
            import traceback
            #print("ERROR:", e)
            traceback.print_exc()
            # startup_india() now catches its own exceptions (captures a
            # screenshot, quits the driver, calls set_error/set_result, and
            # returns an error dict instead of raising) — this branch is a
            # last-resort safety net for anything that manages to escape
            # that handling anyway. Re-raise here so the Flask worker
            # thread in automation_framework.py still records the failure
            # via set_error() instead of it vanishing silently.
            raise