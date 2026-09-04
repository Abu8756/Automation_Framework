"""
application.py
---------------
Application entry point: imports the generic AutomationFramework, wires up
every portal-specific service (Udyam, Startup India, EPFO, ITR notice,
Udyam certificate, ...) and its validation schema, then starts the server.

All Flask/Waitress runtime settings (host, port, debug, which server to
use) come from CONFIG below, which reads environment variables with sane
defaults -- nothing about how the server is served is hardcoded past this
section.
"""

import os
import re

from automation_framework import (
    AutomationFramework,
    AutomationService,
    json_top_level_keys,
    json_extract_values,
    json_build_choice_map,
)


# ======================================================================
# CONFIG -- everything about *how* this app is served lives here.
# Override any of these with environment variables; nothing else in this
# file needs to change to switch server / host / port / debug mode.
# ======================================================================
CONFIG = {
    "HOST": os.environ.get("APP_HOST", "0.0.0.0"),
    "PORT": int(os.environ.get("APP_PORT", "3333")),
    "DEBUG": os.environ.get("APP_DEBUG", "false").strip().lower() in ("1", "true", "yes", "on"),
    # "flask"    -> Flask's built-in dev server (app.run), fine for local dev
    # "waitress" -> production-grade WSGI server, recommended for real deployments
    "SERVER": os.environ.get("APP_SERVER", "flask").strip().lower(),
    "LOG_PATH": os.environ.get("APP_LOG_PATH", "logs/sessions.log"),
    "THREADS": int(os.environ.get("APP_THREADS", "8")),  # waitress-only
}

framework = AutomationFramework(__name__, log_path=CONFIG["LOG_PATH"], port=CONFIG["PORT"])
app = framework.app  # exposed for WSGI servers / gunicorn-style launchers


_ALPHA_ONLY_RE      = re.compile(r"^[A-Za-z\s]+$")            # letters and spaces only (names)
_SINGLE_DIGIT_RE    = re.compile(r"^\d$")                     # exactly one numeric digit
_NUMERIC_ONLY_RE    = re.compile(r"^\d+$")                    # digits only, any length
_MOBILE_RE          = re.compile(r"^\d{10}$")                 # exactly 10 digits
_AADHAAR_RE         = re.compile(r"^\d{12}$")                 # exactly 12 digits
_PIN_6_RE           = re.compile(r"^\d{6}$")                  # exactly 6 digits (generic PIN code)
_DATE_DDMMYYYY_RE   = re.compile(r"^\d{2}/\d{2}/\d{4}$")      # DD/MM/YYYY
_PAN_RE             = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")  # e.g. ABCDE1234F
_EMAIL_RE           = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")  # local@domain.tld
_OPTIONAL_MOBILE_RE = re.compile(r"^(\d{10})?$")              # blank OR exactly 10 digits

# -------- EPFO-specific --------
_EPFO_USERNAME_RE   = re.compile(r"^[A-Z]{5}[0-9]{10}$")      # 5 uppercase letters + 10 digits
_UAN_RE             = re.compile(r"^\d{12}$")                 # UAN — 12 digits (same shape as Aadhaar, kept separate for its own message)
_SINGLE_ALPHA_RE    = re.compile(r"^[A-Za-z]$")                # exactly one alphabetic character (marital status code)

# -------- udyam_certificate-specific --------
_UDYAM_RE           = re.compile(r"^UDYAM-[A-Z]{2}-\d{2}-\d{7}$")  # e.g. UDYAM-KR-06-0043268


_ADDRESS_SCHEMA = {
    "flat":     {"type": str, "required": True},
    "building": {"type": str, "required": False},
    "village":  {"type": str, "required": False},
    "block":    {"type": str, "required": False},
    "road":     {"type": str, "required": False},
    "city":     {"type": str, "required": True},
    "pin": {
        "type": str, "required": True,
        "pattern": _PIN_6_RE,
        "pattern_message": "'pin' must be exactly 6 digits",
    },
    "state": {
        "type": str, "required": True,
        "pattern": _NUMERIC_ONLY_RE,
        "pattern_message": "'state' must be numeric only",
    },
    "district": {
        "type": str, "required": True,
        "pattern": _NUMERIC_ONLY_RE,
        "pattern_message": "'district' must be numeric only",
    },
}

