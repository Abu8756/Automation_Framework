import base64
import requests
import time
import urllib3

urllib3.disable_warnings(
    urllib3.exceptions.InsecureRequestWarning
)

class IncomeTaxNotice:

    def __init__(self):
        self.BASE = "https://eportal.incometax.gov.in"

        self.session = requests.Session()

        self.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,ta;q=0.8",
            "Connection": "keep-alive",
            "Content-Type": "application/json",
            "Origin": self.BASE,
            "Referer": self.BASE + "/iec/foservices/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
            "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"'
        })

        # First request to generate cookies
        self.session.get(self.BASE + "/iec/foservices/")

    def _headers(self, service_name=None):
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,ta;q=0.8",
            "Connection": "keep-alive",
            "Content-Type": "application/json",
            "Origin": self.BASE,
            "Referer": self.BASE + "/iec/foservices/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
            "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"'
        }

        # Add "sn" only if service_name is provided
        if service_name:
            headers["sn"] = service_name
        print(headers)
        return headers

    ########################################################
    # STEP 1
    ########################################################

    def validate_pan(self, pan):

        service_name = "wLoginService"

        payload = {
            "entity": pan,
            "serviceName": service_name
        }

        r = self.session.post(
            self.BASE + "/iec/loginapi/login",
            headers=self._headers(service_name),
           json=payload,    timeout=30
        )

        print(r.text)

        try:
            data = r.json()
        except ValueError:
            return {
                "registered": False,
                "message_code": None,
                "desc": "Invalid/empty response from server",
                "reqId": None,
                "entityType": None,
                "role": None
            }

        messages = data.get("messages", [])
        message_code = messages[0].get("code") if messages else None
        desc = messages[0].get("desc") if messages else None

        if message_code == "EF00036":
            registered = False
        elif message_code == "EF00000":
            registered = True
        else:
            registered = None

        return {
            "registered": registered,
            "message_code": message_code,
            "desc": desc,
            "reqId": data.get("reqId"),
            "entityType": data.get("entityType"),
            "role": data.get("role")
        }

    ########################################################
    # STEP 2
    ########################################################

    def validate_password(
            self,
            pan,
            password,
            reqId,
            entityType,
            role):

        service_name = "loginService"

        password64 = base64.b64encode(
            password.encode()
        ).decode()
        print(pan,password64,reqId,entityType,role)
        payload = {
            "errors": [],
            "reqId": reqId,
            "entity": pan,
            "entityType": entityType,
            "role": role,
            "uidValdtnFlg": "true",
            "aadhaarMobileValidated": "false",
            "secAccssMsg": "India",
            "secLoginOptions": "",
            "dtoService": "LOGIN",
            "exemptedPan": "false",
            "userConsent": "",
            "imgByte": None,
            "pass": password64,
            "passValdtnFlg": None,
            "otpGenerationFlag": None,
            "otp": None,
            "otpValdtnFlg": None,
            "otpSourceFlag": None,
            "contactPan": None,
            "contactMobile": None,
            "contactEmail": None,
            "email": None,
            "mobileNo": None,
            "forgnDirEmailId": None,
            "imagePath": None,
            "serviceName": service_name
        }

        r = self.session.post(
            self.BASE + "/iec/loginapi/login",
            headers=self._headers(service_name),
           json=payload,
    timeout=30

        )
        print(r.text)

        try:
            data = r.json()
        except ValueError:
            return {
                "status_code": r.status_code,
                "valid": False,
                "message_code": None,
                "desc": "Invalid/empty response from server",
                "response": None
            }

        messages = data.get("messages", [])
        message_code = messages[0].get("code") if messages else None
        desc = messages[0].get("desc") if messages else None

        if message_code == "EF00027":
            valid = False
        elif message_code == "EF00000":
            valid = True
        else:
            valid = None

        return {
            "status_code": r.status_code,
            "valid": valid,
            "message_code": message_code,
            "desc": desc,
            "response": data
        }

    ########################################################
    # STEP 3
    ########################################################

    def save_entity(self, pan):

        service_name = "userProfileService"

        payload = {
            "serviceName": service_name,
            "userId": pan
        }

        r = self.session.post(
            self.BASE + "/iec/servicesapi/auth/saveEntity",
            headers=self._headers(service_name),
           json=payload,
    timeout=30

        )
        print(r.text)

        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text}

        return {
            "status_code": r.status_code,
            "response": data
        }
        
    def get_proceedings(self, pan):
        service_name = "eProceedingsPaginatedService"
        
        payload = {
            "serviceName": service_name,
            "pan": pan,
            "prcdngStatusFlag": "FYA",
            "prcdngTypeFlag": "self",
            "pageConfig": {
                "pageSize": 10,
                "pageNo": 1,
                "searchTerm": "",
                "sortBy": "createdDt",
                "sortAsc": False,
                "filters": {}
            },
            "header": {
                "formName": "FO-041_PCDNG"
            }
        }
        r = self.session.post(
            self.BASE + "/iec/returnservicesapi/auth/getEntity",
            headers=self._headers(service_name),
           json=payload,
    timeout=30

        )
        print("After Sessions",r.text)
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text}
        ids = []
        year_map = {}
        proceeding_map = {}
        for item in data.get("eProceedingPaginatedRequests", []):
            proceeding_id = item.get("proceedingReqId")
            ids.append(proceeding_id)
            year_map[proceeding_id] = item.get("assessmentYear")
            proceeding_map[proceeding_id] = item
        return {
            "status_code": r.status_code,
            "response": data,
            "proceeding_ids": ids,
            "proceeding_year_map": year_map,
            "proceeding_map": proceeding_map
        }
    
    def get_proceeding_details(self, pan, proceedingReqId):

        service_name = "eProceedingDetailsService"
        payload = {
            "serviceName": service_name,
            "proceedingReqId": proceedingReqId,
            "pan": pan,
            "header": {
                "formName": "FO-041_PCDNG"
            }
            }
        r = self.session.post(
            self.BASE + "/iec/returnservicesapi/auth/getEntity",
            headers=self._headers(service_name),
           json=payload,
    timeout=30

        )
        print(r.text)
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text}

        # The API can return MULTIPLE notice entries for the same
        # proceedingReqId (e.g. two Show Cause Notices under one
        # penalty proceeding). Return all of them, not just the first.
        notices = []
        if isinstance(data, list):
            for item in data:
                if item.get("headerSeqNo"):
                    notices.append({
                        "headerSeqNo": item.get("headerSeqNo"),
                        "proceedingReqId": item.get("proceedingReqId"),
                        "noticeSection": item.get("noticeSection"),
                        "description": item.get("description"),
                        "issuedOn": item.get("issuedOn"),
                    })

        return {
            "status_code": r.status_code,
            "response": data,
            "notices": notices
        }
               
    def get_notice_pdf(self, pan, headerSeqNo, proceedingReqId):

        service_name = "noticeletterpdf"

        payload = {
            "serviceName": service_name,
            "headerSeqNo": str(headerSeqNo),
            "procdngReqId": str(proceedingReqId),
            "loggedInUserId": pan,
            "header": {
                "formName": "FO-041_PCDNG"
            }
        }

        r = self.session.post(
            self.BASE + "/iec/returnservicesapi/auth/saveEntity",
            headers=self._headers(service_name),
           json=payload,
    timeout=30

        )

        return {
            "status_code": r.status_code,
            "response": r.json()
        }


    def get_document_base64(
        self,
        satDocId,
        proceedingReqId=None,
        headerSeqNo=None,
        proceeding_id=None
    ):
        """
        Download document and return Base64.
        """

        url = f"{self.BASE}/iec/document/{satDocId}"

        response = self.session.get(
            url,
            headers=self._headers(),   # No service_name required
            verify=False
        )

        response.raise_for_status()

        # Raw PDF bytes
        pdf_bytes = response.content

        # Convert to Base64
        pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")

        return {
            "proceeding_id": proceeding_id,
            "headerSeqNo": headerSeqNo,
            "proceedingReqId": proceedingReqId,
            "satDocId": satDocId,
            "base64": pdf_base64
        }

    ########################################################
    # KEY NORMALIZATION HELPER
    ########################################################

    @staticmethod
    def _lowercase_keys(obj):
        """
        Recursively lowercase every dict key in a nested
        structure of dicts/lists, leaving values untouched.
        """
        if isinstance(obj, dict):
            return {
                (k.lower() if isinstance(k, str) else k): IncomeTaxNotice._lowercase_keys(v)
                for k, v in obj.items()
            }
        elif isinstance(obj, list):
            return [IncomeTaxNotice._lowercase_keys(item) for item in obj]
        else:
            return obj

    ########################################################
    # COMPLETE LOGIN
    ########################################################
    
    def login(self, pan, password):

        # STEP-1
        time.sleep(5)
        print("STEP-1")
        step1 = self.validate_pan(pan)
        if not step1.get("registered"):
            return step1.get("desc") or "Pan Validation is Falied so check Pan Number in the Portal"

        # STEP-2
        time.sleep(5)
        print("STEP-2")
        step2 = self.validate_password(
            pan,
            password,
            step1["reqId"],
            step1["entityType"],
            step1["role"]
        )
        if step2.get("valid") is False:
            return step2.get("desc") or "Invalid Password, Please retry."

        # STEP-3
        time.sleep(5)
        print("STEP-3")
        step3 = self.save_entity(pan)

        # STEP-4
        time.sleep(5)
        print("STEP-4")
        proceedings = self.get_proceedings(pan)
        year_map = proceedings.get("proceeding_year_map", {})
        proceeding_map = proceedings.get("proceeding_map", {})

        # notices grouped by assessmentYear:
        # { "2024": [ {"eproceeding": {...}, "notice": {...}, "file": {...}}, ... ], ... }
        notices_by_year = {}

        for proceeding_id in proceedings["proceeding_ids"]:

            time.sleep(5)
            details = self.get_proceeding_details(
                pan,
                proceeding_id
            )

            assessment_year = year_map.get(proceeding_id)
            year_key = str(assessment_year) if assessment_year is not None else "unknown"
            eproceeding = proceeding_map.get(proceeding_id, {})

            for notice in details.get("notices", []):

                headerSeqNo = notice.get("headerSeqNo")
                proceedingReqId = notice.get("proceedingReqId")

                if not headerSeqNo or not proceedingReqId:
                    continue

                time.sleep(5)
                pdf = self.get_notice_pdf(
                    pan,
                    headerSeqNo,
                    proceedingReqId
                )

                response = pdf.get("response", {})
                satDocId = response.get("satDocId")

                if not satDocId:
                    continue

                time.sleep(5)
                document = self.get_document_base64(
                    satDocId=satDocId,
                    proceedingReqId=proceedingReqId,
                    headerSeqNo=headerSeqNo,
                    proceeding_id=proceeding_id
                )

                # match the parent eProceedingPaginatedRequests item
                # (by proceedingReqId) against this notice's own
                # proceedingReqId, in case they ever diverge
                matched_eproceeding = proceeding_map.get(proceedingReqId, eproceeding)

                notices_by_year.setdefault(year_key, []).append({
                    "eproceeding": matched_eproceeding,
                    "notice": notice,
                    "file": document
                })

        final_data = {
            "details": step3.get("response"),
            "notices": notices_by_year
        }

        # normalize every key in the final payload to lowercase
        return self._lowercase_keys(final_data)