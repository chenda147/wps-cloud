"""
WPS 365 Cookie WebDAV 服务器
直接映射「我的云文档」为根路径 + 团队空间为子路径
基于 V3 API: drive.kdocs.cn/api/v3/
"""
import json, os, sys, time, socket, urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from xml.etree.ElementTree import Element, SubElement, tostring
import requests

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookie_config.json")

API_BASE = "https://drive.kdocs.cn/api/v3"
DEFAULT_GROUP_ID = "2203072050"  # 我的企业文档

# ─── 配置 & Session ─────────────────────────────

def load_cfg():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None

def build_session(cookie_str):
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.0.0",
        "Accept": "application/json",
        "Referer": "https://365.kdocs.cn/latest",
        "x-requested-with": "XMLHttpRequest",
    })
    for item in cookie_str.split(";"):
        item = item.strip()
        if "=" in item:
            k, _, v = item.partition("=")
            s.cookies.set(k.strip(), v.strip(), domain=".kdocs.cn")
    return s

# ─── WPS API 封装 ───────────────────────────────

class WPSClient:
    def __init__(self, session):
        self.s = session
        self._groups_cache = None
    
    def get_groups(self):
        """获取所有群组（含个人空间）"""
        if self._groups_cache:
            return self._groups_cache
        
        all_groups = []
        offset = 0
        while True:
            r = self.s.get(f"{API_BASE}/groups", params={"count": 200, "offset": offset}, timeout=15)
            if r.status_code != 200:
                break
            data = r.json()
            groups = data.get("groups", [])
            all_groups.extend(groups)
            if not data.get("has_next"):
                break
            offset += len(groups)
        
        self._groups_cache = all_groups
        return all_groups
    
    def get_files(self, group_id, parent_id=None, offset=0, count=200):
        """获取目录文件列表"""
        params = {"offset": offset, "count": count}
        if parent_id:
            params["parentid"] = parent_id  # v3 用 parentid
        
        r = self.s.get(f"{API_BASE}/groups/{group_id}/files", params=params, timeout=15)
        if r.status_code != 200:
            return []
        data = r.json()
        return data.get("files", [])
    
    def get_all_files(self, group_id, parent_id=None):
        """分页获取所有文件"""
        all_files = []
        offset = 0
        page = 200  # WPS 接口单页上限 200
        while True:
            batch = self.get_files(group_id, parent_id, offset=offset, count=page)
            all_files.extend(batch)
            if len(batch) < page:
                break
            offset += page
        return all_files
    
    def get_download_url(self, group_id, file_id):
        """获取下载链接（V3 API: 返回 fileinfo.url）"""
        r = self.s.get(f"{API_BASE}/groups/{group_id}/files/{file_id}/download", timeout=15)
        if r.status_code != 200:
            raise Exception(f"获取下载链接失败: HTTP {r.status_code}")
        data = r.json()
        # V3 API: {"fileinfo": {"url": "https://..."}}
        fileinfo = data.get("fileinfo", data)
        url = fileinfo.get("url") or data.get("url") or data.get("download_url")
        if not url:
            raise Exception(f"下载链接解析失败: {data}")
        return url
    
    def get_file_info(self, group_id, file_id):
        """获取单个文件信息 (通过列表搜索)"""
        all_files = self.get_all_files(group_id)
        for f in all_files:
            if str(f.get("id")) == str(file_id):
                return f
        return None
    
    def find_by_path(self, group_id, path_parts):
        """按路径找到文件/目录（V3: 用 fname 字段）"""
        current_parent = None
        for i, part in enumerate(path_parts):
            files = self.get_all_files(group_id, current_parent)
            found = None
            for f in files:
                fname = f.get("fname") or f.get("name") or ""
                if fname == part:
                    found = f
                    break
            if not found:
                return None
            if i == len(path_parts) - 1:
                return found
            if found.get("ftype") == "folder":
                current_parent = found["id"]
            else:
                return None
        return None

# ─── WebDAV Handler ─────────────────────────────

XML_NS = "DAV:"