# -------- allowed values for udyamreg dropdown/radio fields --------
ORG_TYPE_CHOICES = [str(n) for n in range(1, 12)]              # 1..11
SOCIAL_CATEGORY_CHOICES = ["1", "2", "3", "4"]
GENDER_CHOICES = ["1", "2", "3"]
DIVYANG_CHOICES = ["0", "1"]
MAJOR_ACTIVITY_CHOICES = ["1", "2"]
MAJOR_ACTIVITY_UNDER_SERVICES_CHOICES = ["1", "2"]
PREVIOUS_EM_CHOICES = ["0", "2", "4"]
NIC_ACTIVITY_CHOICES = ["1", "2", "3"]
COMMENCED_CHOICES = ["0", "1"]
GST_CHOICES = ["0", "1", "2"]
CATEGORY_CHOICES = ["0", "1", "2", "3"]

@framework.service("udyamreg", schema={
    # -------- Aadhaar Verification --------
    "aadhaar_name": {
        "type": str, "required": True,
        "pattern": _ALPHA_ONLY_RE,
        "pattern_message": "'aadhaar_name' must contain alphabetic characters only",
    },
    "aadhaar_number": {
        "type": str, "required": True,
        "pattern": _AADHAAR_RE,
        "pattern_message": "'aadhaar_number' must be exactly 12 digits",
    },

    # -------- PAN Verification --------
    "org_type": {
        "type": str, "required": True,
        "pattern": _SINGLE_DIGIT_RE,
        "pattern_message": "'org_type' must be a single numeric digit",
        "choices": ORG_TYPE_CHOICES,
        "choices_message": f"'org_type' must be one of: {', '.join(ORG_TYPE_CHOICES)}",
    },
    "pan_number": {
        "type": str, "required": True,
        "pattern": _PAN_RE,
        "pattern_message": "'pan_number' must be a valid PAN (e.g. ABCDE1234F)",
    },
    "pan_name": {
        "type": str, "required": True,
        "pattern": _ALPHA_ONLY_RE,
        "pattern_message": "'pan_name' must contain alphabetic characters only",
    },
    "dob": {
        "type": str, "required": True,
        "pattern": _DATE_DDMMYYYY_RE,
        "pattern_message": "'dob' must be in DD/MM/YYYY format",
    },

    # -------- Investment / Turnover --------
    "wdv": {
        "type": str, "required": True,
        "pattern": _NUMERIC_ONLY_RE,
        "pattern_message": "'wdv' must be numeric only",
    },
    "exclusion_cost": {
        "type": str, "required": True,
        "pattern": _NUMERIC_ONLY_RE,
        "pattern_message": "'exclusion_cost' must be numeric only",
    },
    "total_turnover": {
        "type": str, "required": True,
        "pattern": _NUMERIC_ONLY_RE,
        "pattern_message": "'total_turnover' must be numeric only",
    },

    # -------- Basic Details --------
    "company_name": {"type": str, "required": True},
    "mobile": {
        "type": str, "required": True,
        "pattern": _MOBILE_RE,
        "pattern_message": "'mobile' must be exactly 10 digits",
    },
    "email": {
        "type": str, "required": True,
        "pattern": _EMAIL_RE,
        "pattern_message": "'email' must be a valid email address (e.g. name@domain.com)",
    },
    "social_category": {
        "type": str, "required": True,
        "pattern": _SINGLE_DIGIT_RE,
        "pattern_message": "'social_category' must be a single numeric digit",
        "choices": SOCIAL_CATEGORY_CHOICES,   # 1=General 2=SC 3=ST 4=OBC
        "choices_message": f"'social_category' must be one of: {', '.join(SOCIAL_CATEGORY_CHOICES)}",
    },
    "gender": {
        "type": str, "required": True,
        "pattern": _SINGLE_DIGIT_RE,
        "pattern_message": "'gender' must be a single numeric digit",
        "choices": GENDER_CHOICES,
        "choices_message": f"'gender' must be one of: {', '.join(GENDER_CHOICES)}",
    },
    "divyang": {
        "type": str, "required": True,
        "pattern": _SINGLE_DIGIT_RE,
        "pattern_message": "'divyang' must be a single numeric digit",
        "choices": DIVYANG_CHOICES,   # 0=No 1=Yes
        "choices_message": f"'divyang' must be one of: {', '.join(DIVYANG_CHOICES)}",
    },

    # -------- Addresses (nested — validated field by field) --------
    "official_address": {"type": dict, "required": True, "schema": _ADDRESS_SCHEMA},
    # units is a dict of UNKNOWN keys (unit names), each shaped like an address
    "units": {"type": dict, "required": True, "each": _ADDRESS_SCHEMA},

    # -------- Status of Enterprise --------
    "previous_em": {
        "type": str, "required": True,
        "pattern": _SINGLE_DIGIT_RE,
        "pattern_message": "'previous_em' must be a single numeric digit",
        "choices": PREVIOUS_EM_CHOICES,   # 0=N/A 2=EM-II 4=Previous UAM
        "choices_message": f"'previous_em' must be one of: {', '.join(PREVIOUS_EM_CHOICES)}",
    },
    "incorporation_date": {
        "type": str, "required": True,
        "pattern": _DATE_DDMMYYYY_RE,
        "pattern_message": "'incorporation_date' must be in DD/MM/YYYY format",
    },
    "commenced": {
        "type": str, "required": True,
        "pattern": _SINGLE_DIGIT_RE,
        "pattern_message": "'commenced' must be a single numeric digit",
        "choices": COMMENCED_CHOICES,   # 1=Yes 0=No
        "choices_message": f"'commenced' must be one of: {', '.join(COMMENCED_CHOICES)}",
    },
    # only required when commenced == "1"
    "commencement_date": {
        "type": str, "required_if": {"field": "commenced", "equals": "1"},
        "pattern": _DATE_DDMMYYYY_RE,
        "pattern_message": "'commencement_date' must be in DD/MM/YYYY format",
    },

    # -------- Bank Details --------
    "bank_name":      {"type": str, "required": True},
    "ifsc":           {"type": str, "required": True},
    "account_number": {"type": str, "required": True},

    # -------- Major Activity --------
    "major_activity": {
        "type": str, "required": True,
        "pattern": _SINGLE_DIGIT_RE,
        "pattern_message": "'major_activity' must be a single numeric digit",
        "choices": MAJOR_ACTIVITY_CHOICES,   # 1=Manufacturing 2=Services
        "choices_message": f"'major_activity' must be one of: {', '.join(MAJOR_ACTIVITY_CHOICES)}",
    },
    "major_activity_under_services": {
        "type": str, "required_if": {"field": "major_activity", "equals": "2"},
        "pattern": _SINGLE_DIGIT_RE,
        "pattern_message": "'major_activity_under_services' must be a single numeric digit",
        "choices": MAJOR_ACTIVITY_UNDER_SERVICES_CHOICES,
        "choices_message": f"'major_activity_under_services' must be one of: {', '.join(MAJOR_ACTIVITY_UNDER_SERVICES_CHOICES)}",
    },
    "nic_activity": {
        "type": str, "required": True,
        "pattern": _NUMERIC_ONLY_RE,
        "pattern_message": "'nic_activity' must be numeric only",
        "choices": NIC_ACTIVITY_CHOICES,
        "choices_message": f"'nic_activity' must be one of: {', '.join(NIC_ACTIVITY_CHOICES)}",
    },
    "nic": {"type": dict, "required": True, "schema": {
        "nic2": {
            "type": str, "required": True,
            "pattern": _NUMERIC_ONLY_RE,
            "pattern_message": "'nic2' must be numeric only",
        },
        "nic4": {
            "type": str, "required": True,
            "pattern": _NUMERIC_ONLY_RE,
            "pattern_message": "'nic4' must be numeric only",
        },
        "nic5": {
            "type": str, "required": True,
            "pattern": _NUMERIC_ONLY_RE,
            "pattern_message": "'nic5' must be numeric only",
        },
    }},

    # -------- Employees --------
    "employees": {"type": dict, "required": True, "schema": {
        "male": {
            "type": str, "required": True,
            "pattern": _NUMERIC_ONLY_RE,
            "pattern_message": "'male' must be numeric only",
        },
        "female": {
            "type": str, "required": True,
            "pattern": _NUMERIC_ONLY_RE,
            "pattern_message": "'female' must be numeric only",
        },
        "others": {
            "type": str, "required": True,
            "pattern": _NUMERIC_ONLY_RE,
            "pattern_message": "'others' must be numeric only",
        },
    }},
})
class UdyamRegService(AutomationService):
    def run(self, data):
        from udyam_reg_v2 import UdyamRegistration

        self.add_log("Launching Udyam automation")
        # Pass this AutomationService instance itself (not a raw session_id/
        # sessions dict) — UdyamRegistration has no session, logging, or OTP
        # storage of its own; it calls back into this service for all of
        # that, so the framework is the single source of truth (same pattern
        # as StartupIndiaService above).
        obj = UdyamRegistration(data=data, service=self)
        return obj.run()

