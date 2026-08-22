# -*- coding: utf-8 -*-
"""
反向隧道客户端（内网穿透）—— 集成到 WPS 服务端

作用: 把本机 WPS 管理面板端口(默认 58000)通过公网服务器转发出去,
      让外网用户直接访问管理面板。

协议与「远程」目录的 server.py 完全兼容, 公网服务器端无需任何改动:
    python3 server.py --secret 你的密钥 --listen 13390 [--tunnel 7000 --data 7001]

用法(config.json 的 tunnel 段):
    "tunnel": {
        "server": "1.2.3.4",        # 公网服务器地址(域名或IP), 空=不启用
        "secret": "你的密钥",        # 访问密钥(必须与服务端 --secret 一致)
        "port": 13390               # 外网访问端口(公网 server.py --listen 的端口)
    }

提示:
    - 控制通道/数据通道端口固定用默认 7000/7001, 如需修改请改本文件开头常量
    - 公网服务器安全组需放行: listen端口 + 7000 + 7001
"""

import logging
import socket
import struct
import threading
import time

log = logging.getLogger("tunnel-client")

_tunnel_ref = {}


def start_tunnel_if_configured(cfg):
    """根据 config 的 tunnel 段启动反向隧道线程；未配置(server/secret 为空)则停止并返回 None。

    返回已启动的 TunnelClient 实例；重复调用会先停止旧的。
    """
    tun = (cfg or {}).get("tunnel") or {}
    server = (tun.get("server") or "").strip()
    secret = (tun.get("secret") or "").strip()
    old = _tunnel_ref.get("client")
    if not server or not secret:
        if old is not None:
            old.stop()
            _tunnel_ref["client"] = None
            log.info("[隧道] 配置为空, 已停止内网穿透")
        return None
    if old is not None:
        old.stop()
    local_port = int(cfg.get("server_port", 58000))
    ctrl_port = int(tun.get("ctrl_port") or TUNNEL_PORT)
    data_port = int(tun.get("data_port") or DATA_PORT)
    client = TunnelClient(server, secret, local_port,
                          tunnel_port=ctrl_port, data_port=data_port)
    _tunnel_ref["client"] = client
    threading.Thread(target=client.run, daemon=True).start()
    log.info("[隧道] 已启动内网穿透: 外网访问 http://%s:%s (控制 %d / 数据 %d, 本机 %d)",
             server, tun.get("port") or 13390, ctrl_port, data_port, local_port)
    return client

# 与公网 server.py 默认端口一致
TUNNEL_PORT = 7000   # 控制通道端口
DATA_PORT = 7001     # 数据通道端口
RECONNECT_DELAY = 5  # 断线重连间隔(秒)


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except Exception:
            return None
        if not chunk:
            return None
        buf += chunk
    return buf


def _auth(sock, secret: bytes) -> bool:
    """连接后立即发送鉴权头并等待服务器确认"""
    try:
        sock.sendall(struct.pack(">I", len(secret)) + secret)
        return _recv_exact(sock, 1) == b"1"
    except Exception:
        return False


def _pipe_sockets(a, b):
    def pump(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except Exception:
            pass
        finally:
            for s in (a, b):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
            for s in (a, b):
                try:
                    s.close()
                except Exception:
                    pass

    threading.Thread(target=pump, args=(a, b), daemon=True).start()
    threading.Thread(target=pump, args=(b, a), daemon=True).start()


class TunnelClient:
    """反向隧道客户端: 把本机 local_port 转发到公网服务器的访问端口"""

    def __init__(self, server: str, secret: str, local_port: int,
                 tunnel_port: int = TUNNEL_PORT, data_port: int = DATA_PORT,
                 reconnect_delay: int = RECONNECT_DELAY):
        self.server = server
        self.secret = secret.encode()
        self.local_port = local_port
        self.tunnel_port = tunnel_port
        self.data_port = data_port
        self.reconnect_delay = reconnect_delay
        self._stop = threading.Event()
        self._ctrl = None

    def stop(self):
        self._stop.set()
        if self._ctrl is not None:
            try:
                self._ctrl.close()
            except Exception:
                pass

    def run(self):
        log.info("[隧道] 启动: 服务器=%s:%d, 本机面板=127.0.0.1:%d",
                 self.server, self.tunnel_port, self.local_port)
        while not self._stop.is_set():
            try:
                self._run_once()
            except Exception as e:
                log.error("[隧道] 连接异常: %s", e)
            if self._stop.is_set():
                break
            log.info("[隧道] 将在 %d 秒后重连...", self.reconnect_delay)
            time.sleep(self.reconnect_delay)

    def _run_once(self):
        try:
            ctrl = socket.create_connection((self.server, self.tunnel_port), timeout=8)
        except socket.gaierror:
            raise RuntimeError("无法解析服务器地址 %r, 请检查填写的地址" % self.server)
        except ConnectionRefusedError:
            raise RuntimeError("连接被拒绝 %s:%d, 请确认服务器端已启动且端口一致" %
                               (self.server, self.tunnel_port))
        except socket.timeout:
            raise RuntimeError("连接服务器超时 %s:%d, 请检查防火墙/安全组" %
                               (self.server, self.tunnel_port))
        except OSError as e:
            raise RuntimeError("连接失败: %s" % e)
        self._ctrl = ctrl
        if not _auth(ctrl, self.secret):
            ctrl.close()
            self._ctrl = None
            raise RuntimeError("鉴权失败, 请检查密钥是否与服务端一致")
        log.info("[隧道] 已连接服务器控制通道")

        def heartbeat():
            while not self._stop.is_set():
                time.sleep(30)
                try:
                    ctrl.sendall(b"H")
                except Exception:
                    break

        threading.Thread(target=heartbeat, daemon=True).start()
        try:
            ctrl.settimeout(1.0)
            while not self._stop.is_set():
                try:
                    cmd = ctrl.recv(1)
                except socket.timeout:
                    continue
                except Exception:
                    break
                if not cmd:
                    raise ConnectionError("控制通道已断开")
                if cmd == b"C":
                    threading.Thread(target=self._handle_connect,
                                     daemon=True).start()
                else:
                    log.warning("[隧道] 未知指令: %r", cmd)
        finally:
            try:
                ctrl.close()
            except Exception:
                pass
            self._ctrl = None

    def _handle_connect(self):
        if self._stop.is_set():
            return
        # 1. 连接本机 WPS 面板(重试几次, 防止服务刚启动)
        local = None
        for i in range(5):
            try:
                local = socket.create_connection(("127.0.0.1", self.local_port), timeout=5)
                break
            except Exception:
                log.warning("[隧道] 连接本机面板(127.0.0.1:%d)失败, 第 %d/5 次, 1秒后重试...",
                            self.local_port, i + 1)
                time.sleep(1)
        if local is None:
            log.error("[隧道] 本机面板连接失败, 请确认 WPS 服务已启动")
            return
        # 2. 拨号到服务器数据通道
        try:
            data = socket.create_connection((self.server, self.data_port), timeout=15)
        except Exception as e:
            log.error("[隧道] 连接服务器数据通道失败: %s", e)
            local.close()
            return
        if not _auth(data, self.secret):
            data.close()
            local.close()
            return
        log.info("[隧道] 数据通道已建立, 开始转发")
        _pipe_sockets(local, data)