def xml_element(tag, text=None, **attrs):
    el = Element(tag)
    if text is not None:
        el.text = str(text)
    for k, v in attrs.items():
        el.set(k, str(v))
    return el

def make_response(status_code, content_type="application/xml; charset=utf-8"):
    return status_code, content_type

class WebDAVHandler(BaseHTTPRequestHandler):
    wps = None  # will be set by server
    
    def log_message(self, *args): pass
    
    def _send_xml(self, code, xml_str):
        self.send_response(code)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", len(xml_str.encode("utf-8")))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("DAV", "1")
        self.end_headers()
        self.wfile.write(xml_str.encode("utf-8"))
    
    def _send_redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()
    
    def _send_raw(self, code, data, content_type="application/octet-stream"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(data) if isinstance(data, bytes) else str(len(data.encode("utf-8"))))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.wfile.write(data)
    
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("DAV", "1")
        self.send_header("Allow", "OPTIONS, GET, PROPFIND")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, PROPFIND, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Depth, Content-Type, Authorization")
        self.end_headers()
    
    def do_PROPFIND(self):
        path = urllib.parse.unquote(self.path.rstrip("/"))
        depth = self.headers.get("Depth", "1")
        
        print(f"PROPFIND depth={depth} path='{path}'")
        
        multistatus = Element(f"{{{XML_NS}}}multistatus")
        
        try:
            # 路径解析：全部在 DEFAULT_GROUP_ID (我的企业文档) 下
            parts = [p for p in path.split("/") if p]
            
            # 找到当前目录的 parent_id
            parent_id = None
            sub_path = parts  # 每个 part 是目录名
            
            for pname in sub_path:
                files = self.wps.get_all_files(DEFAULT_GROUP_ID, parent_id)
                found = None
                for f in files:
                    fn = f.get("fname") or f.get("name") or ""
                    if fn == pname and f.get("ftype") == "folder":
                        found = f
                        break
                if not found:
                    self._send_xml(404, f"<error>Path not found: {pname}</error>")
                    return
                parent_id = found["id"]
            
            # 列出文件（V3 API 字段: fname, ftype, fsize, mtime）
            files = self.wps.get_all_files(DEFAULT_GROUP_ID, parent_id)
            
            for f in files:
                fid = f["id"]
                fname = f.get("fname") or f.get("name") or "unnamed"
                ftype = f.get("ftype") or "file"
                fsize = int(f.get("fsize") or f.get("size") or 0)
                mtime = f.get("mtime") or int(time.time())
                if isinstance(mtime, str):
                    mtime = int(mtime)
                
                if ftype == "folder":
                    href = f"/{'/'.join(sub_path)}/{fname}/" if sub_path else f"/{fname}/"
                else:
                    href = f"/{'/'.join(sub_path)}/{fname}" if sub_path else f"/{fname}"
                href = href.replace("//", "/")
                
                resp = SubElement(multistatus, f"{{{XML_NS}}}response")
                SubElement(resp, f"{{{XML_NS}}}href").text = href
                propstat = SubElement(resp, f"{{{XML_NS}}}propstat")
                prop = SubElement(propstat, f"{{{XML_NS}}}prop")
                SubElement(prop, f"{{{XML_NS}}}displayname").text = fname
                
                if ftype == "folder":
                    SubElement(prop, f"{{{XML_NS}}}resourcetype").append(Element(f"{{{XML_NS}}}collection"))
                else:
                    SubElement(prop, f"{{{XML_NS}}}resourcetype")
                SubElement(prop, f"{{{XML_NS}}}getcontentlength").text = str(fsize)
                SubElement(prop, f"{{{XML_NS}}}getlastmodified").text = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(mtime if isinstance(mtime, (int, float)) else time.time()))
                
                SubElement(propstat, f"{{{XML_NS}}}status").text = "HTTP/1.1 200 OK"
            
            # depth != 0 时列出当前目录本身
            if depth != "0":
                cur_href = f"/{'/'.join(sub_path)}/" if sub_path else "/"
                cur_href = "/" + cur_href.lstrip("/")
                resp = SubElement(multistatus, f"{{{XML_NS}}}response")
                SubElement(resp, f"{{{XML_NS}}}href").text = cur_href
                propstat = SubElement(resp, f"{{{XML_NS}}}propstat")
                prop = SubElement(propstat, f"{{{XML_NS}}}prop")
                SubElement(prop, f"{{{XML_NS}}}displayname").text = sub_path[-1] if sub_path else "我的企业文档"
                SubElement(prop, f"{{{XML_NS}}}resourcetype").append(Element(f"{{{XML_NS}}}collection"))
                SubElement(propstat, f"{{{XML_NS}}}status").text = "HTTP/1.1 200 OK"
            
            self._send_xml(207, '''<?xml version="1.0" encoding="utf-8"?>\n''' + tostring(multistatus, encoding="unicode"))
        
        except Exception as e:
            print(f"  PROPFIND ERROR: {e}")
            import traceback
            traceback.print_exc()
            self.send_error(500, str(e))
    
    def do_GET(self):
        path = urllib.parse.unquote(self.path)
        print(f"GET {path}")
        
        if path == "/" or path == "":
            # 根路由：简单索引页
            try:
                files = self.wps.get_all_files(DEFAULT_GROUP_ID, None)
                lines = ["<html><body><h1>我的企业文档</h1><ul>"]
                for f in files:
                    fname = f.get("fname") or f.get("name") or "unnamed"
                    ftype = f.get("ftype") or "file"
                    if ftype == "folder":
                        lines.append(f'<li>[DIR] <a href="/{urllib.parse.quote(fname)}/">{fname}</a></li>')
                    else:
                        lines.append(f'<li><a href="/{urllib.parse.quote(fname)}">{fname}</a></li>')
                lines.append("</ul></body></html>")
                self._send_raw(200, "\n".join(lines), "text/html; charset=utf-8")
            except Exception as e:
                self.send_error(500, str(e))
            return
        
        # 路径: /{subfolder...}/{filename}
        parts = path.strip("/").split("/")
        filename = parts[-1]
        sub_folders = parts[:-1]
        
        try:
            # 导航到目标目录
            parent_id = None
            for folder_name in sub_folders:
                files = self.wps.get_all_files(DEFAULT_GROUP_ID, parent_id)
                found = False
                for f in files:
                    fn = f.get("fname") or f.get("name") or ""
                    if fn == folder_name and f.get("ftype") == "folder":
                        parent_id = f["id"]
                        found = True
                        break
                if not found:
                    self.send_error(404, f"Folder not found: {folder_name}")
                    return
            
            # 找目标文件
            files = self.wps.get_all_files(DEFAULT_GROUP_ID, parent_id)
            target_file = None
            for f in files:
                fn = f.get("fname") or f.get("name") or ""
                if fn == filename:
                    target_file = f
                    break
            
            if not target_file:
                self.send_error(404, f"File not found: {filename}")
                return
            
            # 代理下载：用 Cookie 从 CDN 拉取，流式传给客户端
            download_url = self.wps.get_download_url(DEFAULT_GROUP_ID, target_file["id"])
            fsize = int(target_file.get("fsize") or target_file.get("size") or 0)
            fname = target_file.get("fname") or filename
            print(f"  -> proxy download: {download_url[:100]}...")
            
            self._proxy_download(download_url, fsize, fname)
            
        except Exception as e:
            print(f"  GET ERROR: {e}")
            import traceback
            traceback.print_exc()
            try:
                self.send_error(500, "Internal server error")
            except:
                pass
    
    def _proxy_download(self, url, size, filename):
        """用 authenticated session 代理下载文件并流式传输"""
        headers = {}
        range_header = self.headers.get("Range")
        if range_header:
            headers["Range"] = range_header
        
        with self.wps.s.get(url, headers=headers, stream=True, timeout=(30, 3600)) as r:
            if r.status_code >= 400:
                self.send_error(r.status_code, f"Upstream error: {r.status_code}")
                return
            
            if r.status_code == 206:
                self.send_response(206)
                cr = r.headers.get("Content-Range")
                if cr:
                    self.send_header("Content-Range", cr)
                cl = r.headers.get("Content-Length")
                if cl:
                    self.send_header("Content-Length", cl)
            else:
                self.send_response(200)
                self.send_header("Content-Length", str(size))
            
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            
            # 流式传输
            try:
                for chunk in r.iter_content(chunk_size=65536):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # 客户端断开连接
    
    def _resolve_path(self, path_parts):
        """解析路径，返回 (target_file, is_dir) 或 None"""
        if not path_parts:
            return None
        filename = path_parts[-1]
        sub_folders = path_parts[:-1]
        
        parent_id = None
        for folder_name in sub_folders:
            files = self.wps.get_all_files(DEFAULT_GROUP_ID, parent_id)
            found = None
            for f in files:
                fn = f.get("fname") or f.get("name") or ""
                if fn == folder_name and f.get("ftype") == "folder":
                    found = f
                    break
            if not found:
                return None
            parent_id = found["id"]
        
        files = self.wps.get_all_files(DEFAULT_GROUP_ID, parent_id)
        for f in files:
            fn = f.get("fname") or f.get("name") or ""
            if fn == filename:
                return (f, f.get("ftype") == "folder")
        return None
    
    def do_HEAD(self):
        path = urllib.parse.unquote(self.path)
        if path == "/" or path == "":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("DAV", "1")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return
        
        parts = path.strip("/").split("/")
        result = self._resolve_path(parts)
        
        if not result:
            self.send_error(404, "Not found")
            return
        
        target_file, is_dir = result
        fname = target_file.get("fname") or parts[-1]
        fsize = int(target_file.get("fsize") or target_file.get("size") or 0)
        
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(fsize))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

