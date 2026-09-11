# 红薯雷达

红薯雷达是一套本地优先的跨平台商品监控工具：用户主动添加小红书公开商品后，程序记录可信的累计已售、实际价格和店铺信息，并以相邻快照的高水位差计算小时、今日、昨日与历史销量。

> 仅供学习交流，严禁用于商业用途，请于 24 小时内删除。请遵守目标网站规则、适用法律及数据使用约定。

## 项目结构

```text
core/       共享采集、SQLite、统计、调度、企业微信逻辑
desktop/    PySide6 macOS/Windows 图形客户端
server/     FastAPI REST 服务与静态 Web 托管
web/        React + TypeScript + Vite 响应式界面
mcp_server.py 只读 stdio MCP 服务
tests/      共享逻辑单元测试
```

桌面端、Web API 和 MCP 都调用 `core` 中相同的数据库和统计函数；Web 浏览器不会直接读取数据库、Cookie、浏览器 Profile 或本机文件。

## 首次安装与本地运行

项目目标 Python 为 3.11 或 3.12。

```bash
python3.11 -m venv .venv
source .venv/bin/activate                 # Windows：.venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium      # 明确安装，不在程序中静默下载
pytest -q
```

启动桌面版：

```bash
python app.py
```

启动 Web 开发服务（终端一）：

```bash
cd web && npm ci && npm run dev
```

终端二启动 API：

```bash
uvicorn server.main:app --reload --host 127.0.0.1 --port 8000
```

Vite 默认会把 `/api` 请求交给同源服务；生产模式请先执行 `cd web && npm run build`，再执行 `scripts/start-web.sh`，FastAPI 会托管 `web/dist` 和 API。可访问 `/docs` 查看 OpenAPI。

## 数据、隐私与网络边界

- macOS 数据在 `~/Library/Application Support/RedPotatoRadar`；Windows 在 `%LOCALAPPDATA%/RedPotatoRadar`。用 `RED_POTATO_RADAR_DATA_DIR` 覆盖。
- SQLite 数据库为 `monitor.db`，启用 WAL、事务、参数化 SQL 和自动字段迁移。Web/Docker 必须将此目录挂载为持久化卷。
- 默认 Web 仅应监听 `127.0.0.1`。如需对局域网/公网开放，必须放到 HTTPS 反向代理后，设置严格的 `RED_POTATO_RADAR_ALLOWED_ORIGINS`，并在代理层使用身份认证及 CSRF 策略；不要暴露浏览器 Profile、数据库路径或 Webhook。
- 设置 `RED_POTATO_RADAR_API_TOKEN` 后，除 `/api/health` 外的 API 必须携带 `Authorization: Bearer <token>`；浏览器端应由可信反向代理注入认证信息，避免把 token 编译进静态前端。
- 通知渠道（企业微信或飞书机器人 Webhook，二选一）只存于服务端/当前用户 SQLite；GET API 仅返回掩码。日志不会写入完整 Webhook 或 Cookie。

## Docker 部署

```bash
cp .env.example .env
docker compose up -d --build
```

示例 Compose 只把端口绑定到 `127.0.0.1:8000`。生产环境应由 Caddy/Nginx/Traefik 终结 TLS，再代理到本机端口；备份 Docker volume 或 `monitor.db`（建议先执行 WAL checkpoint），并配置进程自动重启。镜像不自动下载 Playwright 浏览器：需要浏览器兜底时，在自建镜像/运行环境中明确安装对应资源，或提供 Chrome。

## 构建桌面安装包

PyInstaller 不能交叉编译，必须在目标系统构建与验收：

- Windows：在 Windows + Python 3.11 下运行 `build-windows.bat`，产出 `dist/红薯雷达.exe` 和 `dist/红薯雷达MCP.exe`，主程序为无控制台窗口的 EXE。
- macOS：在 macOS + Python 3.11 下执行 `chmod +x build-macos.sh && ./build-macos.sh`，产出 `dist/红薯雷达.app` 和 `dist/红薯雷达MCP`。请针对 Apple Silicon 或 Intel 使用原生解释器构建；公开分发前需自行签名与公证。

`assets/app-icon.ico` 与 `assets/app-icon.icns` 是打包图标入口。仓库暂未提供品牌图标源，请在正式打包前放入对应格式的授权图标；未提供时 PyInstaller 会明确报错，而不是伪造已完成的安装包。

GitHub Actions 会在 Windows 和 macOS runner 上分别运行测试并原生构建，不会将任一系统的包伪装成另一个系统产物。

## MCP（只读）

在同一台保存数据库的主机上运行：

```bash
python mcp_server.py
```

示例客户端配置：

```json
{
  "mcpServers": {
    "xhs-sales-monitor": {
      "command": "python",
      "args": ["/绝对路径/红薯雷达/mcp_server.py"],
      "env": {"RED_POTATO_RADAR_DATA_DIR": "/与主程序相同的数据目录"}
    }
  }
}
```

MCP 工具包括 `list_shops`、`list_products`、`get_product_sales`、`get_shop_sales`、`get_product_ranking`、`get_shop_ranking`、`search_products` 和 `get_overview`。它们只读数据库，所有返回包含销量口径和数据时间。

## 验证范围与待目标平台验证项

`pytest -q` 覆盖高水位、防负销量、跨日、缺少基线、数据库迁移、删除、商品字段解析与下架识别。本机可运行 Web 构建和 Python 单测；Windows EXE、macOS `.app`、可见浏览器交互、企业微信真实发送及目标站点网络响应必须在拥有 Python 3.11、浏览器和相应授权环境的目标系统实际验证，不能由其他系统替代。
