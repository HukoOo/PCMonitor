"""바탕화면 PC 모니터 위젯: CPU/RAM/GPU/디스크 사용량과 네트워크 대역폭·핑을 1초마다 표시한다.

조작: 왼쪽 드래그로 이동, 우클릭으로 메뉴(속도 측정, 항상 위, 투명도, 시작 시 실행, 종료).
"""
import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import winreg
from collections import deque

import psutil

try:
    import pynvml

    pynvml.nvmlInit()
    GPU = pynvml.nvmlDeviceGetHandleByIndex(0)
except Exception:
    GPU = None

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "PCMonitor"
PING_HOST = "8.8.8.8"
HISTORY = 60
UA = {"User-Agent": "Mozilla/5.0 PCMonitor"}

BG = "#111418"
GRAPH_BG = "#161a20"
TRACK = "#262b33"
FG = "#e6e8eb"
MUTED = "#8a9099"
C_CPU = "#4ea1ff"
C_RAM = "#b18cff"
C_GPU = "#4cd08a"
C_DOWN = "#36c5f0"
C_UP = "#f0a336"
WARN = "#f0a336"
CRIT = "#ff5c5c"


def level_color(pct, base):
    if pct >= 90:
        return CRIT
    if pct >= 75:
        return WARN
    return base


def fmt_rate(bytes_per_sec):
    mbps = bytes_per_sec * 8 / 1e6
    if mbps >= 100:
        return f"{mbps:.0f} Mbps"
    if mbps >= 1:
        return f"{mbps:.1f} Mbps"
    return f"{mbps * 1000:.0f} Kbps"


def fmt_bytes_rate(bytes_per_sec):
    mb = bytes_per_sec / 1024**2
    return f"{mb:.1f} MB/s" if mb >= 0.1 else f"{bytes_per_sec / 1024:.0f} KB/s"


def pick_nic():
    """기본 인터넷 어댑터 추정: 켜져 있는 어댑터 중 누적 수신량이 가장 큰 것."""
    stats = psutil.net_if_stats()
    counters = psutil.net_io_counters(pernic=True)
    candidates = [n for n, s in stats.items() if s.isup and n in counters and "loopback" not in n.lower()]
    return max(candidates, key=lambda n: counters[n].bytes_recv, default=None)


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except OSError:
        pass


def startup_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
            return True
    except OSError:
        return False


def set_startup(enabled):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, f'"{pythonw}" "{os.path.abspath(__file__)}"')
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except OSError:
                pass


class PingMonitor(threading.Thread):
    """2초마다 ping 한 번, 최근 30회(약 1분) 결과로 지연·지터·손실률을 낸다."""

    def __init__(self, host):
        super().__init__(daemon=True)
        self.host = host
        self.results = deque(maxlen=30)

    def run(self):
        while True:
            start = time.time()
            self.results.append(self._ping())
            time.sleep(max(0.0, 2 - (time.time() - start)))

    def _ping(self):
        try:
            out = subprocess.run(
                ["ping", "-n", "1", "-w", "1000", self.host],
                capture_output=True, text=True, timeout=3,
                creationflags=subprocess.CREATE_NO_WINDOW,
            ).stdout
        except Exception:
            return None
        m = re.search(r"[=<](\d+)ms", out)
        return int(m.group(1)) if m else None

    def stats(self):
        results = list(self.results)
        if not results:
            return None
        ok = [v for v in results if v is not None]
        loss = 100 * (len(results) - len(ok)) / len(results)
        jitter = sum(abs(a - b) for a, b in zip(ok, ok[1:])) / (len(ok) - 1) if len(ok) > 1 else 0
        return results[-1], loss, jitter


class SpeedTest:
    """Cloudflare 속도 측정 서버로 다운로드 25MB, 업로드 10MB를 보내 실측한다."""

    def __init__(self):
        self.status = "클릭해서 측정"
        self.running = False

    def start(self):
        if self.running:
            return
        self.running = True
        self.status = "다운로드 측정 중..."
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            down = self._download()
            self.status = f"↓ {down:.0f} Mbps · 업로드 측정 중..."
            up = self._upload()
            self.status = f"↓ {down:.0f} Mbps  ↑ {up:.0f} Mbps  ({time.strftime('%H:%M')})"
        except Exception as e:
            self.status = f"측정 실패 ({e.__class__.__name__})"
        finally:
            self.running = False

    def _download(self):
        req = urllib.request.Request("https://speed.cloudflare.com/__down?bytes=25000000", headers=UA)
        start = time.perf_counter()
        total = 0
        with urllib.request.urlopen(req, timeout=20) as r:
            while chunk := r.read(65536):
                total += len(chunk)
        return total * 8 / (time.perf_counter() - start) / 1e6

    def _upload(self):
        data = bytes(10_000_000)
        req = urllib.request.Request("https://speed.cloudflare.com/__up", data=data, headers=UA, method="POST")
        start = time.perf_counter()
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        return len(data) * 8 / (time.perf_counter() - start) / 1e6


