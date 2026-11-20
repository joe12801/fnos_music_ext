"""mp3api-service · fnmusic-ext 的第四音源适配器。

把外部「Music MP3 API」(FastAPI + yt-dlp + ffmpeg + 本地缓存) 包装成
fnmusic-ext 已有的 musicdl 音源 HTTP 契约，这样代理层无需为它重写取链 /
Range / 边听边存 / 转码 / 歌词等链路：所有非 netease / 非 lx 的 GUID 都会
自然落到 musicdl 分支。

对外端点（与 musicdl-service 对齐）:
    GET /healthz                      健康检查
    GET /sources                      平台列表
    GET /search?keyword=&limit=       搜索，返回 {"ok":true,"items":[...],"errors":{},...}
    GET /info?id=<source>:<video_id>  曲目元数据
    GET /stream?id=...&proxy=true     MP3 字节流，支持 Range / 206

内部调用上游 Music MP3 API:
    GET /api/search?q=&limit=
    GET /api/resolve?video_id=&bitrate=
    GET /api/music?video_id=&bitrate=

安全约定：
  * API Key 只保存在本进程环境变量 (MP3API_API_KEY)，绝不出现在对外 JSON、
    Location 头或日志里。
  * 不实现 /internal/resolve 与 /internal/stream —— 播放直连会把带密钥的
    上游地址暴露给客户端。启用本音源时必须保持
    FNMUSIC_MUSICDL_DIRECT_PLAY / FNMUSIC_MP3API_DIRECT_PLAY = false。
  * id 一律形如 "mp3api:<video_id>"；GUID 由代理生成 online:mp3api:<video_id>。

合规：仅用于你拥有版权或已获授权的内容、个人 / 家庭自用，请勿公开分发或商用。
"""

from __future__ import annotations

import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, StreamingResponse


def _load_dotenv() -> None:
    """从项目 .env 补齐 MP3API_*（仅当进程环境未显式设置该键时）。

    Docker / native 两种形态都由 supervisor 注入 FNMUSIC_ENV_FILE，适配器只读
    该文件即可拿到上游地址与 API Key；密钥不写进 supervisor 的 environment，
    因此不会扩散到容器内其它音源子进程，也不会出现在进程表/日志里。
    直接用 uvicorn 本机启动时回退到仓库根 / 服务目录 / cwd 下的 .env。
    设置 MP3API_NO_DOTENV=1 可完全关闭此行为（离线测试用）。
    """
    if os.environ.get("MP3API_NO_DOTENV", "").strip().lower() in ("1", "true", "yes"):
        return
    keys = (
        "MP3API_BASE_URL", "MP3API_API_KEY", "MP3API_BITRATE",
        "MP3API_MAX_DURATION", "MP3API_TIMEOUT", "MP3API_SOURCE_NAME",
        "MP3API_TRUST_ENV", "MP3API_LOG_LEVEL",
    )
    here = Path(__file__).resolve().parent
    candidates: list[Path] = []
    env_file = os.environ.get("FNMUSIC_ENV_FILE", "").strip()
    if env_file:
        candidates.append(Path(env_file))
    candidates += [here.parent / ".env", here / ".env", Path.cwd() / ".env"]
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
            if resolved in seen or not resolved.is_file():
                continue
            seen.add(resolved)
            for line in resolved.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:].lstrip()
                key, _, raw = line.partition("=")
                key = key.strip()
                if key not in keys or key in os.environ:
                    continue
                value = raw.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                os.environ[key] = value
        except OSError:
            continue


_load_dotenv()

LOG = logging.getLogger("mp3api")
logging.basicConfig(level=os.environ.get("MP3API_LOG_LEVEL", "INFO"))


# ---------------------------------------------------------------- 配置
def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


BASE_URL = _env_str("MP3API_BASE_URL", "http://127.0.0.1:8765").rstrip("/")
API_KEY = _env_str("MP3API_API_KEY", "")
BITRATE = _env_int("MP3API_BITRATE", 192, 64, 320)
# 与上游 MUSIC_MAX_DURATION_SEC 对齐：搜索阶段就剔除必然取流失败的超长视频；0 = 不过滤
MAX_DURATION = _env_int("MP3API_MAX_DURATION", 900, 0, 86400)
# 首次请求需要真实下载 + 转码，超时要足够长
TIMEOUT = float(os.environ.get("MP3API_TIMEOUT", "300") or 300)
SOURCE = _env_str("MP3API_SOURCE_NAME", "mp3api")
TRUST_ENV = _env_str("MP3API_TRUST_ENV", "0").lower() in ("1", "true", "yes")
VALID_BITRATES = (64, 96, 128, 192, 256, 320)
if BITRATE not in VALID_BITRATES:
    BITRATE = min(VALID_BITRATES, key=lambda b: abs(b - BITRATE))

