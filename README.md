# Telegram API 凭据自助申请

当前版本：`v0.2.1`。

通过网页申请或查询 Telegram 的 `api_id` 和 `api_hash`，支持复制结果及下载凭据文件。首页分别统计申请成功、查询成功、验证码发送及失败原因。申请指创建新凭据，查询指读取已有凭据；两项相加为成功合计，验证码发送成功不代表申请成功。

[电脑端预览](docs/screenshots/desktop.png) · [手机端预览](docs/screenshots/mobile.png)

## 使用方法

1. 填写带国家区号的手机号，例如 `+14155550132`。
2. 在 Telegram App 内接收验证码，粘贴到网页提交。
3. 获取凭据后立即保存，也可下载为 `.txt` 文件。

验证码不是短信。每个手机号只能关联一个 `api_id`；如遇 Telegram 限流，请停止连续尝试，按页面提示等待。

同一标签页刷新会继续使用尚未过期的人机验证，不会重复占用验证名额；服务重启后会自动重新验证。

读取页面失败或提交前连接失败时，系统沿用同一代理会话自动重试一次。验证码发送等提交若无法确认结果，不自动重复提交；请先查看 Telegram 消息。错误和等待信息显示在当前表单按钮下方，5 秒后自动消失，不遮挡其他内容，也不改变表单位置。

## 部署

建议使用 Python 3.12 或更新的受维护版本。以下为 Linux/macOS 示例：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

编辑根目录 `.env`，填写代理配置后启动：

```bash
.venv/bin/python -m uvicorn --app-dir app main:app --host 127.0.0.1 --port 8000 --workers 1 --no-proxy-headers --timeout-graceful-shutdown 15
```

访问 `http://127.0.0.1:8000` 即可使用。Windows 将 `python3` 换为 `python`，解释器路径换为 `.venv\Scripts\python.exe`，复制配置使用 `Copy-Item .env.example .env`。

## 配置说明

代理 IP 获取入口：[IPFoxy（邀请链接）](https://www.ipfoxy.com/invite/xyxGLZ)。

| 配置项 | 用途 |
|---|---|
| `PROXY_HOST` / `PROXY_PORT` | 代理网关地址和端口 |
| `PROXY_SCHEME` | 代理协议，如 `socks5h` |
| `PROXY_PASS` | 代理密码 |
| `PROXY_USER_TEMPLATE` | 代理商提供的 sticky 用户名模板，必须包含 `{session}` |

代理必须支持 sticky 会话，确保验证码发送与登录沿用同一出口。完整配置和默认值见 [.env.example](.env.example)；只读取根目录 `.env`，已有环境变量优先。启动命令的端口由 `--port` 决定，配置中的 `PORT` 仅用于直接运行 `python app/main.py`。

## 公网部署与管理

- 使用 Caddy/Nginx 提供 HTTPS；后端仅监听回环地址。
- 反向代理的请求等待时间至少设为 300 秒，覆盖申请流程及网络重试的处理时间。
- 本机反向代理使用 `--proxy-headers --forwarded-allow-ips=127.0.0.1` 替换启动命令中的 `--no-proxy-headers`。
- 保持 `--workers 1`。流程、限流和统计保存在内存中，重启后清空；公网保持 `ENABLE_DOCS` 关闭。
- 未完成的过期流程会自动释放；停止服务时关闭剩余会话，实时统计连接最多等待 15 秒后结束。
- 首页展示全部汇总统计和固定顺序的失败原因，无记录的原因显示 0 次。数据变化时由服务端推送，页面仅更新变化内容，无需手动刷新；连接中断会自动重连。反向代理需关闭 `/api/stats/events` 的响应缓冲，保持实时推送。统计自服务启动以来累计，重启清零，不展示手机号、验证码或凭据。`/health` 检查服务进程，不检测代理或 Telegram。
- 浏览器会保存手机号和申请状态，可通过清除网站数据删除。结果凭据不自动持久保存，下载文件请自行保管。
- 页面解析失败时，`app/debug_pages/` 会保存脱敏排查页面，运维可按需清理。

## 联系与许可

频道 [@LongQingHQ](https://t.me/LongQingHQ) · 社群 [@LongQingClub](https://t.me/LongQingClub) · 管理员 [@HengteGo](https://t.me/HengteGo)

本项目采用 [MIT 许可证](LICENSE)，第三方哈希库声明见 [js-sha256 许可证](app/static/sha256.LICENSE.txt)。本项目为非官方工具，使用须遵守 [Telegram API 服务条款](https://core.telegram.org/api/terms)。
