"""
追更引擎 - 集数提取、匹配、增量计算
用于从分享链接中检测新剧集并按模板重命名
"""
import re
import os


def extract_episode(filename, pattern=""):
    """
    从文件名中提取集数。
    pattern: 正则表达式，需包含一个捕获组捕获集数。
             空则自动尝试常见格式。

    返回 (episode_number: int or None, show_name: str)
    """
    base = os.path.splitext(filename)[0]

    if pattern and pattern.strip():
        m = re.search(pattern.strip(), base)
        if m:
            try:
                return int(m.group(1)), base
            except (ValueError, IndexError):
                pass
        return None, base  # 自定义正则不匹配 → 跳过，不回退

    # 自动识别常见剧集格式
    formats = [
        r'S\d+E(\d+)',                                 # S01E185
        r'[Ee](\d+)',                                   # E185 / e185
        r'第\s*(\d+)\s*集',                             # 第185集
        r'第\s*(\d+)\s*话',                             # 第185话
        r'第\s*(\d+)\s*回',                             # 第185回
        r'[\[\(（](\d+)[\]\)）]',                      # (185) [185] （185）
        r'\s+-\s+(\d+)\b',                              #  - 185
        r'\.(\d{2,4})$',                                # .185 (结尾的纯数字)
        r'_(\d{2,4})$',                                 # _185
        r'^(\d+)$',                                     # 13 (纯数字文件名，如 13.mp4)
    ]
    # 弱格式（括号/横线/结尾数字/纯数字）可能把年份误识别成集数，
    # 例如 "九门 (2026) [tmdbid=271016].mp4" 的 (2026)。命中年份时跳过，
    # 继续尝试下一格式，避免误报出 2026 这类虚高集数。
    weak_formats = {
        r'[\[\(（](\d+)[\]\)）]',
        r'\s+-\s+(\d+)\b',
        r'\.(\d{2,4})$',
        r'_(\d{2,4})$',
        r'^(\d+)$',
    }
    for fmt in formats:
        m = re.search(fmt, base)
        if m:
            try:
                ep = int(m.group(1))
                if fmt in weak_formats and 1900 <= ep <= 2100:
                    continue
                return ep, base
            except (ValueError, IndexError):
                continue
    return None, base


def extract_show_name(filename, ep_pattern=""):
    """从文件名提取剧名（去掉集数部分）"""
    base = os.path.splitext(filename)[0]
    if ep_pattern and ep_pattern.strip():
        m = re.search(ep_pattern.strip(), base)
        if m:
            return base[:m.start()].strip().rstrip('.-_[]()（） ')
    ep, _ = extract_episode(filename)
    if ep is not None:
        # 去掉集数部分
        fmt2 = [
            r'S\d+E\d+',
            r'[Ee]\d+',
            r'第\s*\d+\s*[集话回]',
            r'[\[\(（]\d+[\]\)）]',
            r'\s+-\s+\d+$',
            r'\.\d{2,4}$',
            r'_\d{2,4}$',
        ]
        for f in fmt2:
            base = re.sub(f, '', base).strip().rstrip('.-_[]()（） ')
        return base
    return base


def rename_with_template(filename, template, episode_num, season=0, tmdb_id=""):
    """
    按模板批量重命名。
    template: 模板字符串，{ep} 会被替换为集数，{season} 会被替换为季数
      例: "凡人修仙传 S{season:02d}E{ep:03d}" → "凡人修仙传 S01E185"
      或: "凡人修仙传（2020）E{ep}.{tmdb-21734}" → "凡人修仙传（2020）E185.{tmdb-21734}"
    episode_num: 集数 (int)
    season: 季数 (int, 默认0=不分季)
    tmdb_id: TMDB ID (str, 用于媒体库识别)
    """
    import re
    tpl = template.strip()
    # 扩展名保持不变
    ext = os.path.splitext(filename)[1]
    # {season} / {season:02d} / {s:02d}
    def _season_repl(m):
        pad = int(m.group(1)) if m.group(1) else 0
        return str(season).zfill(pad) if pad else str(season)
    tpl = re.sub(r'\{season(?::(\d+)d)?\}', _season_repl, tpl)
    tpl = re.sub(r'\{s(?::(\d+)d)?\}', _season_repl, tpl)
    # {tmdb_id} / {tmdb} → {tmdb-XXXX}
    if tmdb_id:
        tpl = tpl.replace("{tmdb_id}", f"{{{{tmdb-{tmdb_id}}}}}")
        tpl = tpl.replace("{tmdb}", f"{{{{tmdb-{tmdb_id}}}}}")
    else:
        tpl = tpl.replace("{tmdb_id}", "")
        tpl = tpl.replace("{tmdb}", "")
    # 支持格式化语法 {ep} {ep:03d} {ep:02d}
    try:
        result = tpl.format(ep=episode_num)
    except (ValueError, KeyError):
        # 简单替换 {ep}
        result = tpl.replace("{ep}", str(episode_num))
    return result + ext


def find_new_episodes(files, base_ep, ep_pattern=""):
    """
    从文件列表中找出大于 base_ep 的集数。
    同一集号有多个文件时，优选容量最大的。
    files: [{name, size, ...}, ...]
    base_ep: 当前追到的最新集数
    ep_pattern: 集数提取正则

    返回 [(ep_num, file_dict, new_name_from_template), ...]
    按集数升序排列
    """
    ep_map = {}  # ep_num -> file_dict, 同一集号保留最大的
    for f in files:
        fname = f.get("name", "")
        ep_num, _ = extract_episode(fname, ep_pattern)
        if ep_num is not None and ep_num > base_ep:
            if ep_num not in ep_map or f.get("size", 0) > ep_map[ep_num].get("size", 0):
                ep_map[ep_num] = f

    # 按集数排序
    new_eps = sorted(ep_map.items(), key=lambda x: x[0])
    return [(ep_num, f) for ep_num, f in new_eps]


def generate_batch_rename(files, base_ep, ep_pattern, rename_template, season=0, tmdb_id=""):
    """
    批量生成重命名映射。用于批量下载+重命名+上传。
    返回 [(ep_num, file_dict, new_name), ...]
    """
    new_eps = find_new_episodes(files, base_ep, ep_pattern)
    result = []
    for ep_num, f in new_eps:
        fname = f.get("name", "")
        new_name = rename_with_template(fname, rename_template, ep_num, season, tmdb_id)
        result.append((ep_num, f, new_name))
    return result


def find_max_episode(files, ep_pattern=""):
    """找文件列表中最大的集数"""
    max_ep = 0
    for f in files:
        ep_num, _ = extract_episode(f.get("name", ""), ep_pattern)
        if ep_num is not None and ep_num > max_ep:
            max_ep = ep_num
    return max_ep if max_ep > 0 else 0
