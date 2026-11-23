# meow-service · Meow 聚合总源

把自建的 **MeowMusicServer**（Go，10+ 音源聚合总源）接入 **fnmusic-ext**（飞牛音乐扩展），作为音源选择器中的「Meow 聚合总源」。

它实现的是项目已有的 **musicdl 兼容 HTTP 契约**，所以代理层（`proxy/app.py`）已有的搜索聚合、取链、`Range`/206、边听边存、封面、歌词、转码、健康检查等链路全部复用，不需要为它重写一遍。

```
飞牛音乐 Web / App
        │
        ▼
fnmusic-ext 代理 (proxy/app.py)
        │  FNMUSIC_MEOW_ENABLED=true 时，musicdl client 的 base_url 指向本服务
        ▼
meow-service  (host 127.0.0.1:8778 / 容器 8008)
        │  MEOW_BASE_URL（Meow 总源地址）
        ▼
MeowMusicServer  (/api/search  /stream_live)
        │  酷我/网易/xfabe/QQ/酷狗/NCM/GDStudio/mp3api/yuafeng …
        ▼   ffmpeg 转码落盘 → 定长 MP3
```

> 本目录只负责「协议适配」。MeowMusicServer 本身是独立 Go 项目，可部署在同一台 NAS、局域网另一台机器，或经 HTTPS 域名远程访问。

## 上游服务（MeowMusicServer）

上游接口：

| 端点 | 说明 |
|---|---|
| `GET /api/search?query=<词>` | 搜索，返回 `[{title, artist, cover_url, lyric_url, duration}, ...]` |
| `GET /stream_live?song=<歌名>&singer=<歌手>` | 定长 MP3 字节流（`Content-Length`，无 chunked，内部 ffmpeg 转码落盘） |
| `GET /` | 根路径，用于 `/healthz` 探活 |

典型部署：

- 与 NAS 同机：`MEOW_BASE_URL=http://127.0.0.1:2233`
- 局域网：`MEOW_BASE_URL=http://192.168.2.220:2233`（默认）
- 远程经反代：`MEOW_BASE_URL=https://你的域名`，并按需填 `MEOW_API_KEY`

上游不可用时，本适配器 `/healthz` 返回 `ok:false` 与 `upstream_status`，搜索软失败为空结果，不会拖垮代理的其它本地功能。

## 契约端点

| 端点 | 说明 |
|---|---|
| `GET /healthz` | 健康检查，探测上游根路径 |
| `GET /sources` | `{"enabled":["meow"],"registered":["meow"]}` |
| `GET /search?keyword=&limit=[&sources=]` | 调上游 `/api/search`；一条都没有时才兜底成可点播候选；命中元数据缓存时直接带封面/专辑/时长 |
| `GET /info?id=meow:<b64>` | 从可逆 id / 搜索缓存 + 元数据补全返回曲目元数据（含 `cover_url`、`lyric`） |
| `GET /stream?id=meow:<b64>&proxy=true` | 调上游 `/stream_live`，支持 `Range` / 206，透传定长字节流 |

条目 id 一律为 `meow:<base64url(song\x1fsinger)>`，可逆、无状态，适配器重启也不失效。
代理据此生成 `online:meow:<b64>`，既不是 `netease` 也不是 `lx`，因此自动落入 musicdl 分支。

## 接入上游的具体音源（v3.2）

上游 MeowMusicServer 本身是「多音源聚合」：`/api/search?query=&source=<键名>` 可以
指定只搜某一家。本适配器把这件事透出来，配置 `MEOW_SOURCES` 即可：

```dotenv
MEOW_SOURCES=kuwo,bilibili,ximalaya
```

| 键名 | 平台 | 键名 | 平台 |
|---|---|---|---|
| `kuwo` | 酷我音乐 | `fivesing` | 5sing |
| `netease` | 网易云音乐 | `lizhi` | 荔枝 FM |
| `kugou` | 酷狗音乐 | `bilibili` | 哔哩哔哩 |
| `qq` | QQ 音乐 | `qishui` | 汽水音乐 |
| `ximalaya` | 喜马拉雅 | `ext` | 扩展源（mp3api 类） |

