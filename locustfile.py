from datetime import date
import logging
import math
import os
from pathlib import Path
import random
from uuid import uuid4

from faker import Faker
from dotenv import load_dotenv
from locust import HttpUser, SequentialTaskSet, constant_throughput, task
from locust.exception import StopUser


ENV_FILE = Path(__file__).resolve().with_name(".env")
load_dotenv(dotenv_path=ENV_FILE, override=False, encoding="utf-8-sig")

logger = logging.getLogger(__name__)
PATIENT_FIELDS = ("surname", "name", "middleName", "birthDate", "gender", "phone", "email")
READ_REPEATS = 20
REQUEST_TIMEOUT = 10


def require(condition, message):
    if not condition:
        raise ValueError(message)


def is_integer(value, minimum=0):
    return type(value) is int and value >= minimum


def validate_page(data):
    require(isinstance(data, dict), "Expected a page object")
    require(isinstance(data.get("content"), list), "Page content must be a list")
    for field in ("totalElements", "totalPages", "number"):
        require(is_integer(data.get(field)), f"Invalid page field: {field}")
    require(is_integer(data.get("size"), 1), "Invalid page size")
    require(len(data["content"]) <= data["size"], "Page exceeds requested size")


def validate_patient(data, expected=None):
    require(isinstance(data, dict), "Expected a patient object")
    require(is_integer(data.get("id"), 1), "Invalid patient ID")
    for field in ("surname", "name"):
        value = data.get(field)
        require(isinstance(value, str) and bool(value.strip()), f"Missing patient {field}")
    for field in ("birthDate", "regDate"):
        require(isinstance(data.get(field), str), f"Missing patient {field}")
        try:
            date.fromisoformat(data[field])
        except ValueError as error:
            raise ValueError(f"Invalid patient {field}") from error
    require(data.get("gender") in ("М", "Ж"), "Invalid patient gender")
    require(type(data.get("isDeleted")) is bool, "Missing isDeleted")
    for field in ("middleName", "phone", "email"):
        require(field in data and (data[field] is None or isinstance(data[field], str)), f"Invalid patient {field}")
    for field, value in (expected or {}).items():
        require(data.get(field) == value, f"Patient field was not preserved: {field}")


def validate_reference(data):
    require(isinstance(data, dict), "Expected a reference object")
    require(is_integer(data.get("id"), 1), "Invalid reference ID")
    require(isinstance(data.get("name"), str) and bool(data["name"].strip()), "Invalid reference name")
    require(type(data.get("isActive")) is bool, "Missing isActive")


def validate_reference_page(data):
    validate_page(data)
    for item in data["content"]:
        validate_reference(item)


def validate_autocomplete(data):
    require(isinstance(data, list), "Autocomplete must return a list")
    for item in data:
        validate_reference(item)


def validate_analysis(data, symptom_ids):
    """An empty list is valid; scores describe matching, not clinical probability."""
    require(isinstance(data, list), "Analysis must return a list")
    previous_score = 100.0
    diseases_seen = set()
    for result in data:
        require(isinstance(result, dict), "Invalid analysis item")
        disease_id = result.get("diseaseId")
        require(is_integer(disease_id, 1) and disease_id not in diseases_seen, "Invalid/duplicate disease ID")
        diseases_seen.add(disease_id)
        for field in ("diseaseName", "icd10Code"):
            require(isinstance(result.get(field), str) and bool(result[field].strip()), f"Missing {field}")
        score = result.get("probability")
        require(type(score) in (int, float) and math.isfinite(score), "Invalid numeric score")
        require(15 <= score <= previous_score, "Score is outside threshold/range or results are not sorted")
        previous_score = score
        require(result.get("isSelectedByDoctor") is False, "Unexpected selected diagnosis")
        matched = result.get("matchedSymptoms")
        require(isinstance(matched, list), "Invalid matchedSymptoms")
        seen = set()
        for symptom in matched:
            require(isinstance(symptom, dict), "Invalid matched symptom")
            symptom_id = symptom.get("symptomId")
            require(is_integer(symptom_id, 1) and symptom_id in symptom_ids and symptom_id not in seen,
                    "Matched symptom was not submitted or is duplicated")
            seen.add(symptom_id)
            require(isinstance(symptom.get("symptomName"), str), "Missing symptomName")
            require(is_integer(symptom.get("weight")) and symptom["weight"] <= 100, "Invalid symptom weight")


