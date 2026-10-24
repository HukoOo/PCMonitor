"""바탕화면 PC 모니터 위젯: CPU/RAM/GPU/디스크 사용량과 네트워크 대역폭·핑을 1초마다 표시한다.

조작: 왼쪽 드래그로 이동, 우클릭으로 메뉴(속도 측정, 항상 위, 투명도, 시작 시 실행, 종료).
오른쪽 위 » 아이콘을 누르면 옆으로 펼쳐져 항목별 상위 프로세스 순위를 보여준다.
앱별 네트워크 대역폭은 관리자 권한으로 실행했을 때만 보인다(ETW 사용). 아니면 앱별 TCP 연결 수를 보여준다.
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.request
import uuid
import winreg
from collections import deque
from xml.sax.saxutils import escape

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
TASK_NAME = "PCMonitor"
PING_HOST = "8.8.8.8"
HISTORY = 60
TOP_N = 5
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


def mix(c1, c2, t):
    """두 #rrggbb 색을 t(0~1) 비율로 섞는다. tk 캔버스는 반투명이 없어서 어두운 막대색을 이렇게 만든다."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


_user32 = ctypes.windll.user32
_user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
_user32.MonitorFromPoint.restype = wt.HMONITOR
_user32.GetMonitorInfoW.argtypes = [wt.HMONITOR, ctypes.POINTER(MONITORINFO)]


def monitor_info(x, y):
    """(x, y)가 있는 모니터의 전체 영역(rcMonitor)과 작업 영역(rcWork, 작업 표시줄 제외)."""
    mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    _user32.GetMonitorInfoW(_user32.MonitorFromPoint(wt.POINT(x, y), 2), ctypes.byref(mi))  # DEFAULTTONEAREST
    return mi


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wt.USHORT), ("MaximumLength", wt.USHORT), ("Buffer", ctypes.c_void_p)]


class SYSTEM_PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("NextEntryOffset", wt.ULONG), ("NumberOfThreads", wt.ULONG),
        ("WorkingSetPrivateSize", ctypes.c_longlong), ("HardFaultCount", wt.ULONG),
        ("NumberOfThreadsHighWatermark", wt.ULONG), ("CycleTime", ctypes.c_ulonglong),
        ("CreateTime", ctypes.c_longlong), ("UserTime", ctypes.c_longlong), ("KernelTime", ctypes.c_longlong),
        ("ImageName", UNICODE_STRING), ("BasePriority", wt.LONG),
        ("UniqueProcessId", ctypes.c_void_p), ("InheritedFromUniqueProcessId", ctypes.c_void_p),
        ("HandleCount", wt.ULONG), ("SessionId", wt.ULONG), ("UniqueProcessKey", ctypes.c_size_t),
        ("PeakVirtualSize", ctypes.c_size_t), ("VirtualSize", ctypes.c_size_t), ("PageFaultCount", wt.ULONG),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivatePageCount", ctypes.c_size_t),
        ("ReadOperationCount", ctypes.c_longlong), ("WriteOperationCount", ctypes.c_longlong),
        ("OtherOperationCount", ctypes.c_longlong), ("ReadTransferCount", ctypes.c_longlong),
        ("WriteTransferCount", ctypes.c_longlong), ("OtherTransferCount", ctypes.c_longlong),
    ]


_ntdll = ctypes.windll.ntdll
_ntdll.NtQuerySystemInformation.argtypes = [wt.ULONG, ctypes.c_void_p, wt.ULONG, ctypes.POINTER(wt.ULONG)]
_ntdll.NtQuerySystemInformation.restype = ctypes.c_long


