# ---------------------------------------
# Company Entity OPtion value fot json in "org_type"
#
# "Proprietary": "1",
# "Hindu Undivided Family": "2",
# "Partnership": "3",
# "Co-Operative": "4",
# "Private Limited Company": "5",
# "Public Limited Company": "6",
# "Self Help Group": "7",
# "Others": "8",
# "Limited Liability Partnership": "9",
# "Society": "10",
# "Trust": "11"
# ----------------------------------------
# gst_map in json
# "Yes": "0",
# "No": "1",
# "Exempted": "2"
# ----------------------------------------
# category_map in json
# "General": "0",
# "SC": "1",
# "ST": "2",
# "OBC": "3"
# ----------------------------------------
# gender_map in json
# "Male": "0",
# "Female": "1",
# "Others": "2"
# ----------------------------------------
# 19. Major Activity of Unit
# "Manufacturing" : "1"
#                      |----  1 = Non-Trading
# "Services" : "2" ----|
#                      |----  2 = Trading
#----------------------------------------
# 19.1 Major Activity Under Services
# "Non-Trading" : "1"
# "Trading" : "2"

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select
from selenium.webdriver.support.ui import Select as DropdownSelect
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException
from selenium.common.exceptions import TimeoutException, NoAlertPresentException, NoSuchElementException

from selenium.webdriver.common.alert import Alert
import random
import time
import base64
import os
import json
import datetime
import cv2
import easyocr
import numpy as np

# Folder setup
captcha_folder = "UDYAM_CAPTCHA"
os.makedirs(captcha_folder, exist_ok=True)
# -----------------------------
# Function to handle alert popup
# -----------------------------


