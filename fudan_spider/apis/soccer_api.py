"""Booking-platform API client for the soccer venue."""
import base64
import email.utils
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import requests
from captcha_slide import SlideCaptcha
from soccer_browser import BrowserSession, BrowserUnavailable, SubmissionSkipped
from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
from cryptography.hazmat.primitives.serialization import load_der_public_key, load_pem_public_key

from fudan_spider.uis import LoginCallback
from fudan_spider.utils import save_json

BASE = "https://booking.fudan.edu.cn"
API = BASE + "/reservation"
PAGE = BASE + "/reservation/fe/site/reservationInfo?id=1029"
SSO = API + "/api/login/main?redirect_url=" + quote(PAGE, safe="")
IDP = "https://id.fudan.edu.cn/idp"
VENUE = "国福路田径场-足球场"
RESOURCE = 1029
PROJECT = Path(__file__).resolve().parents[2]
LOCAL = PROJECT / ".local"
STATE = LOCAL / "soccer_session_v2.json"
CREDENTIALS = LOCAL / "credentials.env"
DEFAULT_LOG_PATH = PROJECT / "logs" / "soccer.log"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
USER = ""
PASSWORD = ""
PHONE = ""


class SessionExpired(RuntimeError):
    """会话失效：必须重新登录，属于不可重试的致命状态。"""


class AccessBlocked(RuntimeError):
    """服务端拒绝自动访问：停止提交，避免把防护响应当成登录失效反复恢复。"""


class NoticeUnread(RuntimeError):
    """账号尚未在官网确认预约须知。"""