_DIR_EMP_ROW_SCHEMA = {
    "directors": {"type": int, "required": True},
    "employees": {"type": int, "required": True},
}

# -------- Chennai-specific regex (city-scoped pincode; shared patterns live near the top of the file) --------
_PINCODE_RE = re.compile(r"^600\d{3}$")                 # must start with 600, 6 digits total
_PROFESSIONAL_TAX_NUMBER = re.compile(r"^\d{2}-\d{3}-\d{5}-\d{3}$")
CATEGORY_OPTIONS = {
    "1": {
        "option_name": "Central Government",
        "sub": [
            {"option_name": "BANKS", "value": "72"},
            {"option_name": "CENTRAL GOVERNMENT EMPLOYEES", "value": "2"},
            {"option_name": "CENTRAL PUBLIC SECTOR EMPLOYEES", "value": "4"},
        ],
    },
    "2": {
        "option_name": "Private Establishment",
        "sub": [
            {"option_name": "TRADERS", "value": "6"},
            {"option_name": "PUBLIC LIMITED COMPANIES", "value": "7"},
            {"option_name": "PRIVATE LIMITED COMPANIES", "value": "8"},
            {"option_name": "SOCIETIES", "value": "9"},
            {"option_name": "TRUSTS", "value": "10"},
        ],
    },
    "3": {
        "option_name": "Individual",
        "sub": [
            {"option_name": "CONSULTANTS", "value": "55"},
            {"option_name": "ENGINEERS", "value": "54"},
            {"option_name": "ADVOCATES", "value": "52"},
            {"option_name": "DOCTORS", "value": "53"},
            {"option_name": "PROFESSIONALS", "value": "5"},
            {"option_name": "OTHERS", "value": "11"},
        ],
    },
    "5": {
        "option_name": "State Government",
        "sub": [
            {"option_name": "TAMILNADU GOVERNMENT EMPLOYEES", "value": "1"},
            {"option_name": "TAMILNADU PUBLIC SECTOR EMPLOYEE", "value": "3"},
        ],
    },
}

