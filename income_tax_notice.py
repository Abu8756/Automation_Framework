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

        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text}
        print(r.text)

        return {
            "status_code": r.status_code,
            "response": data,
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

        return {
            "status_code": r.status_code,
            "response": r.json()
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

        return {
            "status_code": r.status_code,
            "response": r.text
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
        for item in data.get("eProceedingPaginatedRequests", []):
            ids.append(item.get("proceedingReqId"))
        return {
            "status_code": r.status_code,
            "response": data,
            "proceeding_ids": ids
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
    # COMPLETE LOGIN
    ########################################################
    
    def login(self, pan, password):

        responses = []
        
        # STEP-1
        time.sleep(5)
        print("STEP-1")
        step1 = self.validate_pan(pan)
        responses.append(step1)
        
        if not step1.get("reqId"):
            return responses

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
        responses.append(step2)

        # STEP-3
        time.sleep(5)
        print("STEP-3")
        step3 = self.save_entity(pan)
        responses.append(step3)

        
        # STEP-4
        time.sleep(5)
        print("STEP-4")
        proceedings = self.get_proceedings(pan)
        responses.append(proceedings)

        notice_base64 = []

        for proceeding_id in proceedings["proceeding_ids"]:

            time.sleep(5)
            details = self.get_proceeding_details(
                pan,
                proceeding_id
            )

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
                # carry along a bit of context for identification
                document["noticeSection"] = notice.get("noticeSection")
                document["description"] = notice.get("description")

                notice_base64.append(document)

        responses.append({
            "notice": {
                "proceeding_ids": proceedings["proceeding_ids"],
                "count": len(notice_base64),
                "notice_base64": notice_base64
            }
        })

        return responses