class TransientError(RuntimeError):
    """服务端临时故障；5xx 可退避重试，429 由调度层暂停本轮。"""

    def __init__(self, message, status_code=None, retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


def _retry_after(value):
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            moment = email.utils.parsedate_to_datetime(value)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return max(0.0, moment.timestamp() - datetime.now(timezone.utc).timestamp())
        except (TypeError, ValueError, OverflowError):
            return None


def _noop_log(_message):
    pass


def _noop_audit(_event, **_fields):
    pass


class Client:
    def __init__(self, state_path=STATE, user=None, password=None, phone=None,
                 browser=None, logger=None, auditor=None):
        self.state_path = state_path
        self.user = USER if user is None else user
        self.password = PASSWORD if password is None else password
        self.phone = PHONE if phone is None else phone
        self.logger = logger or _noop_log
        self.auditor = auditor or _noop_audit
        self.browser = browser
        if browser is not None:
            self.http = BrowserSession(browser)
            return
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = {}
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": state.get("user_agent", USER_AGENT), "Referer": PAGE, "Origin": BASE})
        for c in state.get("cookies", []):
            if c["domain"].lstrip(".") not in {"booking.fudan.edu.cn", "fudan.edu.cn"}:
                raise RuntimeError("预约缓存含非预期域名。")
            self.http.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])

    def fork(self):
        child = type(self)(self.state_path, self.user, self.password, phone=self.phone, browser=self.browser, logger=self.logger, auditor=self.auditor)
        if self.browser is not None:
            child.http.window = self.http.window
        return child

    def request(self, path, method="GET", prepared=None, **kwargs):
        started = time.perf_counter()
        try:
            if prepared is None:
                response = self.http.request(method, API + path, timeout=(10, 20), allow_redirects=False, **kwargs)
            else:
                request, settings = prepared
                if request.url != API + path or request.method != method or kwargs:
                    raise ValueError("Prepared request mismatch")
                response = self.http.send(request, timeout=5, allow_redirects=False, **settings)
        except requests.RequestException as exc:
            self.auditor("http_error", path=path, method=method, error=type(exc).__name__, elapsed_ms=round((time.perf_counter()-started)*1000, 1))
            raise
        self.auditor("http", path=path, method=method, http=response.status_code,
              content_type=response.headers.get("content-type"), bytes=len(response.content),
              elapsed_ms=round((time.perf_counter()-started)*1000, 1))
        content_type = str(response.headers.get('content-type', '')).lower()
        if response.status_code in {301, 302, 303, 307, 308, 401}:
            raise SessionExpired(f"新预约平台会话需要恢复（HTTP {response.status_code}）。")
        if response.status_code in {403, 412} or (
                response.status_code == 400 and 'text/html' in content_type):
            raise AccessBlocked(f"新预约平台拒绝当前自动请求（HTTP {response.status_code}）。")
        if response.status_code == 429 or response.status_code >= 500:
            # 429/5xx 只在有限次数内退避；429 的 Retry-After 优先于本地默认间隔。
            raise TransientError(f"{path}: HTTP {response.status_code}", response.status_code,
                                 _retry_after(response.headers.get('Retry-After')))
        if response.status_code != 200:
            raise RuntimeError(f"{path}: HTTP {response.status_code}")
        if 'text/html' in content_type:
            raise AccessBlocked('业务请求返回防护 HTML，停止自动提交。')
        result = response.json()
        if not isinstance(result, dict) or "e" not in result:
            raise RuntimeError("新预约平台响应结构变化。")
        self.auditor("api", path=path, code=str(result["e"])[:80])
        if result["e"] == "UN_AUTH":
            raise SessionExpired("新预约平台会话已失效。")
        return result

    def data(self, path, **kwargs):
        result = self.request(path, **kwargs)
        if result["e"] != "OK":
            raise RuntimeError(str(result.get("m") or result["e"]))
        return result["d"]

    def login(self, deadline=None):
        if self.browser is not None:
            self.browser.call("login", timeout=50, user=self.user, password=self.password,
                              deadlineMs=deadline.timestamp()*1000 if deadline else None)
            self.logger("浏览器登录通过，账号已核对。")
            return
        try:
            user = self.data("/site/user/baseinfo")
        except SessionExpired:
            self.cas_login()
            user = self.data("/site/user/baseinfo")
        if str(user["number"]) != self.user:
            raise RuntimeError("会话账号与配置账号不一致。")
        save_json(self.state_path, {"cookies": [dict(name=c.name,value=c.value,domain=c.domain,path=c.path)
                  for c in self.http.cookies if c.domain.lstrip(".") in {"booking.fudan.edu.cn", "fudan.edu.cn"}],
                  "user_agent": self.http.headers["User-Agent"]})
        self.logger("新平台登录通过，账号已核对。")

    def recover(self, deadline):
        if self.browser is not None:
            self.browser.restart(deadline.timestamp())
        self.login(deadline)

    def cas_login(self):
        """走统一身份认证密码登录，再回跳建立新预约平台会话。"""
        self.logger("开始登录：复旦大学统一身份认证。")
        self.http.cookies.clear()
        page = self.http.get(SSO, timeout=(10, 30), allow_redirects=True)
        if page.status_code != 200:
            raise RuntimeError(f"统一身份认证入口返回 HTTP {page.status_code}，尚未进入账号密码认证。")
        fragment = urlsplit(page.url).fragment
        if "?" not in fragment:
            raise RuntimeError("统一身份认证跳转结构变化，请人工确认登录入口。")
        query = parse_qs(fragment.split("?", 1)[1])
        lck, entity = query.get("lck", [None])[0], query.get("entityId", [None])[0]
        if not lck or not entity:
            raise RuntimeError("统一身份认证参数缺失，请人工确认登录入口。")
        methods = self.http.post(IDP + "/authn/queryAuthMethods", timeout=(10, 30),
                                 json={"lck": lck, "entityId": entity}).json()
        if str(methods.get("code")) != "200" or methods.get("second"):
            raise RuntimeError("统一身份认证要求额外认证，请人工登录一次。")
        chain = next((c for c in methods.get("data") or [] if c.get("moduleCode") == "userAndPwd"), None)
        if not chain:
            raise RuntimeError("统一身份认证没有可用的密码登录方式，请人工登录。")
        verify = self.http.post(IDP + "/authn/verifyCodeIsNeed", timeout=(10, 30), json={
            "lang": "zh_CN", "loginName": self.user, "chainCode": chain["authChainCode"],
            "authModuleCode": "userAndPwd"}).json()
        if verify.get("result") is not False:
            self.auditor("verification_required", stage="login")
            raise RuntimeError("统一身份认证要求图片验证码，请人工登录一次。")
        key = self.http.post(IDP + "/authn/getJsPublicKey", timeout=(10, 30)).json()
        if str(key.get("code")) != "200":
            raise RuntimeError("拿不到统一身份认证的加密公钥。")
        raw = key["data"]
        public = load_pem_public_key(raw.encode()) if "BEGIN" in raw else load_der_public_key(base64.b64decode(raw))
        encrypted = base64.b64encode(public.encrypt(self.password.encode(), PKCS1v15())).decode()
        result = self.http.post(IDP + "/authn/authExecute", timeout=(10, 30), allow_redirects=False, json={
            "authModuleCode": "userAndPwd", "authChainCode": chain["authChainCode"],
            "entityId": methods["entityId"], "requestType": methods["requestType"], "lck": methods["lck"],
            "authPara": {"loginName": self.user, "password": encrypted, "verifyCode": ""}}).json()
        if str(result.get("code")) != "200":
            raise RuntimeError(f"统一身份认证拒绝登录（code {result.get('code')}），请核对账号密码。")
        if result.get("second") or not result.get("loginToken"):
            raise RuntimeError("统一身份认证要求二次验证，请人工登录一次。")
        page = self.http.post(IDP + "/authCenter/authnEngine", timeout=(10, 30),
                              data={"loginToken": result["loginToken"]})
        callback = LoginCallback(page.text)
        url, method, payload = callback.action, callback.method, callback.fields
        if not url:
            raise RuntimeError("统一身份认证回跳表单结构变化，请人工登录一次。")
        self.finish_login(requests.compat.urljoin(page.url, url), method, payload)

    def finish_login(self, url, method="GET", payload=None):
        for _ in range(12):
            destination = urlsplit(url)
            if (destination.scheme != "https" or destination.hostname not in
                    {"booking.fudan.edu.cn", "id.fudan.edu.cn", "uis.fudan.edu.cn", "workflow1.fudan.edu.cn"}
                    or destination.port not in (None,443) or destination.username or destination.password):
                raise RuntimeError("非预期的登录回跳地址。")
            kwargs = {"params" if method == "GET" else "data": payload}
            response = self.http.request(method, url, timeout=(10,20), allow_redirects=False, **kwargs)
            if destination.hostname == "booking.fudan.edu.cn":
                try:
                    self.data("/site/user/baseinfo")
                    return
                except SessionExpired:
                    pass
            location = response.headers.get("Location")
            if response.status_code in {301,302,303,307,308} and location:
                url = requests.compat.urljoin(url, location)
                if response.status_code in {301,302,303}:
                    method, payload = "GET", None
            else:
                break
        raise RuntimeError("新平台登录回跳未建立会话。")

    def settings(self):
        info = self.data("/site/resource/detail", params={"id": RESOURCE})
        if info["id"] != RESOURCE or info["name"] != VENUE or info["type"] != 6 or info["is_group"] != 1:
            raise RuntimeError("国福路足球场的资源配置发生变化。")
        config = info["config"]
        if config.get("view_type") != "week_calendar_auto":
            raise RuntimeError("场地分配方式变化，停止。")
        limit_info = info.get("limit_info")
        if isinstance(limit_info, dict):
            has_limit = bool(limit_info.get("error") or limit_info.get("reason"))
        else:
            has_limit = bool(limit_info)
        if not info["usable"] or has_limit:
            raise RuntimeError(limit_info or "当前账号无预约权限。")
        self.auditor("verification_config", stage="reservation", anti_bot=config["anti_bot"])
        if config["anti_bot"] == 2:
            raise RuntimeError("场地要求短信/学校微信验证码（anti_bot=2），无法自动完成，请人工处理。")
        if config["anti_bot"] == 1:
            self.logger("场地要求预约时过滑块（anti_bot=1），提交将携带自动求解的验证码。")
        if config.get("approval", {}).get("relate_app"):
            raise RuntimeError("预约新增关联审批表，需要人工处理。")
        if config["data_colle"] != [{"name":"手机号","type":"mobile","verifyType":"required"}]:
            raise RuntimeError("预约表单字段变化。")
        if not re.fullmatch(r"1[3-9]\d{9}", self.phone):
            raise RuntimeError("手机号格式错误。")
        if not any(r.get("class")=="RangeRule" and r.get("time")==2 and r.get("format")=="day" for r in config["rule"]):
            raise RuntimeError("提前两天的规则发生变化，请重新核对目标日期。")
        if not info["read_description"]:
            raise NoticeUnread("新平台要求先阅读预约须知，请在官网完成。")
        return info

    def calendar(self, day, end_day=None):
        return self.data("/site/resource/calendar", params={"id":RESOURCE,"collective":0,
                         "date":json.dumps({"start_date":day.isoformat(),"end_date":(end_day or day).isoformat()})})

    def mine(self, audit_records=False):
        records, page, seen = [], 1, set()
        while True:
            result = self.data("/site/appointment/appointment-list", params={"p":page,"pageSize":100})
            for record in result["data"]:
                if record["id"] in seen:
                    raise RuntimeError("本人预约分页重复，停止。")
                seen.add(record["id"])
                records.append(record)
            if len(records) == int(result["total"]):
                break
            if not result["data"] or len(records) > int(result["total"]):
                raise RuntimeError("本人预约列表不完整。")
            page += 1
        if audit_records:
            self.auditor("appointment_records", records=records)
        return records

    def prepare_submission(self, payload):
        request = self.http.prepare_request(requests.Request("POST", API + "/site/resource/launch",
            files={key:(None,str(value)) for key,value in payload.items()}))
        return request, self.http.merge_environment_settings(request.url, {}, None, None, None)

    def submit(self, payload, prepared=None):
        return self.request("/site/resource/launch", method="POST", prepared=prepared or self.prepare_submission(payload))