class Monitor:
    def __init__(self):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        self.cfg = load_config()
        self.root = root = tk.Tk()
        self.scale = root.winfo_fpixels("1i") / 96
        self.w = self.px(300)
        self.pad = self.px(14)

        root.overrideredirect(True)
        root.configure(bg=BG)
        root.geometry(f"+{self.cfg.get('x', 40)}+{self.cfg.get('y', 40)}")
        self.topmost = tk.BooleanVar(value=self.cfg.get("topmost", False))
        self.locked = tk.BooleanVar(value=self.cfg.get("locked", False))
        self.alpha = tk.DoubleVar(value=self.cfg.get("alpha", 0.9))
        self.startup = tk.BooleanVar(value=startup_enabled())
        root.attributes("-topmost", self.topmost.get())
        root.attributes("-alpha", self.alpha.get())

        self.c = tk.Canvas(root, width=self.w, height=self.px(400), bg=BG, highlightthickness=0)
        self.c.pack()
        self.f_title = ("Malgun Gothic", 9, "bold")
        self.f_label = ("Malgun Gothic", 9)
        self.f_value = ("Malgun Gothic", 10, "bold")
        self.f_big = ("Malgun Gothic", 13, "bold")
        self.f_small = ("Malgun Gothic", 8)

        self.menu = tk.Menu(root, tearoff=0)
        self.menu.add_command(label="인터넷 속도 측정", command=self.run_speedtest)
        self.menu.add_separator()
        self.menu.add_checkbutton(label="항상 위에 표시", variable=self.topmost, command=self.apply_topmost)
        self.menu.add_checkbutton(label="위치 고정 (드래그 잠금)", variable=self.locked, command=self.save)
        alpha_menu = tk.Menu(self.menu, tearoff=0)
        for a in (0.6, 0.75, 0.9, 1.0):
            alpha_menu.add_radiobutton(label=f"{int(a * 100)}%", variable=self.alpha, value=a, command=self.apply_alpha)
        self.menu.add_cascade(label="불투명도", menu=alpha_menu)
        self.menu.add_checkbutton(label="Windows 시작 시 실행", variable=self.startup, command=self.apply_startup)
        self.menu.add_separator()
        self.menu.add_command(label="종료", command=self.quit)

        self.dragging = False
        self.pressed = False
        self.c.bind("<ButtonPress-1>", self.press)
        self.c.bind("<B1-Motion>", self.drag_move)
        self.c.bind("<ButtonRelease-1>", self.release)
        self.c.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))
        self.c.tag_bind("speedtest", "<Button-1>", lambda e: self.run_speedtest())

        root.update_idletasks()
        self.hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        try:  # Windows 11 둥근 모서리
            ctypes.windll.dwmapi.DwmSetWindowAttribute(self.hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4)
        except Exception:
            pass

        self.nic = pick_nic()
        self.last_net = psutil.net_io_counters(pernic=True).get(self.nic)
        self.last_disk = psutil.disk_io_counters()
        self.last_t = time.perf_counter()
        self.down_hist = deque(maxlen=HISTORY)
        self.up_hist = deque(maxlen=HISTORY)
        psutil.cpu_percent(None)

        self.ping = PingMonitor(PING_HOST)
        self.ping.start()
        self.speedtest = SpeedTest()

    def px(self, v):
        return int(v * self.scale)

    # ---- 창 조작 ----
    def press(self, e):
        # 누르고 있는 동안은 맨 앞으로
        self.pressed = True
        self.root.attributes("-topmost", True)
        self.drag_start(e)

    def release(self, e):
        self.pressed = False
        self.drag_end(e)
        self.root.attributes("-topmost", self.topmost.get())
        if not self.topmost.get():
            self.send_to_back()

    def send_to_back(self):
        # 다른 창들 뒤(바탕화면 위)로 보낸다. SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
        ctypes.windll.user32.SetWindowPos(self.hwnd, 1, 0, 0, 0, 0, 0x0013)

    def drag_start(self, e):
        if self.locked.get():
            return
        self.dragging = True
        self.drag_off = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def drag_move(self, e):
        if self.dragging:
            self.root.geometry(f"+{e.x_root - self.drag_off[0]}+{e.y_root - self.drag_off[1]}")

    def drag_end(self, e):
        if self.dragging:
            self.dragging = False
            self.save()

    def apply_topmost(self):
        self.root.attributes("-topmost", self.topmost.get())
        self.save()

    def apply_alpha(self):
        self.root.attributes("-alpha", self.alpha.get())
        self.save()

    def apply_startup(self):
        set_startup(self.startup.get())

    def run_speedtest(self):
        self.speedtest.start()

    def save(self):
        self.cfg.update(x=self.root.winfo_x(), y=self.root.winfo_y(),
                        topmost=self.topmost.get(), locked=self.locked.get(), alpha=self.alpha.get())
        save_config(self.cfg)

    def quit(self):
        self.save()
        self.root.destroy()

    # ---- 측정 ----
    def sample(self):
        now = time.perf_counter()
        dt = max(now - self.last_t, 1e-3)
        self.last_t = now

        net = psutil.net_io_counters(pernic=True).get(self.nic)
        if net is None:
            self.nic = pick_nic()
            net = psutil.net_io_counters(pernic=True).get(self.nic)
        down = up = 0.0
        if net and self.last_net:
            down = max(0, net.bytes_recv - self.last_net.bytes_recv) / dt
            up = max(0, net.bytes_sent - self.last_net.bytes_sent) / dt
        self.last_net = net
        self.down_hist.append(down)
        self.up_hist.append(up)

        disk = psutil.disk_io_counters()
        disk_r = disk_w = 0.0
        if disk and self.last_disk:
            disk_r = (disk.read_bytes - self.last_disk.read_bytes) / dt
            disk_w = (disk.write_bytes - self.last_disk.write_bytes) / dt
        self.last_disk = disk

        m = dict(cpu=psutil.cpu_percent(None), procs=len(psutil.pids()), vm=psutil.virtual_memory(),
                 down=down, up=up, disk_r=disk_r, disk_w=disk_w, gpu=None)
        if GPU is not None:
            try:
                mem = pynvml.nvmlDeviceGetMemoryInfo(GPU)
                m["gpu"] = dict(
                    util=pynvml.nvmlDeviceGetUtilizationRates(GPU).gpu,
                    mem_used=mem.used, mem_total=mem.total,
                    temp=pynvml.nvmlDeviceGetTemperature(GPU, pynvml.NVML_TEMPERATURE_GPU),
                    power=pynvml.nvmlDeviceGetPowerUsage(GPU) / 1000,
                )
            except Exception:
                pass
        return m

    def tick(self):
        self.draw(self.sample())
        if not self.topmost.get() and not self.pressed:
            self.send_to_back()
        self.root.after(1000, self.tick)

    # ---- 그리기 ----
    def meter(self, y, label, value, pct, color, sub=None):
        c, p, w, px = self.c, self.pad, self.w, self.px
        c.create_text(p, y, text=label, fill=FG, font=self.f_label, anchor="w")
        c.create_text(w - p, y, text=value, fill=FG, font=self.f_value, anchor="e")
        y += px(12)
        c.create_rectangle(p, y, w - p, y + px(5), fill=TRACK, width=0)
        fill = (w - 2 * p) * max(0, min(pct, 100)) / 100
        if fill > 0:
            c.create_rectangle(p, y, p + fill, y + px(5), fill=level_color(pct, color), width=0)
        y += px(10)
        if sub:
            c.create_text(p, y, text=sub, fill=MUTED, font=self.f_small, anchor="nw")
            y += px(16)
        return y + px(10)

    def sparkline(self, y, h):
        c, p, w, px = self.c, self.pad, self.w, self.px
        x0, x1 = p, w - p
        c.create_rectangle(x0, y, x1, y + h, fill=GRAPH_BG, width=0)
        peak = max(max(self.down_hist, default=0), max(self.up_hist, default=0), 125_000)  # 최소 1 Mbps 눈금
        step = (x1 - x0) / (HISTORY - 1)
        for hist, color in ((self.up_hist, C_UP), (self.down_hist, C_DOWN)):
            if len(hist) < 2:
                continue
            offset = HISTORY - len(hist)
            pts = []
            for i, v in enumerate(hist):
                pts += [x0 + (offset + i) * step, y + h - 1 - (v / peak) * (h - px(4))]
            c.create_line(*pts, fill=color, width=max(1, px(1.5)))
        c.create_text(x1 - px(4), y + px(3), text=fmt_rate(peak), fill=MUTED, font=self.f_small, anchor="ne")
        c.create_text(x0 + px(4), y + h - px(3), text="최근 60초", fill=MUTED, font=self.f_small, anchor="sw")
        return y + h

    def draw(self, m):
        c, p, w, px = self.c, self.pad, self.w, self.px
        c.delete("all")
        y = px(12)
        c.create_text(p, y, text="PC MONITOR", fill=MUTED, font=self.f_title, anchor="nw")
        c.create_text(w - p, y, text=time.strftime("%H:%M:%S"), fill=MUTED, font=self.f_title, anchor="ne")
        y += px(32)

        y = self.meter(y, "CPU", f"{m['cpu']:.0f}%", m["cpu"], C_CPU,
                       f"{psutil.cpu_count()} 스레드 · 프로세스 {m['procs']}개")
        vm = m["vm"]
        y = self.meter(y, "RAM", f"{vm.percent:.0f}%", vm.percent, C_RAM,
                       f"{vm.used / 1024**3:.1f} / {vm.total / 1024**3:.1f} GB")
        g = m["gpu"]
        if g:
            vram_pct = 100 * g["mem_used"] / g["mem_total"]
            y = self.meter(y, "GPU", f"{g['util']}%", g["util"], C_GPU,
                           f"{g['temp']}°C · {g['power']:.0f} W")
            y = self.meter(y, "VRAM", f"{vram_pct:.0f}%", vram_pct, C_GPU,
                           f"{g['mem_used'] / 1024**3:.1f} / {g['mem_total'] / 1024**3:.1f} GB")
        else:
            c.create_text(p, y, text="GPU 정보 없음", fill=MUTED, font=self.f_small, anchor="w")
            y += px(20)

        c.create_text(p, y, text="디스크", fill=FG, font=self.f_label, anchor="w")
        c.create_text(w - p, y, text=f"R {fmt_bytes_rate(m['disk_r'])}   W {fmt_bytes_rate(m['disk_w'])}",
                      fill=FG, font=self.f_label, anchor="e")
        y += px(18)

        c.create_line(p, y, w - p, y, fill=TRACK)
        y += px(10)
        c.create_text(p, y, text=f"네트워크 · {self.nic or '-'}", fill=MUTED, font=self.f_small, anchor="nw")
        y += px(18)
        c.create_text(p, y, text="↓", fill=C_DOWN, font=self.f_big, anchor="nw")
        c.create_text(p + px(18), y, text=fmt_rate(m["down"]), fill=FG, font=self.f_big, anchor="nw")
        c.create_text(w / 2 + px(6), y, text="↑", fill=C_UP, font=self.f_big, anchor="nw")
        c.create_text(w / 2 + px(24), y, text=fmt_rate(m["up"]), fill=FG, font=self.f_big, anchor="nw")
        y += px(30)
        y = self.sparkline(y, px(46)) + px(14)

        st = self.ping.stats()
        if st is None:
            ping_text, ping_color = "Ping 측정 중...", MUTED
        else:
            last, loss, jitter = st
            last_text = "시간 초과" if last is None else f"{last} ms"
            ping_text = f"Ping {last_text} · 지터 {jitter:.0f} ms · 손실 {loss:.0f}%"
            if loss >= 5 or last is None:
                ping_color = CRIT
            elif loss > 0 or (last or 0) > 100:
                ping_color = WARN
            else:
                ping_color = C_GPU
        c.create_oval(p, y - px(3), p + px(6), y + px(3), fill=ping_color, width=0)
        c.create_text(p + px(12), y, text=ping_text, fill=FG, font=self.f_label, anchor="w")
        y += px(22)

        c.create_text(p, y, text=f"속도 측정: {self.speedtest.status}", fill=MUTED, font=self.f_small,
                      anchor="w", tags="speedtest")
        y += px(18)

        c.config(height=y)

    def run(self):
        self.tick()
        self.root.mainloop()


if __name__ == "__main__":
    # 중복 실행 방지
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _mutex = kernel32.CreateMutexW(None, False, "PCMonitorWidgetMutex")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        sys.exit(0)
    Monitor().run()
