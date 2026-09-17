"""LIMS discussion-room API client."""
import base64
import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import requests
from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
from cryptography.hazmat.primitives.serialization import load_der_public_key, load_pem_public_key

from fudan_spider.uis import LoginCallback, login_variables
from fudan_spider.utils import load_env_file, save_json

BASE = "https://lims.fudan.edu.cn"
APP = "/api/appointment/"
ROOM = "551"
ROOMS = {"551": "A103", "591": "B101"}
LOCAL = Path(__file__).resolve().parents[2] / ".local"
STATE = LOCAL / "session.json"
CREDENTIALS = LOCAL / "credentials.env"
_credentials = load_env_file(CREDENTIALS)
USER, PASSWORD, PHONE = (os.environ.get("FUDAN_" + key) or _credentials.get("FUDAN_" + key, "")
                         for key in ("USER", "PASSWORD", "PHONE"))
VERBOSE_HTTP = False


class SessionExpired(RuntimeError):
    pass


class RateLimited(RuntimeError):
    pass


def import_state(source, state_path=STATE, logger=None):
    raw = json.loads(Path(source).read_text(encoding="utf-8-sig"))
    cookies = [c for c in raw["cookies"] if c["domain"].lstrip(".") == "lims.fudan.edu.cn"]
    storage = next((o["localStorage"] for o in raw.get("origins", []) if o["origin"] == BASE), [])
    refresh = next((x["value"] for x in storage if x["name"] == "refresh_token_web"), None)
    if not cookies:
        raise ValueError("No LIMS login cookies in the supplied browser state.")
    save_json(state_path, {"cookies": cookies, "refresh_token": refresh})
    if logger is not None:
        logger("Imported LIMS session.")


