# Telegram API 凭据自助申请

通过网页申请或查询 Telegram 的 `api_id` 和 `api_hash`，基于 [my.telegram.org](https://my.telegram.org/) 的登录与应用创建流程。

**使用流程：**填写国际格式手机号 → 在 Telegram App 内接收验证码 → 提交验证码 → 保存凭据。验证码不是短信；已有应用的号码会返回原有凭据。

[电脑端预览](docs/screenshots/desktop.png) · [手机端预览](docs/screenshots/mobile.png)

## 快速开始

使用仍受维护的 [Python](https://devguide.python.org/versions/)；本项目已在 Python 3.12 上验证。以下为 Linux/macOS 示例：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

编辑根目录 `.env`，填写代理地址、端口、密码及用户名模板。代理须支持 **sticky 会话**，`PROXY_USER_TEMPLATE` 必须包含 `{session}`，具体语法以代理商提供的连接信息为准。`ADMIN_TOKEN` 可选，留空时关闭管理统计接口。

```bash
.venv/bin/python -m uvicorn --app-dir app main:app --host 127.0.0.1 --port 8000 --workers 1 --no-proxy-headers
```

启动后访问 `http://127.0.0.1:8000`。Windows 将 `python3` 换为 `python`，解释器路径换为 `.venv\Scripts\python.exe`，复制配置使用 `Copy-Item .env.example .env`。

全部配置和默认值见 [.env.example](.env.example)。只读取根目录 `.env`，已有环境变量优先；Uvicorn CLI 的端口由 `--port` 决定，配置中的 `PORT` 仅用于直接运行 `python app/main.py`。

## 部署要求

- **单进程运行：**流程、限流和统计保存在内存中，请保持 `--workers 1`；重启会清空状态。
- **公网使用 HTTPS：**通过 Caddy/Nginx 反向代理，后端仅监听回环地址。
- **正确识别客户端 IP：**本机反向代理使用 `--proxy-headers --forwarded-allow-ips=127.0.0.1` 替换上面的 `--no-proxy-headers`；其他部署方式须按实际代理地址配置并限制后端入口。
- **流程内保持同一出口：**每次申请使用独立代理会话，失败明确返回，不自动换 IP 重试。

## 功能与数据

提供 PoW 计算校验、IP 限流、手机号冷却、并发限制和验证码尝试次数限制；重复提交不会并行执行同一流程。

登录会话仅在服务端内存中临时使用。浏览器保存手机号及未结束的流程状态，可通过清除网站数据删除；结果凭据不自动持久保存，下载文件由用户自行保管。解析失败时会在 `app/debug_pages/` 保存脱敏 HTML，运维需按需清理。

Telegram 限流没有本项目可以保证的固定恢复时间。网页结构变化可能影响解析；当前测试覆盖模拟业务及本地 HTTP，尚未使用真实账号验证完整申请流程。每个手机号只能关联一个 `api_id`，以 [Telegram 官方说明](https://core.telegram.org/api/obtaining_api_id)为准。

## 接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/pow` | 获取绑定客户端 IP 的计算挑战及 `expires_at` |
| POST | `/api/send-code` | 提交 `phone`、`challenge_id`、`nonce`，返回流程 ID 和有效期 |
| POST | `/api/create-app` | 提交 `request_id`、`code`，返回凭据及 `existed` 标志 |
| GET | `/api/stats?token=...` | 管理统计；令牌未配置或错误时返回 HTTP 404 |
| GET | `/health` | 进程健康检查，不检测代理或 Telegram |

业务响应格式为 `{code, message, data}`，`code=0` 表示成功；业务错误通常仍返回 HTTP 200。统计自进程启动累计；本地校验和发码前限流不计入发码失败。`ENABLE_DOCS=1` 开放接口文档，公网部署保持关闭。

## 测试与依赖

```bash
.venv/bin/python -B -m unittest discover -s tests -p "test_*.py"
node --test tests/frontend.test.cjs
```

测试不发送真实验证码。前端测试使用 Node.js 内置测试运行器，无需安装 npm 依赖。依赖已于 **2026-11-04** 核对稳定版本并固定，详见 [Python 依赖](requirements.txt)和[第三方依赖记录](docs/dependencies.json)。

## 联系与许可

频道 [@LongQingHQ](https://t.me/LongQingHQ) · 社群 [@LongQingClub](https://t.me/LongQingClub) · 管理员 [@HengteGo](https://t.me/HengteGo)

本项目采用 [MIT 许可证](LICENSE)，第三方哈希库声明见 [js-sha256 许可证](app/static/sha256.LICENSE.txt)。本项目为非官方工具，使用须遵守 [Telegram API 服务条款](https://core.telegram.org/api/terms)。