CATEGORY_TYPE_CHOICES = json_top_level_keys(CATEGORY_OPTIONS)                  # ["1", "2", "3", "5"]
CATEGORY_SUB_TYPE_CHOICES = json_extract_values(CATEGORY_OPTIONS, "value")     # flat list, all sub-values
CATEGORY_SUB_TYPE_MAP = json_build_choice_map(CATEGORY_OPTIONS)                # {"1": ["72","2","4"], "2": [...], ...}

@framework.service("chennaipt", schema={
    "username": {"type": str, "required": True, "default": "ABDUR RAHIM"},
    "password": {"type": str, "required": True, "default": "ABDURAHIM@123"},
    "comp_name": {"type": str, "required": True},
    "category_type": {
        "type": str, "required": True,
        "pattern": _SINGLE_DIGIT_RE,
        "pattern_message": "'category_type' must be a single numeric digit",
        "choices": CATEGORY_TYPE_CHOICES,
        "choices_message": f"'category_type' must be one of: {', '.join(CATEGORY_TYPE_CHOICES)}",
    },
    "category_sub_type": {
        "type": str, "required": True,
        "pattern": _NUMERIC_ONLY_RE,
        "pattern_message": "'category_sub_type' must be numeric only",
        "depends_on": "category_type",
        "choices_map": CATEGORY_SUB_TYPE_MAP,
        "depends_on_message": "'category_sub_type' cannot be validated: 'category_type' is missing or invalid",
        "choices_message": "'category_sub_type' is not a valid sub-category for the given 'category_type'",
    },

    "property_tax_number": {
        "type": str,
        "required_if_empty": {"field": "zone"},
        "pattern":_PROFESSIONAL_TAX_NUMBER,
    },
    "authorized_person": {"type": str, "required": True},
    "authorized_mobile_number": {
        "type": str, "required": True,
        "pattern": _MOBILE_RE,
        "pattern_message": "'authorized_mobile_number' must be exactly 10 digits",
    },

    "details_dir_emp": {"type": dict, "required": True, "schema": {
        "rows": {"type": list, "required": True, "items": _DIR_EMP_ROW_SCHEMA},
    }},

    "date_of_commencement": {
        "type": str, "required": True,
        "pattern": _DATE_DDMMYYYY_RE,
        "pattern_message": "'Date_of_commencement' must be in DD/MM/YYYY format",
    },
    "half_yearly_gross_income": {
        "type": str, "required": False,   # defaults to "0" in fill_financial_details
        "pattern": _NUMERIC_ONLY_RE,
        "pattern_message": "'half_yearly_gross_income' must be numeric only",
    },
    "remarks": {"type": str, "required": False},                 # defaults to "New Registration" if blank/missing

    # -------- only required when property_tax_number == "0" (manual address) --------
    "building_name": {"type": str, "required_if": {"field": "property_tax_number", "equals": "0"}, "allow_blank": True},
    "door_no":    {"type": str, "required_if": {"field": "property_tax_number", "equals": "0"}},
    "zone":       {"type": str, "required_if": {"field": "property_tax_number", "equals": "0"}},
    "ward_or_division": {"type": str, "required_if": {"field": "property_tax_number", "equals": "0"}},
    "area":       {"type": str, "required_if": {"field": "property_tax_number", "equals": "0"}},
    "locality":   {"type": str, "required_if": {"field": "property_tax_number", "equals": "0"}},
    "street":     {"type": str, "required_if": {"field": "property_tax_number", "equals": "0"}},
    "pincode": {
        "type": str, "required_if": {"field": "property_tax_number", "equals": "0"},
        "pattern": _PINCODE_RE,
        "pattern_message": "'pincode' must start with 600 and be exactly 6 digits",
    },
})
class ChennaiProfessionalTaxService(AutomationService):
    def run(self, data):
        from Chennai_Professional_Tax import Chennai_Professional_Tax as CPT

        if not data.get("remarks"):
            data["remarks"] = "New Registration"
            self.add_log("No remarks supplied — defaulting to 'New Registration'")

        self.add_log("Launching Chennai Professional Tax filing")

        obj = CPT(data=data, session=self.framework.sessions.get(self.session_id))
        obj.logger = self
        return obj.run()

