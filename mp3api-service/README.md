# mp3api-service · Music MP3 API 音源

把外部的 [Music MP3 API](https://mp3.994938.xyz/docs)（FastAPI + yt-dlp + ffmpeg + 本地缓存）
接入 **fnmusic-ext**（飞牛音乐扩展），作为第四类主音源「Music MP3 API」。

它实现的是项目已有的 **musicdl 兼容 HTTP 契约**，所以代理层（`proxy/app.py`）
已有的搜索聚合、取链、`Range`/206、边听边存、封面、歌词、转码、健康检查等
链路全部复用，不需要为它重写一遍。

```
飞牛音乐 Web / App
        │
        ▼
fnmusic-ext 代理 (proxy/app.py)
        │  FNMUSIC_MP3API_ENABLED=true 时，musicdl client 的 base_url 指向本服务
        ▼
mp3api-service  (127.0.0.1:8776 / 容器 8006)
        │  MP3API_BASE_URL + MP3API_API_KEY（仅服务端可见）
        ▼
Music MP3 API  (/api/search  /api/resolve  /api/music)
```

> 本目录只负责「协议适配」。Music MP3 API 本身是独立项目，可部署在同一台 NAS、
> 局域网另一台机器，或经 HTTPS 域名远程访问。

## 上游服务（Music MP3 API）

本仓库不包含上游源码，只提供接入它的适配器。上游是独立的 FastAPI 项目：
`yt-dlp` 下载 → `ffmpeg / libmp3lame` 转 MP3 → 本地缓存（可配 500 GB / 90 天），
对外暴露 `/api/search`、`/api/resolve`、`/api/music`（支持 Range / 206）、
`/api/health`，并可经 Nginx 反代 + API Key 远程访问（在线文档 `/docs`、
`docs/API.md`、`docs/openapi.json`）。

典型部署：

- 与 NAS 同机：`MP3API_BASE_URL=http://127.0.0.1:8765`
- 局域网或远程：`MP3API_BASE_URL=https://mp3.example.com`，并填写 `MP3API_API_KEY`
- 上游出口 IP 若被 YouTube 限制，需自行配置代理（如 Cloudflare WARP）换出口

上游不可用时，本适配器 `/healthz` 返回 `ok:false` 与 `upstream_status`，
搜索软失败为空结果，不会拖垮代理的其它本地功能。

## 契约端点

| 端点 | 说明 |
|---|---|
| `GET /healthz` | 健康检查，返回 dict 且 `ok` 不为 `false`；同时探测上游 `/api/health` |
| `GET /sources` | `{"enabled":["mp3api"],"registered":["mp3api"]}`，供管理页平台列表 / `extend.sh` 读取 |
| `GET /search?keyword=&limit=[&sources=]` | 调上游 `/api/search`，返回 `{"ok":true,"items":[...],"errors":{},"engine":"mp3api"}` |
| `GET /info?id=mp3api:<video_id>` | 调上游 `/api/resolve`，返回标题 / 艺人 / 时长 / 封面 / `lyric` 等契约字段 |
| `GET /stream?id=...&proxy=true[&quality=mp3]` | 调上游 `/api/music`，透传 MP3 字节流、`Range`/`206`、`Content-Range` |

条目 id 一律为 `mp3api:<video_id>`；代理据此生成 `online:mp3api:<video_id>`，
既不是 `netease` 也不是 `lx`，因此自动落入 musicdl 分支。

`quality=mp3`（代理低音质档会传）映射到 `<=128 kbps`，其余使用 `MP3API_BITRATE`。

## 配置（环境变量）

`MP3API_*` 有两个来源，**进程环境变量优先**：

1. 直接 `export MP3API_*`（本机手工 `uvicorn` 启动时）；
2. **推荐**：写进仓库根 `.env`，由适配器启动时按 `FNMUSIC_ENV_FILE`
   （Docker = `/repo/.env`，native = 渲染后的仓库 `.env`）只读加载。
   密钥不写进 supervisor 的 `environment`，因此只在 mp3api 进程内可见，
   不会扩散到容器内其它音源进程，也不会出现在进程表 / 日志里。
   设 `MP3API_NO_DOTENV=1` 可关闭该行为（离线测试用）。

| 变量 | 默认 | 说明 |
|---|---|---|
| `FNMUSIC_ENV_FILE` | 由 supervisor 注入 | 适配器只读该 `.env` 补齐 `MP3API_*` |
| `MP3API_BASE_URL` | `http://127.0.0.1:8765` | Music MP3 API 地址，可填 `https://mp3.994938.xyz` |
| `MP3API_API_KEY` | 空 | 上游 API Key，**只保存在服务端**，绝不写进对外 JSON / 重定向 |
| `MP3API_BITRATE` | `192` | 码率 kbps，自动收敛到 `64/96/128/192/256/320` |
| `MP3API_TIMEOUT` | `300` | 上游超时秒；首次请求需下载 + 转码，别设太小 |
| `MP3API_MAX_DURATION` | `900` | 搜索阶段剔除超过该秒数的视频（与上游 `MUSIC_MAX_DURATION_SEC` 对齐）；`0` = 不过滤 |
| `MP3API_SOURCE_NAME` | `mp3api` | 音源短名（GUID 前缀） |
| `MP3API_TRUST_ENV` | `0` | 是否让 httpx 读取 `HTTP_PROXY` 等环境代理 |
| `FNMUSIC_MP3API_ENABLED` | `false` | **代理侧**开关，见下 |
| `FNMUSIC_MP3API_URL` | `http://127.0.0.1:8776` | 代理访问本服务的地址 |
| `FNMUSIC_MUSICDL_URL` | — | 兼容：代理在 mp3api 模式下会把 musicdl client 指向 `FNMUSIC_MP3API_URL` |

## 启用（源码部署）

1. 装依赖：`python3 -m pip install -r mp3api-service/requirements.txt`
2. 在仓库根 `.env` 里配置（四选一，只写一个 `true`）：
   ```dotenv
   FNMUSIC_MUSICDL_ENABLED=false
   FNMUSIC_NETEASE_ENABLED=false
   FNMUSIC_LX_ENABLED=false
   FNMUSIC_MP3API_ENABLED=true

   FNMUSIC_MP3API_URL=http://127.0.0.1:8776
   # 代理已对 mp3api 豁免 online_sources 白名单；置空只是避免管理页显示混淆
   FNMUSIC_ONLINE_SOURCES=
   # 直连必须关闭（本服务不暴露上游地址）
   FNMUSIC_MUSICDL_DIRECT_PLAY=false

   # 适配器侧：写进 .env 即可，mp3api 进程启动时按 FNMUSIC_ENV_FILE 只读加载，
   # 无需（也不应）把密钥传给 supervisor / compose environment
   MP3API_BASE_URL=http://127.0.0.1:8765
   MP3API_API_KEY=<你的密钥>
   MP3API_BITRATE=192
   ```
3. 在管理页「音乐源」选择 **Music MP3 API** 并保存（或 `./install.sh --sources mp3api --extend`）。
4. 验证：`./mp3api-service/smoke_test.sh http://127.0.0.1:8776 "Chopin Nocturne"`

Docker 形态下端口为 `127.0.0.1:8776 -> 8006`，无需手工启动进程；
`[program:mp3api]` 只注入 `FNMUSIC_ENV_FILE=/repo/.env`，`MP3API_*` 由本服务只读加载。
native 形态监听宿主 `127.0.0.1:8776`，公共 environment 已带渲染后的 `FNMUSIC_ENV_FILE`，
WebUI 另由 `WEBUI_MP3API_URL` 指向该地址。

## 安全约定

- **API Key 只在本服务进程内**：上游请求用 `X-API-Key` 头；搜索结果不带
  `download_url` / `url`，302 只重定向回本服务自身，不会把带密钥的地址交给客户端。
- **不要开播放直连**：本服务刻意不实现 `/internal/resolve` 与 `/internal/stream`
  （那会把上游地址暴露给客户端，且客户端通常也访问不到 NAS 的 127.0.0.1）。
  保持 `FNMUSIC_MUSICDL_DIRECT_PLAY=false` / `FNMUSIC_MP3API_DIRECT_PLAY=false`。
- 上游启用认证时务必设置 `MP3API_API_KEY`；`/api/health` 探活无需密钥。
- `MP3API_BASE_URL` 请指向可信地址；适配器对 `id` 做了 `^[A-Za-z0-9_-]{1,32}$` 校验。

## 测试

```bash
python3 -m pytest mp3api-service/test_app.py -q     # 离线契约测试（MockTransport）
./mp3api-service/smoke_test.sh                      # 对运行中的服务做真实冒烟
```

## 合规

仅用于你拥有版权或已获授权的内容、个人 / 家庭自用；请勿公开分发或商用。
歌曲可用性、码率与缓存行为由上游 Music MP3 API 决定。
