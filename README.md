# R2 Hub

> 多账号 Cloudflare R2 聚合图床 · 一个接口上传，一套永久直链

把任意多个 Cloudflare 账号的免费 R2 桶接入同一个面板：统一 API 上传、自动轮询分发额度、坏号自动切换、图片永久直链对外访问。单用户设计，中文界面，Docker 一条命令部署。

## 功能特性

- **多账号桶管理** — 每个桶独立凭证（Account ID / AK / SK），支持批量导入（JSON 数组或每行一条）
- **轮询分发 + 故障切换** — 上传自动在健康桶间均摊；某号临时故障自动换下一个重试
- **容量配额防护** — 单桶累计超 `MAX_BUCKET_GB`（默认 9.5G）自动跳过，杜绝超额扣费
- **自动巡检** — 后台定时检查所有桶连通性，鉴权失败死号自动停用
- **统一短链** — `/b/{key}` 一个域名入口，302 跳转到对应桶公开域名，隐藏真实归属
- **预签名直传** — 图片字节绕过服务器直达 R2，VPS 流量趋近于零
- **API 密钥** — 程序用独立密钥，与管理员密码隔离，可启停/删除/记录使用时间
- **双数据库后端** — 默认 SQLite 零依赖；设 `DATABASE_URL` 切 PostgreSQL 支持多 worker 高并发
- **中文管理页 + 公开 API 文档** — 文档页无需登录可直接分享

## 快速开始

### Docker Compose（推荐）

```bash
git clone https://github.com/YOUR_USERNAME/r2hub.git
cd r2hub
cp .env.example .env        # 默认管理员密码 zs1236547
docker compose up -d --build
```

打开 `http://服务器IP:8100`，输入 `zs1236547` 登录。

> ⚠️ `ADMIN_TOKEN=zs1236547` 是初始默认密码。部署到公网 VPS 前请修改 `.env` 中的 `ADMIN_TOKEN` 为长随机字符串后重启容器。

### 使用 CI 发布的镜像

push 到 main 分支后 GitHub Actions 自动构建推送，直接拉取：

```bash
docker pull YOUR_DOCKERHUB/r2hub:latest

docker run -d --name r2hub -p 8100:8100 \
  -e ADMIN_TOKEN=zs1236547 \
  -v r2hub-data:/data \
  YOUR_DOCKERHUB/r2hub:latest
```

### 本地裸跑（无 Docker）

```bash
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
set ADMIN_TOKEN=zs1236547
python main.py          # Windows 可直接双击 start.bat
```

## 接入 Cloudflare R2 账号（6 步）

| # | 操作 | 入口 |
|---|------|------|
| 1 | 开通 R2 计划（免费，需绑定支付方式） | CF 控制台左侧 R2 |
| 2 | 创建存储桶 | R2 → 概述 → 创建存储桶 |
| 3 | **开启公共访问** | 桶 → 设置 → 公共访问 → 允许访问（r2.dev） |
| 4 | 创建 API 令牌（对象读和写） | R2 → 管理 R2 API 令牌 |
| 5 | 复制 Account ID + AK + Secret | Secret 只显示一次 |
| 6 | 粘贴到本平台「添加桶」或「批量导入」 | 管理页 |

详细图文教程见部署后的公开文档：**`http://你的地址:8100/docs#cf-setup`**

## API

完整文档（公开、免登录）：**`/docs`**

所有写操作需请求头 `Authorization: Bearer <密钥>`，密钥分两种：

- **API 密钥**（`r2h_...`）：控制台「API 密钥」页创建，仅限上传相关接口，推荐程序使用
- **管理员 Token**：即 `ADMIN_TOKEN`，拥有全部权限

### 方式一：multipart 直传

```bash
curl -X POST http://localhost:8100/upload \
  -H "Authorization: Bearer r2h_你的密钥" \
  -F "file=@cat.jpg"
```

不指定桶时自动在启用且未满的桶之间轮询；也可 `?bucket=acc01` 强制指定。

### 方式二：预签名直传（推荐）