INDUSTRY_SECTOR_OPTIONS = {
    "Analytics": ["Big Data", "Business Intelligence", "Data Science", "Others"],
    "Advertising": ["Adtech", "Online Classified", "Others"],
    "Architecture Interior Design": ["Others"],
    "AR VR (Augmented + Virtual Reality)": ["Others"],
    "Automotive": ["Auto & Truck Manufacturers", "Auto Truck & Motor Cycle Parts", "Electric Vehicles", "Tires And Rubber Products", "Others"],
    "Art & Photography": ["Art", "Handicraft", "Photography", "Others"],
    "Animation": ["Others"],
    "Chemicals": ["Agricultural Chemicals", "Commodity Chemicals", "Diversified Chemicals", "Specialty Chemicals", "Others"],
    "Computer Vision": ["Others"],
    "Telecommunication & Networking": ["Integrated Communication services", "Network Technology Solutions", "Wireless", "Others"],
    "Construction": ["Construction & Engineering", "Construction Materials", "Construction Suppliers & Fixtures", "Homebuilding", "New – Age Construction Technologies", "Others"],
    "Agriculture": ["Agri – Tech", "Animal Husbandry", "Dairy Farming", "Fisheries", "Food Processing", "Horticulture", "Organic Agriculture", "Others"],
    "Aeronautics Aerospace & defense": ["Aviation & Others", "Defence Equipment", "Drones", "Space Technology", "Others"],
    "AI": ["Machine Learning", "NLP", "Others"],
    "Green Technology": ["Clean Tech", "Waste Management", "Others"],
    "Events": ["Events Management", "Weddings", "Others"],
    "Fashion": ["Apparel", "Fan Merchandise", "Fashion Technology", "Jewellery", "Lifestyle", "Others"],
    "Finance Technology": ["Accounting", "Billing and Invoicing", "Bitcoin and Block chain", "Business Finance", "Crowd funding", "Foreign Exchange", "Insurance", "Micro Finance", "Mobile Wallets Payments", "P2P Lending", "Payment Platforms", "Personal Finance", "Point Of Sales", "Trading", "Others"],
    "Enterprise Software": ["CXM", "Cloud", "Collaboration", "Customer Support", "ERP", "Enterprise Mobility", "Location Based", "SCM", "Others"],
    "Food And Beverage": ["Food Processing", "Food Technology / Food Delivery", "Microbrewery", "Restaurants", "Others"],
    "Design": ["Industrial Design", "Web Design", "Others"],
    "Dating Matrimonial": ["Others"],
    "Education": ["Coaching", "E-Learning", "Education Technology", "Skill Development", "Others"],
    "Renewable Energy": ["Manufacture Of Electrical Equipment", "Manufacture Of Machinery & Equipment", "Renewable Energy Solutions", "Renewable Nuclear Energy", "Renewable Solar Energy", "Renewable Wind Energy", "Others"],
    "Technology Hardware": ["3D Printing", "Electronics", "Embedded", "Manufacturing", "Semiconductor", "Others"],
    "Healthcare And Life science": ["Assistance Technology", "Biotechnology", "Health And Wellness", "Healthcare IT", "Healthcare Services", "Healthcare Technology", "Medical Devices Biomedical", "Pharmaceutical", "Others"],
    "Internet Of Things": ["Manufacturing & Warehouse", "Smart Home", "Wearable's", "Others"],
    "IT Services": ["Application Development", "BPO", "IT Consulting", "IT Management", "KPO", "Product Development", "Project Management", "Testing", "Web Development", "Others"],
    "Human Resources": ["Internships", "Requirement Jobs", "Skills Assessments", "Talent Management", "Training", "Others"],
    "Marketing": ["Branding", "Digital Marketing (SEO Automation)", "Discovery", "Loyalty", "Market Research", "Sales", "Others"],
    "Nanotechnology": ["Others"],
    "Non-renewable Energy": ["Oil & Gas Drilling", "Oil & Gas Exploration And Production", "Oil & Gas Transportation Service", "Oil Related Services And Equipment", "Others"],
    "Pets And Animals": ["Others"],
    "Media And Entertainment": ["Digital Media", "Digital Media Blogging", "Digital Media News", "Digital Media Publishing", "Digital Media Video", "Entertainment", "Movies", "OOH Media", "Social Media", "Others"],
    "Retail": ["Comparison Shopping", "Retail Technology", "Social Commerce", "Others"],
    "House-Hold Services": ["Baby Care", "Home Care", "Laundry", "Personal Care", "Others"],
    "Professional & Commercial Services": ["Business Support Services", "Business Support Supplies", "Commercial Printing Services", "Employment Services", "Environmental Services & Equipments", "Professional Information Services", "Others"],
    "Sports": ["Fantasy Sports", "Sports Promotion And Networking", "Others"],
    "Social Impact": ["Corporate Social Responsibility", "NGO", "Others"],
    "Social Network": ["Others"],
    "Textiles and Apparels": ["Apparel & Accessories", "Leather Footwear", "Leather Textiles Goods", "Non – Leather Footwear", "Non – Leather Textiles Goods", "Others"],
    "Indic Language Startups": ["E-Commerce", "Education", "Media And Entertainment", "Natural Language Processing", "Social Media", "Utility Services"],
    "Transportation And Storage": ["Freight & Logistics Services", "Passenger Transportation Services", "Traffic Management", "Transport Infrastructure", "Others"],
    "Logistics": ["Others"],
    "Travel And Tourisms": ["Experiential Travel", "Facility Management", "Holiday Rentals", "Hospitality", "Hotel", "Ticketing", "Wayside Amenities", "Others"],
    "Security Solutions": ["Cyber Security", "Home Security Solutions", "Public Citizen Security Solutions", "Others"],
    "Airport Operations": ["Others"],
    "Real Estate": ["Co working Spaces", "Housing", "Others"],
    "Other Specialty retailers": ["Auto Vehicles Parts & Service Retailers", "Computer & Electronics Retailers", "Home Furnishings Retailers", "Home Improvement Products & Service Retailers", "Others"],
    "Safety": ["Personal Security", "Others"],
    "Robotics": ["Robotics Application", "Robotics Technology", "Others"],
    "Passenger Experience": ["Others"],
    "Biotechnology": ["Others"],
    "Waste Management": ["Others"],
    "Others": ["Others"],
    "Toys and Games": ["Physical Toys And Games", "Virtual Games"],
}

