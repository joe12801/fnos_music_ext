# Docker 部署指南（含第五音源 Meow）

本页说明如何用 Docker / Docker Compose 运行 `fnmusic-ext` 的**音源服务与管理页**，
包含第五音源「Meow 聚合总源」的完整配置。源码构建与预构建镜像两种方式都可用。

完整安装（FPK / native / 向导）见 [INSTALL.md](INSTALL.md)；第五音源适配器契约见
[meow-service/README.md](../meow-service/README.md)；全部配置键见 [.env.example](../.env.example)。

---

## 1. 先分清：容器跑什么，宿主机跑什么

| 层次 | 运行位置 | 说明 |
| :--- | :--- | :--- |
| 官方音乐后端 | 飞牛 OS | 不变，官方曲库数据库只读访问 |
| **fnmusic-ext 代理** | **宿主机 systemd（`fnmusic-ext.service`）** | 官方 `trim_music.socket` 接管、在线搜索/播放/歌词/封面适配、缓存转码、飞牛桌面网关 |
| 音源服务 + WebUI | **单个 `fnmusic-sources` 容器** | supervisor 按 `.env` 五选一按需启停，另可开 WebUI |

**容器不负责 socket 接管，也不挂 `docker.sock`。** 因此 Docker 部署的完整前提是宿主机上
`fnmusic-ext.service` 已按 [INSTALL.md](INSTALL.md) 安装并 active（此服务与部署形态无关，
Docker / native 都在宿主机）。只起容器、不装宿主机代理，飞牛音乐里不会出现在线结果。

两种部署形态（Docker / native）**不可同时启用**，也不要从多个目录同时接管同一个官方 Socket。

---

## 2. 前置条件

- fnOS / Linux + Docker 24+ 与 `docker compose` v2
- 仓库根目录 `.env`（从 `.env.example` 复制；由 WebUI 或手工维护）
- 宿主机 `fnmusic-ext.service` 已启用并通过接管预检
- 选 **meow** 时：上游 `MeowMusicServer` 可达（如 `http://127.0.0.1:2233`），
  本仓库只提供适配器，不提供该上游服务
- 容器只需访问本机回环，所有端口映射到 `127.0.0.1`，不暴露公网

---

## 3. 方式 A：预构建镜像（推荐，免本地编译）

已发布多架构镜像（`linux/amd64` + `linux/arm64`），三个镜像源内容完全一致：

| 镜像源 | 地址 | 适用场景 |
| :--- | :--- | :--- |
| Docker Hub（默认） | `joe1280/fnmusic-sources:3.1.2-meow` | 境外 / 网络通畅 |
| 阿里云 ACR 个人版 | `crpi-wv2uaegt8g2y0g3c.cn-guangzhou.personal.cr.aliyuncs.com/joeloveqt-project/joeloveqt:3.1.2-meow` | 国内直连更稳 |
| GitHub GHCR | `ghcr.io/joe12801/fnos_music_ext:3.1.2-meow` | 公开镜像，免登录 |

**从零到可用只需四步：**

1. 按 [INSTALL.md](INSTALL.md) 安装宿主机代理，确认 `systemctl is-active fnmusic-ext.service` 返回 `active`；
2. 在仓库根目录准备 `.env`（主音源五选一只开一个，见第 5 节）；
3. 执行下面的 `docker compose ... up -d`；
4. 回到飞牛音乐重新搜索（第五音源需自备上游 MeowMusicServer）。

```bash
cd /path/to/fnmusic_ext

# Docker Hub（默认）
docker compose -f docker-compose.prebuilt.yml pull
docker compose -f docker-compose.prebuilt.yml up -d

# 或改用阿里云 ACR 国内镜像（国内网络更稳）
FNMUSIC_IMAGE=crpi-wv2uaegt8g2y0g3c.cn-guangzhou.personal.cr.aliyuncs.com/joeloveqt-project/joeloveqt:3.1.2-meow \
  docker compose -f docker-compose.prebuilt.yml up -d

# 或改用 GitHub Container Registry（ghcr.io，公开镜像免登录）
FNMUSIC_IMAGE=ghcr.io/joe12801/fnos_music_ext:3.1.2-meow \
  docker compose -f docker-compose.prebuilt.yml up -d
```

镜像内已打包六个服务的运行依赖（Python + Node 22 + ffmpeg + supervisor）与洛雪
`lxserver` 运行目录，启动时按 `.env` 只拉起所选音源进程，未启用的不驻留内存。

固定版本请把 tag 换成对应发行 tag，不要用浮动 `latest` 跨大版本升级。

---

## 4. 方式 B：本地构建

```bash
cd /path/to/fnmusic_ext
docker compose up -d --build
```

`docker-compose.yml` 的 `build.args` 可由根目录 `.env` 覆盖：

