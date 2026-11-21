# 浏览器 Agent（卖家本机运行）

把亚马逊搜索/详情、ABA 报表、卖家精灵导出、POE 商机探测器这 5 个「必须复用已登录紫鸟会话」的操作，从服务器下沉到**卖家自己的 Windows 机器**上执行。服务器只负责编排/DB/LLM 与纯业务，需要浏览器时通过 HTTP 调本 Agent。

## 启动

在卖家本机（已安装紫鸟，且店铺已登录）：

```powershell
cd backend
# 建议用 agent 模式打开店铺，确保内核带 --remote-debugging-port：
#   C:\Users\<用户>\AppData\Roaming\ziniaobrowser\cli\ziniao.exe browser start --id <店铺id> --mode agent
$env:KEYWORD_AGENT_HOST = "127.0.0.1"
$env:KEYWORD_BROWSER_AGENT_TOKEN = "<至少32个随机字符>"
$env:KEYWORD_AGENT_PORT = "7100"
.\.venv\Scripts\python.exe -m agent.run
```

- 默认数据目录 `backend/agent/data/`，可用 `KEYWORD_AGENT_DATA_DIR` 覆盖（存放 `cdp_port`/`store_id`/`aba_auth.json`/spool）。
- 紫鸟 CLI 自动发现逻辑与原来一致：`KEYWORD_AMAZON_CDP_PORT`/`KEYWORD_ABA_CDP_PORT`/`KEYWORD_SELLERSPRITE_CDP_PORT`、`KEYWORD_ZINIAO_CLI`、`KEYWORD_ZINIAO_STORE_ID` 均可选。

## 服务器侧对接

服务器设置环境变量：

```bash
KEYWORD_BROWSER_AGENT_URL=http://<卖家机可达地址>:7100
KEYWORD_BROWSER_AGENT_TOKEN=<与 Agent 相同的至少32字符随机 Token>
```

设置后，`create_app` 会用远程客户端替换本地浏览器模块；不设置则仍走本机紫鸟（单机开发/测试不变）。

## 安全

Agent 默认只绑定 `127.0.0.1`。除 `/v1/health` 外，所有业务端点都要求 `Authorization: Bearer <token>`。Token 少于 32 个字符会拒绝启动；未配置 Token 时只允许 `KEYWORD_AGENT_HOST=127.0.0.1/localhost/::1` 的本机开发模式。

生产服务器连接使用 `deploy/windows-agent/`：安装脚本注册当前用户登录时运行的 `KeywordLibrary-Ziniao-Agent`，监督脚本先等待本机 health、核对固定 SSH 主机指纹与服务器 Docker network ID，再建立 `<Docker Gateway>:17100:127.0.0.1:7100` 反向转发。Agent 或 SSH 退出后有上限退避重启；配置、持久幂等信封、锁和专用私钥放在 `%LOCALAPPDATA%\KeywordLibraryAgent` 并限制为当前用户/SYSTEM/Administrators，不得写入仓库或同步盘。

带 `X-Keyword-Operation-ID` 的成功业务调用会按“操作 ID + 请求内容”原子保存完成信封；同一请求在断线重试或 Agent 重启后直接重放，避免 ABA、卖家精灵和 POE 重复导出。错误信封不缓存，登录或验证码仍必须人工处理。

远程访问必须再叠加 VPN（推荐 Tailscale/WireGuard）或 SSH/受控 FRP 隧道，**不得把 7100 直接开放到公网**。必须使用 `python -m agent.run` 启动；该入口把同一个 `KEYWORD_AGENT_HOST` 同时交给安全校验和 Uvicorn，避免监听参数绕过限制。

## 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/health` | 存活检查（不占浏览器线程） |
| POST | `/v1/session` | 打开搜索会话，返回 `session_id` |
| POST | `/v1/session/{id}/search` | 翻页搜索，返回 `SearchPage` + 截图/诊断文件 |
| POST | `/v1/session/{id}/product` | 会话内读 ASIN 详情 |
| DELETE | `/v1/session/{id}` | 关闭会话 |
| POST | `/v1/product` | 一次性 ASIN 详情 |
| POST | `/v1/aba/report` | ABA 周报（含 `raw_rows`） |
| GET | `/v1/aba/status` | ABA 凭据/端口状态 |
| POST | `/v1/sellersprite/export` | 卖家精灵导出，返回原件字节 |
| POST | `/v1/poe/download` | 商机探测器 CSV，返回原件字节 |

响应是 JSON 信封：`{"result": …}`、`{"result":…,"files":{"name":base64}}` 或 `{"error":…,"files":…}`。