行为：

- **空（默认）**：完全不发 `source` 参数，走上游自己的聚合链路——与旧版逐字一致，
  一行配置都不用改。
- **配了值**：`/search` 按顺序逐个音源**并发**请求上游（并发上限
  `MEOW_SOURCE_CONCURRENCY`），结果按「歌名+歌手」跨源去重后合并；`/sources`
  会列出 `meow` 与 `meow-<键名>`，`/search?sources=meow-bilibili` 可临时只搜一家。
- 单个音源失败只记进 `errors`，不影响其它音源出结果。
- 全部音源都空时才退化成「歌名 → 可点播候选」的兜底。

### 为什么 id 要带 xid / bv

上游 `/api/search` 每条结果都带一个 `url`，里面已经钉好了「取哪一条具体音频」：

- 喜马拉雅 `xid=<trackId>`、5sing `xid=fs:<songId>:<type>`、荔枝 `xid=lz:<voiceId>:<日期>`
- B 站 `bv=BV1xxxxxxxxx`

这些内容靠「歌名+歌手」二次解析**必然张冠李戴**——搜到的播客会被解析成同名的歌，
B 站的不同现场版会互相串台。所以适配器把这两个参数编码进 id（`xid`/`bv` 白名单，
其它参数一律不透传），`/stream` 再原样带到 `/stream_live`。id 仍是可逆 base64url，
适配器重启不失效；旧的两段式 id（只有 song/singer）继续可用。

## 歌词与海报（元数据补全）
上游 MeowMusicServer 的搜索结果只给歌名 / 歌手 / 时长（`cover_url` 多为空），缓存目录里
也只有 `music.mp3`（没有 `cover.jpg` / `lyric.lrc`），而飞牛代理的封面出口和歌词出口
分别读取音源 `/info` 的 `cover_url` 与 `lyric`。若不补全，meow 源的歌曲能播但**没有歌词、
海报、专辑和时长**。

本服务因此内置一层轻量补全（默认开启，可关闭）：

```
/search  ── 后台预热 ─┐
                      ├─→ 网易云公开接口 /api/search/get
/info   ── 同步取 ───┘      ├─ /api/song/detail  → album.picUrl（封面直链）
                            └─ /api/song/lyric   → lrc.lyric（标准 LRC 文本）
                      ↑ 可选：上游 /stream_pcm 校正真实歌手 / 时长（默认关闭）
                      ↓
        内存 + 磁盘缓存（默认 7 天）→ /info 的 cover_url / lyric / album / duration_s
```

- **失败静默降级**：网易云不可达、超时、无匹配都只返回空元数据，播放链路完全不受影响。
- **不阻塞搜索**：`/search` 只做后台预热；`/info` 用独立短超时（默认 2.8s，代理对 `/info` 有 4s 硬超时）。
- **不重复打外网**：同 key 的并发补全合并为一个任务，结果落内存 + 磁盘缓存。
- **封面由代理同源直出**：返回的是 CDN 直链，代理 `_online_cover_delivery` 会自动代理成同源图片，客户端不直连外网。
- `/healthz` 增加 `meta_enabled` / `meta_base` / `meta_cache_entries` 便于诊断。

详细说明见下方配置表与 `.env.example`。

## 配置（环境变量）

`MEOW_*` 有两个来源，**进程环境变量优先**：

1. 直接 `export MEOW_*`（本机手工 `uvicorn` 启动时）；
2. **推荐**：写进仓库根 `.env`，由适配器启动时按 `FNMUSIC_ENV_FILE` 只读加载。

设 `MEOW_NO_DOTENV=1` 可关闭该行为（离线测试用）。

