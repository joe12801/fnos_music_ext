# fnmusic-ext 3.1.2-0 + mp3api 第四音源（FPK 发行说明）

这是 `joe12801/fnos_music_ext` 的自建发行版：在官方 `javycoder/fnos_music_ext`
v3.1.2 基础上，加入**第四类主音源 `mp3api`**，把自建 Music MP3 API 接入飞牛音乐。

- 基础提交：上游 `ff31fc2`（v3.1.2）
- 本次改动：`52f8364` — `feat(source): 新增第四音源 mp3api，接入自建 Music MP3 API`
- 安装版本：`3.1.2-0`（沿用上游，便于直接替换安装）
- 发行 tag：`mp3api-3.1.2-1`
- 发布包类型：**通用包 `platform=all`**（不含预构建 Docker 镜像与宿主 wheelhouse，
  首次安装时在 NAS 上现场构建；这是项目官方支持的通用包形态）

---

## 附件

| 文件 | 说明 |
| :--- | :--- |
| `fnmusic-ext-3.1.2-0.fpk` | 飞牛 fnOS 应用安装包（可在 NAS 上直接安装） |
| `fnmusic-ext-3.1.2-0.fpk.sha256` | 校验文件 |
| `RELEASE_MP3API.md` | 本说明 |
| `MP3API_INTEGRATION.md` | 完整技术说明：架构、契约、安全、部署、验证、合规 |

校验（在 NAS 或本地）：

```bash
sha256sum -c fnmusic-ext-3.1.2-0.fpk.sha256
```

---

## 本次包含什么

新增独立的 `mp3api-service/` 适配器，把自建 Music MP3 API（yt-dlp 搜索/下载 +
ffmpeg 转 MP3 + 本地缓存）包装成项目已有的 **musicdl 兼容 HTTP 契约**
（`/healthz` `/sources` `/search` `/info` `/stream`）。条目 id 为
`mp3api:<video_id>`，GUID 为 `online:mp3api:<video_id>`，自动落入代理既有
musicdl 分支，因此搜索聚合、取链、Range/206、边听边存、封面与转码链路全部复用，
无需重写代理内 16 处以上音源分派。

随包落地的还有：

- `proxy/app.py`：仅在开关打开时切换音源 client（超时 45s → 300s），支持运行期热切换。
- `webui-service`：管理页「音乐源」由三选一改为**四选一**，新增配置面板。
- 完整部署编排：Docker Compose 端口 `8776→8006`、supervisord 容器/native 进程表、
  `entrypoint.sh` 四选一分支、`healthcheck.sh`、`.dockerignore`、`Dockerfile`、
  native 依赖清单。
- 安装链路：`install.sh --sources mp3api`、交互向导第 4 项、`extend.sh`、
  FPK 安装向导第 4 个选项与安装回调白名单。
- 测试与文档：适配器契约测试、测试编排纳管、CHANGELOG、接入说明文档。

**默认关闭**：`FNMUSIC_MP3API_ENABLED=false`，未启用时原三音源
（musicdl / netease / lxmusic）行为完全不变。

---

## 安装与启用

1. 在飞牛 NAS 的「应用中心 / 手动安装」选择下载的 `.fpk`；或在扩展管理里走
   FPK 向导，`初始音源` 选择 **mp3api**。
2. 源码安装等价命令：

   ```bash
   ./install.sh --non-interactive --sources mp3api --webui --extend
   ```

3. 装好后打开管理页「音乐源 → Music MP3 API」，填写：

   | 项 | 示例 |
   | :--- | :--- |
   | `MP3API_BASE_URL` | 同机 `http://127.0.0.1:8765`；远程 `https://mp3.994938.xyz` |
   | `MP3API_API_KEY` | 上游 API 的密钥（仅服务端可见） |
   | `MP3API_BITRATE` | `192`（64–320 kbps） |
   | `MP3API_MAX_DURATION` | `900`（秒，过滤超长资源） |

4. 保持以下两项：

   ```dotenv
   FNMUSIC_ONLINE_SOURCES=
   FNMUSIC_MUSICDL_DIRECT_PLAY=false
   ```

   白名单置空是为了不让在线音源过滤掉 `mp3api` 条目；直连关闭是因为适配器
   刻意不实现 `/internal/*`，不会把上游地址暴露给客户端。

> **上游 Music MP3 API 需要单独部署**，本安装包只包含适配器，不包含上游源码。
> 上游已支持经 Nginx 反代 + API Key 的远程访问，以及 500 GB / 90 天的本地缓存。

---

## 安全约定

- `MP3API_API_KEY` 仅由 `mp3api` 进程从 `FNMUSIC_ENV_FILE` 只读加载（显式环境变量
  优先），不写入搜索结果、302 `Location` 或日志，也不放进 supervisor 公共
  `environment`，避免扩散到同容器其他进程。
- 未加入安装脚本的 `ENV_EXPLICIT`，升级/重装不会清空用户已保存的密钥。
- `MP3API_MAX_DURATION` 在搜索阶段过滤超长资源；video_id 使用
  `^[A-Za-z0-9_-]{11}$` 白名单校验。
- 上游不可达时 `/healthz` 如实反映 `upstream_status`，搜索阶段上游 502 软失败，
  不拖垮聚合搜索。

---

## 与上游发行版的区别

| 项 | 上游 v3.1.2 | 本发行版 |
| :--- | :--- | :--- |
| 主音源 | 三选一 | **四选一**（新增 mp3api，默认关闭） |
| FPK 向导 | 3 个音源选项 | 4 个选项（`mp3api` 已加入回调白名单） |
| 进程表 | 4 个 program | 5 个（新增 `[program:mp3api]`） |
| 端口 | 8768/8770/8772/8774 | 另加 `8776 → 8006` |
| 文档 | — | 新增 `MP3API_INTEGRATION.md` 与适配器 README |

本包为 **fork 自定义构建**，未改动上游的 VERSION / RELEASE_CHANNEL，因此
`version.py` 计算出的正式 tag 仍是 `v3.1.2`；本发行版使用独立 tag
`mp3api-3.1.2-1`（不带 `v` 前缀），避免误触发上游那条会构建三种变体
（generic / prebuilt-amd64 / prebuilt-arm64）的正式发布流水线。若需要
预构建镜像包，可在具备 Docker 的 Linux 环境另行执行官方 `release.yml`。

---

## 验证情况

- `bash -n`、`py_compile`、`git diff --check`、compose YAML、supervisord 进程表全通过。
- 适配器离线契约测试 11 passed；相关回归合计 54 passed。
- 真实链路冒烟：`/search` 返回 `mp3api:<id>`，`/stream` 返回
  `206 + Content-Range`，MP3 字节流正常。
- CI 构建此 FPK 时额外校验包内必须存在 `repo/mp3api-service/app.py`，
  确保第四音源确实随包交付。

已知环境限制（与本次改动无关）：Windows git-bash 下个别中文编码 / 并发时序测试
不稳定，完整 `python scripts/run_tests.py` 建议在 Linux / CI 复跑。

---

## 合规声明

本音源仅用于**拥有版权或已获授权的内容**，以及个人 / 家庭自用的学习场景。
请勿用于公开分发、商业用途或绕过平台方的访问限制。YouTube 相关内容的下载
行为需遵守平台服务条款与所在地法律法规；本项目不提供、也不保证任何第三方
平台的会员或付费内容权限。