STARTUP_CATEGORY_CHOICES = [
    "Governments", "Hyperlocal", "Discovery", "Location Based Services", "Manufacturing", "Marketplace",
    "Mobile", "Offline", "Online Aggregator", "Peer to Peer", "Platform", "Consulting",
    "Consumer Internet", "Engineering", "E-commerce", "Others", "Rental", "Enterprise Mobility",
    "Research", "Sharing Economy", "Social Enterprise", "SaaS", "Subscription Commerce",
]

INDUSTRY_CHOICES = list(INDUSTRY_SECTOR_OPTIONS.keys())

_STARTUP_ADDRESS_SCHEMA = {
    "address1": {"type": str, "required": True},
    "address2": {"type": str, "required": False, "allow_blank": True},
    "state":    {"type": str, "required": False, "allow_blank": True},
    "district": {"type": str, "required": False, "allow_blank": True},
    "city":     {"type": str, "required": False, "allow_blank": True},
    "pincode": {
        "type": str, "required": True,
        "pattern": _PIN_6_RE,
        "pattern_message": "'pincode' must be exactly 6 digits",
    },
}

_DIRECTOR_GENDER_CHOICES = ["Male", "Female", "Other"]

_DIRECTOR_SCHEMA = {
    "Name": {
        "type": str, "required": True,
        "pattern": _ALPHA_ONLY_RE,
        "pattern_message": "director 'Name' must contain alphabetic characters only",
    },
    "Gender": {
        "type": str, "required": True,
        "choices": _DIRECTOR_GENDER_CHOICES,
        "choices_message": f"director 'Gender' must be one of: {', '.join(_DIRECTOR_GENDER_CHOICES)}",
    },
    "Address": {"type": str, "required": True},
    "Mobile_no": {
        "type": str, "required": False, "allow_blank": True,
        "pattern": _OPTIONAL_MOBILE_RE,
        "pattern_message": "director 'Mobile_no' must be blank or exactly 10 digits",
    },
    "Email": {
        "type": str, "required": True,
        "pattern": _EMAIL_RE,
        "pattern_message": "director 'Email' must be a valid email address (e.g. name@domain.com)",
    },
    "Dob": {
        "type": str, "required": True,
        "pattern": _DATE_DDMMYYYY_RE,
        "pattern_message": "director 'Dob' must be in DD/MM/YYYY format",
    },
}

