"""my.telegram.org 自动申请客户端（curl_cffi 浏览器指纹模拟）。

流程：GET 首页建立 cookie → /auth/send_password（发验证码）→ /auth/login（换 stel_token）
     → GET /apps（判断"已有 app"或"申请表单"）→ POST /apps/create（提交一次）
     → GET /apps（创建后的统一结果确认）。

一个申请流程对应一个 TelegramWebClient 实例：send_code 与 create_app 之间必须复用
同一会话（cookie 保持）。stel_token 只存在于会话内存中，不落盘、不打日志。
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from curl_cffi.requests import AsyncSession
from curl_cffi.const import CurlECode
from curl_cffi.requests.exceptions import RequestException

BASE_URL = "https://my.telegram.org"
TIMEOUT = 30
# 只重试一次，沿用同一代理会话；POST 仅在尚未建立连接时重试。
_CONNECT_ERRORS = frozenset({CurlECode.COULDNT_RESOLVE_PROXY, CurlECode.COULDNT_RESOLVE_HOST,
                             CurlECode.COULDNT_CONNECT, CurlECode.SSL_CONNECT_ERROR})
_READ_ERRORS = _CONNECT_ERRORS | {CurlECode.OPERATION_TIMEDOUT, CurlECode.GOT_NOTHING,
                                 CurlECode.SEND_ERROR, CurlECode.RECV_ERROR,
                                 CurlECode.HTTP2, CurlECode.HTTP2_STREAM, CurlECode.PARTIAL_FILE}


class _AppPageParser(HTMLParser):
    """按项目现有页面样本字段解析；登录后的 DOM 尚需真实账号复核。"""

    def __init__(self):
        super().__init__()
        self.api_ids: list[int] = []
        self.api_hashes: list[str] = []
        self.form_hashes: list[str] = []
        self.is_login_page = False
        self._span_text: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("id") == "phone":
            self.is_login_page = True
        if tag == "span" and "vv" in (attrs.get("class") or "").split():
            self._span_text = []
        if attrs.get("id") == "api_hash_value":
            value = attrs.get("data-value") or ""
            if re.fullmatch(r"[a-f0-9]{32}", value):
                self.api_hashes.append(value)
        if tag == "input" and attrs.get("name") == "hash" and attrs.get("value"):
            self.form_hashes.append(attrs["value"])

    def handle_data(self, data):
        if self._span_text is not None:
            self._span_text.append(data)

    def handle_endtag(self, tag):
        if tag == "span" and self._span_text is not None:
            value = "".join(self._span_text).strip()
            if re.fullmatch(r"[0-9]{1,10}", value):
                self.api_ids.append(int(value))
            self._span_text = None

    @property
    def credentials(self) -> tuple[int, str] | None:
        if len(self.api_ids) == 1 and len(self.api_hashes) == 1:
            return self.api_ids[0], self.api_hashes[0]
        return None


def _parse_app_page(text: str) -> _AppPageParser:
    parser = _AppPageParser()
    parser.feed(text)
    parser.close()
    return parser


def redact_sensitive(text: str) -> str:
    """统一脱敏：不依赖 DOM 属性顺序，覆盖日志、错误响应和调试存档。"""
    text = re.sub(r"\+\d{5,15}", "+***", text)
    return re.sub(r"(?<![a-zA-Z0-9])[a-fA-F0-9]{32}(?![a-zA-Z0-9])", "[REDACTED]", text)


class MyTelegramError(Exception):
    """统一业务错误。original 经脱敏后提供给前端和日志排查。"""

    def __init__(self, kind: str, message: str, original: str = "", *, request_not_sent: bool = False):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.original = redact_sensitive(original)
        self.request_not_sent = request_not_sent


def normalize_phone(raw: str) -> str:
    """校验并归一化手机号：去空格/横线，补 +，要求 8~15 位数字。"""
    phone = re.sub(r"[\s\-()]", "", (raw or "").strip())
    if not phone.startswith("+"):
        phone = "+" + phone
    if not re.fullmatch(r"\+\d{8,15}", phone):
        raise MyTelegramError(
            "phone_invalid", "手机号格式不正确，请使用国际格式，如 +8613800138000"
        )
    return phone


@dataclass
class CreateResult:
    api_id: int
    api_hash: str
    existed: bool


def _extract_credentials(text: str) -> tuple[int, str] | None:
    """只解析现有样本中的凭据字段；不使用其他结构猜测凭据。"""
    return _parse_app_page(text).credentials


def _dump_debug_page(html: str, tag: str) -> None:
    """解析失败时存档真实页面，便于对照修复。存档前脱敏：手机号、api_hash。"""
    try:
        masked = redact_sensitive(html)
        debug_dir = Path(__file__).parent / "debug_pages"
        debug_dir.mkdir(exist_ok=True)
        stamp = str(time.time_ns()) + "_" + uuid.uuid4().hex[:8]
        (debug_dir / f"{stamp}_{tag}.html").write_text(masked, encoding="utf-8")
        print(f"[flow] 已存档解析失败页面 debug_pages/{stamp}_{tag}.html（已脱敏）", flush=True)
    except Exception as e:
        print(f"[flow] debug page write failed: {type(e).__name__}", flush=True)


class TelegramWebClient:
    def __init__(self, proxy: str):
        self.session = AsyncSession(impersonate="chrome", proxies={"http": proxy, "https": proxy})

    async def close(self) -> None:
        await self.session.close()

    async def _request(self, method: str, path: str, **kw):
        headers = {
            "Referer": BASE_URL + "/",
            "X-Requested-With": "XMLHttpRequest",
            **kw.pop("headers", {}),
        }
        for attempt in range(2):
            try:
                resp = await self.session.request(
                    method, BASE_URL + path, headers=headers, timeout=TIMEOUT,
                    allow_redirects=(method == "GET"), **kw
                )
                break
            except RequestException as e:
                retryable = e.code in (_READ_ERRORS if method == "GET" else _CONNECT_ERRORS)
                if attempt == 0 and retryable:
                    await asyncio.sleep(1)
                    continue
                message = "网络请求失败，请联系管理员检查服务器网络或代理配置"
                if attempt:
                    message = "网络请求失败，已自动重试一次，请稍后再试或联系管理员"
                if method == "POST" and e.code not in _CONNECT_ERRORS:
                    message = ("验证码发送结果未能确认，请先查看 Telegram 消息，暂勿重复发送"
                               if path == "/auth/send_password" else
                               "操作结果未能确认，为避免重复提交，本次流程已结束，请稍后再试")
                raise MyTelegramError("network", message, f"curl={int(e.code)}; {type(e).__name__}",
                                      request_not_sent=e.code in _CONNECT_ERRORS) from e
        body = resp.text or ""
        if resp.status_code in (403, 503, 429) or "Just a moment" in body[:2000]:
            raise MyTelegramError(
                "blocked",
                "请求被网站拦截（可能是 Cloudflare 或 IP 质量问题），请更换 IP 或稍后再试",
                f"HTTP {resp.status_code}: {body[:200]}",
            )
        return resp

    # ---- 第 1 步：发送验证码 ----
    async def send_code(self, phone: str) -> str:
        try:
            await self._request("GET", "/")  # 建立基础 cookie
        except MyTelegramError as e:
            if e.kind == "network":
                e.request_not_sent = True  # 还未调用验证码发送接口。
            raise
        resp = await self._request("POST", "/auth/send_password", data={"phone": phone})
        try:
            payload = resp.json()
        except Exception:
            raise MyTelegramError(
                "blocked", "验证码接口返回异常，可能被风控，请稍后再试", resp.text[:200]
            ) from None
        if isinstance(payload, dict) and payload.get("random_hash"):
            return payload["random_hash"]

        original = str(payload)
        upper = original.upper()
        if "INVALID" in upper:
            kind, msg = "phone_invalid", "手机号无效或未注册 Telegram，请检查国际格式"
        elif "MANY" in upper or "FLOOD" in upper or "ATTEMPT" in upper:
            kind, msg = (
                "flood",
                "该手机号申请验证码过于频繁，已被 Telegram 临时限流。官方仅提示"
                "\"稍后再试\"，未告知具体恢复时长。请停止连续重试，稍后再申请",
            )
        else:
            kind, msg = "telegram_error", "Telegram 返回错误，请稍后再试"
        raise MyTelegramError(kind, msg, original)

    # ---- 第 2 步：验证码登录 ----
    async def login(self, phone: str, random_hash: str, code: str) -> None:
        # 注意：/auth/login 的表单字段名是 "password"（值为收到的登录验证码），不是 "code"
        resp = await self._request(
            "POST",
            "/auth/login",
            data={"phone": phone, "random_hash": random_hash, "password": code},
        )
        if self.session.cookies.get("stel_token"):
            print("[flow] login OK", flush=True)
            return
        text = (resp.text or "").strip()
        upper = text.upper()
        if "CODE" in upper and "INVALID" in upper:
            raise MyTelegramError(
                "code_invalid",
                "验证码错误：请确认输入的是 Telegram 最新一条验证码消息中的代码，注意区分大小写",
                text[:200],
            )
        if "MANY" in upper or "FLOOD" in upper or "EXPIRED" in upper:
            raise MyTelegramError(
                "flood", "验证码已过期或尝试次数过多，请重新发送验证码", text[:200]
            )
        raise MyTelegramError("telegram_error", "登录失败，请稍后重试", text[:200])

    # ---- 第 3 步：创建应用（或返回已有凭据）----
    async def create_app(self) -> CreateResult:
        resp = await self._request("GET", "/apps")
        html = resp.text or ""
        print(f"[flow] GET /apps status={resp.status_code} len={len(html)}", flush=True)

        parsed = _parse_app_page(html)
        creds = parsed.credentials
        if creds:
            return CreateResult(creds[0], creds[1], existed=True)
        if "app configuration" in html.lower() or parsed.api_ids or parsed.api_hashes:
            # 配置页解析失败时存档脱敏页面，不再尝试其他结构。
            _dump_debug_page(html, "apps_config_unparsed")
            raise MyTelegramError("telegram_error", "配置页凭据解析失败，请联系管理员检查官网页面结构")

        # 没登录成功时会返回登录页
        if parsed.is_login_page:
            raise MyTelegramError(
                "telegram_error", "登录态未生效，请重新走一遍流程", html[:200]
            )

        form_hashes = parsed.form_hashes
        if len(form_hashes) != 1:
            _dump_debug_page(html, "apps_no_form")
            raise MyTelegramError(
                "telegram_error", "未能识别申请表单，站点可能已改版，请把此信息反馈给维护者",
                html[:300],
            )

        create_resp = await self._request(
            "POST", "/apps/create",
            data={
                "hash": form_hashes[0],
                "app_title": f"MyApp{uuid.uuid4().hex[:4]}",
                "app_shortname": f"app{uuid.uuid4().hex[:10]}",
                "app_url": "",
                "app_platform": "desktop",
                "app_desc": "Personal application for Telegram API access.",
            },
        )
        print(f"[flow] POST /apps/create status={create_resp.status_code}", flush=True)
        # 创建后的唯一成功判据是重新读取配置页；不猜测 POST 响应体版式。
        page = await self._request("GET", "/apps")
        page_text = page.text or ""
        creds = _extract_credentials(page_text)
        if creds:
            return CreateResult(creds[0], creds[1], existed=False)
        _dump_debug_page(page_text, "apps_after_create")
        raise MyTelegramError(
            "telegram_error", "创建后未能读取凭据，请联系管理员检查官网响应",
            f"HTTP {create_resp.status_code}",
        )