| 变量 | 默认 | 说明 |
|---|---|---|
| `FNMUSIC_ENV_FILE` | 由 supervisor 注入 | 适配器只读该 `.env` 补齐 `MEOW_*` |
| `MEOW_BASE_URL` | `http://192.168.2.220:2233` | Meow 总源地址 |
| `MEOW_API_KEY` | 空 | 上游 API Key（如上游启用鉴权），**只保存在服务端** |
| `MEOW_TIMEOUT` | `300` | 上游超时秒；首次请求需转码，别设太小 |
| `MEOW_SOURCE_NAME` | `meow` | 音源短名（GUID 前缀） |
| `MEOW_SOURCES` | 空 | 接入哪些上游音源（逗号分隔，见下节）；空 = 只用上游聚合搜索 |
| `MEOW_SOURCE_CONCURRENCY` | `4` | 逐个音源并发请求上游时的并流上限 |
| `MEOW_MAX_DURATION` | `0` | 搜索阶段剔除超过该秒数的曲目；`0` = 不过滤 |
| `MEOW_TRUST_ENV` | `0` | 是否让 httpx 读取 `HTTP_PROXY` 等环境代理 |
| `FNMUSIC_MEOW_ENABLED` | `false` | **代理侧**开关 |
| `FNMUSIC_MEOW_URL` | `http://127.0.0.1:8778` | 代理访问本服务的地址 |
| `MEOW_METADATA_ENABLED` | `true` | 歌词/海报补全总开关，设 `0` 完全关闭 |
| `MEOW_METADATA_BASE` | `https://music.163.com` | 元数据接口地址 |
| `MEOW_METADATA_NETEASE` | `true` | 是否用网易云取封面 + LRC |
| `MEOW_METADATA_PCM` | `false` | 是否额外请求上游 `/stream_pcm` 校正歌手/时长（会给 Go 服务加压） |
| `MEOW_METADATA_TIMEOUT` | `2.8` | 单次补全总预算（秒） |
| `MEOW_METADATA_HOP_TIMEOUT` | `1.2` | 每一跳超时（秒） |
| `MEOW_METADATA_TTL` | `604800` | 缓存有效期（秒） |
| `MEOW_METADATA_CACHE_DIR` | 服务目录 `.meta_cache` | 元数据磁盘缓存目录 |

## 启用（源码部署）

1. 装依赖：`python3 -m pip install -r meow-service/requirements.txt`
2. 在仓库根 `.env` 里配置（音源互斥，只写一个 `true`）：
   ```dotenv
   FNMUSIC_MUSICDL_ENABLED=false
   FNMUSIC_NETEASE_ENABLED=false
   FNMUSIC_LX_ENABLED=false
   FNMUSIC_MP3API_ENABLED=false
   FNMUSIC_MEOW_ENABLED=true

   FNMUSIC_MEOW_URL=http://127.0.0.1:8778
   FNMUSIC_ONLINE_SOURCES=
   FNMUSIC_MUSICDL_DIRECT_PLAY=false

   MEOW_BASE_URL=http://192.168.2.220:2233
   ```
3. 在管理页「音乐源」选择 **Meow 聚合总源** 并保存（或 `./install.sh --sources meow --extend`）。
4. 验证：`./meow-service/smoke_test.sh http://127.0.0.1:8778 "晴天"`

Docker 形态下端口为 `127.0.0.1:8778 -> 8008`。

## 安全约定

- **不要开播放直连**：本服务刻意不实现 `/internal/resolve` 与 `/internal/stream`
  （那会把上游地址暴露给客户端）。保持 `FNMUSIC_MEOW_DIRECT_PLAY=false` /
  `FNMUSIC_MUSICDL_DIRECT_PLAY=false`。
- 搜索结果不带 `download_url` / `url`，302 只重定向回本服务自身。
- `MEOW_BASE_URL` 请指向可信地址；适配器对 `id` 做了严格 base64url 校验。

## 测试

```bash
python3 -m pytest meow-service/test_app.py -q     # 离线契约测试（MockTransport）
./meow-service/smoke_test.sh                      # 对运行中的服务做真实冒烟
```

## 合规

仅用于你拥有版权或已获授权的内容、个人 / 家庭自用；请勿公开分发或商用。
歌曲可用性、码率与缓存行为由上游 MeowMusicServer 决定。