@framework.service("startup_india", schema={
    "comp_address": {"type": dict, "required": True, "schema": _STARTUP_ADDRESS_SCHEMA},
    "directors": {"type": list, "required": True, "items": _DIRECTOR_SCHEMA},
    "business": {"type": str, "required": True},
    "designation": {"type": str, "required": True},
    "username": {"type": str, "required": True},
    "mobile": {
        "type": str, "required": True,
        "pattern": _MOBILE_RE,
        "pattern_message": "'mobile' must be exactly 10 digits",
    },
    "email_id": {
        "type": str, "required": True,
        "pattern": _EMAIL_RE,
        "pattern_message": "'email_id' must be a valid email address (e.g. name@domain.com)",
    },
    "password": {"type": str, "required": True},
    "website": {"type": str, "required": False},
    "Industry": {
        "type": str, "required": True,
        "choices": INDUSTRY_CHOICES,
        "choices_message": "'Industry' must be one of the recognised Startup India industries",
    },
    # Sector is validated against the sub-list for whichever Industry was given —
    # same depends_on/choices_map pattern used for category_sub_type in chennaipt below.
    "Sector": {
        "type": str, "required": True,
        "depends_on": "Industry",
        "choices_map": INDUSTRY_SECTOR_OPTIONS,
        "depends_on_message": "'Sector' cannot be validated: 'Industry' is missing or invalid",
        "choices_message": "'Sector' is not a valid sector for the given 'Industry'",
    },
    "Catogries": {
        "type": list, "required": True,
        "each_choice": STARTUP_CATEGORY_CHOICES,
        "each_choice_message": f"'Catogries' may only contain: {', '.join(STARTUP_CATEGORY_CHOICES)}",
    },
    "startup_deeptech": {"type": int, "required": False},
    "stage": {"type": str, "required": False},
    "startup_ipr": {"type": int, "required": False},
    "no_of_emp": {
        "type": str, "required": False,
        "pattern": _NUMERIC_ONLY_RE,
        "pattern_message": "'no_of_emp' must be numeric only",
    },
    "problem_statement": {"type": str, "required": False},
    "aboutcompany":      {"type": str, "required": False},
    "who_we_are":        {"type": str, "required": False},
    "solution":          {"type": str, "required": False},
    "uniqueness":        {"type": str, "required": False},
    "revenue_growth":    {"type": str, "required": False},

})
class StartupIndiaService(AutomationService):
    def run(self, data):
        from startup_india import Startup_india

        self.add_log("Launching Startup India DPIIT recognition automation")
        # Pass this AutomationService instance itself (not a raw session_id/
        # sessions dict) — Startup_india has no session, logging, or OTP
        # storage of its own; it calls back into this service for all of
        # that, so the framework is the single source of truth.
        obj = Startup_india(data=data, service=self)
        return obj.run()


