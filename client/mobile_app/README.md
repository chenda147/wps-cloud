# WPS传输工具 · 手机版（Android APK）

手机端真正的下载/上传客户端：连接你部署的 WPS 传输服务端后，手机即可领取服务端推送的任务，像 PC 客户端一样执行 **139/夸克分享 → 下载到手机 → 解压 → 上传 WPS**。

## 技术方案

- **Kivy + python-for-android（buildozer）** 打包 APK
- 复用 PC 客户端的纯 Python 核心：`yidong_core.py`（139）、`quark_core.py`（夸克）、`wps_uploader.py`（WPS 上传）、`chase_engine.py`（追更检查）
- 传输引擎 `transfer_mobile.py`：与 PC 版逻辑一致，**支持 7z / rar / zip / tar 解压**。
  7z/rar 由 APK 内打包的 7zz（7-Zip 26.02 arm64 完全静态二进制）调用解压，
  zip/tar 用标准库（zipfile/tarfile）解压，7z 无 7zz 环境可回退 py7zr
- 任务协议完全复用：`/api/client/poll` 轮询领取、`/api/client/task_status` 上报、`/api/client/chase_done` / `chase_result` 追更上报

## 文件结构

```
client/mobile_app/
├── main.py              # Kivy 主界面（连接设置 / 任务列表 / 日志）
├── worker.py            # 任务 Worker：轮询→执行→上报（移植自 gui.py）
├── transfer_mobile.py   # 手机版传输引擎（纯 Python 解压）
├── yidong_core.py       # 139 网盘客户端（复用 PC 版）
├── quark_core.py        # 夸克网盘客户端（复用 PC 版）
├── wps_uploader.py      # WPS 上传（复用 PC 版）
├── chase_engine.py      # 追更检查（复用 PC 版）
├── config_helper.py     # 配置读写（Android 私有目录）+ 下载临时目录解析
├── tools/7zz            # 7-Zip 26.02 arm64 静态二进制（解压 7z/rar/zip）
├── buildozer.spec       # buildozer 打包配置
└── requirements.txt     # 依赖
```

## 手机端配置

打开 APP → 「连接」页填写：

| 配置 | 说明 |
|------|------|
| 服务器地址 | 例如 `http://192.168.1.100:58000` |
| 服务端账号/密码 | 可空（服务端开启了登录校验则必填） |
| 139 Token | 与 PC 客户端 `config.json` 中一致，用于 139 分享下载 |
| 夸克 Cookie | 与 PC 客户端一致，用于夸克分享下载 |
| WPS Cookie | 与 PC 客户端一致，用于上传 |
| WPS 目标目录ID | 默认 `0`（根目录） |
| 下载临时目录 | 可空。默认存应用私有目录，上传后自动清理；自定义如 `/sdcard/Download` 需在系统设置授予「所有文件访问权限」 |

点击「保存配置」→「开始」即连接服务端轮询任务。

## 压缩包支持

- **zip / tar / tgz / tar.gz / tar.bz2 / tar.xz**：标准库直接解压（zip 失败时自动改用 7zz）
- **7z**：优先用 `tools/7zz` 解压；找不到 7zz 时回退 py7zr（需手动安装）
- **rar（RAR4 / RAR5）**：用 `tools/7zz` 解压（7-Zip 官方静态二进制）
- 解压目录为 `下载临时目录/_extract_*`，上传完成后整目录自动删除

## 打包 APK

### 方式一：GitHub Actions 云打包（推荐，无需本机 Android SDK）

1. 将本仓库推送到 GitHub（`.github/workflows/build-apk.yml` 已配置好）
2. 打开仓库 → Actions → **Build Android APK** → Run workflow
3. 构建完成后在 Artifacts 中下载 `WPS传输工具-APK`

### 方式二：本机打包（需安装 Android SDK / NDK / JDK）

```bash
cd client/mobile_app
pip install buildozer
buildozer android debug
```

APK 输出在 `bin/` 目录。

## 桌面调试（可选）

Kivy 支持 Windows 桌面运行，可先在本机验证界面与逻辑：

```bash
pip install -r requirements.txt
python main.py
```

> 注意：安卓端下载文件默认保存在应用私有目录，上传完成后自动删除，不占用手机相册/下载目录。
> 如需保存到公共目录（如 `/sdcard/Download`），在设置页填写「下载临时目录」，并先在系统设置中授予本应用「所有文件访问权限」。