class UdyamRegistration_captcha:

    def __init__(self, driver, wait, data=None):
        self.driver = driver
        self.wait = wait
        self.data = data
        self.reader = easyocr.Reader(["en"], gpu=False)

    def get_captcha_base64(self):
        captcha_img = self.wait.until(
            EC.presence_of_element_located(
                (By.XPATH, "//img[@id='ctl00_ContentPlaceHolder1_imgCaptcha']")
            )
        )

        return captcha_img.screenshot_as_base64

    def udyam_reg_captcha_resolve(self):
        try:
            captcha_base64 = self.get_captcha_base64()

            image_bytes = base64.b64decode(captcha_base64)

            nparr = np.frombuffer(image_bytes, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

            if img is None:
                return "000000"

            lower_blue = np.array([100, 40, 10])
            upper_blue = np.array([160, 80, 40])

            mask = cv2.inRange(img, lower_blue, upper_blue)

            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
            cleaned = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

            final_processed = cv2.bitwise_not(cleaned)

            results = self.reader.readtext(final_processed, detail=0)

            captcha_value = "".join(
                c for c in "".join(results)
                if c.isalnum()
            ).upper()

            return captcha_value

        except Exception as e:
            print(f"Captcha Function Error: {e}")
            return "000000"
        

class UdyamRegistration:

    def __init__(self, data, service):
        self.data = data
        # `service` is the UdyamRegService (AutomationService) instance that
        # created this run. It is the ONLY place session state, progress,
        # logging, error/result storage, and OTP storage live — this class
        # holds none of that itself, it just calls back into the framework
        # through this object (same pattern as Startup_india in
        # startup_india.py).
        self.service = service
        self.session_id = service.session_id  # read-only, handy for log text
        self.driver = None

    def handle_alert(self):
    
        try:
            WebDriverWait(self.driver, 3).until(EC.alert_is_present())
            alert = Alert(self.driver)
    
            print("Alert detected:", alert.text)
    
            # Accept alert (OK button)
            alert.accept()
    
            print("Alert accepted successfully.")
    
        except TimeoutException:
            pass
        except NoAlertPresentException:
            pass
    
    def wait_loader_loop(self, timeout=90):
    
        self.service.add_log("Waiting for loader to disappear")
        start_time = time.time()
        time.sleep(1)
    
        while True:
            try:
                loader = self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_UpdateProgress4")
    
                if not loader.is_displayed():
                    self.service.add_log("Loader disappeared")
                    return True
    
            except:
                self.service.add_log("Loader not found")
                return True
    
            if time.time() - start_time > timeout:
                self.service.add_log("Loader timeout")
                raise Exception("Loader timeout")
    
            time.sleep(0.5)
    
    def select_random_radio(self, group_name):
    
        radios = self.wait.until(EC.presence_of_all_elements_located((By.NAME, group_name)))    
        # Filter only visible & enabled radios
        valid_radios = [r for r in radios if r.is_displayed() and r.is_enabled()]    
        if not valid_radios:
            raise Exception(f"No selectable radios found for {group_name}")    
        choice = random.choice(valid_radios)    
        # Scroll into view (important for govt sites)
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", choice)
        time.sleep(0.3)    
        # Try normal click first
        try:
            choice.click()
        except:
            self.driver.execute_script("arguments[0].click();", choice)    
        # Small self.wait for UI update
        time.sleep(0.5)

    def udyam_reg_v2(self):
    
        try:
            self.service.add_log("Starting Udyam Registration")
            self.service.set_progress(2)
    
    
            # Configure Chrome options
            chrome_options = Options()
            # chrome_options.add_argument("--headless=new")   # Run in headless mode (latest)
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--window-size=1920,1080")  # Important for proper rendering
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    
            # Launch browser in headless mode
            self.driver = webdriver.Chrome(options=chrome_options)

            self.driver.get("https://udyamregistration.gov.in/")
            self.service.add_log("Portal opened successfully")
    
            self.wait = WebDriverWait(self.driver, 90)
            for i in range(5):
                try:
                    btn = self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "a[href='UdyamRegistration.aspx']")))
                    self.driver.execute_script("arguments[0].click();", btn)
                    self.service.add_log("Site loaded & button clicked successfully")
                    break
    
                except Exception as e:
                    page_text = self.driver.page_source.strip()
                    current_url = self.driver.current_url
    
                    if (
                        not page_text or
                        "This site can’t be reached" in page_text or
                        "took too long to respond" in page_text or
                        "ERR_CONNECTION" in page_text or
                        "ERR_INTERNET" in page_text or
                        "about:blank" in current_url or
                        len(page_text) < 200   # blank/minimal DOM
                    ):
                        self.service.add_log(f"Attempt {i+1}: Site not loaded properly (blank/error). Refreshing...")
                    else:
                        self.service.add_log(f"Attempt {i+1}: Element not found. Refreshing...")
    
                self.driver.refresh()
                time.sleep(3)
    
            self.service.add_log("Portal Loaded Successfully")
            self.service.set_progress(8)
    
            self.service.add_log("Udyam Registration clicked successfully")
    
            #--------------------------------------------------------------#
            #                                                              #  
            #  Aadhaar Verification Details                                #  
            #                                                              #
            #--------------------------------------------------------------#
    
            # Fill Aadhaar
            aadhaar = self.wait.until(EC.visibility_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_txtadharno")))
            aadhaar.clear()
            aadhaar.send_keys(self.data["aadhaar_number"])
    
            # Fill Name
            name = self.wait.until(EC.visibility_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_txtownername")))
            name.clear()
            name.send_keys(self.data["aadhaar_name"])
    
            # Click Validate & Generate OTP
            otp_btn = self.wait.until(EC.element_to_be_clickable((By.ID, "ctl00_ContentPlaceHolder1_btnValidateAadhaar")))
            self.driver.execute_script("arguments[0].click();", otp_btn)
            self.service.add_log("OTP sent successfully")
    
            # Wait for OTP input
            otp_input = self.wait.until(EC.presence_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_txtOtp1")))
    
            # Scroll to OTP field
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", otp_input)
            time.sleep(1)
    
            # Fill OTP
            # Wait for the Aadhaar OTP to arrive on the session. wait_for_otp()
            # polls the session, silently discards any malformed value it sees
            # (so a fresh one still gets a chance), and — if the timeout
            # passes with nothing valid — quits the driver and raises
            # OTPTimeoutError so this run fails cleanly and can be retried.
            OTP = self.service.wait_for_otp(
                otp_type="otp",
                timeout=280,
                poll_interval=2,
                driver=self.driver,
            )
            otp_input.clear()
            otp_input.send_keys(OTP)  # replace with real OTP
    
            # Wait for Validate button
            validate_btn = self.wait.until(EC.presence_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_btnValidate")))
    
            # Scroll to button
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", validate_btn)
            time.sleep(1)
    
            # Click Validate
            self.driver.execute_script("arguments[0].click();", validate_btn)
            self.wait_loader_loop()
            time.sleep(2)
            try:
                otp_error = self.driver.find_element(By.ID,"ctl00_ContentPlaceHolder1_lblOtp1")
            except:
                otp_error=None
            if otp_error!=None and "Incorrect OTP" in otp_error.text:
                self.service.add_log("Incorrect OTP So after 5 Minutes Retry it. Automation Closed")
                raise "Incorrect OTP So after 5 Minutes Retry it. Automation Closed"

            self.service.add_log("OTP validated successfully")
            self.service.set_progress(18)
            time.sleep(4)
    
            #--------------------------------------------------------------#
            #                                                              #  
            #  PAN Verification Details                                    #  
            #                                                              #
            #--------------------------------------------------------------#
    
            # 🔹 Scroll to PAN Verification section (center)
            pan_header = self.wait.until(EC.presence_of_element_located((By.XPATH, "//h2[contains(text(),'PAN Verification')]")))
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", pan_header)
            time.sleep(1)
    
            # 🔹 Select Type of Organisation (dynamic input: 1,2,3...)
            org_value = str(self.data["org_type"])   # example: 1 / 2 / 3
    
            org_dropdown = self.wait.until(EC.presence_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_ddlTypeofOrg")))
            # Select(org_dropdown).select_by_value(org_value)
            DropdownSelect(org_dropdown).select_by_value(org_value)
            self.wait_loader_loop()
            time.sleep(5)
    
            # 🔹 Fill PAN Number (letter by letter)
            pan_input = self.wait.until(EC.visibility_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_txtPan")))
            pan_input.clear()
            for ch in self.data["pan_number"]:
                pan_input.send_keys(ch)
                time.sleep(0.1)
            time.sleep(1)
            # 🔹 Fill PAN Name
            pan_name = self.wait.until(EC.visibility_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_txtPanName")))
            pan_name.clear()
            pan_name.send_keys(self.data["pan_name"])
            time.sleep(1)
    
            # 🔹 Fill DOB / DOI (letter by letter format DD/MM/YYYY)
            dob_input = self.wait.until(EC.visibility_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_txtdob")))
            dob_input.clear()
            for ch in self.data["dob"]:
                dob_input.send_keys(ch)
                time.sleep(0.1)
            time.sleep(1)
    
            # 🔹 Click Declaration checkbox
            checkbox = self.wait.until(EC.element_to_be_clickable((By.ID, "ctl00_ContentPlaceHolder1_chkDecarationP")))
            self.driver.execute_script("arguments[0].click();", checkbox)
            time.sleep(1)
    
            # 🔹 Scroll to PAN Validate button (center)
            validate_btn = self.wait.until(EC.presence_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_btnValidatePan")))
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", validate_btn)
            time.sleep(1)
            # input("Press here to continue after PAN verification...")
    
            # 🔹 Click PAN Validate
            self.driver.execute_script("arguments[0].click();", validate_btn)
            self.service.add_log("PAN validated successfully")
            self.service.set_progress(28)
            time.sleep(2)
            self.wait_loader_loop()
            try:
                error = self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_lblPanError")
                message = error.text.strip()

                if "You can not verify PAN more than 5 times in a day." in error.text:
                    return {
                        "status": False,
                        "message": error.text.strip()
                    }

                if "Your PAN has been successfully verified" in message:
                    pass  # Continue

                elif "Udyam Registration has already done" in message:
                    return {
                        "status": False,
                        "message": message
                    }

            except NoSuchElementException:
                pass  # Element not found, continue if appropriate

            time.sleep(2)
    
            # Click "Continue" button (PAN section)
    
            continue_btn = self.wait.until(EC.presence_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_btnGetPanData")))
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", continue_btn)
            self.driver.execute_script("arguments[0].click();", continue_btn)
            self.service.add_log("Continue button clicked successfully")
            time.sleep(4)
    
            #--------------------------------------------------------------#
            #                                                              #  
            #  4.3 Do you have GSTIN ?                                     #  
            #                                                              #
            #--------------------------------------------------------------#
    
    
            # 🔹 Scroll to "Do you have GSTIN ?" text (center)
            gst_label = self.wait.until(EC.presence_of_element_located((By.XPATH, "//b[contains(text(),'Do you have GSTIN')]")))
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", gst_label)
    
            # 🔹 Click "No" radio button (value=2)
            gst_no = self.wait.until(EC.element_to_be_clickable((By.ID, "ctl00_ContentPlaceHolder1_rblWhetherGstn_1")))
            self.driver.execute_script("arguments[0].click();", gst_no)
            self.service.add_log("GSTIN radio button clicked successfully")
            time.sleep(1)
    
    
    
            #--------------------------------------------------------------#
            #                                                              #
            #  5. Investment in Plant and Machinery OR Equipment (in Rs.)  #
            #                                                              #
            #--------------------------------------------------------------#
    
    
            # 🔹 Scroll to Investment section (center using first label)
            inv_section = self.wait.until(EC.presence_of_element_located((By.XPATH, "//b[contains(text(),'Written Down Value')]")))
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", inv_section)
            time.sleep(2)
            self.service.add_log("Investment section scrolled to successfully")
    
            # 🔹 Fill Written Down Value (A)
            wdv_input = self.wait.until(EC.visibility_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_txtDepCost")))
            wdv_input.clear()
            wdv_input.send_keys(self.data["wdv"])   # example: "200000.00"
    
            # 🔹 Fill Exclusion Cost (B)
            ex_input = self.wait.until(EC.visibility_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_txtExCost")))
            ex_input.clear()
            ex_input.send_keys(self.data["exclusion_cost"])   # example: "50000.00"
    
            # 🔹 Scroll to Turnover section
            turnover_section = self.wait.until(EC.presence_of_element_located((By.XPATH, "//b[contains(text(),'Total Turnover')]")))
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", turnover_section)
            time.sleep(1)
    
            # 🔹 Fill Total Turnover (A)
            turnover_input = self.wait.until(EC.visibility_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_txtTotalTurnoverA")))
            turnover_input.clear()
            turnover_input.send_keys(self.data["total_turnover"])   # example: "300000.00"
    
    
            # Click "Continue..." button (Enterprise Details)
    
            continue_btn = self.wait.until(EC.presence_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_btnEnterprisedetail")))
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", continue_btn)
            self.driver.execute_script("arguments[0].click();", continue_btn)
            self.service.add_log("Enterprise Details filled successfully")
            self.service.set_progress(38)
    
    
            #----------------------------------------------------#
            #                                                    #  
            #  Basic Details Section                             #
            #                                                    #
            # mapping reference                                  #  
            # social_category → 1=General, 2=SC, 3=ST, 4=OBC     #
            # gender → 1=Male, 2=Female, 3=Others                #
            # divyang → 1=Yes, 0=No                              #
            #                                                    #
            #----------------------------------------------------#
    
    
            # 🔹 Scroll to Enterprise section (Name of Enterprise)
            enterprise = self.wait.until(EC.presence_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_txtenterprisename")))
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", enterprise)
    
            # 🔹 8. Name of Enterprise
            enterprise.clear()
            enterprise.send_keys(self.data["company_name"])
    
            # 🔹 9. Mobile Number
            mobile = self.wait.until(EC.visibility_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_txtmobile")))
            mobile.clear()
            mobile.send_keys(self.data["mobile"])
    
            # 🔹 10. Email
            email = self.wait.until(EC.visibility_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_txtemail")))
            email.clear()
            email.send_keys(self.data["email"])
    
            # 🔹 11. Social Category (1=General,2=SC,3=ST,4=OBC)
            category = self.wait.until(EC.presence_of_element_located((By.NAME, "ctl00$ContentPlaceHolder1$rdbcategory")))
            options = self.driver.find_elements(By.NAME, "ctl00$ContentPlaceHolder1$rdbcategory")
            for opt in options:
                if opt.get_attribute("value") == str(self.data["social_category"]):
                    self.driver.execute_script("arguments[0].click();", opt)
                    break
    
            # 🔹 12. Gender (1=Male,2=Female,3=Others)
            gender_options = self.driver.find_elements(By.NAME, "ctl00$ContentPlaceHolder1$rbtGender")
            for opt in gender_options:
                if opt.get_attribute("value") == str(self.data["gender"]):
                    self.driver.execute_script("arguments[0].click();", opt)
                    break
    
            # 🔹 13. Specially Abled (1=Yes,0=No)
            ph_options = self.driver.find_elements(By.NAME, "ctl00$ContentPlaceHolder1$rbtPh")
            for opt in ph_options:
                if opt.get_attribute("value") == str(self.data["divyang"]):
                    self.driver.execute_script("arguments[0].click();", opt)
                    break
            self.service.add_log("Basic Details filled successfully")
            
    
            #------------------------------------#
            #                                    #
            # 14. Official Address of Enterprise #
            #                                    #
            #------------------------------------#
    
            # 🔹 Scroll to "Official Address of Enterprise"
            address_header = self.wait.until(EC.presence_of_element_located((By.XPATH, "//h4[contains(text(),'Official Address of Enterprise')]")))
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", address_header)
            time.sleep(1)
    
            # 🔹 Fill Address Fields
            self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_txtOffFlatNo").send_keys(self.data["official_address"]["flat"])
            self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_txtOffBuilding").send_keys(self.data["official_address"]["building"])
            self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_txtOffVillageTown").send_keys(self.data["official_address"]["village"])
    
            self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_txtOffBlock").send_keys(self.data["official_address"]["block"])
            self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_txtOffRoadStreetLane").send_keys(self.data["official_address"]["road"])
            self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_txtOffCity").send_keys(self.data["official_address"]["city"])
    
            self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_txtOffPin").clear()
            self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_txtOffPin").send_keys(self.data["official_address"]["pin"])
    
            # 🔹 Select State
            state_dropdown = Select(self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_ddlstate"))
            state_dropdown.select_by_value(self.data["official_address"]["state"])
            self.wait_loader_loop()
            time.sleep(2)
    
            # 🔹 Select District
            district_dropdown = Select(self.wait.until(EC.presence_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_ddlDistrict"))))
            district_dropdown.select_by_value(self.data["official_address"]["district"])
            self.wait_loader_loop()
            time.sleep(2)
            self.service.add_log("Official address filled successfully")
            self.service.set_progress(48)
    
            #----------------------------------------------#
            #                                              #
            # 15. Location of Plant(s)/Unit(s) and details #
            #                                              #
            #----------------------------------------------#
    
            # 🔹 Scroll to "Location of Plant(s)/Unit(s)"
            plant_header = self.wait.until(EC.presence_of_element_located((By.XPATH, "//h4[contains(text(),'Location of Plant')]")))
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", plant_header)
            time.sleep(1)
    
            # 🔹 1. Add Units (loop)
            for unit_name in self.data["units"]:
                unit_input = self.wait.until(EC.visibility_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_txtUnitName")))
                unit_input.clear()
                unit_input.send_keys(unit_name)
    
                add_btn = self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_btnAddUnit")
                self.driver.execute_script("arguments[0].click();", add_btn)
                self.wait_loader_loop()
    
                time.sleep(2)  # gap after each add
    
            # 🔹 2. Fill each unit details
            for unit_name, details in self.data["units"].items():
    
                # select unit
                unit_dropdown = Select(self.wait.until(EC.presence_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_ddlUnitName"))))
                unit_dropdown.select_by_visible_text(unit_name)
                self.wait_loader_loop()
    
                time.sleep(2)
    
                # fill address
                self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_txtPFlat").send_keys(details["flat"])
                self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_txtPBuilding").send_keys(details["building"])
                self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_txtPVillageTown").send_keys(details["village"])
                self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_txtPBlock").send_keys(details["block"])
                self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_txtPRoadStreetLane").send_keys(details["road"])
                self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_txtPCity").send_keys(details["city"])
                self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_txtPpin").send_keys(details["pin"])
    
                # state
                Select(self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_ddlPState"))\
                    .select_by_value(details["state"])
                self.wait_loader_loop()
                time.sleep(2)
    
                # district
                Select(self.wait.until(EC.presence_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_ddlPDistrict")))).select_by_value(details["district"])
                self.wait_loader_loop()
                time.sleep(2)
    
                # click Add Plant
                add_plant = self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_BtnPAdd")
                self.driver.execute_script("arguments[0].click();", add_plant)
                self.wait_loader_loop()
                time.sleep(5)
    
                # 🔹 Scroll to section
                prev_em_section = self.wait.until(EC.presence_of_element_located((By.XPATH, "//b[contains(text(),'Previous EM-II')]")))
                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", prev_em_section)
            time.sleep(2)
            self.service.add_log("Location of Plant(s)/Unit(s) filled successfully")
            self.service.set_progress(58)
    
            #--------------------------------------------------------------------#
            #                                                                    #
            # 16. Previous EM-II/UAM Registration Number, If Any EM-II/UAM       #
            #                                                                    #
            #  Select option based on input                                      #
            #  mapping: 0 = N/A, 2 = EM-II, 4 = Previous UAM                     #
            #                                                                    #
            #--------------------------------------------------------------------#
    
            options = self.driver.find_elements(By.NAME, "ctl00$ContentPlaceHolder1$rdbPreviousEM")
    
            for opt in options:
                if opt.get_attribute("value") == str(self.data["previous_em"]):
                    self.driver.execute_script("arguments[0].click();", opt)
                    break
            # 🔹 Scroll to section
            inc_section = self.wait.until(EC.presence_of_element_located((By.XPATH, "(//b[contains(text(),'Date of Incorporation')])[2]")))
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", inc_section)
            time.sleep(2)
            self.service.add_log("Previous EM-II/UAM Registration Number filled successfully")
    
    
            #----------------------------#
            #                            #
            # 17. Status of Enterprise   #
            #                            #
            #----------------------------#
    
            # 🔹 Fill Date of Incorporation (letter by letter)
            inc_date = self.wait.until(EC.visibility_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_txtdateIncorporation")))
            inc_date.clear()
            for ch in self.data["incorporation_date"]:
                inc_date.send_keys(ch)
                time.sleep(0.1)
    
            # 🔹 Select Commenced (1=Yes, 0=No)
            options = self.driver.find_elements(By.NAME, "ctl00$ContentPlaceHolder1$rblcommenced")
            for opt in options:
                if opt.get_attribute("value") == str(self.data["commenced"]):
                    self.driver.execute_script("arguments[0].click();", opt)
                    break
    
            # 🔹 If Yes → fill Date of Commencement
            if str(self.data["commenced"]) == "1":
                com_date = self.wait.until(EC.visibility_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_txtcommencedate")))
                com_date.clear()
                for ch in self.data["commencement_date"]:
                    com_date.send_keys(ch)
                    time.sleep(0.1)
            time.sleep(2)
            self.service.add_log("Status of Enterprise filled successfully")
            self.service.set_progress(66)
            #----------------------------#
            #                            #
            # 18. Bank Details           #
            #                            #
            #----------------------------#
    
            # 🔹 Scroll to Bank Details section
            bank_section = self.wait.until(EC.presence_of_element_located((By.XPATH, "//b[contains(text(),'Bank Name')]")))
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", bank_section)
            time.sleep(2)
    
            # 🔹 Bank Name
            bank_name = self.wait.until(EC.visibility_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_txtBankName")))
            bank_name.clear()
            bank_name.send_keys(self.data["bank_name"])
    
            # 🔹 IFSC Code (letter by letter)
            ifsc = self.wait.until(EC.visibility_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_txtifsccode")))
            ifsc.clear()
            for ch in self.data["ifsc"]:
                ifsc.send_keys(ch)
                time.sleep(0.1)
    
            # 🔹 Account Number
            acc = self.wait.until(EC.visibility_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_txtaccountno")))
            acc.clear()
            acc.send_keys(self.data["account_number"])
            time.sleep(2)
            self.service.add_log("Bank Details filled successfully")
            self.service.set_progress(74)
    
    
            #----------------------------------#
            #                                  #
            # 19. Major Activity of Unit       #
            #                                  #
            #----------------------------------#
    
    
    
            # 🔹 Scroll to Major Activity section
            activity_section = self.wait.until(EC.presence_of_element_located((By.XPATH, "//b[contains(text(),'Major Activity')]")))
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", activity_section)
            time.sleep(1)
    
            # 🔹 Select option
            # 1 = Manufacturing, 2 = Services
    
            options = self.driver.find_elements(By.NAME, "ctl00$ContentPlaceHolder1$rdbCatgg")
    
            for opt in options:
                if opt.get_attribute("value") == str(self.data["major_activity"]):
                    self.driver.execute_script("arguments[0].click();", opt)
                    break
            # 🔹 Scroll to Multiple Activity section
            multi_section = self.wait.until(EC.presence_of_element_located((By.XPATH, "//table[@id='ctl00_ContentPlaceHolder1_rdbCatggMultiple']")))
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", multi_section)
            time.sleep(2)
            self.service.add_log("Major Activity filled successfully")

            if self.data["major_activity"] == "2" or self.data["major_activity"] == 2:
                time.sleep(5)
                radio_id = f"ctl00_ContentPlaceHolder1_rdbSubCategg_{int(self.data['major_activity_under_services']) - 1}"
                self.driver.find_element(By.ID, radio_id).click()    
            self.wait_loader_loop()
    
            #---------------------------------------------------------------------------------------------------------#
            #                                                                                                         #
            # 20. National Industrial Classification (NIC) Code for Activities(One or more activities can be added)   #
            #  Select option                                                                                          #
            #  1 = Manufacturing, 2 = Services, 3 = Trading                                                           #
            #                                                                                                         #
            #---------------------------------------------------------------------------------------------------------#
    
            options = self.driver.find_elements(By.NAME, "ctl00$ContentPlaceHolder1$rdbCatggMultiple")
    
            for opt in options:
                if opt.get_attribute("value") == str(self.data["nic_activity"]):
                    self.driver.execute_script("arguments[0].click();", opt)
                    break
    
            # 🔹 Scroll to NIC section
            nic_section = self.wait.until(EC.presence_of_element_located((By.XPATH, "//b[contains(text(),'NIC 2 Digit Code')]")))
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", nic_section)
            time.sleep(5)
            self.service.add_log("NIC section loaded successfully")
    
            #---------------------------------------------------------------------------------------------------------#
            #                                                                                                         #
            # NIC 2 Digit Code, NIC 4 Digit Code, NIC 5 Digit Code                                                    #
            #                                                                                                         #
            #---------------------------------------------------------------------------------------------------------#
            time.sleep(5)
    
            # 🔹 Select NIC 2 Digit
            nic2 = Select(self.wait.until(EC.presence_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_ddl2NicCode"))))
            nic2.select_by_value(self.data["nic"]["nic2"])
            self.wait_loader_loop()
            time.sleep(2)
    
            # 🔹 Select NIC 4 Digit
            nic4 = Select(self.wait.until(EC.presence_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_ddl4NicCode"))))
            nic4.select_by_value(self.data["nic"]["nic4"])
            self.wait_loader_loop()
            time.sleep(2)
    
            # 🔹 Select NIC 5 Digit
            nic5 = Select(self.wait.until(EC.presence_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_ddl5NicCode"))))
            nic5.select_by_value(self.data["nic"]["nic5"])
            self.wait_loader_loop()
            time.sleep(2)
    
            # 🔹 Click Add Activity
            add_btn = self.wait.until(EC.presence_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_btnAddMore")))
            self.driver.execute_script("arguments[0].click();", add_btn)
            self.wait_loader_loop()
            self.service.add_log("Add NIC 2,4,5 Activity Succussfully added")
            self.service.set_progress(82)
    
            #---------------------------------------------------------------------------------------------------------#
            #                                                                                                         #
            # Number of persons employed : Male, Female, Others                                                       #
            #                                                                                                         #
            #---------------------------------------------------------------------------------------------------------#
    
    
            # 🔹 Scroll to Employee section
            emp_section = self.wait.until(EC.presence_of_element_located((By.XPATH, "//b[contains(text(),'Number of persons employed')]")))
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", emp_section)
            time.sleep(1)
    
            # # 🔹 Fill Male
            # male = self.wait.until(EC.visibility_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_txtNoofpersonMale")))
            # male.clear()
            # male.send_keys(self.data["employees"]["male"])
    
            # # 🔹 Fill Female
            # female = self.wait.until(EC.visibility_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_txtNoofpersonFemale")))
            # female.clear()
            # female.send_keys(self.data["employees"]["female"])
    
            # # 🔹 Fill Others
            # others = self.wait.until(EC.visibility_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_txtNoofpersonOthers")))
            # others.clear()
            # others.send_keys(self.data["employees"]["others"])
            # time.sleep(2)
    
    
            male = self.wait.until(EC.visibility_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_txtNoofpersonMale")))
            female = self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_txtNoofpersonFemale")
            others = self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_txtNoofpersonOthers")
    
            # Clear
            male.clear()
            female.clear()
            others.clear()
    
            # Type slowly (simulate real user)
            male.send_keys(str(self.data["employees"]["male"]))
            time.sleep(0.3)
    
            female.send_keys(str(self.data["employees"]["female"]))
            time.sleep(0.3)
    
            others.send_keys(str(self.data["employees"]["others"]))
            time.sleep(0.3)
    
            # Trigger JS
            others.send_keys(Keys.TAB)
            self.service.add_log("Employees filled successfully 0")
    
            # Wait for total auto calculation
            total = self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_txttotalemp")
    
            self.wait.until(lambda d: total.get_attribute("value") != "")
    
            self.service.add_log(f"Total employees: {total.get_attribute('value')}")
            time.sleep(2)
            self.service.add_log("Employees filled successfully 1")
    
    
            # 🔹 Scroll to section (optional)
            section = self.wait.until(EC.presence_of_element_located((By.XPATH, "//b[contains(text(),'Are you interested')]")))
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", section)
            time.sleep(2)
            self.service.add_log("Employees filled successfully 2")
            self.service.set_progress(90)
    
            self.select_random_radio( "ctl00$ContentPlaceHolder1$rblGeM")
            time.sleep(1)
            self.select_random_radio( "ctl00$ContentPlaceHolder1$rblTReDS")
            time.sleep(1)
            self.select_random_radio( "ctl00$ContentPlaceHolder1$rblNCS")
            time.sleep(1)
            self.select_random_radio( "ctl00$ContentPlaceHolder1$rblnsic")
            time.sleep(1)
            self.select_random_radio( "ctl00$ContentPlaceHolder1$rblsid")
            time.sleep(1)
            self.service.add_log("Radio Buttons are Selected in Randomly")
            self.service.set_progress(94)
    
            # # 🔹 Scroll to top (optional)
            # self.driver.execute_script("window.scrollTo(0, 0)")
            # time.sleep(1)
    
            # # 🔹 Capture full page using Chrome DevTools
            # screenshot = self.driver.execute_cdp_cmd("Page.captureScreenshot", {
            #     "captureBeyondViewport": True,
            #     "fromSurface": True
            # })
    
            # # 🔹 Save file
            # file_name = f"{self.data['pan_name']}_fullpage.png"
    
            # with open(file_name, "wb") as f:
            #     f.write(
            #         base64.b64decode(
            #             screenshot['data']
            #         )
            #     )
    
            # log.info(f"✅ Full page screenshot saved: {file_name}")
    
            #----------------------------#
            #                            #
            # Final Sumbit and otp       #
            #                            #
            #----------------------------#
    
    
            # input("Press Enter after filling the form...")
            
            time.sleep(2)
            
            submit_btn = WebDriverWait(self.driver, 20).until(EC.element_to_be_clickable((By.ID, "ctl00_ContentPlaceHolder1_btnsubmit")))

            # Scroll button to center of screen
            self.driver.execute_script("""arguments[0].scrollIntoView({behavior: 'instant',block: 'center',inline: 'center'});""", submit_btn)

            # Click button
            submit_btn.click()
    
            time.sleep(3)
            self.handle_alert()
            time.sleep(2)
            self.wait_loader_loop()
            time.sleep(2)         
    
            # -----------------------------
            # STEP 3: Captcha Loop
            # -----------------------------
            captcha_obj = UdyamRegistration_captcha(driver=self.driver,wait=self.wait)
            captcha_solve=False
            otp_attempts=3
            otp_attempts = 0

            while not captcha_solve:

                # Wait for the final-submit OTP (OTP #2) to arrive on the
                # session. Same wait_for_otp() semantics as the Aadhaar OTP
                # above — discards malformed values, quits the driver and
                # raises OTPTimeoutError on a real timeout.
                otp_value = self.service.wait_for_otp(
                    otp_type="otp",
                    timeout=280,
                    poll_interval=2,
                    driver=self.driver,
                )

                otp_input = self.wait.until(
                    EC.presence_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_txtOtp"))
                )
                otp_input.clear()
                otp_input.send_keys(otp_value)

                # captcha_path = self.save_captcha()
                captcha_value = captcha_obj.udyam_reg_captcha_resolve()
                captcha_input = self.wait.until(EC.presence_of_element_located((By.XPATH, "//input[@id='ctl00_ContentPlaceHolder1_txtCaptcha']")))
                captcha_input.clear()
                captcha_input.send_keys(captcha_value)
                time.sleep(2)
                final_submit = self.wait.until(EC.element_to_be_clickable((By.ID, "ctl00_ContentPlaceHolder1_btn_finalsubmit")))

                # Scroll element to the center
                self.driver.execute_script("""
                    arguments[0].scrollIntoView({
                        behavior: 'smooth',
                        block: 'center'
                    });
                """, final_submit)

                time.sleep(1)

                # Click
                final_submit.click()
                time.sleep(2)
    
                # Handle alert after click
                self.handle_alert()
    
                time.sleep(5)
                
                try:
                    otp_error = WebDriverWait(self.driver, 2).until(
                        EC.visibility_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_lblMssgg"))
                    )

                    if "Your OTP Number is Wrong." in otp_error.text:
                        otp_attempts += 1

                        if otp_attempts >= 3:
                            raise Exception("3 OTP attempts are over. Restart the automation.")

                        WebDriverWait(self.driver, 5).until(
                            EC.element_to_be_clickable((By.ID, "ctl00_ContentPlaceHolder1_btnsubmit"))
                        ).click()

                        time.sleep(4)

                        alert = WebDriverWait(self.driver, 10).until(lambda d: d.switch_to.alert)

                        print(alert.text)  # Are you sure that you have entered correct data...

                        alert.accept()   

                        continue

                except TimeoutException:
                    # OTP accepted
                    break
    
                # -----------------------------
                # STEP 5: Check Captcha Error
                # -----------------------------
                try:
                    WebDriverWait(self.driver, 10).until(EC.presence_of_element_located((By.XPATH, "//span[@id='ctl00_ContentPlaceHolder1_lblCaptcha']"))) #//div[@id='ctl00_ContentPlaceHolder1_divcaptcha1'] 
                    error_msg = self.driver.find_element(By.XPATH,"//span[@id='ctl00_ContentPlaceHolder1_lblCaptcha']").text.strip()
    
                    if "Incorrect verification code" in error_msg:
                        print("Captcha incorrect. Refreshing captcha...")
    
                        refresh_btn = self.wait.until(EC.element_to_be_clickable((By.XPATH, "//input[@id='ctl00_ContentPlaceHolder1_ImgRefresh']")))
                        self.driver.execute_script("""arguments[0].scrollIntoView({behavior: 'instant',block: 'center',inline: 'center'});""", refresh_btn)
                        time.sleep(2)
                        refresh_btn.click()
                        time.sleep(2)
                        continue
                    else :
                        captcha_solve = True
                except Exception as e:
                    print(e)    
                print("Form submitted successfully!")  
            # time.sleep(5)

            # ---------------- Second Alert ----------------
            alert = self.wait.until(EC.alert_is_present())

            print("Second Alert:", alert.text)
            import re
            # Extract Registration Number
            match = re.search(r"UDYAM-[A-Z]{2}-\d+", alert.text)
            if match:
                print("Registration Number:", match.group())
                self.service.add_log(f"Registration Number: {match.group()}")

            # Click OK on second alert
            alert.accept()            
            udyam_no = WebDriverWait(self.driver, 20).until(lambda d: d.find_element(By.ID, "ctl00_ContentPlaceHolder1_lblmsg").text.strip() != "")
            print(self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_lblmsg").text)
            # input("Press Enter after filling the form...")
            time.sleep(5)
            
            # ---------------------------------------
            # Success
            # ---------------------------------------

            self.service.set_progress(100)
            self.service.add_log("Udyam Registration Completed Successfully")

            self.driver.quit()
            self.service.add_log("Browser closed successfully")

            result = {
                "status": 200,
                "message": "Success completed",
                "timestamp": datetime.datetime.now().isoformat(),
                "Udyam_No": udyam_no,
            }
            self.service.set_result(result)
            return result

        except Exception as e:
            self.service.set_progress(100)
            self.service.add_log(f"Automation failed: {e}")
            self.service.set_error(str(e))

            # Grab a screenshot of whatever the browser is showing at the
            # moment of failure — this has to happen BEFORE driver.quit(),
            # since a quit driver can no longer be screenshotted. If the
            # driver never even got created, there's nothing to shoot.
            screenshot_base64 = None
            if self.driver is not None:
                try:
                    screenshot_base64 = self.driver.get_screenshot_as_base64()
                    self.service.add_log("Captured error screenshot")
                except Exception as screenshot_error:
                    self.service.add_log(f"Could not capture error screenshot: {screenshot_error}")

            # Quit only after the screenshot attempt above. Guarded because
            # the driver may already be closed (self.service.wait_for_otp()
            # quits it itself on an OTP timeout) — quitting again should
            # never raise and mask the real error.
            if self.driver is not None:
                try:
                    self.driver.quit()
                    self.service.add_log("Browser closed after error")
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
            return self.udyam_reg_v2()
        except Exception as e:
            import traceback
            traceback.print_exc()
            # udyam_reg_v2() now catches its own exceptions (captures a
            # screenshot, quits the driver, calls set_error/set_result, and
            # returns an error dict instead of raising) — this branch is a
            # last-resort safety net for anything that manages to escape
            # that handling anyway. Re-raise here so the Flask worker thread
            # in automation_framework.py still records the failure via
            # set_error() instead of it vanishing silently.
            raise