_EPFO_MEMBER_SCHEMA = {
    "uan": {
        "type": str, "required": True,
        "pattern": _UAN_RE,
        "pattern_message": "member 'uan' must be exactly 12 digits",
    },
    "name": {
        "type": str, "required": True,
        "pattern": _ALPHA_ONLY_RE,
        "pattern_message": "member 'name' must contain alphabetic characters only",
    },
    "aadhar": {
        "type": str, "required": True,
        "pattern": _AADHAAR_RE,
        "pattern_message": "member 'aadhar' must be exactly 12 digits",
    },
    "dob": {
        "type": str, "required": True,
        "pattern": _DATE_DDMMYYYY_RE,
        "pattern_message": "member 'dob' must be in DD/MM/YYYY format",
    },
    "doj": {
        "type": str, "required": True,
        "pattern": _DATE_DDMMYYYY_RE,
        "pattern_message": "member 'doj' must be in DD/MM/YYYY format",
    },
    "wages": {
        "type": str, "required": True,
        "pattern": _NUMERIC_ONLY_RE,
        "pattern_message": "member 'wages' must be numeric only",
    },
    "marital_status": {
        "type": str, "required": True,
        "pattern": _SINGLE_ALPHA_RE,
        "pattern_message": "member 'marital_status' must be a single alphabetic character",
    },
    "qualification": {
        "type": str, "required": True,
        "pattern": _SINGLE_DIGIT_RE,
        "pattern_message": "member 'qualification' must be a single numeric digit",
    },
    "pan": {
        "type": str, "required": False,
        "pattern": _PAN_RE,
        "pattern_message": "member 'pan' must be a valid PAN (e.g. ABCDE1234F)",
    },
    "email": {
        "type": str, "required": False,
        "pattern": _EMAIL_RE,
        "pattern_message": "member 'email' must be a valid email address (e.g. name@domain.com)",
    },
}

# EPFO's UAN member portal has no OTP step in the onboarding flow itself,
# so this service is registered with needs_otp=False — /epfo/otp is never
# wired up and returns a plain 404 instead of a route that does nothing.
@framework.service("epfo", needs_otp=False, schema={
    "user_name": {
        "type": str, "required": True,
        "pattern": _EPFO_USERNAME_RE,
        "pattern_message": "'user_name' must be 5 uppercase letters followed by 10 digits",
    },
    "password": {"type": str, "required": True},
    "company_name": {"type": str, "required": True},
    "member": {"type": list, "required": True, "items": _EPFO_MEMBER_SCHEMA},
})
class EPFOService(AutomationService):
    def run(self, data):
        from epfo import EPFOOnboarding

        self.add_log("Launching EPFO PF member onboarding automation")
        obj = EPFOOnboarding(data=data, session=self.framework.sessions.get(self.session_id))
        return obj.run()


@framework.service("itr_notice", needs_otp=False, schema={
    "username": {
        "type": str, "required": True,
        "pattern": _PAN_RE,
        "pattern_message": "'username' must be a valid PAN (e.g. ABCDE1234F)",
    },
    "password": {"type": str, "required": True},
})
class ITRNoticeService(AutomationService):
    def run(self, data):
        from income_tax_notice import IncomeTaxNotice

        self.add_log("Launching Income Tax e-Proceedings notice download automation")
        client = IncomeTaxNotice()
        # income_tax_notice.IncomeTaxNotice.login() expects a PAN — the
        # public field is called "username" here since that's what the
        # e-filing portal login screen itself calls it.
        responses = client.login(pan=data["username"], password=data["password"])
        if isinstance(responses, dict):
            result = responses.get("result", [])
        else:
            result = responses

        return result


@framework.service("udyam_certificate", schema={
    "udyam_no": {
        "type": str, "required": True,
        "pattern": _UDYAM_RE,
        "pattern_message": "'udyam_no' must look like UDYAM-XX-00-0000000",
    },
    "phone": {
        "type": str, "required": True,
        "pattern": _MOBILE_RE,
        "pattern_message": "'phone' must be exactly 10 digits",
    },
})
class UdyamCertificateService(AutomationService):
    def run(self, data):
        from udyam_certificate import UdyamCertificate

        self.add_log("Launching Udyam certificate download automation")
        # Same pattern as StartupIndiaService/EPFOService: hand this
        # AutomationService instance itself to the module so it can log,
        # report progress, and block on wait_for_otp() through the
        # framework rather than keeping any session state of its own.
        obj = UdyamCertificate(data=data, service=self)
        return obj.run()


def main():
    if CONFIG["SERVER"] == "waitress":
        from waitress import serve
        print(f"Starting with waitress on {CONFIG['HOST']}:{CONFIG['PORT']} "
              f"(debug={CONFIG['DEBUG']} has no effect under waitress)")
        serve(app, host=CONFIG["HOST"], port=CONFIG["PORT"], threads=CONFIG["THREADS"])
    else:
        print(f"Starting Flask dev server on {CONFIG['HOST']}:{CONFIG['PORT']} "
              f"(debug={CONFIG['DEBUG']})")
        framework.run(host=CONFIG["HOST"], port=CONFIG["PORT"], debug=CONFIG["DEBUG"])


if __name__ == "__main__":
    main()
