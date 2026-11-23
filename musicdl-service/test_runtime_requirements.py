"""`requirements-runtime.txt` 与 musicdl 真实依赖树的漂移守卫。

背景见该文件头部：容器与宿主都自带 Node/ffmpeg，所以 musicdl 声明的
`nodejs-wheel`（约 58MB）与 `av`（约 40MB，自带一份 FFmpeg）被刻意剔除。
这里把它变成一个**会失败的断言**，避免以后升级 musicdl 时漏掉新依赖——
漏装的症状是运行期才 ImportError，比测试失败难查得多。
"""

from __future__ import annotations

import importlib.metadata
import re
from pathlib import Path

import pytest

_HERE = Path(__file__).parent
_RUNTIME_REQ = _HERE / "requirements-runtime.txt"

# 刻意不装的包：容器/宿主已有等价物（Node）或运行期根本不用（PyAV 仅类型标注）
INTENTIONALLY_EXCLUDED = {"av", "nodejs-wheel", "nodejs-wheel-binaries"}
# 剔除后必须仍然在列的关键依赖，防止有人「顺手」把清单删空
MUST_KEEP = {
    "click", "requests", "curl-cffi", "lxml", "orjson", "cryptography",
    "pycryptodome", "pycryptodomex", "mutagen", "m3u8", "pywidevine",
    "ytmusicapi", "beautifulsoup4", "brotli", "rich",
}


def _canonical(spec: str) -> str:
    """`puremagic<3,>=2.1.1; python_version >= "3.12"` -> `puremagic`。"""
    name = re.split(r"[\s<>=!~;\[\]]", spec.strip(), maxsplit=1)[0]
    return name.strip().lower().replace("_", "-")


def _runtime_requirements() -> dict[str, str]:
    """返回 {规范包名: 原始行}，忽略注释与空行。"""
    out: dict[str, str] = {}
    for raw in _RUNTIME_REQ.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out[_canonical(line)] = line
    return out


def test_runtime_requirements_excludes_heavy_unused_wheels():
    reqs = _runtime_requirements()
    assert reqs, "requirements-runtime.txt 不应为空"
    for name in INTENTIONALLY_EXCLUDED:
        assert name not in reqs, f"{name} 必须剔除（见文件头部说明）"
    missing = MUST_KEEP - set(reqs)
    assert not missing, f"关键依赖缺失: {sorted(missing)}"


def test_runtime_requirements_matches_installed_musicdl_metadata():
    """装得上 musicdl 时逐条比对 Requires-Dist；少一条就失败。"""
    try:
        declared = importlib.metadata.requires("musicdl")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("未安装 musicdl，无法比对元数据（CI 镜像构建时会装）")
    if not declared:
        pytest.skip("musicdl 元数据无 Requires-Dist")

    wanted = {_canonical(spec) for spec in declared} - INTENTIONALLY_EXCLUDED
    have = set(_runtime_requirements())
    assert not (wanted - have), (
        f"musicdl 声明的依赖漏在本清单外: {sorted(wanted - have)}；"
        "升级 musicdl 后请按 `pip show musicdl` 的 Requires 补齐 requirements-runtime.txt"
    )
    # 允许清单比元数据多（如显式钉住某些下限），但多出来的必须能被解释
    extra = have - wanted - INTENTIONALLY_EXCLUDED
    assert not extra, f"清单里有多余条目（可能是已从 musicdl 移除的依赖）: {sorted(extra)}"