class Client:
    def __init__(self, room=ROOM, state_path=STATE, logger=None):
        self.room = room
        self.state_path = state_path
        self.logger = logger or (lambda _message: None)
        try:
            self.state = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.state = {"cookies": []}
        self.http = requests.Session()
        for c in self.state["cookies"]:
            if c["domain"].lstrip(".") != "lims.fudan.edu.cn":
                raise ValueError("Unexpected cookie domain in session file.")
            value = c["value"] if c["value"].isascii() else quote(c["value"])
            self.http.cookies.set(c["name"], value, domain=c["domain"], path=c["path"],
                                  secure=c.get("secure", True),
                                  expires=c.get("expires") if c.get("expires", -1) > 0 else None)
        datasource = next((c["value"] for c in self.state["cookies"] if c["name"] == "datasource.cookie"), "limsproduct")
        self.http.headers.update({"x-datasource": datasource, "Origin": BASE,
                                  "Referer": BASE + "/teacherInformationCenter/openReservation/mainindex"})

    def prepare_submission(self, payload):
        prepared = self.http.prepare_request(requests.Request("POST", BASE + APP + "saveAppInfo", json=[payload]))
        settings = self.http.merge_environment_settings(prepared.url, {}, None, None, None)
        return prepared, settings

    def fork(self):
        """Give an independent submission worker its own authenticated connection pool."""
        client = type(self)(self.room, state_path=self.state_path, logger=self.logger)
        client.http.headers = self.http.headers.copy()
        client.http.cookies = self.http.cookies.copy()
        client.user = self.user
        return client

    def request(self, method, path, prepared=None, **kwargs):
        timeout = kwargs.pop("timeout", (10, 30))
        started = time.perf_counter()
        try:
            if prepared is None:
                response = self.http.request(method, BASE + path, timeout=timeout,
                                             allow_redirects=False, **kwargs)
            else:
                request, settings = prepared
                if request.method != method or request.url != BASE + path or kwargs:
                    raise ValueError("Prepared request does not match the intended endpoint.")
                response = self.http.send(request, timeout=timeout, allow_redirects=False, **settings)
        finally:
            if VERBOSE_HTTP:
                self.logger(f"[{ROOMS[self.room]}] HTTP {method} {path.split('?')[0]} {(time.perf_counter() - started) * 1000:.0f}ms")
        if response.status_code in {301, 302, 303, 307, 308, 401, 403}:
            raise SessionExpired("LIMS session expired.")
        if response.status_code == 429:
            raise RateLimited("HTTP 429: server rate limit; stopped this window.")
        if response.status_code != 200:
            raise RuntimeError(f"{path}: HTTP {response.status_code}; check login/network.")
        if "json" not in response.headers.get("Content-Type", ""):
            raise SessionExpired(f"{path}: login required (expected JSON).")
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError(f"{path}: unexpected response structure.")
        return result

    def data(self, path, method="GET", **kwargs):
        result = self.request(method, path, **kwargs)
        if result.get("code") in {40010, 40012}:
            raise SessionExpired("LIMS session expired.")
        if result.get("code") != 0:
            raise RuntimeError(f"{path}: {result.get('msg', 'API rejected request')}")
        return result["data"]

    def password_login(self):
        self.logger("Session missing/expired; signing in to UIS with the configured password.")
        self.http.cookies.clear()
        self.http.headers.pop("Authorization", None)

        def fetch(method, url, **kwargs):
            response = self.http.request(method, url, timeout=(10, 30), **kwargs)
            # No exception containing callback URLs, tokens or request payloads.
            if response.status_code != 200:
                raise RuntimeError(f"UIS login failed: HTTP {response.status_code}.")
            return response

        page = fetch("GET", BASE + "/teacherInformationCenter/openReservation/mainindex")
        fields = login_variables(page.text)
        page = fetch("GET", BASE + "/uaa/fudan/login", params={
            k: fields[k] for k in ["redirect_uri", "state", "client_id", "response_type", "authorize_uri"]})
        fields = login_variables(page.text)
        idp = "https://id.fudan.edu.cn/idp"
        if fields.get("authorize_uri") != idp + "/authCenter/authenticate" or fields.get("redirect_uri") != BASE + "/uaa/fudan/callback":
            raise RuntimeError("UIS login redirect changed; stopped.")
        page = fetch("GET", fields["authorize_uri"], params={
            k: fields[k] for k in ["redirect_uri", "response_type", "state", "client_id"]})
        context = parse_qs(urlsplit(page.url).fragment.split("?", 1)[-1])
        methods = fetch("POST", idp + "/authn/queryAuthMethods", json={
            k: context[k][0] for k in ["lck", "entityId"]}).json()
        if str(methods.get("code")) != "200" or methods.get("second"):
            raise RuntimeError("UIS requires additional authentication; please sign in manually.")
        chain = next((c for c in methods["data"] if c["moduleCode"] == "userAndPwd"), None)
        if not chain:
            raise RuntimeError("UIS password login is not available.")
        verify = fetch("POST", idp + "/authn/verifyCodeIsNeed", json={
            "lang": "zh_CN", "loginName": USER, "chainCode": chain["authChainCode"],
            "authModuleCode": "userAndPwd"}).json()
        if verify.get("result") is not False:
            raise RuntimeError("UIS requires a captcha; please sign in manually.")
        result = fetch("POST", idp + "/authn/getJsPublicKey").json()
        if str(result.get("code")) != "200":
            raise RuntimeError("Could not obtain the UIS password encryption key.")
        key = result["data"]
        public_key = load_pem_public_key(key.encode()) if "BEGIN" in key else load_der_public_key(base64.b64decode(key))
        encrypted = base64.b64encode(public_key.encrypt(PASSWORD.encode(), PKCS1v15())).decode()
        result = fetch("POST", idp + "/authn/authExecute", allow_redirects=False, json={
            "authModuleCode": "userAndPwd", "authChainCode": chain["authChainCode"],
            "entityId": methods["entityId"], "requestType": methods["requestType"], "lck": methods["lck"],
            "authPara": {"loginName": USER, "password": encrypted, "verifyCode": ""}}).json()
        if str(result.get("code")) != "200":
            raise RuntimeError(f"UIS password login rejected (code {result.get('code')}); check password or sign in manually.")
        if result.get("second") or not result.get("loginToken"):
            raise RuntimeError("UIS requires additional verification; please sign in manually.")
        page = fetch("POST", idp + "/authCenter/authnEngine", data={"loginToken": result["loginToken"]})
        callback = LoginCallback(page.text)
        if callback.action != BASE + "/uaa/fudan/callback" or callback.method != "GET" or set(callback.fields) != {"code", "state"}:
            raise RuntimeError("UIS callback form changed; stopped.")
        page = fetch("GET", callback.action, params=callback.fields)
        fields = login_variables(page.text)
        if fields.get("authorize_uri") != BASE + "/uaa/oauth/authorize" or fields.get("redirect_uri") != BASE + "/teacherInformationCenter/webapp/callback":
            raise RuntimeError("LIMS application callback changed; stopped.")
        page = fetch("GET", fields["authorize_uri"], params={
            k: fields[k] for k in ["redirect_uri", "response_type", "state", "client_id"]})
        redirect = login_variables(page.text).get("redirect_uri", "")
        destination = urlsplit(redirect)
        if destination.scheme != "https" or destination.netloc != "lims.fudan.edu.cn" or destination.path != "/teacherInformationCenter/webapp/callback":
            raise RuntimeError("LIMS final login redirect changed; stopped.")
        page = fetch("GET", redirect)
        callback = LoginCallback(page.text)
        if callback.action != "/teacherInformationCenter/login" or callback.method != "POST" or set(callback.fields) != {"username", "password"}:
            raise RuntimeError("LIMS session initialization form changed; stopped.")
        fetch("POST", BASE + callback.action, data=callback.fields)
        # Persist only LIMS cookies; identity-provider cookies are not needed for the next run.
        self.state = {"cookies": []}

    def login(self):
        try:
            token = self.request("GET", "/uaa/oauth/token")
            if token.get("code") != 0 and self.state.get("refresh_token"):
                token = self.request("POST", "/uaa/oauth/token", data={
                    "grant_type": "refresh_token", "refresh_token": self.state["refresh_token"],
                    "client_id": "GvsunUserCenter"})
            if token.get("code") != 0:
                raise SessionExpired("LIMS session expired.")
            self.user = self.request("GET", "/teacherInformationCenter/openReservation/getCurrentUser")
        except SessionExpired:
            self.password_login()
            token = self.request("GET", "/uaa/oauth/token")
            self.user = self.request("GET", "/teacherInformationCenter/openReservation/getCurrentUser")
        if token.get("code") != 0:
            raise RuntimeError("UIS login did not establish a LIMS session.")
        data = token["data"]
        access = data.get("accessToken") or data.get("access_token")
        if not access:
            raise RuntimeError("Login returned no access token.")
        self.http.headers["Authorization"] = "Bearer " + access
        self.state["refresh_token"] = data.get("refreshToken") or data.get("refresh_token") or self.state.get("refresh_token")
        self.state["cookies"] = [dict(name=c.name, value=c.value, domain=c.domain, path=c.path,
                                       secure=c.secure, expires=c.expires or -1)
                                 for c in self.http.cookies if c.domain.lstrip(".") == "lims.fudan.edu.cn"]
        if self.user.get("username") != USER:
            raise RuntimeError("Session does not belong to the configured account; stopped.")
        save_json(self.state_path, self.state)
        access = self.data("/api/access/access", params={"entityId": self.room, "entityType": "LAB", "accessUser": USER})
        if access.get("isAccess") is not True:
            raise RuntimeError(f"Current account is not allowed to reserve {ROOMS[self.room]}.")

    def settings(self):
        basic = self.data(APP + "getAppBasicSettings", params={"insUid": self.room, "configType": "LAB"})
        if str(basic["instrument"]) != self.room or basic.get("configType") != "LAB":
            raise ValueError("Unexpected room configuration.")
        if basic.get("isAppCharging"):
            raise RuntimeError("Room now requires payment; stopped.")
        machine = self.data(APP + "getConfigMachineInfo", params={"configUid": basic["uid"]})
        if machine.get("appMode") is not True:
            raise RuntimeError("Room no longer uses a time grid; stopped.")
        fields = self.data(APP + "getAppOpeningCustomFieldList", params={"configUid": basic["uid"]})
        if len(fields) != 1 or fields[0]["fieldName"] != "预约用途" or fields[0]["displayStyle"] != "TEXT":
            raise RuntimeError("Reservation form changed; review required fields before submitting.")
        opening = self.data(APP + "getMachineOpenTimes", params={"configMachineUid": machine["uid"]})
        closed = self.data(APP + "getMachineNotOpenTimes", params={"configMachineUid": machine["uid"]})
        return basic, machine, fields, opening, closed

    def mine(self):
        records, page = [], 1
        while True:
            result = self.request("GET", APP + "appList/my", params={"page": page, "limit": 100,
                "username": USER, "authority": "STUDENT", "schoolAcademy": self.user["schoolAcademy"]["academyNumber"], "appUser": USER})
            if result.get("code") != 0 or not isinstance(result.get("data"), list):
                raise RuntimeError("Could not read own reservations.")
            batch = result["data"]
            if set(r["uid"] for r in records) & set(r["uid"] for r in batch):
                raise RuntimeError("Reservation pagination repeated records; stopped.")
            records.extend(batch)
            if len(records) >= int(result["count"]):
                return [r for r in records if str(r["labRoomId"]) == self.room]
            if not batch:
                raise RuntimeError("Incomplete reservation list; stopped.")
            page += 1
