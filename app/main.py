"""Telegram API 凭据自助申请服务 — FastAPI 入口。

安全与防滥用：PoW 工作量证明人机验证（防脚本轰炸）、每 IP 限流、
每手机号冷却、全局并发上限、统一响应包络 + 错误码表、统计打点（ADMIN_TOKEN 保护）。
流程状态为内存态（默认有效期 10 分钟，可配置）；部署约束见 README。
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

import hashlib

from mytelegram import MyTelegramError, TelegramWebClient, normalize_phone

BASE_DIR = Path(__file__).parent             # app/ 目录（代码与静态资源）
PROJECT_ROOT = Path(__file__).parent.parent  # 项目根目录（.env 所在）


def _int_env(key: str, default: int) -> int:
    value = int(os.getenv(key, "").strip() or default)
    if value <= 0:
        raise ValueError(f"{key} 必须是正整数")
    return value


def _load_env() -> None:
    """只读取根目录 .env；保留已有环境变量，不展开配置值中的 ${...}。"""
    load_dotenv(PROJECT_ROOT / ".env", override=False, interpolate=False)


_load_env()

# 运行参数：全部可由 .env 覆盖，不填使用默认值
TTL_SECONDS = _int_env("TTL_SECONDS", 10 * 60)  # 申请流程状态有效期（秒）
CODE_ATTEMPT_LIMIT = _int_env("CODE_ATTEMPT_LIMIT", 3)  # 单流程验证码最大输错次数（输错也消耗 Telegram 频控配额）
IP_SEND_LIMIT = _int_env("IP_SEND_LIMIT", 5)  # 每 IP 每小时最多发起发码次数
PHONE_COOLDOWN = _int_env("PHONE_COOLDOWN", 180)  # 同一手机号两次发码最小间隔（秒）
FLOW_SLOTS = _int_env("FLOW_SLOTS", 5)  # 全局同时在途申请流程上限


def build_proxy(session_id: str) -> str:
    """只使用分项配置及 sticky 用户名模板；配置错误时禁止直连。"""
    host = os.getenv("PROXY_HOST", "").strip()
    scheme = os.getenv("PROXY_SCHEME", "socks5h").strip()
    port = os.getenv("PROXY_PORT", "1080").strip()
    pwd = os.getenv("PROXY_PASS", "").strip()
    template = os.getenv("PROXY_USER_TEMPLATE", "").strip()
    if not host or not template or "{session}" not in template:
        raise ValueError("必须配置 PROXY_HOST 和含 {session} 的 PROXY_USER_TEMPLATE")
    if scheme not in {"http", "https", "socks5", "socks5h"} or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("代理协议或端口配置无效")
    user = template.replace("{session}", session_id)
    return f"{scheme}://{quote(user, safe='')}:{quote(pwd, safe='')}@{host}:{port}"


ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
# 接口文档（/docs、/openapi.json）默认关闭，防止公网暴露接口说明书；本地调试设 ENABLE_DOCS=1
ENABLE_DOCS = os.getenv("ENABLE_DOCS", "").strip() == "1"
# PoW（工作量证明）人机验证：客户端需在 [0, maxnumber) 中找到使
# sha256(salt+nonce) 等于 challenge 的 nonce，相当于强制每次请求先消耗算力。
POW_MAXNUMBER = _int_env("POW_MAXNUMBER", 500000)
POW_TTL = _int_env("POW_TTL", 10 * 60)
POW_IP_LIMIT = _int_env("POW_IP_LIMIT", 20)  # 每 IP 每分钟获取挑战上限
POW_IP_PENDING_LIMIT = _int_env("POW_IP_PENDING_LIMIT", 3)  # 每 IP 未消费挑战上限
POW_POOL_LIMIT = _int_env("POW_POOL_LIMIT", 8000)

app = FastAPI(
    title="Telegram API 凭据自助申请",
    description="自动化完成 my.telegram.org 创建应用流程，代用户获取 api_id / api_hash",
    docs_url="/docs" if ENABLE_DOCS else None,
    redoc_url=None,
    openapi_url="/openapi.json" if ENABLE_DOCS else None,
)

# ---- 统一响应包络：{code, message, data}，code=0 为成功 ----
E_PHONE_INVALID = 1001
E_CAPTCHA = 1002
E_IP_LIMITED = 1003
E_PHONE_COOLDOWN = 1004
E_BUSY = 1005
E_FLOOD = 2001
E_CODE_INVALID = 2002
E_STATE_EXPIRED = 2003
E_TELEGRAM = 2004
E_BLOCKED = 2005
E_NETWORK = 2006


def ok(data: dict | None = None, message: str = "ok") -> JSONResponse:
    return JSONResponse({"code": 0, "message": message, "data": data or {}})


def fail(code: int, message: str, original: str = "", extra: dict | None = None) -> JSONResponse:
    data = dict(extra or {})
    if original:
        data["original"] = original
    if code == E_TELEGRAM:
        # 诊断日志：只记录 Telegram 返回的响应内容，不记录手机号/验证码等用户输入
        print(f"[mytelegram] code={code} original={original[:200]}", flush=True)
    return JSONResponse({"code": code, "message": message, "data": data})


# ---- 内存态（重启即丢）----
# request_id -> {phone, random_hash, client, created_at, slot, code_attempts, processing, closed}
_state: dict[str, dict] = {}
_flow_slots = asyncio.BoundedSemaphore(FLOW_SLOTS)

# 每 IP 发码时间戳（滑动窗口）；手机号 -> 上次发码时间
# PoW 挑战：challenge_id -> {ip, salt, number, challenge, maxnumber, created}（单次有效）
_pow_challenges: dict[str, dict] = {}
_pow_ip_hits: dict[str, list[float]] = {}
_ip_hits: dict[str, list[float]] = {}
_phone_last: dict[str, float] = {}

# 统计打点
_stats: dict = {
    "send_code_ok": 0,
    "send_code_fail": {},
    "create_ok": 0,
    "create_existed": 0,
    "create_fail": {},
}


def _bump(bucket: str, kind: str = "ok") -> None:
    if kind == "ok":
        _stats[bucket] = _stats.get(bucket, 0) + 1
    else:
        fails = _stats.setdefault(bucket, {})
        fails[kind] = fails.get(kind, 0) + 1


def _tg_err(e: MyTelegramError) -> JSONResponse:
    code_map = {
        "phone_invalid": E_PHONE_INVALID,
        "flood": E_FLOOD,
        "code_invalid": E_CODE_INVALID,
        "blocked": E_BLOCKED,
        "network": E_NETWORK,
    }
    return fail(code_map.get(e.kind, E_TELEGRAM), e.message, e.original)


async def _close_flow(state: dict) -> None:
    """流程结束的唯一资源清理入口；先标记，确保名额只归还一次。"""
    if state.get("closed"):
        return
    state["closed"] = True
    try:
        if state.get("client") is not None:
            await asyncio.shield(state["client"].close())
    except Exception as e:
        print(f"[flow] close failed: {type(e).__name__}", flush=True)
    finally:
        state["slot"].release()


async def _cleanup_expired() -> None:
    now = time.time()
    for key in list(_state):
        item = _state.get(key)
        if item and not item.get("processing") and now - item["created_at"] > TTL_SECONDS:
            _state.pop(key)
            await _close_flow(item)

    # 惰性清理限流窗口与过期 PoW 挑战
    for cid in [c for c, v in _pow_challenges.items() if now - v["created"] > POW_TTL]:
        _pow_challenges.pop(cid, None)
    horizon = now - 3600
    for ip in list(_ip_hits):
        _ip_hits[ip] = [t for t in _ip_hits[ip] if t > horizon]
        if not _ip_hits[ip]:
            _ip_hits.pop(ip, None)
    for ip in list(_pow_ip_hits):
        _pow_ip_hits[ip] = [t for t in _pow_ip_hits[ip] if t > now - 60]
        if not _pow_ip_hits[ip]:
            _pow_ip_hits.pop(ip, None)
    for phone in [p for p, t in _phone_last.items() if now - t >= PHONE_COOLDOWN]:
        _phone_last.pop(phone, None)


def _new_pow_challenge(ip: str) -> dict:
    # 防内存 DoS：未消费挑战堆积上限（正常流量远达不到，脚本刷量会被拒）
    if len(_pow_challenges) >= POW_POOL_LIMIT:
        return {}
    salt = uuid.uuid4().hex + uuid.uuid4().hex
    number = random.SystemRandom().randrange(POW_MAXNUMBER)
    challenge = hashlib.sha256((salt + str(number)).encode()).hexdigest()
    cid = uuid.uuid4().hex
    _pow_challenges[cid] = {
        "ip": ip,
        "salt": salt,
        "number": number,
        "challenge": challenge,
        "maxnumber": POW_MAXNUMBER,
        "created": time.time(),
    }
    return {
        "challenge_id": cid,
        "algorithm": "SHA-256",
        "salt": salt,
        "challenge": challenge,
        "maxnumber": POW_MAXNUMBER,
        "expires_at": _pow_challenges[cid]["created"] + POW_TTL,
    }


def _check_pow(challenge_id: str, nonce: str, ip: str) -> bool:
    cid = str(challenge_id or "")
    item = _pow_challenges.get(cid)
    if not item or item["ip"] != ip:
        return False
    _pow_challenges.pop(cid)  # 同一 IP 单次有效，用完即废
    if time.time() - item["created"] > POW_TTL:
        return False
    try:
        n = int(str(nonce).strip())
    except ValueError:
        return False
    if not (0 <= n < item["maxnumber"]):
        return False
    expected = hashlib.sha256((item["salt"] + str(n)).encode()).hexdigest()
    return expected == item["challenge"]


# 验证码提取：兼容用户把官方消息整段粘贴进来（中英文格式都支持，
# 含 "code is XXX" / "code: XXX" / "登录代码：XXX" 等写法）。
# 字符集含连字符/下划线：验证码可能是 "3tqZGWDwv-M"、"bGHuB_nBp74" 这类格式
_CODE_WITH_LABEL_RE = re.compile(
    r"(?:登录代码|驗證碼|验证码|代码|\bcode\b|\bкод\b)\s*(?:is)?\s*[:：]?\s*([A-Za-z0-9_-]{5,32})(?=[^A-Za-z0-9_-]|$)",
    re.I,
)
_CODE_PLAIN_RE = re.compile(r"^[A-Za-z0-9_-]{5,32}$")


def extract_code(raw: str) -> str:
    """只接受完整验证码或带明确标签的消息；无法识别时不请求 Telegram。"""
    raw = (raw or "").strip()
    if _CODE_PLAIN_RE.fullmatch(raw):
        return raw
    m = _CODE_WITH_LABEL_RE.search(raw)
    if m:
        return m.group(1)
    raise MyTelegramError("code_invalid", "未能识别验证码，请复制验证码本身或包含验证码的完整消息")


def _client_ip(req: Request) -> str:
    # 由 Uvicorn 按可信代理配置处理转发头；应用不再自行信任客户端请求头。
    return req.client.host if req.client else "?"


@app.get("/api/pow")
async def pow_challenge(req: Request):
    """下发 PoW 挑战（盐值 + 目标哈希），客户端暴力搜索 nonce 后回传验证。"""
    await _cleanup_expired()
    ip = _client_ip(req)
    hits = _pow_ip_hits.setdefault(ip, [])
    if len(hits) >= POW_IP_LIMIT:
        return fail(E_IP_LIMITED, "人机验证请求过于频繁，请一分钟后重试")
    hits.append(time.time())
    if sum(v["ip"] == ip for v in _pow_challenges.values()) >= POW_IP_PENDING_LIMIT:
        return fail(E_IP_LIMITED, "未完成的人机验证过多，请完成已有验证或等待其过期")
    challenge = _new_pow_challenge(ip)
    if not challenge:
        return fail(E_BUSY, "当前请求人数较多，请稍后再试")
    return ok(challenge)


@app.post("/api/send-code")
async def send_code(req: Request):
    await _cleanup_expired()
    body = await req.json()
    ip = _client_ip(req)

    # 人机校验：PoW 工作量证明（零注册、零第三方，客户端消耗算力换取提交资格）
    if not _check_pow(str(body.get("challenge_id") or ""), str(body.get("nonce", "")), ip):
        return fail(E_CAPTCHA, "人机校验未通过，请刷新页面重试")

    try:
        phone = normalize_phone(str(body.get("phone") or ""))
    except MyTelegramError:
        return fail(E_PHONE_INVALID, "手机号格式不正确，请使用国际格式，如 +8613800138000")

    now = time.time()
    hits = _ip_hits.setdefault(ip, [])
    hits[:] = [t for t in hits if t > now - 3600]
    if len(hits) >= IP_SEND_LIMIT:
        return fail(E_IP_LIMITED, "当前 IP 请求过于频繁，请一小时后再试")
    if now - _phone_last.get(phone, 0) < PHONE_COOLDOWN:
        wait = int(PHONE_COOLDOWN - (now - _phone_last[phone]))
        return fail(
            E_PHONE_COOLDOWN,
            f"该手机号刚申请过验证码，请等待 {wait} 秒后再试（旧验证码可能仍有效）",
        )

    slot = _flow_slots
    if slot.locked():  # 无空闲名额（Semaphore.locked() 即 value==0）
        return fail(E_BUSY, "当前申请人数较多，请稍后再试")
    await slot.acquire()
    _ip_hits[ip].append(now)
    _phone_last[phone] = now

    state = {"client": None, "slot": slot, "closed": False, "processing": False}
    retained = False
    try:
        # 一个流程只创建一个代理会话，任何失败明确返回，不自动换 IP 重试。
        client = TelegramWebClient(build_proxy(uuid.uuid4().hex[:12]))
        state["client"] = client
        random_hash = await client.send_code(phone)
        state.update(phone=phone, random_hash=random_hash, created_at=time.time(), code_attempts=0)
        request_id = uuid.uuid4().hex
        response = ok(
            {"request_id": request_id, "expires_at": state["created_at"] + TTL_SECONDS},
            "验证码已发送到你的 Telegram App（官方账号「Telegram」的服务消息），请注意查收",
        )
        _state[request_id] = state
        retained = True
        _bump("send_code_ok")
        return response
    except MyTelegramError as e:
        _bump("send_code_fail", e.kind)
        return _tg_err(e)
    except Exception as e:
        print(f"[flow] send_code failed: {type(e).__name__}", flush=True)
        _bump("send_code_fail", "unknown")
        return fail(E_TELEGRAM, "发送失败，请联系管理员检查服务配置或运行日志")
    finally:
        if not retained:
            await _close_flow(state)


@app.post("/api/create-app")
async def create_app(req: Request):
    await _cleanup_expired()
    body = await req.json()
    request_id = str(body.get("request_id") or "").strip()
    state = _state.get(request_id)
    if not state:
        return fail(E_STATE_EXPIRED, "申请流程已过期或不存在，请重新发送验证码")
    if state["processing"]:
        return fail(E_BUSY, "当前申请正在处理中，请勿重复提交")
    try:
        code = extract_code(str(body.get("code") or ""))
    except MyTelegramError as e:
        return _tg_err(e)

    # 在第一次 await 前占用流程，重复请求直接拒绝，不排队重放登录。
    state["processing"] = True
    retained = False
    client: TelegramWebClient = state["client"]
    # 诊断：只记长度不记内容，用于判断验证码是否被意外截断
    print(f"[flow] code_len={len(code)}", flush=True)
    try:
        await client.login(state["phone"], state["random_hash"], code)
        result = await client.create_app()
        _bump("create_ok")
        if result.existed:
            _bump("create_existed")
        return ok(
            {"api_id": result.api_id, "api_hash": result.api_hash, "existed": result.existed},
            "申请成功",
        )
    except MyTelegramError as e:
        _bump("create_fail", e.kind)
        if e.kind == "code_invalid":
            # 输错次数达到 CODE_ATTEMPT_LIMIT 后强制作废流程。
            state["code_attempts"] += 1
            attempts = state["code_attempts"]
            if attempts >= CODE_ATTEMPT_LIMIT:
                return fail(
                    E_CODE_INVALID,
                    f"验证码已连续输错 {CODE_ATTEMPT_LIMIT} 次，为保护你的号码不被 Telegram 限流，"
                    "本次流程已作废。请等待几分钟后重新获取验证码，并仔细核对最新一条消息",
                    e.original,
                )
            remaining = CODE_ATTEMPT_LIMIT - attempts
            retained = True
            return fail(
                E_CODE_INVALID,
                f"{e.message}（还剩 {remaining} 次机会；输错的登录尝试也会计入 Telegram 限流，"
                "请核对最新一条验证码消息，建议直接复制粘贴）",
                e.original,
            )
        return _tg_err(e)
    except Exception as e:
        print(f"[flow] create_app failed: {type(e).__name__}", flush=True)
        _bump("create_fail", "unknown")
        return fail(E_TELEGRAM, "申请失败，本次流程已结束，请联系管理员检查运行日志")
    finally:
        if retained:
            state["processing"] = False
        else:
            _state.pop(request_id, None)
            await _close_flow(state)


_FAIL_ZH = {
    "flood": "官方限流",
    "code_invalid": "验证码错误",
    "network": "网络异常",
    "blocked": "被网站拦截",
    "telegram_error": "Telegram 拒绝",
    "phone_invalid": "手机号无效",
    "state_expired": "流程过期",
    "captcha": "人机验证未通过",
    "ip_limited": "IP 限流",
    "phone_cooldown": "手机号冷却中",
    "busy": "系统繁忙",
    "unknown": "未知错误",
}


@app.get("/api/stats")
async def stats(token: str = ""):
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        return JSONResponse({"code": 4040, "message": "not found", "data": {}}, status_code=404)

    def zh(d: dict) -> dict:
        return {_FAIL_ZH.get(k, k): v for k, v in d.items()}

    data = {
        "发码成功": _stats.get("send_code_ok", 0),
        "发码失败": zh(_stats.get("send_code_fail", {})),
        "申请成功": _stats.get("create_ok", 0),
        "其中找回已有凭据": _stats.get("create_existed", 0),
        "创建失败": zh(_stats.get("create_fail", {})),
        "统计时段": "自服务启动以来（重启清零）",
    }
    return ok(data)


@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/sha256.min.js")
async def sha256_lib():
    """自托管的 js-sha256 库（前端 PoW 计算用）。"""
    return FileResponse(
        BASE_DIR / "static" / "sha256.min.js", media_type="application/javascript"
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=_int_env("PORT", 8000), workers=1, proxy_headers=False)