| 变量 | 默认 | 用途 |
| :--- | :--- | :--- |
| `FNMUSIC_BASE_IMAGE` | `python:3.13-slim` | 基础镜像；`ensure_base_image.sh` 会探测国内可达性并自动维护 |
| `FNMUSIC_PIP_INDEX` | `https://mirrors.tencent.com/pypi/simple/` | pip 源 |
| `FNMUSIC_APT_MIRROR` | `https://mirrors.tencent.com` | apt 源 |

手工构建等价命令：

```bash
docker build --progress=plain \
  --build-arg BASE_IMAGE=python:3.13-slim \
  --build-arg PIP_INDEX_URL=https://mirrors.tencent.com/pypi/simple/ \
  --build-arg APT_MIRROR=https://mirrors.tencent.com \
  -t fnmusic-sources:3.1.2-meow \
  -f container/Dockerfile .
```

> **构建上下文**：根目录 `.dockerignore` 采用「先全排除、逐项放行」的白名单，
> 必须放行 `meow-service/`（Dockerfile 要 `COPY meow-service/requirements.txt` 与
> `app.py`）。漏放行会报 `failed to compute cache key: /meow-service: not found`。
> 同理不要放行数据目录、缓存与含密钥的文件。
>
> **离线构建**：洛雪 `lxserver` 发行包优先取 `container/lxserver-artifact/` 下的预置
> zip（版本与 SHA256 由 `container/lxserver.version` 锁定）；缺失时才走 ghfast /
> gh-proxy / GitHub 镜像链下载。内网无外网时请先预置该 zip。

---

## 5. `.env` 关键配置（五选一）

主音源**只能启用一个**，其余保持 `false`。首次安装或切换后需在飞牛音乐里重新搜索。

```ini
# ── 五选一开关 ──────────────────────────────
FNMUSIC_MUSICDL_ENABLED=false
FNMUSIC_NETEASE_ENABLED=false
FNMUSIC_LX_ENABLED=false
FNMUSIC_MP3API_ENABLED=false
FNMUSIC_MEOW_ENABLED=true          # ← 第五音源
FNMUSIC_WEBUI_ENABLED=true

# ── 第五音源：Meow 聚合总源 ──────────────────
FNMUSIC_MEOW_URL=http://127.0.0.1:8778
MEOW_BASE_URL=http://127.0.0.1:2233   # 上游 MeowMusicServer
MEOW_API_KEY=                        # 上游如启用鉴权才填
MEOW_TIMEOUT=300                     # 首次点歌含聚合搜索+转码，别设太小

# 歌词 / 海报 / 专辑 / 时长补全（推荐开启）
# 上游 /api/search 恒空且缓存只有 music.mp3，不补则飞牛播放无封面无歌词。
MEOW_METADATA_ENABLED=true
MEOW_METADATA_BASE=https://music.163.com
MEOW_METADATA_TIMEOUT=2.8
MEOW_METADATA_TTL=604800
```

`MEOW_*` 由 meow 进程自行只读 `/repo/.env` 加载，**不经 supervisor 环境扩散**。
`set 0` 可一键关闭元数据补全；补全失败静默降级，绝不影响播放。
完整键名与范围见 [.env.example](../.env.example)。

> 注意：`MEOW_BASE_URL` 是**容器内**视角的地址。上游服务若在宿主机，用
> `127.0.0.1:2233` 在桥接网络里指向容器自身而非宿主机——此时请填宿主机局域网 IP
> （本仓库默认 `http://192.168.2.220:2233`），或给容器加 `host` 网络 / `extra_hosts`。

---

## 6. 端口

| 宿主端口 | 容器端口 | 服务 |
| :--- | :--- | :--- |
| 8768 | 8001 | musicdl |
| 8770 | 8002 | netease (musicbox) |
| 8772 | 8003 | lxmusic |
| 8774 | 8004 | WebUI（浏览器走飞牛网关，勿直接暴露） |
| 8776 | 8006 | mp3api（第四音源） |
| **8778** | **8008** | **meow（第五音源）** |

全部绑定 `127.0.0.1`。`EXPOSE 8001 8002 8003 8004 8006 8008` 已含第五音源端口。

---

## 7. 卷

| 挂载 | 用途 |
| :--- | :--- |
| `./sources-data:/data` | 网易登录态（XDG 目录）、洛雪脚本缓存与 `state.json`、服务日志 |
| `.:/repo` | `entrypoint` / `healthcheck` 读 `.env`，WebUI 写 `.env`，meow / mp3api 只读上游地址与密钥 |
| `/run/fnmusic-ext:/run/fnmusic-ext:ro` | 宿主机维护 socket，供 WebUI 处理 organize / cleanup / music-users 端点 |

不挂 `docker.sock`；`--no-webui` 仍须挂 `.:/repo`（否则按需启动读不到开关）。

---

## 8. 日常操作