def process_snapshot():
    """모든 프로세스의 (pid, 이름, CPU 시간[100ns], 전용 작업 집합, 누적 읽기+쓰기 바이트)를 한 번에 읽는다.

    psutil은 권한이 없는 프로세스마다 이 표 전체를 다시 조회해 1초에 몇 초씩 걸리므로 직접 한 번만 부른다.
    """
    size = wt.ULONG(1 << 20)
    while True:
        buf = ctypes.create_string_buffer(size.value)
        status = _ntdll.NtQuerySystemInformation(5, buf, size, ctypes.byref(size))  # SystemProcessInformation
        if status == 0:
            break
        if status & 0xFFFFFFFF != 0xC0000004:  # STATUS_INFO_LENGTH_MISMATCH 외에는 포기
            return []
        size.value += 1 << 16
    base, off, out = ctypes.addressof(buf), 0, []
    while True:
        e = SYSTEM_PROCESS_INFORMATION.from_address(base + off)
        pid = e.UniqueProcessId or 0
        if pid:  # 0은 System Idle Process
            name = ctypes.wstring_at(e.ImageName.Buffer, e.ImageName.Length // 2) if e.ImageName.Buffer else "?"
            out.append((pid, name.removesuffix(".exe"), e.UserTime + e.KernelTime, e.WorkingSetPrivateSize,
                        e.ReadTransferCount + e.WriteTransferCount))
        if not e.NextEntryOffset:
            return out
        off += e.NextEntryOffset


def top(d):
    return sorted(((k, v) for k, v in d.items() if v > 0), key=lambda kv: -kv[1])[:TOP_N]


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


def is_admin():
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def pythonw_path():
    return os.path.join(os.path.dirname(sys.executable), "pythonw.exe")


def schtasks(*args):
    return subprocess.run(["schtasks", *args], capture_output=True,
                          creationflags=subprocess.CREATE_NO_WINDOW).returncode == 0


def run_key_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
            return True
    except OSError:
        return False


def startup_enabled():
    return run_key_enabled() or schtasks("/Query", "/TN", TASK_NAME)


def set_startup(enabled):
    """관리자 권한이면 작업 스케줄러(가장 높은 권한으로 실행)에, 아니면 Run 레지스트리에 등록한다.

    Run 키로 시작한 프로그램은 관리자 권한을 받을 수 없어 앱별 대역폭을 못 잰다.
    """
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        try:
            winreg.DeleteValue(k, RUN_NAME)
        except OSError:
            pass
    schtasks("/Delete", "/TN", TASK_NAME, "/F")
    if not enabled:
        return
    if not is_admin():
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, f'"{pythonw_path()}" "{os.path.abspath(__file__)}"')
        return
    # schtasks 기본값은 배터리일 때 시작 안 함·3일 뒤 강제 종료라서 XML로 설정을 직접 준다
    user = escape(f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}")
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(pythonw_path())}</Command><Arguments>"{escape(os.path.abspath(__file__))}"</Arguments></Exec></Actions>
</Task>"""
    fd, path = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as f:
            f.write(xml)
        schtasks("/Create", "/TN", TASK_NAME, "/XML", path, "/F")
    finally:
        os.remove(path)


# ---- ETW: 앱별 네트워크 송수신량 (관리자 권한 필요) ----
class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]


class WNODE_HEADER(ctypes.Structure):
    _fields_ = [("BufferSize", wt.ULONG), ("ProviderId", wt.ULONG), ("HistoricalContext", ctypes.c_uint64),
                ("TimeStamp", ctypes.c_int64), ("Guid", GUID), ("ClientContext", wt.ULONG), ("Flags", wt.ULONG)]


class EVENT_TRACE_PROPERTIES(ctypes.Structure):
    _fields_ = [("Wnode", WNODE_HEADER), ("BufferSize", wt.ULONG), ("MinimumBuffers", wt.ULONG),
                ("MaximumBuffers", wt.ULONG), ("MaximumFileSize", wt.ULONG), ("LogFileMode", wt.ULONG),
                ("FlushTimer", wt.ULONG), ("EnableFlags", wt.ULONG), ("AgeLimit", wt.LONG),
                ("NumberOfBuffers", wt.ULONG), ("FreeBuffers", wt.ULONG), ("EventsLost", wt.ULONG),
                ("BuffersWritten", wt.ULONG), ("LogBuffersLost", wt.ULONG), ("RealTimeBuffersLost", wt.ULONG),
                ("LoggerThreadId", wt.HANDLE), ("LogFileNameOffset", wt.ULONG), ("LoggerNameOffset", wt.ULONG)]


class TRACE_PROPS(ctypes.Structure):
    _fields_ = [("p", EVENT_TRACE_PROPERTIES), ("name", ctypes.c_wchar * 64)]


class EVENT_HEADER(ctypes.Structure):
    _fields_ = [("Size", wt.USHORT), ("HeaderType", wt.USHORT), ("Flags", wt.USHORT), ("EventProperty", wt.USHORT),
                ("ThreadId", wt.ULONG), ("ProcessId", wt.ULONG), ("TimeStamp", ctypes.c_int64), ("ProviderId", GUID),
                ("Id", wt.USHORT), ("Version", ctypes.c_ubyte), ("Channel", ctypes.c_ubyte), ("Level", ctypes.c_ubyte),
                ("Opcode", ctypes.c_ubyte), ("Task", wt.USHORT), ("Keyword", ctypes.c_uint64),
                ("ProcessorTime", ctypes.c_uint64), ("ActivityId", GUID)]


class EVENT_RECORD(ctypes.Structure):
    _fields_ = [("EventHeader", EVENT_HEADER), ("ProcessorNumber", ctypes.c_ubyte), ("Alignment", ctypes.c_ubyte),
                ("LoggerId", wt.USHORT), ("ExtendedDataCount", wt.USHORT), ("UserDataLength", wt.USHORT),
                ("ExtendedData", ctypes.c_void_p), ("UserData", ctypes.c_void_p), ("UserContext", ctypes.c_void_p)]


class EVENT_TRACE_LOGFILEW(ctypes.Structure):
    # CurrentEvent(EVENT_TRACE)와 LogfileHeader(TRACE_LOGFILE_HEADER)는 쓰지 않아서 크기만 맞춘다(64비트 기준)
    _fields_ = [("LoggerName", ctypes.c_wchar_p), ("LogFileName", ctypes.c_wchar_p), ("CurrentTime", ctypes.c_int64),
                ("BuffersRead", wt.ULONG), ("ProcessTraceMode", wt.ULONG), ("CurrentEvent", ctypes.c_uint64 * 11),
                ("LogfileHeader", ctypes.c_uint64 * 35), ("BufferCallback", ctypes.c_void_p), ("BufferSize", wt.ULONG),
                ("Filled", wt.ULONG), ("EventsLost", wt.ULONG), ("EventRecordCallback", ctypes.c_void_p),
                ("IsKernelTrace", wt.ULONG), ("Context", ctypes.c_void_p)]


EVENT_RECORD_CALLBACK = ctypes.WINFUNCTYPE(None, ctypes.POINTER(EVENT_RECORD))
_advapi = ctypes.windll.advapi32
_advapi.StartTraceW.argtypes = [ctypes.POINTER(ctypes.c_uint64), wt.LPCWSTR, ctypes.c_void_p]
_advapi.ControlTraceW.argtypes = [ctypes.c_uint64, wt.LPCWSTR, ctypes.c_void_p, wt.ULONG]
_advapi.EnableTraceEx2.argtypes = [ctypes.c_uint64, ctypes.POINTER(GUID), wt.ULONG, ctypes.c_ubyte,
                                   ctypes.c_uint64, ctypes.c_uint64, wt.ULONG, ctypes.c_void_p]
_advapi.OpenTraceW.argtypes = [ctypes.POINTER(EVENT_TRACE_LOGFILEW)]
_advapi.OpenTraceW.restype = ctypes.c_uint64
_advapi.ProcessTrace.argtypes = [ctypes.POINTER(ctypes.c_uint64), wt.ULONG, ctypes.c_void_p, ctypes.c_void_p]

KERNEL_NETWORK = GUID.from_buffer_copy(uuid.UUID("7DD42A49-5329-4832-8DFD-43D979153A88").bytes_le)
# Microsoft-Windows-Kernel-Network 이벤트 ID → 0 수신 / 1 송신 (TCP·UDP × IPv4·IPv6). 페이로드는 PID, 크기 순.
NET_EVENTS = {11: 0, 27: 0, 43: 0, 59: 0, 10: 1, 26: 1, 42: 1, 58: 1}


class NetTrace(threading.Thread):
    """ETW 실시간 세션으로 프로세스별 송수신 바이트를 누적한다. 관리자 권한이 없으면 ok가 False로 남는다."""

    SESSION = "PCMonitorNet"

    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.bytes = {}  # pid -> [수신, 송신]
        self.ok = False
        self._cb = EVENT_RECORD_CALLBACK(self._on_event)

    def _props(self):
        tp = TRACE_PROPS()
        tp.p.Wnode.BufferSize = ctypes.sizeof(TRACE_PROPS)
        tp.p.Wnode.Flags = 0x20000  # WNODE_FLAG_TRACED_GUID
        tp.p.Wnode.ClientContext = 1  # QPC 타임스탬프
        tp.p.BufferSize = 64  # KB
        tp.p.LogFileMode = 0x100  # EVENT_TRACE_REAL_TIME_MODE
        tp.p.FlushTimer = 1  # 1초마다 버퍼를 넘겨 받는다
        tp.p.LoggerNameOffset = TRACE_PROPS.name.offset
        return tp

    def start(self):
        if not is_admin():
            return
        h = ctypes.c_uint64()
        rc = _advapi.StartTraceW(ctypes.byref(h), self.SESSION, ctypes.byref(self._props()))
        if rc == 183:  # ERROR_ALREADY_EXISTS: 강제 종료된 이전 실행이 남긴 세션
            self.stop()
            rc = _advapi.StartTraceW(ctypes.byref(h), self.SESSION, ctypes.byref(self._props()))
        if rc:
            return
        # EVENT_CONTROL_CODE_ENABLE_PROVIDER, 레벨 5, 키워드 IPv4(0x10) | IPv6(0x20)
        if _advapi.EnableTraceEx2(h, ctypes.byref(KERNEL_NETWORK), 1, 5, 0x30, 0, 0, None):
            self.stop()
            return
        self.ok = True
        super().start()

    def run(self):
        lf = EVENT_TRACE_LOGFILEW(LoggerName=self.SESSION, ProcessTraceMode=0x10000100,  # 실시간 | EVENT_RECORD
                                  EventRecordCallback=ctypes.cast(self._cb, ctypes.c_void_p))
        th = _advapi.OpenTraceW(ctypes.byref(lf))
        if th != 0xFFFFFFFFFFFFFFFF:
            _advapi.ProcessTrace(ctypes.byref(ctypes.c_uint64(th)), 1, None, None)
        self.ok = False  # 세션이 끝나면 연결 수 표시로 돌아간다

    def stop(self):
        _advapi.ControlTraceW(0, self.SESSION, ctypes.byref(self._props()), 1)  # EVENT_TRACE_CONTROL_STOP

    def take(self):
        """지난번 호출 이후 쌓인 pid별 [수신, 송신] 바이트를 가져가고 비운다."""
        with self.lock:
            b, self.bytes = self.bytes, {}
        return b

    def _on_event(self, rec):
        r = rec.contents
        kind = NET_EVENTS.get(r.EventHeader.Id)
        if kind is None or r.UserDataLength < 8:
            return
        pid, size = (ctypes.c_uint32 * 2).from_address(r.UserData)
        with self.lock:
            self.bytes.setdefault(pid, [0, 0])[kind] += size


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
        self.zoom = self.cfg.get("zoom", 1.0)  # 모서리 드래그로 바꾸는 배율. 모든 크기·글꼴에 곱한다.

        root.overrideredirect(True)
        root.configure(bg=BG)
        # main_x/main_y는 본체(기본 패널) 위치. 왼쪽으로 펼치면 창 자체는 그만큼 왼쪽에서 시작한다.
        self.main_x, self.main_y = self.cfg.get("x", 40), self.cfg.get("y", 40)
        self.expanded = False
        self.expand_left = False
        root.geometry(f"+{self.main_x}+{self.main_y}")
        self.topmost = tk.BooleanVar(value=self.cfg.get("topmost", False))
        self.locked = tk.BooleanVar(value=self.cfg.get("locked", False))
        self.alpha = tk.DoubleVar(value=self.cfg.get("alpha", 0.9))
        self.startup = tk.BooleanVar(value=startup_enabled())
        root.attributes("-topmost", self.topmost.get())
        root.attributes("-alpha", self.alpha.get())

        self.c = tk.Canvas(root, height=self.px(400), bg=BG, highlightthickness=0)
        self.c.pack(side="left", anchor="n")
        self.d = tk.Canvas(root, height=self.px(400), bg=BG, highlightthickness=0)
        self.apply_zoom()

        self.menu = tk.Menu(root, tearoff=0)
        self.menu.add_command(label="인터넷 속도 측정", command=self.run_speedtest)
        self.menu.add_separator()
        self.menu.add_checkbutton(label="항상 위에 표시", variable=self.topmost, command=self.apply_topmost)
        self.menu.add_checkbutton(label="위치 고정 (드래그 잠금)", variable=self.locked, command=self.apply_locked)
        alpha_menu = tk.Menu(self.menu, tearoff=0)
        for a in (0.6, 0.75, 0.9, 1.0):
            alpha_menu.add_radiobutton(label=f"{int(a * 100)}%", variable=self.alpha, value=a, command=self.apply_alpha)
        self.menu.add_cascade(label="불투명도", menu=alpha_menu)
        self.menu.add_checkbutton(label="Windows 시작 시 실행", variable=self.startup, command=self.apply_startup)
        if not is_admin():
            self.menu.add_command(label="관리자 권한으로 다시 실행 (앱별 대역폭)", command=self.restart_as_admin)
        self.menu.add_separator()
        self.menu.add_command(label="종료", command=self.quit)

        self.dragging = False
        self.resizing = False
        self.pressed = False
        self.last_m = None
        # 최상위 창에 걸면 두 캔버스와, 펼쳤을 때 본체 위에 남는 빈 바탕까지 모두 받는다
        root.bind("<ButtonPress-1>", self.press)
        root.bind("<B1-Motion>", self.drag_move)
        root.bind("<Motion>", self.hover)
        root.bind("<ButtonRelease-1>", self.release)
        root.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))
        self.c.tag_bind("speedtest", "<Button-1>", lambda e: self.run_speedtest())
        self.c.tag_bind("expand", "<ButtonRelease-1>", self.expand_click)

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
        self.net = NetTrace()
        self.net.start()
        if is_admin() and run_key_enabled():
            set_startup(True)  # Run 키 등록을 관리자 권한으로 뜨는 작업 스케줄러 등록으로 옮긴다

        self.details = None
        self.last_counts = {}
        self.last_detail_t = None
        self.gpu_ts = 0
        if self.cfg.get("expanded"):
            self.toggle_expand()

    def px(self, v):
        return int(v * self.scale * self.zoom)

    def apply_zoom(self):
        self.w = self.px(300)
        self.dw = self.px(260)
        self.pad = self.px(14)
        size = lambda pt: max(6, round(pt * self.zoom))  # 포인트 단위라 DPI 배율은 tk가 이미 반영한다
        self.f_title = ("Malgun Gothic", size(9), "bold")
        self.f_label = ("Malgun Gothic", size(9))
        self.f_value = ("Malgun Gothic", size(10), "bold")
        self.f_big = ("Malgun Gothic", size(13), "bold")
        self.f_small = ("Malgun Gothic", size(8))
        self.c.config(width=self.w)
        self.d.config(width=self.dw)

    # ---- 창 조작 ----
    def press(self, e):
        # 누르고 있는 동안은 맨 앞으로
        self.pressed = True
        self.press_pos = (e.x_root, e.y_root)
        self.root.attributes("-topmost", True)
        if self.in_grip(e):
            self.resizing = True
            self.resize_from = (e.x_root - self.main_x, self.zoom)
        else:
            self.drag_start(e)

    def release(self, e):
        self.pressed = False
        if self.resizing:
            self.resizing = False
            self.save()
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
        self.drag_off = (e.x_root - self.main_x, e.y_root - self.main_y)

    def in_grip(self, e):
        """본체 오른쪽 아래 모서리의 크기 조절 손잡이 위인지. 위치 고정 중엔 손잡이가 없다."""
        if self.locked.get():
            return False
        gx, gy = self.c.winfo_rootx() + self.c.winfo_width(), self.c.winfo_rooty() + self.c.winfo_height()
        return gx - self.px(16) <= e.x_root <= gx and gy - self.px(16) <= e.y_root <= gy

    def hover(self, e):
        cursor = "size_nw_se" if self.in_grip(e) else ""
        if self.root.cget("cursor") != cursor:
            self.root.config(cursor=cursor)
            self.c.config(cursor=cursor)

    def drag_move(self, e):
        if self.resizing:
            # 본체 왼쪽 위를 기준으로, 마우스까지의 가로 길이에 비례해 배율을 바꾼다
            start_w, start_zoom = self.resize_from
            zoom = max(0.6, min(2.5, start_zoom * (e.x_root - self.main_x) / max(1, start_w)))
            if abs(zoom - self.zoom) >= 0.01:
                self.zoom = zoom
                self.apply_zoom()
                if self.last_m:
                    self.draw(self.last_m)
                if self.expanded:
                    self.draw_details()
                self.place()
            return
        if self.dragging:
            self.main_x, self.main_y = e.x_root - self.drag_off[0], e.y_root - self.drag_off[1]
            self.place()

    def place(self):
        """본체는 항상 (main_x, main_y)에 두고, 펼친 패널이 화면 아래로 넘치면 창을 위쪽으로 키운다."""
        x, y = self.main_x, self.main_y
        if self.expanded:
            if self.expand_left:
                x -= self.dw
            ch, dh = int(self.c.cget("height")), int(self.d.cget("height"))
            grow_up = self.main_y + dh > self.main_work().bottom
            anchor = "s" if grow_up else "n"  # 위로 키울 땐 두 패널의 아래쪽을 맞춘다
            if self.c.pack_info()["anchor"] != anchor:
                self.c.pack_configure(anchor=anchor)
                self.d.pack_configure(anchor=anchor)
            if grow_up:
                y -= max(0, dh - ch)
        self.root.geometry(f"+{x}+{y}")
        # tk는 위치를 유휴 시점에 반영하는데, 그 전에 send_to_back(SetWindowPos)이 끼면 옛 위치로 덮어쓴다
        self.root.update_idletasks()

    def main_monitor(self):
        return monitor_info(self.main_x + self.w // 2, self.main_y + self.px(20))

    def main_work(self):
        return self.main_monitor().rcWork

    def opens_left(self):
        # 본체가 있는 모니터 안에서 오른쪽 공간이 모자라면 왼쪽으로 펼친다
        return self.main_x + self.w + self.dw > self.main_work().right

    def expand_click(self, e):
        # 드래그하다 놓은 경우는 클릭으로 치지 않는다
        if abs(e.x_root - self.press_pos[0]) + abs(e.y_root - self.press_pos[1]) < self.px(4):
            self.toggle_expand()

    def toggle_expand(self):
        # 본체는 제자리에 두고 같은 모니터 안에서 공간이 있는 쪽으로 펼친다
        self.expanded = not self.expanded
        if self.expanded:
            self.expand_left = self.opens_left()
            if self.expand_left:
                self.d.pack(side="left", anchor="n", before=self.c)
            else:
                self.d.pack(side="left", anchor="n")
            self.details = None
            self.sample_details()  # 프로세스별 CPU·디스크는 직전 값과의 차이라 한 번 미리 재 둔다
            self.draw_details()
        else:
            self.d.pack_forget()
        self.place()
        self.save()

    def drag_end(self, e):
        if self.dragging:
            self.dragging = False
            self.save()

    def apply_topmost(self):
        self.root.attributes("-topmost", self.topmost.get())
        self.save()

    def apply_locked(self):
        if self.last_m:
            self.draw(self.last_m)  # 크기 조절 손잡이를 바로 보이거나 숨긴다
        self.save()

    def apply_alpha(self):
        self.root.attributes("-alpha", self.alpha.get())
        self.save()

    def apply_startup(self):
        set_startup(self.startup.get())

    def run_speedtest(self):
        self.speedtest.start()

    def save(self):
        self.cfg.pop("expanded_pos", None)  # 예전 버전이 따로 저장하던 펼친 위치
        self.cfg.update(x=self.main_x, y=self.main_y, expanded=self.expanded, zoom=round(self.zoom, 3), topmost=self.topmost.get(), locked=self.locked.get(),
                        alpha=self.alpha.get())
        save_config(self.cfg)

    def restart_as_admin(self):
        self.save()
        release_single_instance()  # 새로 뜨는 쪽이 중복 실행으로 오인하지 않게 먼저 놓는다
        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        rc = shell32.ShellExecuteW(None, "runas", pythonw_path(), f'"{os.path.abspath(__file__)}"', APP_DIR, 1)
        if (rc or 0) > 32:
            self.quit()
        else:  # UAC 취소
            acquire_single_instance()

    def quit(self):
        self.save()
        if self.net.ok:
            self.net.stop()
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

    def sample_details(self):
        """프로세스 이름별로 묶어 CPU·메모리·GPU·디스크 I/O·TCP 연결 수를 모은다."""
        now = time.perf_counter()
        dt = now - self.last_detail_t if self.last_detail_t else None
        self.last_detail_t = now
        ncpu = psutil.cpu_count()
        names, cpu, mem, disk, gpu, conns = {}, {}, {}, {}, {}, {}
        now_counts = {}
        for pid, name, cpu_time, private_ws, io_bytes in process_snapshot():
            names[pid] = name
            mem[name] = mem.get(name, 0) + private_ws
            now_counts[pid] = (cpu_time, io_bytes)
            prev = self.last_counts.get(pid)
            if dt and prev:
                cpu[name] = cpu.get(name, 0) + max(0, cpu_time - prev[0]) / 1e7 / dt / ncpu * 100
                disk[name] = disk.get(name, 0) + max(0, io_bytes - prev[1]) / dt
        self.last_counts = now_counts

        if GPU is not None:
            try:
                per_pid = {}
                for smp in pynvml.nvmlDeviceGetProcessUtilization(GPU, self.gpu_ts):
                    per_pid[smp.pid] = max(per_pid.get(smp.pid, 0), smp.smUtil)
                    self.gpu_ts = max(self.gpu_ts, smp.timeStamp)
                for pid, u in per_pid.items():
                    if pid in names:
                        gpu[names[pid]] = gpu.get(names[pid], 0) + u
            except pynvml.NVMLError:  # 새 샘플이 없으면 NotFound
                pass

        net, net_split = {}, {}
        if self.net.ok:
            for pid, (recv, sent) in self.net.take().items():
                if dt and pid in names:
                    down, up = net_split.get(names[pid], (0, 0))
                    net_split[names[pid]] = (down + recv / dt, up + sent / dt)
            net = {name: down + up for name, (down, up) in net_split.items()}
        else:
            try:
                for cn in psutil.net_connections("tcp"):
                    if cn.status == psutil.CONN_ESTABLISHED and cn.pid in names:
                        conns[names[cn.pid]] = conns.get(names[cn.pid], 0) + 1
            except psutil.Error:
                pass

        if dt:
            self.details = dict(cpu=top(cpu), mem=top(mem), gpu=top(gpu), disk=top(disk), conns=top(conns),
                                net=top(net), net_split=net_split)

    def tick(self):
        self.last_m = self.sample()
        self.draw(self.last_m)
        if self.expanded:
            self.sample_details()
            self.draw_details()
            if not self.dragging:
                self.place()  # 패널 높이가 바뀌면 위로 키운 만큼도 바뀐다
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
        c.create_text(w - p - px(22), y, text=time.strftime("%H:%M:%S"), fill=MUTED, font=self.f_title, anchor="ne")
        # 펼치기 아이콘: 패널이 열릴(닫힐) 방향을 가리킨다
        left = self.expand_left if self.expanded else self.opens_left()
        glyph = "«" if self.expanded != left else "»"
        c.create_rectangle(w - p - px(16), y - px(1), w - p, y + px(15), fill=TRACK, width=0, tags="expand")
        c.create_text(w - p - px(8), y + px(7), text=glyph, fill=FG, font=self.f_title, tags="expand")
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

        if not self.locked.get():  # 크기 조절 손잡이: 오른쪽 아래 모서리의 빗금 세 줄
            for i in (4, 8, 12):
                c.create_line(w - px(i), y - px(3), w - px(3), y - px(i), fill=MUTED)
        c.config(height=y)

    def draw_details(self):
        d, p, w, px = self.d, self.pad, self.dw, self.px
        d.delete("all")
        details = self.details or {}

        sections = [
            ("CPU", "cpu", C_CPU, lambda v: f"{v:.1f}%"),
            ("메모리", "mem", C_RAM, lambda v: f"{v / 1024**3:.2f} GB" if v >= 1024**3 else f"{v / 1024**2:.0f} MB"),
            ("GPU", "gpu", C_GPU, lambda v: f"{v:.0f}%"),
            ("디스크 I/O", "disk", WARN, fmt_bytes_rate),
        ]
        if self.net.ok:
            split = details.get("net_split", {})
            sections.append(("네트워크 대역폭", "net", C_DOWN, None))
        else:
            sections.append(("TCP 연결 수 (대역폭은 관리자 실행 필요)", "conns", C_DOWN, lambda v: f"{v}개"))
        sections = [s for s in sections if s[1] != "gpu" or GPU is not None]

        # 높이는 모니터 해상도의 절반으로 고정하고, 그 안에 들어가는 만큼만 순위를 보여준다
        mon = self.main_monitor().rcMonitor
        work = self.main_work()
        height = min(int((mon.bottom - mon.top) / 2 * self.zoom), work.bottom - work.top)  # 배율 1일 때 절반
        row, head, gap, top_h = px(17), px(17), px(8), px(44)
        n = len(sections)
        rows_n = max(1, min(TOP_N, (height - top_h - n * (head + gap)) // (n * row)))
        spare = max(0, height - top_h - n * (head + gap + rows_n * row)) // n  # 남는 높이는 항목 사이에 고르게

        y = px(12)
        status = "측정 중..." if self.details is None else f"상위 {rows_n}"
        d.create_text(p, y, text=f"세부 정보 · {status}", fill=MUTED, font=self.f_title, anchor="nw")
        y = top_h
        for title, key, color, fmt in sections:
            d.create_text(p, y, text=title, fill=color, font=self.f_small, anchor="w")
            y += head
            rows = details.get(key, [])[:rows_n]
            if not rows:
                d.create_text(p, y, text="-", fill=MUTED, font=self.f_small, anchor="w")
            for name, v in rows:
                bar = (w - 2 * p) * v / rows[0][1]
                d.create_rectangle(p, y - px(7), p + bar, y + px(7), fill=mix(BG, color, 0.22), width=0)
                if key == "net":
                    value, name = "↓{} ↑{}".format(*map(fmt_rate, split[name])), name[:14]
                else:
                    value, name = fmt(v), name[:22]
                d.create_text(p + px(4), y, text=name, fill=FG, font=self.f_small, anchor="w")
                d.create_text(w - p - px(4), y, text=value, fill=FG, font=self.f_small, anchor="e")
                y += row
            y += row * (rows_n - len(rows)) + gap + spare  # 순위가 몇 개든 창 크기가 변하지 않게 빈 줄 자리를 남긴다
        d.config(height=max(height, y))

    def run(self):
        self.tick()
        self.root.mainloop()


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.restype = wt.HANDLE
_mutex = None


def acquire_single_instance():
    """중복 실행 방지. 이미 떠 있으면 False."""
    global _mutex
    _mutex = _kernel32.CreateMutexW(None, False, "PCMonitorWidgetMutex")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def release_single_instance():
    _kernel32.CloseHandle(wt.HANDLE(_mutex))


if __name__ == "__main__":
    if not acquire_single_instance():
        sys.exit(0)
    Monitor().run()
