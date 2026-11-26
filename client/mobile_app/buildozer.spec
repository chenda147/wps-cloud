[app]

# (str) Title of your application
title = WPS传输工具

# (str) Package name
package.name = wpstransfer

# (str) Package domain (needed for android/ios packaging)
package.domain = org.wps.transfer

# (str) Source code where the main.py live
source.dir = .

# (list) Source files to include (let empty to include all the files)
source.include_exts = py,png,jpg,kv,atlas,ttf,otf,json

# (list) List of inclusions using pattern matching
# tools/* 为 7-Zip 官方 Linux arm64 完全静态二进制 7zz（26.02），用于手机端解压 7z/rar/zip
source.include_patterns = yidong_core.py,quark_core.py,quark_saver.py,wps_uploader.py,chase_engine.py,transfer_mobile.py,worker.py,config_helper.py,main.py,local_tg_download.py,fonts/*,tools/*

# (list) Source files to exclude (let empty to not exclude anything)
source.exclude_exts = pyc,pyo

# (str) Application versioning (method 1)
version = 1.0.8

# (list) Application requirements
# 注意：
# 1) 锁定 python3==3.11.9 且必须同时锁定 hostpython3==3.11.9：
#    p4a 的 hostpython3（构建期主机解释器）与 python3（目标机解释器）版本必须一致，
#    否则报 "python3 should have same version as hostpython3"。
#    p4a 默认两者都是 3.14.2，而 Kivy 2.3.0 的 Cython 代码与 Python 3.14 API 不兼容
#    （_PyUnicode_FastCopyCharacters 等已移除，gcc -Werror 编译失败）。
#    Kivy 2.3.0 官方支持的最高稳定版本是 3.11/3.12，这里锁 3.11.9 最稳。
# 2) py7zr 依赖 pybcj/pyppmd（Cython3 编译），与 p4a 工具链不兼容，已从打包清单移除。
#    手机端 7z/rar 解压改由 tools/7zz（7-Zip 26.02 arm64 完全静态二进制）subprocess 调用，
#    zip/tar 用标准库（zipfile/tarfile）解压；7z 在无 7zz 环境可回退 py7zr（若手动安装）。
# 3) 必须锁定 chardet==5.2.0 和 charset_normalizer==2.1.0（均为纯 Python 实现）！
#    chardet 6.x（2025 起）新增 Cython 扩展 orchestrator.so，CI 交叉构建时 pip 会
#    误装入 x86_64 manylinux wheel，导致 arm64 真机启动报
#    "dlopen failed: orchestrator.so is for EM_X86_64 instead of EM_AARCH64" 闪退。
#    charset_normalizer 3.x 同样带 C 扩展（_md.so），2.1.0 为纯 Python 版最稳。
#    requests 2.31 依赖两者，缺一不可。
# 4) 不用 pycryptodome！其 C 扩展（_raw_ecb.so 等）在 p4a 交叉编译后，Android 运行时
#    ctypes 加载报 "undefined symbol: PyObject_GetBuffer" 直接崩溃（两次实测确认）。
#    改用纯 Python 的 pyaes 实现 AES-CBC（见 yidong_core.py，含 PKCS7 pad/unpad 等价实现）。
# 5) TG 下载已改为「本机 root 模拟点击官方 Telegram」（local_tg_download.py），
#    与 PC 客户端同一套逻辑，不再需要 telethon（已从依赖与打包清单移除）。
requirements = python3==3.11.9,hostpython3==3.11.9,kivy==2.3.0,requests==2.31.0,chardet==5.2.0,charset_normalizer==2.1.0,pyaes==1.6.1,rsa==4.9,pyasn1==0.6.1,android

# (str) Custom source folders for requirements
#p4a.source_dir =

# (str) Presplash of the application
#presplash.filename = %(source.dir)s/data/presplash.png

# (str) Icon of the application
#icon.filename = %(source.dir)s/icon.png

# (str) Supported orientation
orientation = portrait

# (bool) Indicate if the application should be fullscreen or not
fullscreen = 0

# (list) Permissions
# READ/WRITE/MANAGE_EXTERNAL_STORAGE：支持自定义下载临时目录（如 /sdcard/Download）。
# Android 11+ 的 MANAGE_EXTERNAL_STORAGE 需用户在系统设置中手动授予“所有文件访问权限”。
android.permissions = INTERNET,ACCESS_NETWORK_STATE,WAKE_LOCK,READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE,MANAGE_EXTERNAL_STORAGE,READ_MEDIA_VIDEO

# (int) Target Android API
android.api = 33

# (int) Minimum API
android.minapi = 21

# (int) Android SDK version
#android.sdk = 24

# (str) Android NDK version
android.ndk = 25b

# (str) Android architecture
# Gradle/AGP 版本由 python-for-android 模板硬编码（buildozer 1.6.0 不识别
# android.gradle_version / android.agp_version，此前配置无效）。
# p4a.branch 固定为 v2024.01.21（下方），其 Gradle 8.0.2 + AGP 8.1.1 与 JDK 17 兼容。
# 注意：implicit-dependency 校验失败的真正根因是 android.add_src 空值导致
# --add-source <项目根>（详见下方 android.add_src 注释），与 Gradle 版本无关。
android.archs = arm64-v8a

# (bool) If True, then skip trying to update the Android sdk
android.accept_sdk_license = True

# (str) Android entry point, default is ok for Kivy-based apps
android.entrypoint = org.kivy.android.PythonActivity

# (str) Android app theme
android.apptheme = @android:style/Theme.NoTitleBar

# (str) Java package name for the app
#android.package_name =

# (bool) If True, then the app will be built for release
android.release_artifact = aab

# (bool) Keep the Android library and application in separate folders
# 重要：本配置必须保持注释（不可使用）！
# buildozer 1.6.0 的 getlist 会把空值 `android.add_src =` 解析为 [""]，
# 随后 realpath("") 变成项目根目录，buildozer 会向 p4a 传 --add-source <项目根>。
# 项目根内含 .buildozer 构建中间产物（mergeDebugAssets/mergeExtDexDebug 等的输出目录），
# 会被 javac 的 sourcepath 递归扫描，从而触发 Gradle implicit-dependency 校验失败：
#   Task ':compileDebugJavaWithJavac' uses this output of task ':mergeDebugAssets'
#   without declaring an explicit or implicit dependency.
# 这正是 Gradle 8.14.3 / 8.0.2 两次构建在此失败的真正根因。
#android.add_src =

# (list) Android additional libraries to compile into the app
android.add_libs_armeabi = 

# (str) Python-for-android branch to use
# 固定到 v2024.01.21（2024年1月稳定版）：Gradle 8.0.2 + AGP 8.1.1，
# 兼容 JDK 17 / Python 3.11.9 / NDK 25b / API 33。
# 注意：implicit-dependency 校验失败与 Gradle 版本无关（Gradle 8.0.2 同样报错），
# 真正根因是 android.add_src 空值（已注释修复）。master/develop 的 Gradle 8.14.3
# 在修复 add_src 后理论上也可用，但保持此稳定版最稳妥。
p4a.branch = v2024.01.21

# (str) Log level for python-for-android
#p4a.log_level = info

[buildozer]

# (int) Log level (0 = error only, 1 = info, 2 = debug (with command output))
log_level = 1

# (int) Display warning if buildozer tries to install a system Python package
warn_on_root = 1

# (str) Path to build artifact storage, must be an absolute path
build_dir = ./.buildozer

# (str) Path to build output (i.e. where the apk/ipa files are)
bin_dir = ./bin

# (str) Path to build android library in this folder
android.buildozer = ./buildozer

# (str) Android SDK and NDK path (optional)
#android.sdk_path = 

# (str) Android NDK path (optional)
#android.ndk_path = 
