from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait

import logging as log
import time
import re


log.basicConfig(level=log.INFO,format="%(asctime)s - %(levelname)s - %(message)s")

class Chennai_Professional_Tax:

    def __init__(self,data,session_id=None,sessions=None):

        self.data=data
        self.session_id=session_id
        self.sessions=sessions
        self.driver=None
        self.wait=None

        self.process(log="Application Started")
        self.process(log="Application Started")
    
    def process(self,log=None,progress=None,status=None,error=None,result=None):

        if not self.sessions or not self.session_id:
            return

        session = self.sessions[self.session_id]

        # status
        if status:
            session["status"] = status

        # progress
        if progress is not None:
            session["progress"] = progress

        # error
        if error:
            session["error"] = error

        # result
        if result is not None:
            session["result"] = result

        # logs
        if log:
            log_obj = {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "message": log
            }

            session["logs"].append(log_obj)

            # latest log as current status
            session["status"] = log

            print(log)

        session["updated_at"] = time.strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    def add_log(self,message):

        self.sessions[self.session_id]["logs"].append(message)

        self.sessions[self.session_id]["current_step"] = (message)
    
    # ============================================================
    # DRIVER SETUP
    # ============================================================

    def setup_driver(self):
        options = Options()

        options.add_argument("--start-maximized")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-popup-blocking")
        options.add_argument("--headless=new")

        options.add_experimental_option(
            "prefs",
            {
                "profile.default_content_setting_values.popups": 1
            }
        )

        self.driver = webdriver.Chrome(options=options)
        self.wait = WebDriverWait(self.driver, 20)

    # ============================================================
    # LOGIN
    # ============================================================

    def login(self):

        self.driver.get("https://erp.chennaicorporation.gov.in/e-portal/login.do")

        self.wait.until(
            EC.presence_of_element_located((By.ID, "j_username"))
        ).send_keys(self.data["username"])

        self.wait.until(
            EC.presence_of_element_located((By.NAME, "j_password"))
        ).send_keys(self.data["password"])

        self.wait.until(
            EC.element_to_be_clickable((By.ID, "loginbtn"))
        ).click()

        self.process(log="Login Successful")

    # ============================================================
    # CREATE SERVICE REQUEST
    # ============================================================

    def create_service_request(self):

        self.wait.until(
            EC.element_to_be_clickable(
                (By.LINK_TEXT, "Create Service Request")
            )
        ).click()

        self.wait.until(
            EC.frame_to_be_available_and_switch_to_it(
                (By.NAME, "mainctrl")
            )
        )

        self.process(log="Create Service Request Clicked")

    # ============================================================
    # SELECT CATEGORY & SERVICE
    # ============================================================

    def select_service(self):

        category = self.wait.until(
            EC.presence_of_element_located(
                (By.ID, "servicecategory")
            )
        )

        Select(category).select_by_value("190")

        service = self.wait.until(
            EC.presence_of_element_located(
                (By.ID, "service")
            )
        )

        self.wait.until(
            lambda d: len(Select(service).options) > 1
        )

        Select(service).select_by_value("187")

        self.process(log="Professional Tax Selected")

    # ============================================================
    # HANDLE POPUP
    # ============================================================

    def handle_popup(self):

        # ============================================================
        # CLICK CONTINUE + HANDLE POPUP
        # ============================================================

        main_window = self.driver.current_window_handle

        continue_btn = self.wait.until(
            EC.element_to_be_clickable((By.ID, "continuebtn"))
        )

        # Store old windows
        old_windows = self.driver.window_handles

        # Click using JS
        self.driver.execute_script(
            "arguments[0].click();",
            continue_btn
        )

        popup_url = None

        # ============================================================
        # WAIT FOR POPUP WINDOW
        # ============================================================

        try:

            self.wait.until(
                lambda d: len(d.window_handles) > len(old_windows)
            )

            new_windows = self.driver.window_handles

            for window in new_windows:

                if window not in old_windows:

                    # Switch popup
                    self.driver.switch_to.window(window)

                    # Wait fully loaded
                    self.wait.until(
                        lambda d: d.execute_script(
                            "return document.readyState"
                        ) == "complete"
                    )

                    time.sleep(2)

                    popup_url = self.driver.current_url

                    log.info(f"Popup URL : {popup_url}")

                    # Close popup
                    self.driver.close()

                    # Back to main window
                    self.driver.switch_to.window(main_window)

                    break

        except Exception as e:

            log.info(f"Popup not opened : {e}")

        # ============================================================
        # FALLBACK METHOD
        # ============================================================

        if not popup_url:

            try:

                html = self.driver.page_source

                match = re.search(
                    r"window\.open\(['\"]([^'\"]+)['\"]",
                    html
                )

                if match:

                    popup_url = (
                        "https://erp.chennaicorporation.gov.in"
                        + match.group(1)
                    )

                    log.info(f"Extracted URL : {popup_url}")

            except Exception as e:

                log.info(f"Regex Failed : {e}")

        # ============================================================
        # OPEN URL IN NEW TAB
        # ============================================================

        if popup_url:

            # Create new tab
            self.driver.switch_to.new_window('tab')

            # Open popup URL
            self.driver.get(popup_url)

            # Wait page load
            self.wait.until(
                lambda d: d.execute_script(
                    "return document.readyState"
                ) == "complete"
            )

            # Reset iframe
            self.driver.switch_to.default_content()

            # Wait body load
            self.wait.until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )

            log.info(f"Final URL : {self.driver.current_url}")
            log.info(f"Final Title : {self.driver.title}")

        else:

            raise Exception("Popup URL Not Found")

    
    # ============================================================
    # FILL BASIC DETAILS
    # ============================================================

    def fill_basic_details(self):

        self.wait.until(EC.presence_of_element_located((By.ID, "assesseeName"))).send_keys(self.data["comp_name"])

        # ========================================================
        # CATEGORY TYPE
        # ========================================================

        category_type = self.wait.until(EC.presence_of_element_located((By.ID, "categoryType")))

        Select(category_type).select_by_value(self.data["categoryType"])

        # ========================================================
        # CATEGORY SUB TYPE
        # ========================================================

        category_sub = self.wait.until(EC.presence_of_element_located((By.ID, "categorySelect")))
        self.wait.until(lambda d: len(Select(category_sub).options) > 1)

        Select(category_sub).select_by_value(self.data["categorySelect"])

        self.process(log="Basic Details Filled",progress=45)

    # ============================================================
    # PROPERTY DETAILS
    # ============================================================

    def fill_property_details(self):

        # ========================================================
        # PROPERTY TAX NUMBER
        # ========================================================

        if self.data["propertyTaxNumber"] != "0":

            property_input = self.wait.until(EC.presence_of_element_located((By.ID, "propertyTaxNumber")))

            property_input.send_keys(self.data["propertyTaxNumber"])
            property_input.send_keys(Keys.TAB)

            self.process(log="Property Tax Number Entered",progress=55)
        # ========================================================
        # MANUAL ADDRESS
        # ========================================================

        else:
            if self.data["buildingName"] != "":
                building_name = self.wait.until(EC.presence_of_element_located((By.ID, "buildingName")))
                building_name.send_keys(self.data["buildingName"])
                
            self.process(log="Address Fetching it",progress=55)

            self.wait.until(EC.presence_of_element_located((By.ID, "buildingNo"))).send_keys(self.data["doorno"])

            # ====================================================
            # ZONE
            # ====================================================

            zone = Select(self.wait.until(EC.presence_of_element_located((By.ID, "zoneId"))))
            
            # zone.select_by_value(self.data["zoneId"])
            zone.select_by_visible_text(self.data["zoneId"])

            # ====================================================
            # WARD
            # ====================================================

            self.wait.until(lambda d: len(Select(d.find_element(By.ID, "wardId")).options) > 1)

            ward = Select(self.wait.until(EC.presence_of_element_located((By.ID, "wardId"))))

            # ward.select_by_value(self.data["wardId"])
            ward.select_by_visible_text(self.data["wardId"])

            # ====================================================
            # AREA
            # ====================================================

            self.wait.until(lambda d: len(Select(d.find_element(By.ID, "areaId")).options) > 1)

            area = Select(self.wait.until(EC.presence_of_element_located((By.ID, "areaId"))))

            # area.select_by_value(self.data["areaId"])
            area.select_by_visible_text(self.data["areaId"])

            # ====================================================
            # LOCATION
            # ====================================================

            self.wait.until(lambda d: len(Select(d.find_element(By.ID, "locationId")).options) > 1)

            location = Select(self.wait.until(EC.presence_of_element_located((By.ID, "locationId"))))
            # location.select_by_value(self.data["locationId"])
            location.select_by_visible_text(self.data["locationId"])

            # ====================================================
            # STREET
            # ====================================================

            self.wait.until(lambda d: len(Select(d.find_element(By.ID, "streetId")).options) > 1)

            street = Select(self.wait.until(EC.presence_of_element_located((By.ID, "streetId"))))

            # street.select_by_value(self.data["streetId"])
            street.select_by_visible_text(self.data["streetId"])
            
            # ====================================================
            # PINCODE
            # ====================================================

            pin = self.wait.until(EC.presence_of_element_located((By.ID, "pinCode")))

            pin.clear()

            pin.send_keys(self.data["pincode"])

            self.process(log="Manual Property Details Filled")

        time.sleep(5)

    # ============================================================
    # CONTACT DETAILS
    # ============================================================

    def fill_contact_details(self):

        self.wait.until(EC.presence_of_element_located((By.ID, "remitterName"))).send_keys(self.data["auth_name"])

        self.wait.until(EC.presence_of_element_located((By.ID, "remitterMobileNo"))).send_keys(self.data["auth_mobile_no"])

        self.process(log="Contact Details Filled")

    # ============================================================
    # DIRECTOR / EMPLOYEE TABLE
    # ============================================================

    def fill_director_employee_table(self):

        grid = self.data["details_dir_emp"]

        table = self.wait.until(EC.presence_of_element_located((By.XPATH,"(//table//table//table//table)[2]")))

        rows = table.find_elements(By.XPATH, ".//tr")

        rows = rows[1:]

        row_index = 0

        self.process(log="Director and Employee Details Fetching",progress=75)
        
        for row in rows:

            cols = row.find_elements(By.XPATH, "./td")

            if len(cols) >= 5 and row_index < len(grid["rows"]):

                row_data = grid["rows"][row_index]

                # =================================================
                # DIRECTORS
                # =================================================

                try:

                    director_input = cols[3].find_element(By.XPATH,".//input")

                    self.driver.execute_script("arguments[0].scrollIntoView();",director_input)

                    director_input.clear()

                    director_input.send_keys(str(row_data["directors"]))

                except Exception as e:

                    log.info(f"Director Input Error : {e}")

                # =================================================
                # EMPLOYEES
                # =================================================

                try:

                    employee_input = cols[4].find_element(By.XPATH,".//input")

                    employee_input.clear()

                    employee_input.send_keys(str(row_data["employees"]))

                except Exception as e:

                    log.info(f"Employee Input Error : {e}")

                row_index += 1

        self.process(log="Director / Employee Table Filled")

    # ============================================================
    # FINANCIAL DETAILS
    # ============================================================

    def fill_financial_details(self):
        
        self.wait.until(EC.presence_of_element_located((By.ID, "halfYearlyGrossIncome"))).send_keys(str(self.data.get("halfYearlyGrossIncome") or "0"))
        
        self.wait.until(EC.presence_of_element_located((By.ID, "commdate"))).send_keys(self.data["doi"])

        # If remarks is missing/None/blank, this is a fresh filing —
        # default it to "New Registration" instead of sending an empty value.
        remarks_value = self.data.get("remarks") or "New Registration"
        self.wait.until(EC.presence_of_element_located((By.ID, "remarks"))).send_keys(remarks_value)

        self.process(log="Financial Details Filled")

    # ============================================================
    # SUBMIT FORM
    # ============================================================

    def submit_form(self):

        self.wait.until(EC.element_to_be_clickable((By.XPATH,"//input[@type='submit' or @value='Submit']"))).click()
        time.sleep(2)
        self.wait.until(EC.element_to_be_clickable((By.XPATH,"//input[@type='submit' or @value='Submit']"))).click()
        self.process(log="Submit the form page",progress=93)
        time.sleep(5)
        ptnan = self.wait.until(EC.presence_of_element_located((By.XPATH,"//td[contains(text(),'PTNAN:')]/following-sibling::td/span"))).text
        log.info(f"PT Number : {ptnan}")
        self.process(log="Get the Professional Number",result=f"Professional Tax - {ptnan}",progress=95)
        return 

    # ============================================================
    # MAIN EXECUTION
    # ============================================================

    def run(self):

        try:
            time.sleep(2)
            self.process(log="Page Configuration",progress=5)
            self.setup_driver()
            time.sleep(2)
            self.process(log="Page Login Started",progress=10)
            self.login()
            time.sleep(2)
            self.process(log="Create service request",progress=15)
            self.create_service_request()
            time.sleep(2)
            self.process(log="Select service",progress=20)
            self.select_service()
            time.sleep(2)
            self.process(log="Starting the filings",progress=30)
            self.handle_popup()
            time.sleep(2)
            self.process(log="filling basic details",progress=40)
            self.fill_basic_details()
            time.sleep(2)
            self.process(log="filling property details",progress=50)
            self.fill_property_details()
            time.sleep(2)
            self.process(log="filling contact details",progress=60)
            self.fill_contact_details()
            time.sleep(2)
            self.process(log="filling director employee table",progress=70)
            self.fill_director_employee_table()
            time.sleep(2)
            self.process(log="filling financial details",progress=80)
            self.fill_financial_details()
            time.sleep(2)
            self.process(log="Filings Submitting",progress=90)          
            result = self.submit_form()
            time.sleep(2)
            self.process(log="Successsfully closed",progress=100)
            self.driver.quit()

            return result

        except Exception as e:
            log.error(e)

            if self.driver:
                self.driver.quit()

            return {
                "Status": "500",
                "Message": f"Error - {str(e)}"
            }


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    data1 = {"rows":[{"directors":1,"employees":0},{"directors":0,"employees":1},{"directors":1,"employees":0},{"directors":0,"employees":1},{"directors":1,"employees":0},{"directors":0,"employees":1}]}

    data = {"username":"josephdy","password":"Vignesh@123","comp_name":"IndiaFilings","categoryType":"2","categorySelect":"8","propertyTaxNumber":"0","auth_name":"ABDUR RAHIM","auth_mobile_no":"7418306307","details_dir_emp":data1,"doi":"25/11/2025","halfYearlyGrossIncome":"0","remarks":"New Regr","doorno":"10","zoneId":"N05","wardId":"N059","areaId":"Anna Salai","locationId":"Anna Salai","streetId":"ANNA SALAI","pincode":"55555"}

    obj = Chennai_Professional_Tax(data)

    result = obj.run()

    print(result)