# ─── Main ───────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(description="WPS 365 Cookie WebDAV Server")
    ap.add_argument("command", nargs="?", default="serve", choices=["serve", "probe"])
    ap.add_argument("--port", type=int, default=58000)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    
    cfg = load_cfg()
    if not cfg or not cfg.get("cookie"):
        print("未找到 cookie_config.json，请先运行: python wps_cookie_dav.py probe")
        print("或手动创建 cookie_config.json 包含 'cookie' 字段")
        sys.exit(1)
    
    session = build_session(cfg["cookie"])
    client = WPSClient(session)
    
    if args.command == "probe":
        # 探测模式：列出所有群组和文件
        print("=" * 55)
        print("  WPS 365 Cookie 探测")
        print("=" * 55)
        
        try:
            groups = client.get_groups()
            print(f"\n找到 {len(groups)} 个空间：\n")
            for g in groups:
                gid = g["id"]
                gname = g.get("name", "N/A")
                gtype = g.get("type", "N/A")
                label = ""
                if gtype == "corpspecial":
                    label = " ← 我的云文档"
                elif gtype == "corptmp":
                    label = " (自动备份)"
                print(f"  [{gid}] {gname}  type={gtype}{label}")
            
            print(f"\n{'='*55}")
            print(f"  Cookie 有效，API 连接正常！")
            print(f"  启动 WebDAV: python wps_cookie_dav.py --port 58000")
            print(f"{'='*55}")
            
        except Exception as e:
            print(f"\nCookie 无效或 API 连接失败: {e}")
            print("请重新获取 Cookie 并写入 cookie_config.json")
            sys.exit(1)
    
    else:
        # WebDAV 服务模式
        WebDAVHandler.wps = client
        
        server = HTTPServer((args.host, args.port), WebDAVHandler)
        host_ip = socket.gethostbyname(socket.gethostname())
        
        print("=" * 55)
        print("  WPS 365 Cookie WebDAV Server (V3 API)")
        print("=" * 55)
        print(f"  服务地址: http://{args.host}:{args.port}/")
        print(f"  局域网:   http://{host_ip}:{args.port}/")
        print(f"  Kodi/播放器 添加源:")
        print(f"    WebDAV (HTTP) → http://{host_ip}:{args.port}/")
        print(f"  按 Ctrl+C 停止服务")
        print("=" * 55)
        
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n服务已停止")
            server.server_close()

if __name__ == "__main__":
    main()
