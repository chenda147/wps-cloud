# 139→WPS 传输工具 接口文档

> 适用范围：服务端 Web 后台（`server/app.py`） + 服务端云盘客户端（`server/cloud_clients.py`、`server/quark_client.py`）+ 客户端 GUI（`client/gui.py`、`client/transfer_engine.py`、`client/quark_core.py`）。
>
> 服务端默认地址：`http://<host>:5000`（端口见 `config.json` / `server/config/config.json`）
> 客户端与服务端通过 HTTP JSON 接口对接，客户端需带登录 `session` Cookie。

---

## 目录

- [一、服务端云盘客户端类（内部模块）](#一服务端云盘客户端类内部模块)
- [二、服务端 Web API（管理后台）](#二服务端-web-api-管理后台)
  - [2.1 登录 / 状态](#21-登录--状态)
  - [2.2 云盘存储（storages）](#22-云盘存储storages)
  - [2.3 云盘账号 / 配置（cloud_accounts / cloud）](#23-云盘账号--配置cloud_accounts--cloud)
  - [2.4 传输：浏览源 / 直链 / 上传 / 中继](#24-传输浏览源--直链--上传--中继)
  - [2.5 追更任务（chase）](#25-追更任务chase)
  - [2.6 STRM 任务](#26-strm-任务)
  - [2.7 文件管理](#27-文件管理)
  - [2.8 公共播放 / 分享（无需登录）](#28-公共播放--分享无需登录)
- [三、客户端 ↔ 服务端 对接接口](#三客户端--服务端-对接接口)
  - [3.1 客户端轮询 / 上报](#31-客户端轮询--上报)
  - [3.2 客户端发起的 HTTP 调用](#32-客户端发起的-http-调用)
- [四、客户端内部接口](#四客户端内部接口)
  - [4.1 夸克客户端（client/quark_core.py）](#41-夸克客户端clientquark_corepy)
  - [4.2 139 客户端（client/yidong_core.py）](#42-139-客户端clientyidong_corepy)
  - [4.3 传输引擎（client/transfer_engine.py）](#43-传输引擎clienttransfer_enginepy)

---

## 一、服务端云盘客户端类（内部模块）

工厂函数 `server/cloud_clients.py`：

```python
create_cloud_client(account_type: str, credential: str)
```

| account_type 取值 | 客户端类 | 鉴权方式 |
|---|---|---|
| `139` / `yun139` / `yidong` / `移动` / `和彩云` | `YidongClient` | 手机号+密码 → cookie |
| `wps` / `WPS` | `WPSClient` | client_id + client_secret |
| `ty` / `tianyi` / `天翼` | `TianyiClient` | 手机号+密码 → token |
| `quark` / `夸克` / `quarkcn` / `panquark` | `QuarkClient`（见 `server/quark_client.py`） | 登录 Cookie（仅下载直链需要；浏览分享目录无需账号） |

统一接口（各客户端尽量对齐）：
- `test() -> {"ok": bool, "msg": str, "user": str}`
- `list_files(folder_id="0", page=1, size=50) -> {"ok", "items", "count"}`
- `get_download_url(file_id, ...) -> {"ok", "url"}`
- `get_capacity() -> {"total", "used", "available"}`
- `get_user_info() -> dict`

---

## 二、服务端 Web API（管理后台）

> 除「公共」接口外，其余均带 `@login_required`，需在 Header/Session 中携带登录态。

### 2.1 登录 / 状态

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin` | 后台首页 |
| GET | `/admin/login` | 登录页 |
| POST | `/api/login` | 提交 `username` / `password` 登录 |
| POST | `/api/logout` | 登出 |
| GET | `/admin/logout` | 登出页 |
| POST | `/api/page_unlock` | 解锁后台页面 |
| GET | `/api/admin/status` | 系统状态概览（客户端数、任务数、容量等） |
| GET | `/api/status` | 客户端健康检查（客户端轮询连通性） |

### 2.2 云盘存储（storages）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/storages` | 列出所有目标存储（WPS/天翼/移动等 driver） |
| POST | `/api/admin/storages` | 新增存储 |
| PUT | `/api/admin/storages/<sid>` | 更新存储 |
| DELETE | `/api/admin/storages/<sid>` | 删除存储 |

`driver` 支持（写入时）：`wps` / `ty`（天翼）/ `yidong`（移动）等。
请求体示例（POST）：
```json
{ "name": "我的WPS", "driver": "wps", "config": { "client_id": "...", "client_secret": "..." } }
```

### 2.3 云盘账号 / 配置（cloud_accounts / cloud）

账号来源于两个数据源（追更/浏览时合并使用，主账号优先）：
1. **云盘账号管理**：`type` ∈ {`wps`,`ty`,`yidong`,`quark`}，`cookie`/凭据
2. **云盘配置管理**：`type` ∈ {`139`,`quark`}，`cookie`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/cloud_accounts` | 列出云盘账号 |
| POST | `/api/admin/cloud_accounts` | 新增云盘账号 `{name, type, cookie, is_primary}` |
| PUT/DELETE | `/api/admin/cloud_accounts/<aid>` | 改/删账号 |
| POST | `/api/admin/cloud_accounts/<aid>/test` | 测试账号有效性（支持 wps/ty/yidong） |
| GET | `/api/admin/cloud_accounts/capacity` | 聚合所有账号容量 |
| GET/POST | `/api/admin/cloud_config` | 云盘配置管理（增/列，含 `type:"quark"` + `cookie`，夸克配置主要入口） |
| DELETE | `/api/admin/cloud_config/<cid>` | 删除云盘配置 |
| POST/DELETE | `/api/admin/cloud_config/<cid>/primary` | 设为/取消主账号 |

#### 夸克账号配置（本次新增）
- 在「云盘配置」或「云盘账号」新增一条，类型选 **夸克网盘（quark）**，凭据填 `pan.quark.cn` 的登录 Cookie。
- 浏览分享目录**无需账号**；配置 Cookie 仅用于获取下载直链 / 缓解限速。

### 2.4 传输：浏览源 / 直链 / 上传 / 中继

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/transfer/source_files` | 浏览源分享目录（支持 yidong / quark 分享链接，或 账号+folder_id） |
| GET | `/api/admin/transfer/download_url` | 获取源文件下载直链（支持 yidong / quark 分享链接） |
| POST | `/api/admin/transfer/wps_upload_plan` | WPS 上传预规划（分片/秒传） |
| POST | `/api/admin/transfer/wps_upload_commit` | WPS 上传提交 |
| GET | `/api/admin/transfer/stream_fetch` | 服务端边下边传（流式拉取源并落盘） |
| POST | `/api/admin/transfer/relay` | 中转/代理下载（破解直链防盗链） |
| GET | `/api/admin/transfer/relay_progress` | 查询中转进度 |

**`source_files` 关键参数（GET）**
```
acc_type      : 云盘类型 yidong | quark | wps | ty
account_id    : 账号 id（可选）
share_url     : 分享链接（yidong/quark 必填其一）
share_passwd  : 分享提取码
parent_id     : 目录 id（默认 0/root）
recursive     : 1 递归
```
返回：`{"success": true, "items": [{"id","name","size","is_dir","type"}], "count": N}`

### 2.5 追更任务（chase）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/chase` | 列出追更任务 |
| POST | `/api/admin/chase` | 新增追更任务（含 `share_url`、`pattern`、关键字等） |
| PUT | `/api/admin/chase/<cid>` | 修改任务 |
| DELETE | `/api/admin/chase/<cid>` | 删除任务 |
| POST | `/api/admin/chase/<cid>/toggle` | 启用/暂停 |
| POST | `/api/admin/chase/<cid>/check` | 手动检查更新 |
| POST | `/api/admin/chase/<cid>/stop` | 停止当前检查 |
| GET | `/api/admin/chase/browse_share` | **浏览分享目录**（支持 139 / **夸克**，按链接域名自动识别） |
| POST | `/api/admin/chase/preview_files` | **预览文件选集**（同支持 139 / 夸克，递归子目录） |
| GET | `/api/admin/chase/preview_result/<preview_id>` | 获取预览结果 |
| POST | `/api/admin/chase/test_pattern` | 测试重命名/匹配正则 |

**`browse_share`（GET，本次为夸克修复的核心入口）**
```
share_url     : 分享链接（含 pan.quark.cn 或 share.139.com 等）
share_passwd  : 提取码（可选）
parent_id     : 目录 id（默认 root）
```
- 夸克链接：先用默认会话解析分享目录；失败回退到「云盘配置/账号」中的夸克 Cookie。
- 139 链接：先试网页版 AES，失败回退到 139 账号。

### 2.6 STRM 任务

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/strm_tasks` | 列出 STRM 生成任务 |
| POST | `/api/admin/strm_tasks` | 新增 |
| PUT | `/api/admin/strm_tasks/<idx>` | 修改 |
| DELETE | `/api/admin/strm_tasks/<idx>` | 删除 |
| POST | `/api/admin/strm_tasks/<idx>/toggle` | 启停 |
| POST | `/api/admin/strm_tasks/<idx>/run` | 立即运行 |

### 2.7 文件管理

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/browse_dir` | 浏览目标存储目录树 |
| POST | `/api/admin/files/rename` | 重命名 |
| POST | `/api/admin/files/move` | 移动 |
| POST | `/api/admin/files/copy` | 复制 |
| POST | `/api/admin/files/delete` | 删除 |
| POST | `/api/admin/files/batch_rename` | 批量改名 |
| POST | `/api/admin/files/mkdir` | 新建目录 |
| POST | `/api/admin/files/upload` | 上传文件到存储 |
| POST | `/api/public/files/upload` | 公共上传（前端拖拽） |
| GET | `/api/upload/progress/<task_id>` | 上传进度 |

### 2.7b 客户端管理

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/clients` | 已注册客户端列表（含活跃任务数） |
| POST | `/api/admin/client_config` | 配置客户端并发数 |
| GET/POST/DELETE | `/api/admin/client/tasks` | 客户端任务管理（列/建/删） |

### 2.7c 云盘容量告警

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/capacity_alert` | 容量告警阈值配置 |
| POST | `/api/admin/capacity_alert/set` | 设置容量告警阈值 |

### 2.7d 正则模式（重命名/匹配）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/regex_patterns` | 列出正则模式 |
| POST | `/api/admin/regex_patterns` | 新增 |
| GET | `/api/admin/regex_patterns/<pid>` | 查看 |
| PUT | `/api/admin/regex_patterns/<pid>` | 修改 |
| DELETE | `/api/admin/regex_patterns/<pid>` | 删除 |
| POST | `/api/admin/regex_patterns/test` | 测试正则匹配 |

### 2.7e 设置 / 全局 Cookie

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/settings` | 后台设置页数据 |
| POST | `/api/admin/settings/general` | 保存通用设置（IP/端口等） |
| GET | `/api/admin/cookie` | 查看全局 Cookie |
| POST | `/api/admin/cookie` | 设置全局 Cookie（用于中转/下载） |
| POST | `/api/admin/change_password` | 修改后台密码 |

### 2.7f STRM 任务运行

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/tasks/strm/status` | STRM 生成任务运行状态 |
| POST | `/api/admin/tasks/strm/start` | 启动 STRM 生成 |

### 2.8 公共播放 / 分享（无需登录）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/public/files` | 公开文件列表 |
| GET | `/api/public/auth_status` | 匿名鉴权状态 |
| GET | `/api/public/share_url/<file_id>` | 取分享链接 |
| GET | `/api/public/fileinfo/<file_id>` | 文件信息 |
| GET | `/play/<file_id>` | 网页播放器 |
| GET | `/img/<file_id>` | 图片预览 |
| GET | `/api/public/breadcrumbs` | 面包屑 |
| GET | `/api/public/search` | 公开搜索 |
| GET | `/api/public/stream/<file_id>` | 流式播放（带 Range） |
| GET | `/api/public/raw/<file_id>` | 原始流 |
| GET | `/api/public/download/<file_id>` | 公开下载 |
| GET/POST | `/stream/<file_id>` `/raw/<file_id>` `/dl/<file_id>` `/r/<file_id>` | 兼容短链路由 |
| GET | `/api/debug/cdn-expiry` | 调试直链有效期 |

---

## 三、客户端 ↔ 服务端 对接接口

客户端（`client/gui.py`）启动后登录服务端，并通过轮询拉取传输/追更任务、回传进度。

### 3.1 客户端轮询 / 上报（服务端提供）

| 方法 | 路径 | 说明 | 客户端调用位置 |
|---|---|---|---|
| GET | `/api/client/poll?client=<name>&batch=N&max_concurrent=M&check_cancel=1` | 客户端拉取待执行任务（并发槽位） | `gui.py:621` |
| POST | `/api/client/task_status` | 上报任务状态/进度 | `gui.py:698/783/807/817/932/965/977` |
| POST | `/api/client/chase_result` | 上报追更检查结果（新增集数等） | `gui.py:854/888/894` |
| POST | `/api/client/chase_done` | 追更任务完成回执 | `gui.py:791/940/957/975` |

> 客户端轮询会先把自身注册进 `_CLIENT_REGISTRY`（含 `ip`、心跳时间、`max_concurrent`），服务端据此分配任务、超时回收。

### 3.2 客户端发起的 HTTP 调用

| 调用 | 位置 | 说明 |
|---|---|---|
| `GET {url}/api/status` | `gui.py:513` | 连接/健康检查 |
| `POST {url}/api/login` | `gui.py:580/1019` | 客户端登录拿 session |
| `GET {url}/api/admin/chase` | `gui.py:1020` | 拉取追更任务列表（本地展示） |

客户端实际下载/转存逻辑在本地执行（见第四节），仅把结果回传给服务端。

---

## 四、客户端内部接口

### 4.1 夸克客户端（`client/quark_core.py` → 服务端移植版 `server/quark_client.py`）

```python
QuarkClient(cookie: str)
```
| 方法 | 返回 | 说明 |
|---|---|---|
| `parse_share_url(url)` *(静态)* | `{"ok","pwd_id","pwd","url"}` | 从 `pan.quark.cn/s/xxx` 提取分享 id |
| `list_share_files(share_url, passwd="", folder_id="0")` | `{"ok","items":[{"id","name","size","is_dir","type","path"}]}` | 列单层分享目录 |
| `list_share_files_recursive(share_url, passwd="", parent_id="0")` | 递归列全部文件 | 用于预览选集 |
| `get_share_download_url(share_url, file_id, file_name="", share_path="", share_passwd="")` | `{"ok","url"}` | 取下载直链（含 23018 风控换 UA 重试） |
| `get_download_url(file_id, share_url="", share_passwd="")` | 兼容封装 | 统一下载链路口 |
| `test()` | `{"ok","user"}` | 验证 Cookie（用户信息接口） |
| `get_capacity()` | `{"total","used","available"}` | 容量 |
| `get_user_info()` | dict | 用户信息 |

服务端 `QuarkClient`（`server/quark_client.py`）接口同上，已与 `TianyiClient`/`YidongClient` 对齐。

### 4.2 139 客户端（`client/yidong_core.py`）

```python
YidongClient(cookie: str)
```
| 方法 | 说明 |
|---|---|
| `browse_share_web(share_url, passwd, parent_id)` *(静态)* | 网页版 AES 解析分享（无需账号） |
| `list_share_files(share_url, passwd="", parent_id="0")` | 用账号 cookie 列分享目录 |
| `get_share_download_url(...)` | 取下载直链 |

### 4.3 传输引擎（`client/transfer_engine.py`）

```python
class TransferEngine:
    def __init__(self, yidong_token, wps_cookie, save_dir, ...)
```
| 方法 | 说明 |
|---|---|
| `detect_cloud_type(share_url)` *(模块级)* | 根据域名判断 `quark` / `yidong` / `wps` 等 |
| `build_src_client(clouds, share_url, cloud_id=None)` | 构造源客户端（夸克/139…） |
| `get_share_files(share_url, share_passwd="")` | 获取分享根目录文件列表 |
| `download_file(share_url, file_info, share_passwd="", progress_cb=None)` | 下载源文件（带 socket 优化/限速） |
| `upload_file(local_path, file_name, progress_cb=None, ...)` | 上传到 WPS（含分片/秒传） |
| `transfer(share_url, share_passwd="", progress_cb=None, ...)` | 一体化：下载→解压→上传 |
| `_extract_and_find_videos(archive_path, ...)` | 解压 rar/zip 并找视频 |
| `_find_videos(root_dir)` / `_find_archives(root_dir)` | 扫描媒体/压缩包 |

`build_src_client` 依据 `clouds` 配置选择 `QuarkShareClient`（夸克）或 139 客户端，实现"夸克分享 → WPS 转存"。

---

## 五、本次为夸克网盘新增/修改的接口清单

1. **新增 `server/quark_client.py`** — 服务端夸克分享客户端（见 4.1）。
2. **`server/cloud_clients.py`** — `create_cloud_client` 支持 `quark`。
3. **`server/app.py`**
   - `browse_share`（追更浏览）：按链接域名识别夸克，无需账号即可浏览目录，失败回退夸克账号 Cookie。
   - `preview_files`（追更预览）：同上，支持递归子目录。
   - 新增 `_get_all_quark_accounts()`：聚合「云盘账号」+「云盘配置」两处夸克账号（主账号优先）。
   - `transfer/source_files`、`transfer/download_url` 等分享分支：`acc_type` 支持 `quark`。
4. **`server/templates/admin_settings.html`** — 「云盘配置」下拉新增「夸克网盘」，动态显示 Cookie 填写提示。

> 使用前提：在后台「云盘配置」新增类型为 **夸克网盘** 的配置并粘贴 `pan.quark.cn` 登录 Cookie（浏览目录不强需，下载直链/限速缓解需要）。重启服务端生效。