class TestDataFactory:

    _used_identities = set()

    def __init__(self, seed=None):
        self.fake = Faker("ru_RU")
        self.random = random.Random(seed)
        if seed is not None:
            self.fake.seed_instance(seed)

    def phone(self):
        return "+79" + f"{self.random.randrange(1_000_000_000):09d}"

    def email(self):
        return f"locust.{uuid4().hex}@example.com"

    def make_patient(self):
        for _ in range(100):
            male = self.random.choice((True, False))
            suffix = "male" if male else "female"
            patient = {
                "surname": getattr(self.fake, f"last_name_{suffix}")(),
                "name": getattr(self.fake, f"first_name_{suffix}")(),
                "middleName": getattr(self.fake, f"middle_name_{suffix}")(),
                "birthDate": self.fake.date_of_birth(minimum_age=18, maximum_age=85).isoformat(),
                "gender": "М" if male else "Ж",
                "phone": self.phone(),
                "email": self.email(),
            }
            identity = tuple(patient[field] for field in ("surname", "name", "middleName", "birthDate"))
            if identity not in self._used_identities:
                self._used_identities.add(identity)
                return patient
        raise ValueError("Could not generate a unique synthetic identity")

    def update_patient(self, patient):
        changed = {field: patient[field] for field in PATIENT_FIELDS}
        changed["phone"] = self.phone()
        while changed["phone"] == patient["phone"]:
            changed["phone"] = self.phone()
        changed["email"] = self.email()
        return changed


class YourDocScenario(SequentialTaskSet):

    def page_parameters(self, resource, search=None):
        size = self.user.factory.random.choice((10, 20, 50))
        key = (resource, size, search)
        total_pages = self.user.pages.get(key, 1)
        params = {"page": self.user.factory.random.randrange(max(1, min(total_pages, 5))), "size": size}
        if search:
            params["search"] = search
        return params, key

    def remember_page(self, key, response):
        if response is not None:
            self.user.pages[key] = response["totalPages"]

    @task(READ_REPEATS)
    def list_patients(self):
        patient = self.user.patient
        search = patient["surname"] if patient and self.user.factory.random.choice((True, False)) else None
        params, key = self.page_parameters("patients", search)

        def check(data):
            validate_page(data)
            for item in data["content"]:
                validate_patient(item)

        response = self.user.request_json("GET", "/api/patients/all", name="01 Patients: list/search",
                                          params=params, validator=check)
        self.remember_page(key, response)

    @task(READ_REPEATS)
    def autocomplete_symptoms(self):
        symptom = self.user.factory.random.choice(self.user.symptoms)
        query = symptom["name"][:self.user.factory.random.randint(2, min(12, len(symptom["name"])))]
        self.user.request_json("GET", "/api/symptoms/autocomplete", name="02 Symptoms: autocomplete",
                               params={"query": query}, validator=validate_autocomplete)

    @task(READ_REPEATS)
    def list_diseases(self):
        disease = self.user.factory.random.choice(self.user.diseases)
        search = disease["name"][:self.user.factory.random.randint(2, min(12, len(disease["name"])))]
        if self.user.factory.random.choice((True, False)):
            search = None
        params, key = self.page_parameters("diseases", search)
        params["activeOnly"] = "true"
        response = self.user.request_json("GET", "/api/diseases", name="03 Diseases: list/search",
                                          params=params, validator=validate_reference_page)
        self.remember_page(key, response)

    @task(READ_REPEATS)
    def appointment_history(self):
        patient = self.user.patient
        search = patient["surname"] if patient and self.user.factory.random.choice((True, False)) else None
        params, key = self.page_parameters("history", search)
        response = self.user.request_json("GET", "/api/doctor/appointments/page", name="04 Doctor: appointment history",
                                          params=params, validator=validate_page)
        self.remember_page(key, response)

    @task(1)
    def create_patient(self):
        self.user.patient = None 
        payload = self.user.factory.make_patient()
        patient = self.user.request_json("POST", "/api/patients", name="05 Patients: create",
                                         expected_status=201, json=payload,
                                         validator=lambda data: validate_patient(data, payload))
        if patient is None:
            raise StopUser()
        self.user.patient = patient

    @task(READ_REPEATS)
    def update_patient(self):
        if self.user.patient is None:
            raise StopUser()
        patient_id = self.user.patient["id"]
        payload = self.user.factory.update_patient(self.user.patient)
        response = self.user.request_json("PUT", f"/api/patients/{patient_id}", name="06 Patients: update own test patient",
                                          json=payload,
                                          validator=lambda data: validate_patient(data, {**payload, "id": patient_id}))
        if response is None:
            raise StopUser()
        self.user.patient = response

    @task(READ_REPEATS)
    def read_patient(self):
        if self.user.patient is None:
            raise StopUser()
        patient = self.user.patient
        expected = {field: patient[field] for field in PATIENT_FIELDS}
        expected["id"] = patient["id"]
        self.user.request_json("GET", f"/api/patients/{patient['id']}", name="07 Patients: read own test patient",
                               validator=lambda data: validate_patient(data, expected))

    @task(1)
    def analyze_symptoms(self):
        symptoms = self.user.factory.random.sample(self.user.symptoms, k=self.user.factory.random.randint(1, min(3, len(self.user.symptoms))))
        symptom_ids = [item["id"] for item in symptoms]
        payload = {
            "symptomIds": symptom_ids,
            "complaintsText": "Жалобы: " + ", ".join(item["name"].lower() for item in symptoms)
                             + f". Продолжительность: {self.user.factory.random.randint(1, 7)} дн.",
        }
        self.user.request_json("POST", "/api/doctor/analyze", name="08 Doctor: analyze symptoms (no save)",
                               json=payload, validator=lambda data: validate_analysis(data, symptom_ids))