def own_slot(records, day, period_id, resource_id=None, unknown=None):
    if records is None:
        if unknown is not None:
            unknown.append("appointment_list_unavailable")
        return None
    for record in records:
        # is_cancel=1 means the user may cancel the reservation; it does not
        # mean that the reservation itself is canceled. Status 0 is canceled.
        if record.get("status") in {-1, 0, "-1", "0"}:
            continue
        if "group_id" not in record:
            if unknown is not None:
                unknown.append(record.get("id"))
            continue
        if str(record["group_id"]) != str(RESOURCE):
            continue
        if resource_id is not None and record.get("resource_id") != resource_id:
            continue
        try:
            groups = record["appointment_time"]["groups"]
            if not groups:
                raise ValueError("empty groups")
            for group in groups:
                if not group["dates"]:
                    raise ValueError("empty dates")
                for appointment_day in group["dates"]:
                    if not appointment_day["periods"]:
                        raise ValueError("empty periods")
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(appointment_day["date"])):
                        raise ValueError("unexpected date")
                    if appointment_day["date"] == str(day):
                        if any(str(slot["period_id"]) == str(period_id) for slot in appointment_day["periods"]):
                            return record
        except (KeyError, TypeError, ValueError):
            if unknown is not None:
                unknown.append(record.get("id"))
    return None



def make_payload(day, fields, period, captcha=None, phone=None):
    if isinstance(period, list):
        raise ValueError("每笔预约只能提交一个时段")
    return {"data":json.dumps([{"group_id":RESOURCE,"resource_ids":[f["id"] for f in fields],
            "period":[{"date":day.isoformat(),"period_id":period["id"]}],"number":1}]),
            "data_colle":json.dumps([{"name":"手机号","value": phone if phone is not None else PHONE,"verify":None,"type":"mobile"}],ensure_ascii=False),
            "code":0,"number":1,"collective":0,
            "captcha":json.dumps(captcha, separators=(",", ":")) if captcha else "{}"}



def slide_solver(client):
    """复用已登录会话构造滑块求解器；测试里可以替换这个工厂。"""
    return SlideCaptcha(client.http, RESOURCE)



def calendar_periods(calendar):
    periods = sorted(calendar["time"], key=lambda p: p["start_time"])
    if {f["id"] for f in calendar["resource"]} != {1030, 1031} or {p["id"] for p in periods} != {3892, 3893}:
        raise RuntimeError("足球场子场地或时段配置变化，停止。")
    return periods
