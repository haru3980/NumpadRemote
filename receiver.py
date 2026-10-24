#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
テンキーレシーバー (Windows 用)
Android アプリ「テンキーリモート」からのキー/マウス操作を受け取り、Windows に入力します。
標準ライブラリのみで動作します (Python 3.8+)。

  python receiver.py            ... GUI で起動
  python receiver.py --nogui    ... コンソールで起動
"""
import argparse
import hmac
import json
import os
import random
import socket
import struct
import sys
import threading
import time
from urllib.parse import unquote_plus

TCP_PORT = 52110
DISCOVERY_PORT = 52111
IS_WIN = os.name == "nt"

# --------------------------------------------------------------------------
# 入力の注入 (Windows: SendInput / それ以外: ダミー)
# --------------------------------------------------------------------------
EXT_KEYS = {0x6F, 0x90, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
            0x2D, 0x2E, 0x5B, 0x5C, 0xA3, 0xA5}

if IS_WIN:
    import ctypes
    from ctypes import wintypes

    INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
    KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x1, 0x2, 0x4
    MOUSEEVENTF_MOVE, MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x1, 0x800, 0x1000
    BTN_FLAGS = {"L": (0x2, 0x4), "R": (0x8, 0x10), "M": (0x20, 0x40)}

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD)]

    class _UNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _UNION)]

    _user32 = ctypes.windll.user32
    _user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    _user32.SendInput.restype = wintypes.UINT
    try:
        _user32.SetProcessDPIAware()
    except Exception:
        pass

    class WinInput:
        def _send(self, items):
            if not items:
                return
            arr = (INPUT * len(items))(*items)
            _user32.SendInput(len(items), arr, ctypes.sizeof(INPUT))

        @staticmethod
        def _kbd(vk, scan, flags):
            i = INPUT()
            i.type = INPUT_KEYBOARD
            i.u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
            return i

        @staticmethod
        def _mouse(flags, dx=0, dy=0, data=0):
            i = INPUT()
            i.type = INPUT_MOUSE
            i.u.mi = MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
            return i

        def key(self, vk, down):
            ext = bool(vk & 0x100)
            vk &= 0xFF
            if vk in EXT_KEYS:
                ext = True
            scan = _user32.MapVirtualKeyW(vk, 0)
            flags = (KEYEVENTF_EXTENDEDKEY if ext else 0) | (0 if down else KEYEVENTF_KEYUP)
            self._send([self._kbd(vk, scan, flags)])

        def move(self, dx, dy):
            self._send([self._mouse(MOUSEEVENTF_MOVE, dx, dy)])

        def button(self, b, down):
            f = BTN_FLAGS[b][0 if down else 1]
            self._send([self._mouse(f)])

        def wheel(self, units):
            self._send([self._mouse(MOUSEEVENTF_WHEEL, 0, 0, units)])

        def hwheel(self, units):
            self._send([self._mouse(MOUSEEVENTF_HWHEEL, 0, 0, units)])

        def text(self, s):
            items = []
            raw = s.replace("\r", "").encode("utf-16-le")
            for (u,) in struct.iter_unpack("<H", raw):
                if u == 10:
                    items.append(self._kbd(0x0D, 0, 0))
                    items.append(self._kbd(0x0D, 0, KEYEVENTF_KEYUP))
                elif u == 9:
                    items.append(self._kbd(0x09, 0, 0))
                    items.append(self._kbd(0x09, 0, KEYEVENTF_KEYUP))
                else:
                    items.append(self._kbd(0, u, KEYEVENTF_UNICODE))
                    items.append(self._kbd(0, u, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
            self._send(items)


class DummyInput:
    """Windows 以外 (テスト用)。受け取った操作を記録するだけ。"""
    def __init__(self):
        self.events = []

    def key(self, vk, down): self.events.append(("key", vk, down))
    def move(self, dx, dy): self.events.append(("move", dx, dy))
    def button(self, b, down): self.events.append(("btn", b, down))
    def wheel(self, u): self.events.append(("wheel", u))
    def hwheel(self, u): self.events.append(("hwheel", u))
    def text(self, s): self.events.append(("text", s))


# --------------------------------------------------------------------------
# サーバ
# --------------------------------------------------------------------------
def local_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(ai[4][0])
    except Exception:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


class Server:
    def __init__(self, backend, pin, port=TCP_PORT, discovery_port=DISCOVERY_PORT, log=print):
        self.backend = backend
        self.pin = str(pin)
        self.port = port
        self.discovery_port = discovery_port
        self.log = log
        self.clients = {}          # addr -> name
        self.lock = threading.Lock()
        self.fails = {}            # ip -> (count, until)
        self.running = False
        self._tcp = None
        self._udp = None

    # -- 起動/停止
    def start(self):
        self.running = True
        self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._tcp.bind(("", self.port))
        self.port = self._tcp.getsockname()[1]
        self._tcp.listen(5)
        threading.Thread(target=self._accept_loop, daemon=True).start()
        try:
            self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._udp.bind(("", self.discovery_port))
            threading.Thread(target=self._udp_loop, daemon=True).start()
        except OSError as e:
            self.log("自動検索用ポートを開けませんでした: %s" % e)

    def stop(self):
        self.running = False
        for s in (self._tcp, self._udp):
            try:
                if s:
                    s.close()
            except Exception:
                pass

    # -- 自動検索 (UDP)
    def _udp_loop(self):
        name = socket.gethostname()
        while self.running:
            try:
                data, addr = self._udp.recvfrom(256)
            except OSError:
                break
            if data.strip() == b"NUMPAD_DISCOVER":
                msg = ("NUMPAD_HERE|%s|%d" % (name, self.port)).encode("utf-8")
                try:
                    self._udp.sendto(msg, addr)
                except OSError:
                    pass

    # -- TCP
    def _accept_loop(self):
        while self.running:
            try:
                conn, addr = self._tcp.accept()
            except OSError:
                break
            threading.Thread(target=self._client, args=(conn, addr), daemon=True).start()

    def _locked_out(self, ip):
        c, until = self.fails.get(ip, (0, 0))
        return c >= 5 and time.time() < until

    def _client(self, conn, addr):
        ip = addr[0]
        keys, btns = set(), set()
        key = "%s:%d" % addr
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if self._locked_out(ip):
                conn.sendall(b"NG\n")
                return
            conn.settimeout(10)
            f = conn.makefile("rb")
            line = f.readline(512).decode("utf-8", "replace").strip()
            parts = line.split(" ")
            ok = (len(parts) >= 2 and parts[0] == "HELLO" and
                  hmac.compare_digest(unquote_plus(parts[1]).encode(), self.pin.encode()))
            if not ok:
                c, _ = self.fails.get(ip, (0, 0))
                self.fails[ip] = (c + 1, time.time() + 60)
                self.log("認証失敗: %s" % ip)
                time.sleep(1)
                conn.sendall(b"NG\n")
                return
            self.fails.pop(ip, None)
            name = unquote_plus(parts[2]) if len(parts) > 2 else ip
            conn.sendall(b"OK\n")
            conn.settimeout(30)      # クライアントは2秒毎に PING を送る
            with self.lock:
                self.clients[key] = "%s (%s)" % (name, ip)
            self.log("接続: %s (%s)" % (name, ip))
            while self.running:
                raw = f.readline(4096)
                if not raw:
                    break
                try:
                    self._dispatch(raw.decode("utf-8", "replace").rstrip("\r\n"), keys, btns)
                except (ValueError, KeyError, IndexError):
                    pass
        except OSError:
            pass
        finally:
            for vk in list(keys):
                self._safe(self.backend.key, vk, False)
            for b in list(btns):
                self._safe(self.backend.button, b, False)
            with self.lock:
                had = self.clients.pop(key, None)
            if had:
                self.log("切断: %s" % had)
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _safe(fn, *a):
        try:
            fn(*a)
        except Exception:
            pass

    def _dispatch(self, line, keys, btns):
        if not line:
            return
        cmd, _, arg = line.partition(" ")
        b = self.backend
        if cmd == "M":
            dx, dy = (int(v) for v in arg.split())
            b.move(max(-3000, min(3000, dx)), max(-3000, min(3000, dy)))
        elif cmd in ("KD", "KU", "KT"):
            vk = int(arg)
            if not 0 <= vk <= 0x1FF:
                return
            if cmd == "KD":
                keys.add(vk); b.key(vk, True)
            elif cmd == "KU":
                keys.discard(vk); b.key(vk, False)
            else:
                b.key(vk, True); b.key(vk, False)
        elif cmd in ("BD", "BU", "BC"):
            btn = arg.strip()
            if btn not in ("L", "R", "M"):
                return
            if cmd == "BD":
                btns.add(btn); b.button(btn, True)
            elif cmd == "BU":
                btns.discard(btn); b.button(btn, False)
            else:
                b.button(btn, True); b.button(btn, False)
        elif cmd == "W":
            b.wheel(max(-12000, min(12000, int(arg))))
        elif cmd == "HW":
            b.hwheel(max(-12000, min(12000, int(arg))))
        elif cmd == "T":
            b.text(unquote_plus(arg)[:2000])
        # PING などは無視


# --------------------------------------------------------------------------
# 設定
# --------------------------------------------------------------------------
def config_path():
    if IS_WIN:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "NumpadRemote")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "config.json")
    return os.path.expanduser("~/.numpad_remote.json")


def load_config():
    try:
        with open(config_path(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(config_path(), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
    except Exception:
        pass


def new_pin():
    return "%06d" % random.SystemRandom().randrange(1000000)


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
def run_gui(server, cfg):
    import tkinter as tk
    from tkinter import scrolledtext

    logs = []
    server.log = lambda m: logs.append("%s  %s" % (time.strftime("%H:%M:%S"), m))

    root = tk.Tk()
    root.title("テンキーレシーバー")
    root.geometry("460x420")

    tk.Label(root, text="スマホのアプリに下の情報を入力してください",
             font=("Meiryo UI", 10)).pack(pady=(12, 4))
    ip_var = tk.StringVar()
    tk.Label(root, textvariable=ip_var, font=("Consolas", 14)).pack()
    pin_var = tk.StringVar()
    tk.Label(root, text="PIN", font=("Meiryo UI", 9)).pack(pady=(10, 0))
    tk.Label(root, textvariable=pin_var, font=("Consolas", 34, "bold")).pack()
    status_var = tk.StringVar()
    tk.Label(root, textvariable=status_var, font=("Meiryo UI", 11), fg="#1565C0").pack(pady=6)

    def regen():
        cfg["pin"] = new_pin()
        server.pin = cfg["pin"]
        save_config(cfg)
        pin_var.set(server.pin)
        server.log("PINを再生成しました")

    tk.Button(root, text="PINを再生成", command=regen).pack()
    box = scrolledtext.ScrolledText(root, height=8, state="disabled", font=("Meiryo UI", 9))
    box.pack(fill="both", expand=True, padx=10, pady=10)

    def refresh():
        ip_var.set("   ".join("%s" % ip for ip in local_ips()) + "\nポート %d" % server.port)
        pin_var.set(server.pin)
        with server.lock:
            names = list(server.clients.values())
        status_var.set("接続中: " + ", ".join(names) if names else "待機中…")
        if logs:
            box.configure(state="normal")
            box.insert("end", "\n".join(logs) + "\n")
            box.see("end")
            box.configure(state="disabled")
            del logs[:]
        root.after(700, refresh)

    def on_close():
        server.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh()
    root.mainloop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nogui", action="store_true")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--pin", default=None)
    args = ap.parse_args()

    cfg = load_config()
    if args.pin:
        cfg["pin"] = args.pin
    if not cfg.get("pin"):
        cfg["pin"] = new_pin()
    if args.port:
        cfg["port"] = args.port
    cfg.setdefault("port", TCP_PORT)
    save_config(cfg)

    backend = WinInput() if IS_WIN else DummyInput()
    if not IS_WIN:
        print("(Windows 以外ではダミー動作です)")
    server = Server(backend, cfg["pin"], cfg["port"])
    try:
        server.start()
    except OSError as e:
        print("ポートを開けません: %s" % e)
        sys.exit(1)

    if args.nogui:
        print("IP: %s  ポート: %d  PIN: %s" % (", ".join(local_ips()), server.port, server.pin))
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            server.stop()
    else:
        run_gui(server, cfg)


if __name__ == "__main__":
    main()
