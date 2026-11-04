# Telegram API 凭据自助申请

通过网页申请或查询 Telegram 的 `api_id` 和 `api_hash`，支持读取已有应用凭据、复制结果及下载凭据文件。

[电脑端预览](docs/screenshots/desktop.png) · [手机端预览](docs/screenshots/mobile.png)

## 使用方法

1. 填写带国家区号的手机号，例如 `+14155550132`。
2. 在 Telegram App 内接收验证码，粘贴到网页提交。
3. 获取凭据后立即保存，也可下载为 `.txt` 文件。

验证码不是短信。每个手机号只能关联一个 `api_id`；如遇 Telegram 限流，请停止连续尝试，按页面提示等待。

## 部署

建议使用 Python 3.12 或更新的受维护版本。以下为 Linux/macOS 示例：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

编辑根目录 `.env`，填写代理配置后启动：

```bash
.venv/bin/python -m uvicorn --app-dir app main:app --host 127.0.0.1 --port 8000 --workers 1 --no-proxy-headers
```

访问 `http://127.0.0.1:8000` 即可使用。Windows 将 `python3` 换为 `python`，解释器路径换为 `.venv\Scripts\python.exe`，复制配置使用 `Copy-Item .env.example .env`。

## 配置说明

| 配置项 | 用途 |
|---|---|
| `PROXY_HOST` / `PROXY_PORT` | 代理网关地址和端口 |
| `PROXY_SCHEME` | 代理协议，如 `socks5h` |
| `PROXY_PASS` | 代理密码 |
| `PROXY_USER_TEMPLATE` | 代理商提供的 sticky 用户名模板，必须包含 `{session}` |
| `ADMIN_TOKEN` | 可选，管理统计接口的访问令牌 |

代理必须支持 sticky 会话，确保发码与登录沿用同一出口。完整配置和默认值见 [.env.example](.env.example)；只读取根目录 `.env`，已有环境变量优先。启动命令的端口由 `--port` 决定，配置中的 `PORT` 仅用于直接运行 `python app/main.py`。

## 公网部署与管理

- 使用 Caddy/Nginx 提供 HTTPS；后端仅监听回环地址。
- 本机反向代理使用 `--proxy-headers --forwarded-allow-ips=127.0.0.1` 替换启动命令中的 `--no-proxy-headers`。
- 保持 `--workers 1`。流程、限流和统计保存在内存中，重启后清空；公网保持 `ENABLE_DOCS` 关闭。
- `/health` 检查服务进程，不检测代理或 Telegram。配置 `ADMIN_TOKEN` 后，可通过 `/api/stats?token=你的令牌` 查看统计。
- 浏览器会保存手机号和申请状态，可通过清除网站数据删除。结果凭据不自动持久保存，下载文件请自行保管。
- 页面解析失败时，`app/debug_pages/` 会保存脱敏排查页面，运维可按需清理。

## 联系与许可

频道 [@LongQingHQ](https://t.me/LongQingHQ) · 社群 [@LongQingClub](https://t.me/LongQingClub) · 管理员 [@HengteGo](https://t.me/HengteGo)

本项目采用 [MIT 许可证](LICENSE)，第三方哈希库声明见 [js-sha256 许可证](app/static/sha256.LICENSE.txt)。本项目为非官方工具，使用须遵守 [Telegram API 服务条款](https://core.telegram.org/api/terms)。
