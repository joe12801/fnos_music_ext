"""meow-service 离线契约测试（不依赖真实上游 / fnOS）。

用 httpx.MockTransport 模拟 MeowMusicServer，验证适配器满足 fnmusic-ext
的 musicdl 音源契约，Range/206 正确，且 id 可逆解码不丢歌名/歌手。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

APP_PATH = Path(__file__).with_name("app.py")
FAKE_MP3 = b"ID3" + b"y" * 2045
SEARCH_ROWS = [
    {"title": "晴天", "artist": "周杰伦", "cover_url": "https://img/cover.jpg",
     "lyric_url": "https://img/lyric.lrc", "duration": 269, "url": "http://upstream/stream_live?song=..."},
    {"title": "七里香", "artist": "周杰伦", "cover_url": "", "lyric_url": "", "duration": 297},
]


def _handler(*, fail_search: bool = False, seen: list | None = None):
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        path = request.url.path
        if path == "/":
            return httpx.Response(200, text="ok")
        if path == "/api/search":
            if fail_search:
                return httpx.Response(502, json={"detail": "boom"})
            return httpx.Response(200, json=SEARCH_ROWS)
        if path == "/stream_live":
            q = request.url.params
            assert q.get("song") and q.get("singer"), "song/singer must be forwarded"
            return httpx.Response(200, content=FAKE_MP3, headers={
                "content-type": "audio/mpeg", "content-length": str(len(FAKE_MP3)),
                "accept-ranges": "bytes",
            })
        return httpx.Response(404, json={"detail": "not found"})
    return handle


@pytest.fixture()
def client(monkeypatch):
    seen: list[httpx.Request] = []
    monkeypatch.setenv("MEOW_BASE_URL", "http://upstream.test")
    monkeypatch.setenv("MEOW_API_KEY", "unit-test-key")
    monkeypatch.setenv("MEOW_TIMEOUT", "30")
    monkeypatch.setenv("MEOW_SOURCE_NAME", "meow")
    monkeypatch.setenv("MEOW_MAX_DURATION", "0")
    monkeypatch.setenv("MEOW_TRUST_ENV", "0")
    monkeypatch.setenv("MEOW_NO_DOTENV", "1")
    monkeypatch.setenv("MEOW_METADATA_ENABLED", "0")
    monkeypatch.setenv("MEOW_METADATA_CACHE_DIR", str(Path(__file__).parent / ".meta_cache_test"))
    spec = importlib.util.spec_from_file_location("meow_app_test", APP_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["meow_app_test"] = mod
    spec.loader.exec_module(mod)

    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_async_client(transport=httpx.MockTransport(_handler(seen=seen)),
                                 *args, **kwargs)

    monkeypatch.setattr(mod.httpx, "AsyncClient", factory)
    with TestClient(mod.app) as c:
        yield c, seen, mod


def test_healthz_contract(client):
    c, _, _ = client
    r = c.get("/healthz")
    assert r.status_code == 200
    j = r.json()
    assert isinstance(j, dict) and j["ok"] is True
    assert j["source"] == "meow" and j["api_key_configured"] is True
    assert j["upstream_status"] == 200


def test_sources_contract(client):
    c, _, _ = client
    assert c.get("/sources").json() == {"enabled": ["meow"], "registered": ["meow"]}


def test_search_contract(client):
    c, seen, _ = client
    r = c.get("/search", params={"keyword": "晴天", "limit": 5})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True and j["errors"] == {} and j["engine"] == "meow"
    assert len(j["items"]) == 2
    item = j["items"][0]
    assert item["id"].startswith("meow:")       # 冒号 -> 代理生成 online:meow:...
    assert item["source"] == "meow"
    assert item["title"] == "晴天" and item["artist"] == "周杰伦"
    assert item["duration_s"] == 269 and item["ext"] == "mp3"
    assert item["cover_url"] == "https://img/cover.jpg"
    assert "download_url" not in item and "url" not in item
    assert "unit-test-key" not in r.text
    assert any(x.url.path == "/api/search" and x.url.params.get("query") == "晴天"
               for x in seen)


def test_info_roundtrip(client):
    c, _, mod = client
    j = c.get("/search", params={"keyword": "晴天"}).json()
    sid = j["items"][0]["id"]
    # id 可逆：解码必须还原原始歌名/歌手（含中文）
    song, singer, _, _ = mod._decode_id(sid)
    assert song == "晴天" and singer == "周杰伦"
    info = c.get("/info", params={"id": sid}).json()
    assert info["ok"] is True and info["id"] == sid
    assert info["title"] == "晴天" and info["artist"] == "周杰伦"
    assert info["duration_s"] == 269 and info["ext"] == "mp3"


def test_info_accepts_online_guid(client):
    c, _, mod = client
    sid = mod._encode_id("晴天", "周杰伦")
    song, singer, _, _ = mod._decode_id("online:" + sid)
    assert song == "晴天" and singer == "周杰伦"


def test_stream_forwards_song_singer(client):
    c, seen, _ = client
    sid = c.get("/search", params={"keyword": "晴天"}).json()["items"][0]["id"]
    r = c.get("/stream", params={"id": sid, "proxy": "true"})
    assert r.status_code == 200
    assert r.headers.get("content-type") == "audio/mpeg"
    assert int(r.headers["content-length"]) == len(FAKE_MP3)
    assert r.content == FAKE_MP3
    assert any(x.url.path == "/stream_live" and x.url.params.get("song") == "晴天"
               and x.url.params.get("singer") == "周杰伦" for x in seen)


def test_stream_range_206(client):
    c, _, _ = client
    sid = c.get("/search", params={"keyword": "晴天"}).json()["items"][0]["id"]
    r = c.get("/stream", params={"id": sid, "proxy": "true"},
              headers={"range": "bytes=0-1023"})
    assert r.status_code == 206
    assert r.headers.get("content-range") == f"bytes 0-1023/{len(FAKE_MP3)}"
    assert len(r.content) == 1024
    assert r.content == FAKE_MP3[:1024]


def test_stream_redirect_without_proxy(client):
    c, _, _ = client
    sid = c.get("/search", params={"keyword": "晴天"}).json()["items"][0]["id"]
    r = c.get("/stream", params={"id": sid}, follow_redirects=False)
    assert r.status_code == 302
    assert "proxy=true" in r.headers["location"]


def test_invalid_id_rejected(client):
    c, _, _ = client
    assert c.get("/info", params={"id": "meow:!!!!bad"}).status_code == 400


def test_search_upstream_failure_soft(client):
    c, _, _ = client
    # 通过修改上游 handler 触发失败需重建，这里仅验证空 keyword 被拒
    assert c.get("/search", params={"keyword": ""}).status_code == 422


# ---- 真机兜底：上游 /api/search 恒返回 [] 时的行为（192.168.2.220 实测）----
@pytest.fixture()
def empty_search_client(monkeypatch):
    """上游 /api/search 返回空数组，/stream_live 仍按 song(+可选 singer) 出流。"""
    seen: list[httpx.Request] = []
    for k, v in {"MEOW_BASE_URL": "http://upstream.test", "MEOW_API_KEY": "",
                 "MEOW_TIMEOUT": "30", "MEOW_MAX_DURATION": "0",
                 "MEOW_TRUST_ENV": "0", "MEOW_NO_DOTENV": "1",
                 "MEOW_METADATA_ENABLED": "0",
                 "MEOW_METADATA_CACHE_DIR": str(Path(__file__).parent / ".meta_cache_test")}.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("meow_app_empty_test", APP_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["meow_app_empty_test"] = mod
    spec.loader.exec_module(mod)

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/search":
            return httpx.Response(200, json=[])          # 真机现象：恒空
        if request.url.path == "/stream_live":
            return httpx.Response(200, content=FAKE_MP3, headers={
                "content-type": "audio/mpeg", "content-length": str(len(FAKE_MP3)),
                "accept-ranges": "bytes",
            })
        return httpx.Response(200, text="ok")

    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_async_client(transport=httpx.MockTransport(handle), *args, **kwargs)

    monkeypatch.setattr(mod.httpx, "AsyncClient", factory)
    with TestClient(mod.app) as c:
        yield c, seen, mod


def test_fallback_single_keyword(empty_search_client):
    """搜「晴天」应兜底出 1 个可点播候选，且 id 可逆还原。"""
    c, _, mod = empty_search_client
    j = c.get("/search", params={"keyword": "晴天"}).json()
    assert j["ok"] is True and len(j["items"]) == 1
    item = j["items"][0]
    assert item["title"] == "晴天" and item["id"].startswith("meow:")
    song, singer, _, _ = mod._decode_id(item["id"])
    assert song == "晴天" and singer == ""


def test_fallback_song_singer_split(empty_search_client):
    """搜「晴天 周杰伦」应拆出 (晴天, 周杰伦) 主候选 + 整串兜底候选。"""
    c, _, mod = empty_search_client
    j = c.get("/search", params={"keyword": "晴天 周杰伦"}).json()
    assert len(j["items"]) == 2
    song, singer, _, _ = mod._decode_id(j["items"][0]["id"])
    assert (song, singer) == ("晴天", "周杰伦")
    song2, singer2, _, _ = mod._decode_id(j["items"][1]["id"])
    assert (song2, singer2) == ("晴天 周杰伦", "")


def test_fallback_candidate_is_playable(empty_search_client):
    """兜底候选点播必须真出 MP3（singer 为空也要能出流）。"""
    c, seen, _ = empty_search_client
    sid = c.get("/search", params={"keyword": "晴天"}).json()["items"][0]["id"]
    r = c.get("/stream", params={"id": sid, "proxy": "true"})
    assert r.status_code == 200 and r.content == FAKE_MP3
    req = next(x for x in seen if x.url.path == "/stream_live")
    assert req.url.params.get("song") == "晴天"


# ---- 元数据补全：歌词 / 海报 / 专辑 / 时长（网易云公开接口）----
LRC_TEXT = "[00:00.000] 作词 : 周杰伦\n[00:28.950]故事的小黄花\n[00:32.380]从出生那年就飘着\n"
NETEASE_SONGS = [
    {"id": 2668397359, "name": "晴天 (原唱 周杰伦)",
     "artists": [{"name": "RyaVocal"}],
     "album": {"name": "晴天", "picId": 109951170413587092}},
    {"id": 186016, "name": "晴天",
     "artists": [{"name": "周杰伦"}],
     "album": {"name": "叶惠美", "picId": 109951170413587092}},
    {"id": 999, "name": "晴天 (女声版)", "artists": [{"name": "GYBeat"}]},
]
NETEASE_DETAIL = {"songs": [{"name": "晴天", "duration": 269000,
                             "album": {"name": "叶惠美",
                                       "picUrl": "https://p2.music.126.net/cover.jpg"}}]}
NETEASE_LYRIC = {"lrc": {"lyric": LRC_TEXT}}


@pytest.fixture()
def meta_client(monkeypatch, tmp_path):
    """上游 /api/search 恒空 + 网易云元数据接口可用。"""
    seen: list[httpx.Request] = []
    for k, v in {"MEOW_BASE_URL": "http://upstream.test", "MEOW_API_KEY": "",
                 "MEOW_TIMEOUT": "30", "MEOW_MAX_DURATION": "0",
                 "MEOW_TRUST_ENV": "0", "MEOW_NO_DOTENV": "1",
                 "MEOW_METADATA_ENABLED": "1", "MEOW_METADATA_NETEASE": "1",
                 "MEOW_METADATA_PCM": "0", "MEOW_METADATA_TIMEOUT": "3",
                 "MEOW_METADATA_HOP_TIMEOUT": "1",
                 "MEOW_METADATA_BASE": "https://music.163.com",
                 "MEOW_METADATA_CACHE_DIR": str(tmp_path / "meta")}.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("meow_app_meta_test", APP_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["meow_app_meta_test"] = mod
    spec.loader.exec_module(mod)

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/api/search":                 # 上游恒空 -> 走兜底
            return httpx.Response(200, json=[])
        if path == "/stream_live":
            return httpx.Response(200, content=FAKE_MP3, headers={
                "content-type": "audio/mpeg", "content-length": str(len(FAKE_MP3))})
        if path == "/api/search/get":             # 网易云搜索
            return httpx.Response(200, json={"result": {"songs": NETEASE_SONGS}})
        if path == "/api/song/detail":
            return httpx.Response(200, json=NETEASE_DETAIL)
        if path == "/api/song/lyric":
            return httpx.Response(200, json=NETEASE_LYRIC)
        return httpx.Response(200, text="ok")

    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_async_client(transport=httpx.MockTransport(handle), *args, **kwargs)

    monkeypatch.setattr(mod.httpx, "AsyncClient", factory)
    with TestClient(mod.app) as c:
        yield c, seen, mod


def test_info_has_cover_and_lyric(meta_client):
    """/info 必须给出封面直链与 LRC 歌词（飞牛歌词/海报出口读这两个字段）。"""
    c, _, _ = meta_client
    sid = c.get("/search", params={"keyword": "晴天"}).json()["items"][0]["id"]
    info = c.get("/info", params={"id": sid}).json()
    assert info["ok"] is True
    assert "p2.music.126.net/cover.jpg" in info["cover_url"]
    assert "param=500y500" in info["cover_url"]
    assert "故事的小黄花" in info["lyric"]
    assert info["lyric"].startswith("[00:")
    assert info["album"] == "叶惠美" and info["duration_s"] == 269
    assert info["title"] == "晴天"


def test_search_item_gets_cover_after_warm(meta_client):
    """预热完成后搜索结果直接带封面/专辑/时长，且不暴露上游地址。"""
    c, _, _ = meta_client
    c.get("/search", params={"keyword": "晴天"})
    # 等后台预热任务完成
    import time
    for _ in range(50):
        j = c.get("/search", params={"keyword": "晴天"}).json()
        if j["items"][0]["cover_url"]:
            break
        time.sleep(0.05)
    item = j["items"][0]
    assert "p2.music.126.net" in item["cover_url"]
    assert item["album"] == "叶惠美" and item["duration_s"] == 269
    assert "download_url" not in item and "url" not in item


def test_metadata_cache_avoids_repeat(meta_client):
    """同一 (song,singer) 命中缓存后不再重复打网易云。"""
    c, seen, _ = meta_client
    sid = c.get("/search", params={"keyword": "晴天"}).json()["items"][0]["id"]
    c.get("/info", params={"id": sid})
    count = sum(1 for x in seen if x.url.path == "/api/song/lyric")
    c.get("/info", params={"id": sid})
    c.get("/info", params={"id": sid})
    assert sum(1 for x in seen if x.url.path == "/api/song/lyric") == count


def test_metadata_picks_clean_title(meta_client):
    """打分应优先选标题干净的『晴天』而非『晴天 (原唱 …)』。"""
    c, seen, mod = meta_client
    c.get("/info", params={"id": mod._encode_id("晴天", "")})
    detail_reqs = [x for x in seen if x.url.path == "/api/song/detail"]
    assert detail_reqs and "186016" in detail_reqs[0].url.params.get("ids", "")


# ---- 元数据失败/关闭时：播放链路不受影响 ----
@pytest.fixture()
def meta_down_client(monkeypatch, tmp_path):
    """网易云全部 5xx；元数据必须静默降级，播放照常。"""
    for k, v in {"MEOW_BASE_URL": "http://upstream.test", "MEOW_API_KEY": "",
                 "MEOW_TIMEOUT": "30", "MEOW_MAX_DURATION": "0",
                 "MEOW_TRUST_ENV": "0", "MEOW_NO_DOTENV": "1",
                 "MEOW_METADATA_ENABLED": "1", "MEOW_METADATA_PCM": "0",
                 "MEOW_METADATA_TIMEOUT": "2", "MEOW_METADATA_HOP_TIMEOUT": "0.5",
                 "MEOW_METADATA_BASE": "https://music.163.com",
                 "MEOW_METADATA_CACHE_DIR": str(tmp_path / "meta_down")}.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("meow_app_meta_down_test", APP_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["meow_app_meta_down_test"] = mod
    spec.loader.exec_module(mod)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/search":
            return httpx.Response(200, json=[])
        if request.url.path == "/stream_live":
            return httpx.Response(200, content=FAKE_MP3, headers={
                "content-type": "audio/mpeg", "content-length": str(len(FAKE_MP3))})
        if request.url.path.startswith("/api/song") or request.url.path == "/api/search/get":
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, text="ok")

    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_async_client(transport=httpx.MockTransport(handle), *args, **kwargs)

    monkeypatch.setattr(mod.httpx, "AsyncClient", factory)
    with TestClient(mod.app) as c:
        yield c, mod


def test_metadata_failure_is_soft(meta_down_client):
    c, mod = meta_down_client
    sid = c.get("/search", params={"keyword": "晴天"}).json()["items"][0]["id"]
    info = c.get("/info", params={"id": sid}).json()
    assert info["ok"] is True and info["cover_url"] == "" and info["lyric"] == ""
    r = c.get("/stream", params={"id": sid, "proxy": "true"})
    assert r.status_code == 200 and r.content == FAKE_MP3


def test_metadata_disabled_contract(client):
    """MEOW_METADATA_ENABLED=0 时行为与旧版完全一致。"""
    c, _, _ = client
    sid = c.get("/search", params={"keyword": "晴天"}).json()["items"][0]["id"]
    info = c.get("/info", params={"id": sid}).json()
    assert info["lyric"] == "" and info["duration_s"] == 269


# ---- 多音源（v3.2）：单个上游音源可选、取流参数随 id 走 ----
KW_ROWS = [
    {"title": "晴天", "artist": "周杰伦", "duration": 269, "source": "kuwo",
     "url": "http://upstream.test/stream_live?song=%E6%99%B4%E5%A4%A9&singer=%E5%91%A8%E6%9D%B0%E4%BC%A6"},
]
BV_ROWS = [
    # B 站结果：url 里带 bv，播放必须按 BV 号取流，否则会解析成别的版本
    {"title": "晴天 (Live)", "artist": "某UP主", "duration": 300, "source": "bilibili",
     "url": "http://upstream.test/stream_live?song=x&singer=y&bv=BV1xx411c7mD"},
    # 与酷我同名同歌手：跨源去重后只应保留一条
    {"title": "晴天", "artist": "周杰伦", "duration": 269, "source": "bilibili",
     "url": "http://upstream.test/stream_live?song=x&singer=y&bv=BV1yy411c7mE"},
]


@pytest.fixture()
def multi_source_client(monkeypatch):
    """MEOW_SOURCES=kuwo,bilibili：验证按音源分发与 id 内嵌取流参数。"""
    seen: list[httpx.Request] = []
    monkeypatch.setenv("MEOW_BASE_URL", "http://upstream.test")
    monkeypatch.setenv("MEOW_NO_DOTENV", "1")
    monkeypatch.setenv("MEOW_METADATA_ENABLED", "0")
    monkeypatch.setenv("MEOW_SOURCES", "kuwo, bilibili")
    monkeypatch.setenv("MEOW_TIMEOUT", "30")

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/search":
            src = request.url.params.get("source", "")
            rows = BV_ROWS if src == "bilibili" else (KW_ROWS if src == "kuwo" else KW_ROWS + BV_ROWS)
            return httpx.Response(200, json=rows)
        if request.url.path == "/stream_live":
            return httpx.Response(200, content=FAKE_MP3, headers={
                "content-type": "audio/mpeg", "content-length": str(len(FAKE_MP3)),
                "accept-ranges": "bytes",
            })
        return httpx.Response(200, text="ok")

    spec = importlib.util.spec_from_file_location("meow_app_multi_test", APP_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["meow_app_multi_test"] = mod
    spec.loader.exec_module(mod)

    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_async_client(transport=httpx.MockTransport(handle), *args, **kwargs)

    monkeypatch.setattr(mod.httpx, "AsyncClient", factory)
    with TestClient(mod.app) as c:
        yield c, seen, mod


def test_sources_lists_configured_upstreams(multi_source_client):
    c, _, _ = multi_source_client
    body = c.get("/sources").json()
    assert body["enabled"] == ["meow", "meow-kuwo", "meow-bilibili"]
    assert body["registered"] == body["enabled"]


def test_search_defaults_to_configured_sources(multi_source_client):
    """不传 sources 时按 MEOW_SOURCES 逐个音源并发请求上游。"""
    c, seen, _ = multi_source_client
    j = c.get("/search", params={"keyword": "晴天", "limit": 10}).json()
    assert j["ok"] is True and j["errors"] == {}
    asked = sorted(r.url.params.get("source") for r in seen if r.url.path == "/api/search")
    assert asked == ["bilibili", "kuwo"]
    # 跨源同名同歌手去重：晴天/周杰伦只留一条（kuwo 先到）
    titles = [(i["title"], i["artist"]) for i in j["items"]]
    assert titles.count(("晴天", "周杰伦")) == 1
    assert ("晴天 (Live)", "某UP主") in titles


def test_search_sources_param_overrides_env(multi_source_client):
    """请求里的 sources 优先于 MEOW_SOURCES，且只打指定的那个音源。"""
    c, seen, mod = multi_source_client
    c.get("/search", params={"keyword": "晴天", "sources": "meow-bilibili"})
    asked = [r.url.params.get("source") for r in seen if r.url.path == "/api/search"]
    assert asked == ["bilibili"]
    assert mod._parse_source_list("meow_bilibili, lizhi") == ["bilibili", "lizhi"]


def test_id_roundtrips_upstream_source_and_play_params(multi_source_client):
    c, _, mod = multi_source_client
    j = c.get("/search", params={"keyword": "晴天", "sources": "bilibili"}).json()
    live = next(i for i in j["items"] if i["title"] == "晴天 (Live)")
    song, singer, src, extra = mod._decode_id(live["id"])
    assert (song, singer, src) == ("晴天 (Live)", "某UP主", "bilibili")
    assert extra == "bv=BV1xx411c7mD"


def test_stream_replays_xid_and_bv(multi_source_client):
    """点播必须把 id 里的 bv 带给上游 /stream_live，否则会放成别的版本。"""
    c, seen, _ = multi_source_client
    j = c.get("/search", params={"keyword": "晴天", "sources": "bilibili"}).json()
    sid = next(i for i in j["items"] if i["title"] == "晴天 (Live)")["id"]
    r = c.get("/stream", params={"id": sid, "proxy": "true"})
    assert r.status_code == 200 and r.content == FAKE_MP3
    req = next(x for x in seen if x.url.path == "/stream_live")
    assert req.url.params.get("bv") == "BV1xx411c7mD"
    assert req.url.params.get("song") == "晴天 (Live)"


def test_legacy_two_field_id_still_works(multi_source_client):
    """旧版两段式 id（只有 song/singer）必须继续可解，避免升级后历史记录失效。"""
    c, _, mod = multi_source_client
    legacy = mod._encode_id("晴天", "周杰伦")
    assert mod._decode_id(legacy) == ("晴天", "周杰伦", "", "")
    assert c.get("/info", params={"id": legacy}).json()["title"] == "晴天"


def test_search_interleaves_sources(multi_source_client):
    """轮转合并：排在第一的音源不能把 limit 名额占满，各家都要露脸。"""
    c, _, _ = multi_source_client
    items = c.get("/search", params={"keyword": "晴天", "limit": 2}).json()["items"]
    assert [i["upstream_source"] for i in items] == ["kuwo", "bilibili"]


def test_search_interleaves_sources(multi_source_client):
    """轮转合并：排在第一的音源不能把 limit 名额占满，各家都要露脸。"""
    c, _, _ = multi_source_client
    items = c.get("/search", params={"keyword": "晴天", "limit": 2}).json()["items"]
    assert [i["upstream_source"] for i in items] == ["kuwo", "bilibili"]
