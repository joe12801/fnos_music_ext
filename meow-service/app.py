"""meow-service · fnmusic-ext 的第五音源适配器（Meow 聚合总源）。

把自建的 MeowMusicServer（Go，10+ 音源聚合：酷我/网易/xfabe/QQ/酷狗/
NCM/GDStudio/mp3api/yuafeng…）包装成 fnmusic-ext 已有的 musicdl 音源 HTTP
契约。这样代理层无需为它重写取链 / Range / 边听边存 / 转码 / 歌词等链路：
所有非 netease / 非 lx 的 GUID 都会自然落到 musicdl 分支。

与 mp3api-service 完全同构，区别只在「上游」：
    mp3api-service -> mp3.994938.xyz  (/api/search /api/resolve /api/music)
    meow-service    -> MeowMusicServer(/api/search /stream_live)

对外端点（与 musicdl-service 对齐）:
    GET /healthz                      健康检查
    GET /sources                      平台列表
    GET /search?keyword=&limit=       搜索，返回 {"ok":true,"items":[...],"errors":{},...}
    GET /info?id=meow:<b64>           曲目元数据
    GET /stream?id=meow:<b64>&proxy=true   MP3 字节流，支持 Range / 206

上游 MeowMusicServer:
    GET /api/search?query=<词>        -> [{title, artist, cover_url, lyric_url, duration}]
    GET /stream_pcm?song=&singer=     -> MusicItem（真实 artist / duration，用于元数据补全）
    GET /stream_live?song=&singer=    -> 定长 MP3 字节流（Content-Length，无 chunked）

元数据补全（歌词 / 海报 / 专辑 / 歌手 / 时长）:
    上游 /api/search 恒空、缓存目录只有 music.mp3（无 cover.jpg / lyric.lrc），
    因此适配器内做一次轻量补全：网易云公开接口取专辑封面与 LRC，上游
    /stream_pcm 取真实歌手与时长。结果按 (song, singer) 缓存到内存 + 磁盘，
    /search 时预拉、/info 直接命中，避免代理 4s 硬超时；任何失败静默降级为空，
    绝不影响播放。可用 MEOW_METADATA_ENABLED=0 完全关闭。

id 设计：Meow 搜索结果没有稳定 id，因此用可逆 base64url(song \\x1f singer)
编码，保证 /info、/stream 无状态反查出 song/singer，适配器重启也不失效。
GUID 由代理生成 online:meow:<b64>，既不是 netease 也不是 lx，自动落 musicdl 分支。

安全约定：
  * 不实现 /internal/resolve 与 /internal/stream —— 播放直连会把上游地址暴露给
    客户端。启用本音源时必须保持 FNMUSIC_MEOW_DIRECT_PLAY / FNMUSIC_MUSICDL_DIRECT_PLAY=false。
  * id 一律形如 "meow:<b64url>"；GUID 由代理生成 online:meow:<b64url>。

合规：仅用于你拥有版权或已获授权的内容、个人 / 家庭自用，请勿公开分发或商用。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
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
    """从项目 .env 补齐 MEOW_*（仅当进程环境未显式设置该键时）。

    Docker / native 两种形态都由 supervisor 注入 FNMUSIC_ENV_FILE，适配器只读
    该文件即可拿到上游地址；直接 uvicorn 本机启动时回退到仓库根 / 服务目录
    / cwd 下的 .env。设置 MEOW_NO_DOTENV=1 可完全关闭此行为（离线测试用）。
    """
    if os.environ.get("MEOW_NO_DOTENV", "").strip().lower() in ("1", "true", "yes"):
        return
    keys = (
        "MEOW_BASE_URL", "MEOW_API_KEY", "MEOW_TIMEOUT", "MEOW_SOURCE_NAME",
        "MEOW_MAX_DURATION", "MEOW_TRUST_ENV", "MEOW_LOG_LEVEL",
        "MEOW_METADATA_ENABLED", "MEOW_METADATA_BASE", "MEOW_METADATA_TIMEOUT",
        "MEOW_METADATA_TTL", "MEOW_METADATA_CACHE_DIR", "MEOW_METADATA_NETEASE",
        "MEOW_METADATA_PCM",
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

LOG = logging.getLogger("meow")
logging.basicConfig(level=os.environ.get("MEOW_LOG_LEVEL", "INFO"))


# ---------------------------------------------------------------- 配置
def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


BASE_URL = _env_str("MEOW_BASE_URL", "http://192.168.2.220:2233").rstrip("/")
API_KEY = _env_str("MEOW_API_KEY", "")
TIMEOUT = float(os.environ.get("MEOW_TIMEOUT", "300") or 300)
SOURCE = _env_str("MEOW_SOURCE_NAME", "meow")
MAX_DURATION = _env_int("MEOW_MAX_DURATION", 0, 0, 86400)  # 0 = 不过滤
TRUST_ENV = _env_str("MEOW_TRUST_ENV", "0").lower() in ("1", "true", "yes")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


# 元数据补全：上游 /api/search 恒空且缓存目录只有 music.mp3，飞牛的歌词/海报
# 全靠本服务 /info 的 lyric / cover_url。这里用网易云公开接口补封面与 LRC，
# 用上游 /stream_pcm 补真实歌手与时长。全程短超时 + 缓存 + 失败静默降级。
META_ENABLED = _env_bool("MEOW_METADATA_ENABLED", True)
META_BASE = _env_str("MEOW_METADATA_BASE", "https://music.163.com").rstrip("/")
META_NETEASE = _env_bool("MEOW_METADATA_NETEASE", True)
META_PCM = _env_bool("MEOW_METADATA_PCM", True)
META_TIMEOUT = _env_float("MEOW_METADATA_TIMEOUT", 2.8, 0.5, 8.0)   # 单次补全总预算
META_HOP_TIMEOUT = _env_float("MEOW_METADATA_HOP_TIMEOUT", 1.2, 0.3, 4.0)
META_TTL = _env_int("MEOW_METADATA_TTL", 7 * 86400, 60, 90 * 86400)
META_FAIL_TTL = 120          # 失败/空结果的短 TTL，避免长时间不重试
META_CACHE_DIR = Path(_env_str(
    "MEOW_METADATA_CACHE_DIR", str(Path(__file__).resolve().parent / ".meta_cache")))
META_SEMAPHORE: "asyncio.Semaphore | None" = None

# 搜索结果 id -> item，仅用于 /info 补全（元数据失败时退回缓存）
_ITEMS: dict[str, dict] = {}
# 元数据缓存 (song,singer) -> (ts, data, ok)
_META_MEM: dict[str, tuple[float, dict, bool]] = {}
_META_MEM_MAX = 2000
# 同一 key 的并发补全合并成一个任务，避免 /search 预热与 /info 重复打外网
_META_INFLIGHT: dict[str, "asyncio.Task"] = {}
# base64url 字符集，长度放宽到覆盖较长中文歌名编码
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,600}$")
_SEP = "\x1f"  # unit separator，避免与歌名/歌手里的常见字符冲突


def _upstream_headers(extra: dict | None = None) -> dict[str, str]:
    headers = {"Accept": "application/json, audio/mpeg;q=0.9, */*;q=0.8"}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    if extra:
        headers.update({k: v for k, v in extra.items() if v})
    return headers


def _encode_id(song: str, singer: str) -> str:
    raw = f"{song}{_SEP}{singer}".encode("utf-8")
    return SOURCE + ":" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_id(raw: str) -> tuple[str, str]:
    """接受 'meow:<b64>' / 'online:meow:<b64>'，返回 (song, singer)。"""
    s = str(raw or "").strip()
    if s.startswith("online:"):
        s = s[len("online:"):]
    if ":" in s:
        s = s.split(":", 1)[1]
    if not _ID_RE.match(s):
        raise HTTPException(400, "invalid song id")
    padded = s + "=" * (-len(s) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        raise HTTPException(400, "invalid song id encoding")
    song, sep, singer = decoded.partition(_SEP)
    if not song or not sep:
        raise HTTPException(400, "invalid song id payload")
    return song, singer


def _error(keyword: str, message: str) -> dict:
    return {"ok": False, "cached": False, "keyword": keyword, "items": [],
            "errors": {SOURCE: message}, "engine": SOURCE}


def _fallback_raw_items(keyword: str, limit: int) -> list[dict]:
    """上游 /api/search 为空时的兜底候选。

    MeowMusicServer 的真实能力是「歌名(+歌手) → 最佳单曲」的点歌核心
    (/stream_live?song=&singer=，10+ 音源聚合、定长 audio/mpeg)，而它的
    /api/search 是个弱接口：只读工作目录下的 sources.json + 调用 yuafeng
    旧 API，实测（192.168.2.220）恒返回空数组。因此当上游给不出曲目列表时，
    把用户输入的关键词解析成 1~2 个可点播候选，点击后由 /stream 真实出音频：
      * "晴天"          -> (song="晴天", singer="")
      * "晴天 周杰伦"   -> (song="晴天", singer="周杰伦") + (song="晴天 周杰伦", singer="")
    这样既保留「总源即点即播」的语义，又完全不需要改动上游 Go 服务。
    """
    kw = (keyword or "").strip()
    if not kw:
        return []
    parts = [p for p in re.split(r"\s+", kw) if p]
    combos: list[tuple[str, str]] = []
    if len(parts) >= 2:
        combos.append((parts[0], " ".join(parts[1:])))  # 歌名在前：晴天 周杰伦
    combos.append((kw, ""))                            # 整串当歌名，最稳（已实测可播）
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for song, singer in combos:
        if (song, singer) in seen:
            continue
        seen.add((song, singer))
        out.append({"title": song, "artist": singer, "duration": 0, "cover_url": ""})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 元数据补全
# 背景：MeowMusicServer 的 /api/search 恒空，缓存目录只有 music.mp3（没有
# cover.jpg / lyric.lrc），上游 /stream_pcm 的 lyric_url / cover_url 也为空。
# 而 fnmusic-ext 代理的歌词与封面完全取自音源 /info 的 lyric / cover_url：
#   * proxy/_online_cover_delivery -> _online_info() -> /info["cover_url"]
#   * proxy/resolve_online_lyric   -> _online_info() -> /info["lyric"]
# 因此在这里做一次轻量补全：网易云公开接口取专辑封面 + 标准 LRC，
# 可选再用上游 /stream_pcm 校正真实歌手/时长。结果按 (song,singer) 缓存
# 到内存 + 磁盘；/search 后台预热，/info 同步命中。全流程短超时，失败只
# 返回空 dict，绝不影响播放。MEOW_METADATA_ENABLED=0 可完全关闭。

_NETEASE_HEADERS = {
    "Referer": "https://music.163.com/",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/122.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
}
# 标题里出现这些词说明是翻唱/改编/伴奏，降权（不硬排除，否则有些歌根本
# 搜不到可用 LRC）。真人原唱版通常标题更干净，打分自然更高。
_META_NOISE = (
    "原唱", "翻唱", "cover", "版", "live", "现场", "伴奏", "纯音乐", "dj",
    "remix", "女声", "钢琴", "弦乐", "史诗", "串烧", "试听", "demo",
    "instrumental", "karaoke", "remaster", "重置版",
)


def _meta_key(song: str, singer: str) -> str:
    return hashlib.sha256(f"{song}\x1f{singer}".encode("utf-8")).hexdigest()


def _meta_cache_get(key: str) -> "dict | None":
    hit = _META_MEM.get(key)
    if hit is not None:
        ts, data, ok = hit
        if time.time() - ts < (META_TTL if ok else META_FAIL_TTL):
            return data
        _META_MEM.pop(key, None)
    try:
        path = META_CACHE_DIR / (key + ".json")
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            ts = float(payload.get("ts") or 0)
            ok = bool(payload.get("ok"))
            if time.time() - ts < (META_TTL if ok else META_FAIL_TTL):
                data = payload.get("data") or {}
                _META_MEM[key] = (ts, data, ok)
                return data
    except (OSError, ValueError, TypeError):
        pass
    return None


def _meta_cache_put(key: str, data: dict) -> None:
    ok = bool(data.get("cover_url") or data.get("lyric"))
    ts = time.time()
    _META_MEM[key] = (ts, data, ok)
    if len(_META_MEM) > _META_MEM_MAX:
        for stale in sorted(_META_MEM, key=lambda k: _META_MEM[k][0])[: len(_META_MEM) - _META_MEM_MAX]:
            _META_MEM.pop(stale, None)
    try:
        META_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = META_CACHE_DIR / (key + ".json.tmp")
        tmp.write_text(json.dumps({"ts": ts, "ok": ok, "data": data},
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(META_CACHE_DIR / (key + ".json"))
    except OSError as exc:
        LOG.debug("meta cache write skipped: %s", type(exc).__name__)


def _meta_clean_title(value: str) -> str:
    """去掉括号备注（『晴天 (原唱 周杰伦)』->『晴天』）与空白，便于精确匹配。"""
    text = re.sub(r"[（(【\[].*?[）)】\]]", "", str(value or ""))
    return re.sub(r"\s+", "", text).strip().casefold()


def _meta_pick(rows: object, song: str, singer: str, limit: int = 3) -> list[dict]:
    """给网易云搜索结果打分，返回最像原曲的前 limit 个候选。"""
    if not isinstance(rows, list):
        return []
    target = _meta_clean_title(song)
    if not target:
        return []
    scored: list[tuple[int, dict]] = []
    for it in rows:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "")
        clean = _meta_clean_title(name)
        if not clean:
            continue
        if clean == target:
            score = 12
        elif target in clean or clean in target:
            score = 4
        else:
            continue
        artists = " ".join(str(a.get("name") or "")
                           for a in (it.get("artists") or []) if isinstance(a, dict))
        if singer and singer.casefold() in artists.casefold():
            score += 6
        low = name.casefold()
        score -= sum(3 for w in _META_NOISE if w in low)
        album = it.get("album") if isinstance(it.get("album"), dict) else {}
        if album.get("picId"):
            score += 1
        sid = str(it.get("id") or "")
        if not sid:
            continue
        scored.append((score, it))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [it for score, it in scored[:limit] if score > 0]


async def _netease_json(client: httpx.AsyncClient, path: str, params: dict) -> object:
    try:
        r = await client.get(path, params=params, timeout=META_HOP_TIMEOUT)
        if r.status_code == 200:
            return r.json()
    except Exception as exc:  # noqa: BLE001 - 元数据是尽力而为
        LOG.debug("netease %s failed: %s", path, type(exc).__name__)
    return None


async def _pcm_meta(upstream: httpx.AsyncClient, song: str, singer: str) -> dict:
    """可选：向上游 /stream_pcm 取真实歌手与时长（默认关闭，避免给 Go 服务加压）。"""
    try:
        r = await upstream.get(
            "/stream_pcm", params={"song": song, "singer": singer},
            headers=_upstream_headers(), timeout=META_HOP_TIMEOUT,
        )
        if r.status_code != 200:
            return {}
        d = r.json()
        if not isinstance(d, dict):
            return {}
        out: dict = {}
        artist = str(d.get("artist") or "").strip()
        if artist:
            out["artist"] = artist
        try:
            duration = int(d.get("duration") or 0)
            if duration > 0:
                out["duration_s"] = duration
        except (TypeError, ValueError):
            pass
        return out
    except Exception as exc:  # noqa: BLE001
        LOG.debug("stream_pcm meta failed for %s: %s", song, type(exc).__name__)
        return {}


def _meta_cover_url(detail: dict) -> str:
    """从 /api/song/detail 的 album 里挑封面直链，并限制尺寸。"""
    songs = detail.get("songs") if isinstance(detail, dict) else None
    first = songs[0] if isinstance(songs, list) and songs and isinstance(songs[0], dict) else {}
    album = first.get("album") if isinstance(first.get("album"), dict) else {}
    url = str(album.get("picUrl") or album.get("blurPicUrl") or "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return ""
    joiner = "&" if "?" in url else "?"
    return url + joiner + "param=500y500"


def _meta_album(detail: dict) -> str:
    songs = detail.get("songs") if isinstance(detail, dict) else None
    first = songs[0] if isinstance(songs, list) and songs and isinstance(songs[0], dict) else {}
    album = first.get("album") if isinstance(first.get("album"), dict) else {}
    return str(album.get("name") or "").strip()


def _meta_duration_s(detail: dict) -> int:
    songs = detail.get("songs") if isinstance(detail, dict) else None
    first = songs[0] if isinstance(songs, list) and songs and isinstance(songs[0], dict) else {}
    try:
        ms = int(first.get("duration") or 0)
        return ms // 1000 if ms > 0 else 0
    except (TypeError, ValueError):
        return 0


def _meta_lyric(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    lrc = payload.get("lrc")
    text = str(lrc.get("lyric") or "").strip() if isinstance(lrc, dict) else ""
    if not text:
        return ""
    # 纯音乐 / 暂无歌词 之类的占位不是真歌词
    if len(text) < 20 or "纯音乐" in text[:40] and "[" not in text[:40]:
        return ""
    return text


async def _meta_fetch(song: str, singer: str) -> dict:
    """实际打网易云：搜索 -> 并发取详情封面 + 多个候选的 LRC（取最长的）。"""
    if not META_NETEASE:
        return {}
    client: httpx.AsyncClient = app.state.meta
    keyword = f"{song} {singer}".strip() if singer else song
    payload = await _netease_json(
        client, "/api/search/get",
        {"s": keyword, "type": 1, "offset": 0, "limit": 15},
    )
    rows = (payload or {}).get("result") if isinstance(payload, dict) else None
    candidates = _meta_pick(rows.get("songs") if isinstance(rows, dict) else None,
                            song, singer)
    if not candidates:
        return {}

    jobs = [_netease_json(client, "/api/song/detail",
                          {"ids": "[" + str(candidates[0].get("id")) + "]"})]
    jobs += [_netease_json(client, "/api/song/lyric",
                           {"id": str(it.get("id")), "lv": 1, "kv": 1, "tv": -1})
             for it in candidates]
    results = await asyncio.gather(*jobs, return_exceptions=True)
    detail = results[0] if results and isinstance(results[0], dict) else {}
    lyric = ""
    for res in results[1:]:
        text = _meta_lyric(res if not isinstance(res, BaseException) else None)
        if len(text) > len(lyric):
            lyric = text
    out: dict = {"cover_url": _meta_cover_url(detail),
                 "album": _meta_album(detail),
                 "duration_s": _meta_duration_s(detail),
                 "lyric": lyric}
    return {k: v for k, v in out.items() if v}


async def _meta_compute(key: str, song: str, singer: str) -> dict:
    sem = META_SEMAPHORE
    try:
        if sem is None:
            data = await asyncio.wait_for(_meta_fetch(song, singer), timeout=META_TIMEOUT)
        else:
            async with sem:
                data = await asyncio.wait_for(_meta_fetch(song, singer), timeout=META_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - 超时/网络异常一律静默降级
        LOG.debug("meta fetch failed for %s - %s: %s", song, singer, type(exc).__name__)
        data = {}
    if META_PCM and not singer:
        try:
            pcm = await asyncio.wait_for(
                _pcm_meta(app.state.upstream, song, singer), timeout=META_HOP_TIMEOUT)
            if pcm:
                data = {**pcm, **{k: v for k, v in data.items() if v}}
        except Exception:  # noqa: BLE001
            pass
    _meta_cache_put(key, data)
    return data


async def get_meta(song: str, singer: str) -> dict:
    """带缓存 + 总超时 + 并发上限 + in-flight 合并；任何异常都降级为空 dict。"""
    if not META_ENABLED or not song:
        return {}
    key = _meta_key(song, singer)
    hit = _meta_cache_get(key)
    if hit is not None:
        return hit
    task = _META_INFLIGHT.get(key)
    if task is None or task.done():
        task = asyncio.create_task(_meta_compute(key, song, singer))
        _META_INFLIGHT[key] = task

        def _drop(done: "asyncio.Task", k: str = key) -> None:
            if _META_INFLIGHT.get(k) is done:
                _META_INFLIGHT.pop(k, None)

        task.add_done_callback(_drop)
    try:
        # shield：/info 的外层 wait_for 超时不会取消正在进行的补全，
        # 结果仍会写入缓存，下一次请求直接命中。
        return await asyncio.shield(task)
    except Exception as exc:  # noqa: BLE001
        LOG.debug("meta wait failed for %s: %s", song, type(exc).__name__)
        return {}


def _schedule_meta(song: str, singer: str) -> None:
    """/search 后台预热：让稍后点播时的 /info 直接命中缓存，不拖慢搜索响应。"""
    if not META_ENABLED or not song:
        return
    key = _meta_key(song, singer)
    if _meta_cache_get(key) is not None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(get_meta(song, singer))
    tasks = getattr(app.state, "meta_tasks", None)
    if tasks is None:
        tasks = set()
        app.state.meta_tasks = tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global META_SEMAPHORE
    META_SEMAPHORE = asyncio.Semaphore(4)
    app.state.meta_tasks = set()
    app.state.upstream = httpx.AsyncClient(
        base_url=BASE_URL,
        timeout=httpx.Timeout(TIMEOUT, connect=10.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        follow_redirects=True,
        trust_env=TRUST_ENV,
    )
    # 网易云公开元数据接口，独立短超时客户端，不走系统代理环境变量
    app.state.meta = httpx.AsyncClient(
        base_url=META_BASE,
        timeout=httpx.Timeout(META_HOP_TIMEOUT, connect=3.0),
        headers=_NETEASE_HEADERS,
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        follow_redirects=True,
        trust_env=TRUST_ENV,
    )
    LOG.info("meow-service ready: upstream=%s key=%s source=%s timeout=%.0f meta=%s/%s",
             BASE_URL, "set" if API_KEY else "none", SOURCE, TIMEOUT,
             "on" if META_ENABLED else "off", META_BASE)
    try:
        yield
    finally:
        for task in list(getattr(app.state, "meta_tasks", ()) or ()):
            task.cancel()
        for task in list(_META_INFLIGHT.values()):
            task.cancel()
        _META_INFLIGHT.clear()
        await app.state.meta.aclose()
        await app.state.upstream.aclose()


app = FastAPI(title="meow-service", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------- 契约端点
@app.get("/healthz")
async def healthz() -> dict:
    result: dict = {
        "ok": True,
        "source": SOURCE,
        "provider": "meow",
        "upstream": BASE_URL,
        "api_key_configured": bool(API_KEY),
        "cache_entries": len(_ITEMS),
        "meta_enabled": META_ENABLED,
        "meta_base": META_BASE,
        "meta_cache_entries": len(_META_MEM),
        "checked_at": int(time.time()),
    }
    try:
        # Meow 根路径有 indexHandler，用它做轻量探活
        resp = await app.state.upstream.get("/", headers=_upstream_headers(), timeout=3.0)
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
            params={"query": keyword},
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

    # Meow /api/search 返回数组：[{title, artist, url, cover_url, lyric_url, duration}]
    raw_items = payload if isinstance(payload, list) else (payload.get("results") or payload.get("items") or [])
    if not raw_items:
        # 上游 /api/search 是弱接口：无 sources.json 时恒返回空数组（真机实测）。
        # 兜底成可点播候选，点击后 /stream 走 /stream_live 真实出音频。
        raw_items = _fallback_raw_items(keyword, limit)
    items: list[dict] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        artist = str(raw.get("artist") or "").strip()
        try:
            duration = int(raw.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        if MAX_DURATION and duration > MAX_DURATION:
            LOG.info("skip over-long track %s (%ss > %ss)", title, duration, MAX_DURATION)
            continue
        song_id = _encode_id(title, artist)
        cover = str(raw.get("cover_url") or "")
        item = {
            "id": song_id,
            "source": SOURCE,
            "title": title,
            "artist": artist,
            "album": str(raw.get("album") or ""),
            "duration_s": duration,
            "ext": "mp3",
            "file_size": 0,
            "protocol": "http",
            "delivery": "direct",
            "bitrate": 320000,
            "samplerate": 0,
            "codec": "mp3",
            "channels": 2,
            "quality": "unknown",
            "cover_url": cover,
            # 刻意不返回 download_url / url：由本服务内部取流
        }
        # 元数据补全：命中缓存就立刻把封面/专辑/时长带进搜索结果；未命中则
        # 后台预热，稍后 /info 直接命中，不阻塞本次搜索响应。
        cached_meta = _meta_cache_get(_meta_key(title, artist))
        if cached_meta:
            if not item["cover_url"] and cached_meta.get("cover_url"):
                item["cover_url"] = cached_meta["cover_url"]
            if not item["album"] and cached_meta.get("album"):
                item["album"] = cached_meta["album"]
            if not item["duration_s"] and cached_meta.get("duration_s"):
                item["duration_s"] = cached_meta["duration_s"]
        _schedule_meta(title, artist)
        _ITEMS[song_id] = item
        items.append(item)
        if len(items) >= limit:
            break
    if len(_ITEMS) > 2000:
        for stale in list(_ITEMS)[: len(_ITEMS) - 2000]:
            _ITEMS.pop(stale, None)
    LOG.info("search '%s' -> %d items", keyword, len(items))
    return {"ok": True, "cached": False, "keyword": keyword,
            "items": items, "errors": {}, "engine": SOURCE}


@app.get("/info")
async def info(id: str = Query(...)) -> dict:
    song, singer = _decode_id(id)
    song_id = _encode_id(song, singer)
    cached = _ITEMS.get(song_id, {})
    # 代理 /_online_info 对音源 /info 有 4s 硬超时；这里给补全留一个更紧的
    # 预算，超时/失败一律返回空，保证曲目详情与播放永不因元数据卡住。
    meta: dict = {}
    if META_ENABLED:
        try:
            meta = await asyncio.wait_for(get_meta(song, singer),
                                         timeout=META_TIMEOUT + 0.4)
        except Exception as exc:  # noqa: BLE001
            LOG.debug("info meta skipped for %s: %s", song, type(exc).__name__)
            meta = {}
    return {
        "ok": True,
        "id": song_id,
        "source": SOURCE,
        "title": str(cached.get("title") or song),
        "artist": str(cached.get("artist") or singer or meta.get("artist") or ""),
        "album": str(cached.get("album") or meta.get("album") or ""),
        "duration_s": int(cached.get("duration_s") or meta.get("duration_s") or 0),
        "ext": "mp3",
        "protocol": "http",
        "delivery": "direct",
        "quality": "unknown",
        "bitrate": 320000,
        "samplerate": 0,
        "file_size": 0,
        # proxy 封面出口 / 歌词出口都读这两个字段
        "cover_url": str(cached.get("cover_url") or meta.get("cover_url") or ""),
        "lyric": str(meta.get("lyric") or ""),
    }


@app.get("/stream")
async def stream(
    request: Request,
    id: str = Query(...),
    proxy: bool = Query(False, description="false=302 到本服务 proxy=true（不暴露上游地址）"),
    quality: str = Query("", description="保留参数以兼容 musicdl 契约，本音源固定 mp3"),
):
    song, singer = _decode_id(id)
    song_id = _encode_id(song, singer)
    if not proxy:
        return RedirectResponse("/stream?id=" + song_id + "&proxy=true", status_code=302)

    # Meow /stream_live 定长返回；先把完整流读到内存，再按 Range 切片返回，
    # 以兼容 fnos proxy 的顺序播放（200）与 Range/206（边听边存 / seek）两种场景。
    headers = _upstream_headers()
    headers["Accept-Encoding"] = "identity"
    client: httpx.AsyncClient = app.state.upstream
    req = client.build_request(
        "GET", "/stream_live",
        params={"song": song, "singer": singer},
        headers=headers,
    )
    try:
        resp = await client.send(req, stream=True)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("stream upstream error for %s - %s: %s", song, singer, type(exc).__name__)
        raise HTTPException(502, f"upstream request failed: {type(exc).__name__}")

    if resp.status_code >= 400:
        body = (await resp.aread())[:300].decode("utf-8", "replace")
        await resp.aclose()
        raise HTTPException(502, f"upstream returned {resp.status_code}: {body}")

    content_type = resp.headers.get("content-type") or "audio/mpeg"
    chunks: list[bytes] = []
    try:
        async for chunk in resp.aiter_bytes(64 * 1024):
            if chunk:
                chunks.append(chunk)
    finally:
        await resp.aclose()
    payload = b"".join(chunks)
    total = len(payload)

    out_headers: dict[str, str] = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
        "Content-Type": content_type,
    }

    # 解析 Range: bytes=START-END
    range_header = request.headers.get("range", "")
    m = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
    if m and total > 0:
        start_s, end_s = m.group(1), m.group(2)
        if start_s:
            start = int(start_s)
            end = int(end_s) if end_s else total - 1
        else:
            # suffix range: bytes=-N
            n = int(end_s) if end_s else 0
            start = max(0, total - n) if n else 0
            end = total - 1
        if start >= total:
            raise HTTPException(416, "range not satisfiable")
        end = min(end, total - 1)
        body = payload[start:end + 1]
        out_headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        out_headers["Content-Length"] = str(len(body))
        return StreamingResponse(iter([body]), status_code=206, headers=out_headers)

    out_headers["Content-Length"] = str(total)
    return StreamingResponse(iter([payload]) if payload else iter([]), status_code=200, headers=out_headers)


# 直连 (/internal/resolve, /internal/stream) 刻意不实现：那会把上游地址交给客户端。
# 请在 .env 中保持 FNMUSIC_MEOW_DIRECT_PLAY / FNMUSIC_MUSICDL_DIRECT_PLAY=false。