```bash
# 状态与健康检查（进程层 + HTTP 层两道检查）
docker compose -f docker-compose.prebuilt.yml ps
docker inspect --format '{{.State.Health.Status}}' fnmusic-sources

# 第五音源探活：ok / provider / upstream_status / meta_enabled
curl -s http://127.0.0.1:8778/healthz | python3 -m json.tool

# 日志
docker logs -f --tail 100 fnmusic-sources

# 切换音源：改 .env 的五选一开关后重启容器（或在 WebUI 内热切换）
docker compose -f docker-compose.prebuilt.yml restart

# 升级镜像
docker compose -f docker-compose.prebuilt.yml pull
docker compose -f docker-compose.prebuilt.yml up -d
```

`container/healthcheck.sh` 对 meow 做进程层检查；上游可达性由 meow 的
`/healthz` 用 `upstream_status` / `ok` 自报（WebUI 健康页可见），**不会因外部上游
故障把容器判成 unhealthy**，避免上游抖动导致容器反复重启。

### 回滚

```bash
docker compose -f docker-compose.prebuilt.yml down
```

宿主机执行 `systemctl stop fnmusic-ext.service` 会自动恢复官方 socket。
回滚前请保留 `.env` 与 `sources-data/`；原四源的运行环境（Node、musicdl、
lxserver）都已在本镜像内，无需额外安装。

---

## 9. 维护者：构建并推送镜像

```bash
# Docker Hub
docker login --username=joe1280          # PAT 作密码，走 --password-stdin
docker tag fnmusic-sources:3.1.2-meow joe1280/fnmusic-sources:3.1.2-meow
docker tag fnmusic-sources:3.1.2-meow joe1280/fnmusic-sources:latest
docker push joe1280/fnmusic-sources:3.1.2-meow
docker push joe1280/fnmusic-sources:latest

# 阿里云 ACR 个人版（国内直连；命名空间 joeloveqt-project / 仓库 joeloveqt 已创建）
docker login --username=aliyun1109508239 \
  crpi-wv2uaegt8g2y0g3c.cn-guangzhou.personal.cr.aliyuncs.com
docker tag fnmusic-sources:3.1.2-meow \
  crpi-wv2uaegt8g2y0g3c.cn-guangzhou.personal.cr.aliyuncs.com/joeloveqt-project/joeloveqt:3.1.2-meow
docker push crpi-wv2uaegt8g2y0g3c.cn-guangzhou.personal.cr.aliyuncs.com/joeloveqt-project/joeloveqt:3.1.2-meow

# GitHub Container Registry（由 .github/workflows/docker-publish.yml 用内置
# GITHUB_TOKEN 自动推送；手动推送需具备 write:packages 权限的 PAT）
echo "$GH_PAT" | docker login ghcr.io --username joe12801 --password-stdin
docker tag fnmusic-sources:3.1.2-meow ghcr.io/joe12801/fnos_music_ext:3.1.2-meow
docker push ghcr.io/joe12801/fnos_music_ext:3.1.2-meow
```

三个 registry 的 `3.1.2-meow` / `latest` 由同一份多架构 manifest 生成，
digest 一致（含 `linux/amd64` + `linux/arm64`）；国内优先 ACR，境外优先
Docker Hub / GHCR。

GHCR 由 `.github/workflows/docker-publish.yml` 用内置 `GITHUB_TOKEN` 推送，
package 会自动关联 `joe12801/fnos_music_ext` 仓库并继承其可见性（本仓库为
**public**），因此**免登录即可 `docker pull`**（已实测匿名拉取 manifest 返回 200）。
若改为手工用 PAT 推送，首次可能生成私有 package，需在仓库
Packages → `<package>` → Package settings 里改为 **Public**。

> 若 NAS 有 IPv6 但 Docker Hub 的 AAAA 记录不可达，`docker login/push` 会走 IPv6
> 超时（curl 走 IPv4 却正常）。可在 `/etc/hosts` 为 `registry-1.docker.io`、
> `auth.docker.io`、`production.cloudflare.docker.com` 固定 IPv4，或改用 ACR 镜像。

---

## 10. 常见问题

- **飞牛音乐里搜不到在线结果**：多半是宿主机 `fnmusic-ext.service` 没起来，或
  socket 接管未生效；容器健康不代表代理已接管。
- **容器 healthy 但没有歌词/海报**：确认 `MEOW_METADATA_ENABLED=true`、NAS 能访问
  `music.163.com`，并在飞牛里重新搜索（已缓存结果需刷新详情页触发重新拉取 `/info`）。
- **meow 播放无声音**：检查 `MEOW_BASE_URL` 在容器内是否真的可达；`curl /healthz`
  看 `upstream_status` 与 `error` 字段。
- **Docker 与 native 同时跑**：同一官方 Socket 被多处接管会互相覆盖，务必只留一种。
- **构建报 `/meow-service: not found`**：`.dockerignore` 缺 `!meow-service/`。