class YourDocUser(HttpUser):
    host = "http://localhost:8080"
    wait_time = constant_throughput(3) 
    tasks = [YourDocScenario]

    def request_json(self, method, path, *, name, expected_status=200, validator=None, **kwargs):
        """Keep real HTTP/JSON/contract failures in Locust stats; never mask them."""
        with self.client.request(method, path, name=name, catch_response=True,
                                 timeout=REQUEST_TIMEOUT, **kwargs) as response:
            if response.status_code != expected_status:
                response.failure(f"Expected HTTP {expected_status}, got {response.status_code}")
                return None
            try:
                data = response.json()
                if validator is not None:
                    validator(data)
            except (ValueError, TypeError, KeyError, OverflowError) as error:
                response.failure(f"Invalid JSON/response contract: {error}")
                return None
            return data

    def on_start(self):
        login = os.getenv("YOURDOC_LOGIN", "doctor1").strip()
        password = os.getenv("YOURDOC_PASSWORD", "")
        if os.getenv("YOURDOC_ALLOW_TEST_WRITES") != "1" or not login or not password:
            logger.error("Check %s: set YOURDOC_LOGIN, YOURDOC_PASSWORD and YOURDOC_ALLOW_TEST_WRITES=1. Explicit IDE variables take priority. No requests sent.", ENV_FILE)
            raise StopUser()

        self.factory = TestDataFactory()
        self.patient = None
        self.pages = {}

        def check_login(data):
            require(isinstance(data, dict), "Expected an authentication object")
            require(isinstance(data.get("token"), str) and bool(data["token"].strip()), "Missing JWT")
            require(data.get("role") == "DOCTOR", "Use a DOCTOR account")
            require(is_integer(data.get("userId"), 1), "Invalid authenticated user ID")

        auth = self.request_json("POST", "/api/auth/login", name="00 Setup: doctor login",
                                 json={"login": login, "password": password}, validator=check_login)
        if auth is None:
            raise StopUser()  
        self.client.headers.update({"Authorization": f"Bearer {auth['token']}"})

        def check_fixtures(data):
            validate_reference_page(data)
            require(bool(data["content"]), "Empty reference data: load demo-data.sql into the TEST database first")

        params = {"page": 0, "size": 1000, "activeOnly": "true"}
        symptoms = self.request_json("GET", "/api/symptoms", name="00 Setup: active symptoms",
                                     params=params, validator=check_fixtures)
        if symptoms is None:
            raise StopUser()
        self.symptoms = [item for item in symptoms["content"] if item["isActive"] and len(item["name"]) >= 2]
        if not self.symptoms:
            logger.error("No active symptoms with names of at least two characters. Stopping this user.")
            raise StopUser()
        diseases = self.request_json("GET", "/api/diseases", name="00 Setup: active diseases",
                                     params=params, validator=check_fixtures)
        if diseases is None:
            raise StopUser()
        self.diseases = [item for item in diseases["content"] if item["isActive"] and len(item["name"]) >= 2]
        if not self.diseases:
            logger.error("No active diseases with names of at least two characters. Stopping this user.")
            raise StopUser()