# 搜索结果 id -> item，供 /info 补全标题/封面等（上游 /api/resolve 也会再取一次）
_ITEMS: dict[str, dict] = {}
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _upstream_headers(extra: dict | None = None) -> dict[str, str]:
    headers = {"Accept": "application/json, audio/mpeg;q=0.9, */*;q=0.8"}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    if extra:
        headers.update({k: v for k, v in extra.items() if v})
    return headers


def _video_id(raw: str) -> str:
    """接受 'mp3api:xxx' / 'online:mp3api:xxx'，返回裸 video_id。"""
    s = str(raw or "").strip()
    if s.startswith("online:"):
        parts = s.split(":")
        s = parts[-1] if len(parts) >= 3 else s
    elif ":" in s:
        s = s.split(":", 1)[1]
    if not _VIDEO_ID_RE.match(s):
        raise HTTPException(400, "invalid song id")
    return s


def _song_id(video_id: str) -> str:
    return f"{SOURCE}:{video_id}"


def _error(keyword: str, message: str) -> dict:
    return {"ok": False, "cached": False, "keyword": keyword, "items": [],
            "errors": {SOURCE: message}, "engine": SOURCE}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.upstream = httpx.AsyncClient(
        base_url=BASE_URL,
        timeout=httpx.Timeout(TIMEOUT, connect=10.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        follow_redirects=True,
        trust_env=TRUST_ENV,
    )
    LOG.info("mp3api-service ready: upstream=%s bitrate=%dk key=%s source=%s",
             BASE_URL, BITRATE, "set" if API_KEY else "none", SOURCE)
    try:
        yield
    finally:
        await app.state.upstream.aclose()


app = FastAPI(title="mp3api-service", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------- 契约端点
@app.get("/healthz")
async def healthz() -> dict:
    result: dict = {
        "ok": True,
        "source": SOURCE,
        "provider": "mp3api",
        "upstream": BASE_URL,
        "bitrate": BITRATE,
        "api_key_configured": bool(API_KEY),
        "cache_entries": len(_ITEMS),
        "checked_at": int(time.time()),
    }
    try:
        resp = await app.state.upstream.get(
            "/api/health", headers=_upstream_headers(), timeout=3.0)
        result["upstream_status"] = resp.status_code
        if resp.status_code >= 500:
            result["ok"] = False
            result["error"] = "upstream returned 5xx"
    except Exception as exc:  # noqa: BLE001 - 健康检查只报告，不抛
        result["ok"] = False
        result["error"] = type(exc).__name__
    return result


@app.get("/sources")
async def sources() -> dict:
    """WebUI 平台页 / extend.sh 会读这里；本音源只有一个逻辑平台。"""
    return {"enabled": [SOURCE], "registered": [SOURCE]}


@app.get("/search")
async def search(
    keyword: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=30),
    sources: str = Query("", description="musicdl 平台白名单，本音源忽略"),
) -> dict:
    try:
        resp = await app.state.upstream.get(
            "/api/search",
            params={"q": keyword, "limit": limit},
            headers=_upstream_headers(),
        )
    except Exception as exc:  # noqa: BLE001
        LOG.warning("search upstream error: %s", type(exc).__name__)
        return _error(keyword, f"upstream error: {type(exc).__name__}")
    if resp.status_code >= 400:
        return _error(keyword, f"upstream returned {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError:
        return _error(keyword, "upstream returned invalid JSON")

    items: list[dict] = []
    for raw in payload.get("results") or []:
        vid = str(raw.get("id") or "").strip()
        if not vid or not _VIDEO_ID_RE.match(vid):
            continue
        duration = int(raw.get("duration") or 0)
        if MAX_DURATION and duration > MAX_DURATION:
            LOG.info("skip over-long video %s (%ss > %ss)", vid, duration, MAX_DURATION)
            continue
        cover = str(raw.get("thumbnail") or "")
        channel = str(raw.get("channel") or "").strip()
        item = {
            # 必须带冒号，代理据此生成 online:mp3api:<video_id>
            "id": _song_id(vid),
            "source": SOURCE,
            "title": str(raw.get("title") or vid),
            "artist": channel,
            "album": "",
            "duration_s": duration,
            "ext": "mp3",
            "file_size": 0,
            "protocol": "http",
            "delivery": "direct",
            "bitrate": BITRATE * 1000,
            "samplerate": 0,
            "codec": "mp3",
            "channels": 2,
            "quality": "unknown",
            "cover_url": cover,
            # 刻意不返回 download_url / url：由本服务内部取流，避免泄漏带密钥的地址
        }
        _ITEMS[_song_id(vid)] = item
        items.append(item)
    if len(_ITEMS) > 2000:  # 简单上限，避免常驻进程无限增长
        for stale in list(_ITEMS)[: len(_ITEMS) - 2000]:
            _ITEMS.pop(stale, None)
    return {"ok": True, "cached": False, "keyword": keyword,
            "items": items, "errors": {}, "engine": SOURCE}


@app.get("/info")
async def info(id: str = Query(...)) -> dict:
    vid = _video_id(id)
    song_id = _song_id(vid)
    cached = _ITEMS.get(song_id, {})
    video: dict = {}
    try:
        resp = await app.state.upstream.get(
            "/api/resolve",
            params={"video_id": vid, "bitrate": BITRATE},
            headers=_upstream_headers(),
        )
        if resp.status_code < 400:
            video = (resp.json() or {}).get("video") or {}
    except Exception as exc:  # noqa: BLE001 - 元数据失败时退回搜索缓存
        LOG.warning("info upstream error for %s: %s", vid, type(exc).__name__)
    return {
        "ok": True,
        "id": song_id,
        "source": SOURCE,
        "title": str(video.get("title") or cached.get("title") or vid),
        "artist": str(video.get("channel") or cached.get("artist") or ""),
        "album": str(cached.get("album") or ""),
        "duration_s": int(video.get("duration") or cached.get("duration_s") or 0),
        "ext": "mp3",
        "protocol": "http",
        "delivery": "direct",
        "quality": "unknown",
        "bitrate": BITRATE * 1000,
        "samplerate": 0,
        "file_size": 0,
        "cover_url": str(video.get("thumbnail") or cached.get("cover_url") or ""),
        "lyric": "",
    }


@app.get("/stream")
async def stream(
    request: Request,
    id: str = Query(...),
    proxy: bool = Query(False, description="false=302 到本服务 proxy=true（不暴露上游地址）"),
    quality: str = Query("", description="mp3=有损档，映射到 <=128 kbps"),
):
    vid = _video_id(id)
    if not proxy:
        # 不给客户端上游真实地址；重定向回本服务的代理流
        return RedirectResponse("/stream?id=" + _song_id(vid) + "&proxy=true", status_code=302)

    bitrate = min(BITRATE, 128) if quality == "mp3" else BITRATE
    headers = _upstream_headers()
    rng = request.headers.get("range")
    if rng:
        headers["Range"] = rng
    # 明确要求 identity：代理会拒绝 content-encoding != identity 的响应
    headers["Accept-Encoding"] = "identity"

    client: httpx.AsyncClient = app.state.upstream
    req = client.build_request(
        "GET", "/api/music",
        params={"video_id": vid, "bitrate": bitrate},
        headers=headers,
    )
    try:
        resp = await client.send(req, stream=True)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("stream upstream error for %s: %s", vid, type(exc).__name__)
        raise HTTPException(502, f"upstream request failed: {type(exc).__name__}")

    if resp.status_code >= 400:
        body = (await resp.aread())[:300].decode("utf-8", "replace")
        await resp.aclose()
        raise HTTPException(502, f"upstream returned {resp.status_code}: {body}")

    out_headers: dict[str, str] = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
        "Content-Type": resp.headers.get("content-type") or "audio/mpeg",
    }
    # 只透传这三个头；绝不带 Content-Encoding（代理要求 identity）
    for src, dst in (("content-length", "Content-Length"),
                     ("content-range", "Content-Range")):
        value = resp.headers.get(src)
        if value:
            out_headers[dst] = value
    status = 206 if "Content-Range" in out_headers or resp.status_code == 206 else 200

    async def body_iter() -> AsyncIterator[bytes]:
        try:
            async for chunk in resp.aiter_bytes(64 * 1024):
                if chunk:
                    yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(body_iter(), status_code=status, headers=out_headers)


# 直连 (/internal/resolve, /internal/stream) 刻意不实现：那会把带 API Key 的
# 上游地址交给客户端。请在 .env 中保持 FNMUSIC_*_DIRECT_PLAY=false。