```python
import requests

BASE = "http://你的地址:8100"
H = {"Authorization": "Bearer r2h_你的密钥"}

p = requests.post(f"{BASE}/api/presign", headers=H,
                  json={"filename": "gen.png", "size": len(img)}).json()
requests.put(p["upload_url"], data=img, headers={"Content-Type": "image/png"})
print(p["short_url"])   # 永久短链
```

### 响应字段说明

| 字段 | 说明 |
|------|------|
| `short_url` | 统一短链 `/b/{key}`，302 到真实地址，**对外分享首选** |
| `url` | 桶公开域名的永久直链 |
| `proxied_url` | 未绑域名时走平台代理的通道 |
| `bucket` / `key` | 实际落桶与对象路径（删除、排查用） |

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `ADMIN_TOKEN` | `zs1236547` | 管理员密码（网页登录 + 全部 API）。**公网部署务必修改** |
| `PORT` | `8100` | 服务端口 |
| `MAX_UPLOAD_MB` | `50` | 单文件上传上限（MB） |
| `MAX_BUCKET_GB` | `9.5` | 单桶平台内累计容量阈值，超过自动跳过 |
| `UNHEALTHY_FAILS` | `5` | 连续失败次数达到后标记异常优先跳过 |
| `CHECK_INTERVAL_MIN` | `60` | 自动巡检间隔（分钟），设 0 关闭 |
| `THREADPOOL_TOKENS` | `100` | 同步线程池大小（同时处理的 S3 操作数） |
| `WORKERS` | `1` | uvicorn 进程数；compose 默认 2，多 worker 建议配 PostgreSQL |
| `DATABASE_URL` | 空 = SQLite | PostgreSQL 连接串，compose 默认指向内置 PG |
| `DB_PATH` | `/data/r2hub.db` | SQLite 路径 |
| `PG_PASSWORD` | `r2hub_change_me` | compose 内置 PostgreSQL 密码 |

## 数据库

- **SQLite**（默认）：零依赖，数据在 `/data` 卷，备份即拷贝 `r2hub.db`
- **PostgreSQL**（compose 默认）：解除多进程写串行，适配高并发与多 worker
- 两种库互不迁移；切换前在管理页查看桶配置，用「批量导入」搬到新库即可

## Docker 镜像发布（CI 自动化）

push 到 `main` 分支 → GitHub Actions 自动：amd64 冒烟测试 → 多架构（amd64+arm64）构建 → 推送 `<DOCKER_HUB_USERNAME>/r2hub:1.0` 和 `:latest` 双标签。

所需仓库 Secrets：`DOCKER_HUB_USERNAME`、`DOCKER_HUB_TOKEN`（Docker Hub Access Token）。发新版本只需修改 workflow 中 `VERSION:` 一行再 push。master 分支 push 仅构建验证不推送。

## PicGo / Typora 对接

| 项 | 值 |
|----|----|
| API 地址 | `http://你的地址:8100/upload` |
| POST 参数名 | `file` |
| JSON 路径 | `url` 或 `short_url` |
| 自定义请求头 | `{"Authorization": "Bearer r2h_你的密钥"}` |

## 常见问题

**图片链接 404？** 九成是该桶没开公共访问（CF 控制台 → 桶 → 设置 → 公共访问 → 允许访问）。

**某账号被封了怎么办？** 巡检会自动停用它，新图自动进其他号；该号里已存图片随号失效——重要图建议分散存储并保留本地副本。

**r2.dev 链接会不会限速？** CF 对 r2.dev 有非公开限流，日常看图够用；爆量场景给桶绑自定义域名（需域名托管在同一 CF 账号）。

**忘记管理员密码？** 修改 `.env` 的 `ADMIN_TOKEN` 重启容器即可，密钥管理的 API 密钥不受影响。

## 文件结构

```
r2hub/
├── main.py            # FastAPI 全部路由
├── s3util.py          # boto3 客户端缓存、选桶策略、签名
├── db.py              # SQLite / PostgreSQL 双后端
├── static/index.html  # 中文管理控制台
├── static/docs.html   # 公开 API 文档
├── full_test.py       # 46 项回归测试
├── stress_test.py     # 高并发压测脚本
├── start.bat          # Windows 一键启动
├── Dockerfile
├── docker-compose.yml # 含内置 PostgreSQL
└── .github/workflows/docker-publish.yml
```

## License

MIT
