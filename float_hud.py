#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
悬浮监控栏 · Float HUD
================================================================
常驻桌面的极简悬浮条：
  · 左侧四枚环形仪表：CPU / 内存 / 磁盘活动 / 网络吞吐
  · 右侧「占用最高」进程榜，可切换按 CPU / 内存 排序
  · 无边框 + 圆角 + 柔和投影，可拖动、自动吸边、置顶、深/浅主题
  · 托盘常驻，右键菜单控制一切

依赖: psutil, PySide6-Essentials
================================================================
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

import psutil
from PySide6.QtCore import (
    QPoint,
    QPointF,
    QRectF,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

APP_NAME = "悬浮监控栏"
APP_DIR = Path(__file__).resolve().parent
CONF_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "error.log"
SERVER_NAME = "FloatHUD-SingleInstance"


def fatal_dialog(title: str, text: str) -> None:
    """无控制台时把错误摆到用户眼前，而不是静默退出。"""
    try:
        from PySide6.QtWidgets import QApplication as _App, QMessageBox as _Box
        app = _App.instance() or _App(sys.argv)
        _Box.critical(None, title, text)
        del app
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
        except Exception:
            pass


# ---------------------------------------------------------------- 主题 ----
@dataclass(frozen=True)
class Theme:
    name: str
    bg_top: str
    bg_bottom: str
    border: str
    track: str
    text: str
    text2: str
    text3: str
    cpu: str
    mem: str
    disk: str
    net: str
    temp: str
    warn_mid: str
    warn_hi: str
    hover: str
    chip: str
    shadow_a: int
    highlight: str


DARK = Theme(
    name="dark",
    bg_top="#1C2029",
    bg_bottom="#121520",
    border="#2C3242",
    track="#282E3C",
    text="#EEF1F7",
    text2="#98A1B4",
    text3="#6A7387",
    cpu="#5B9DFF",
    mem="#A78BFA",
    disk="#34D399",
    net="#F5B942",
    temp="#FF7A9B",
    warn_mid="#F5B942",
    warn_hi="#FF6B6B",
    hover="#202634",
    chip="#232A38",
    shadow_a=62,
    highlight="#FFFFFF",
)

LIGHT = Theme(
    name="light",
    bg_top="#FFFFFF",
    bg_bottom="#F2F5FA",
    border="#DCE2EC",
    track="#E7EBF3",
    text="#141922",
    text2="#5E6B85",
    text3="#96A0B2",
    cpu="#2F7BF6",
    mem="#7C5CF0",
    disk="#0EA371",
    net="#D9920A",
    temp="#E0407A",
    warn_mid="#D9920A",
    warn_hi="#E5484D",
    hover="#EBF0F8",
    chip="#E9EEF7",
    shadow_a=48,
    highlight="#FFFFFF",
)

THEMES = {t.name: t for t in (DARK, LIGHT)}
SKIP_PROCS = {"System Idle Process", "Idle", "Memory Compression", "MemCompression"}


# ------------------------------------------------------------ 小工具 ----
def mkfont(px: int, weight=QFont.Weight.Normal, family: str = "Microsoft YaHei UI") -> QFont:
    f = QFont(family)
    f.setPixelSize(px)
    f.setWeight(weight)
    f.setStyleStrategy(QFont.PreferAntialias)
    return f


def nice_ceiling(mbps: float) -> float:
    """把吞吐量映射到一个「好读」的上限刻度，保证低速时环也有可见幅度。"""
    ladder = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000]
    need = max(mbps * 1.05, 0.5)
    for v in ladder:
        if v >= need:
            return float(v)
    return float(ladder[-1])


def human_bytes(n: float) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f}G"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.0f}M"
    return f"{n / 1024:.0f}K"


def human_speed(bytes_per_sec: float) -> str:
    """bytes/s -> Mbps 字符串（贴近任务管理器的网络列口径）"""
    mbps = bytes_per_sec * 8 / 1_000_000
    if mbps >= 1000:
        return f"{mbps / 1000:.1f}G"
    if mbps >= 100:
        return f"{mbps:.0f}"
    if mbps >= 10:
        return f"{mbps:.1f}"
    if mbps >= 0.1:
        return f"{mbps:.1f}".rstrip("0").rstrip(".")
    return "0"


# --------------------------------------------------------------- 采样 ----
@dataclass
class ProcRow:
    name: str
    count: int
    cpu: float      # 占整机百分比（已除核数）
    mem: float      # RSS 字节


@dataclass
class Snapshot:
    cpu: float = 0.0
    mem_pct: float = 0.0
    mem_used: float = 0.0
    mem_total: float = 0.0
    disk_pct: float = 0.0
    disk_read: float = 0.0
    disk_write: float = 0.0
    net_down: float = 0.0
    net_up: float = 0.0
    temp_c: float | None = None        # CPU/主板热区温度，None = 本机读不到
    procs_cpu: list = field(default_factory=list)
    procs_mem: list = field(default_factory=list)


class TempReader:
    """ACPI 热区温度读取。

    Windows 上没有通用、免驱动的 CPU 温度接口，唯一原生可用的是
    Win32_PerfFormattedData_Counters_ThermalZoneInformation
    （ACPI 热区，字段 HighPrecisionTemperature 单位 = 绝对温度 × 10）。

    要点：
      · 必须在使用的线程里 CoInitialize，否则 QThread 里 COM 会漏 IUnknown；
      · WMI 查询约 10–20 ms，不必每秒都查，按 min_interval 降频；
      · 读不到就永久关闭，不反复尝试拖慢采样。
    """

    def __init__(self, min_interval: float = 3.0) -> None:
        self.min_interval = min_interval
        self._last_t = 0.0
        self._value: float | None = None
        self._disabled = False
        self._svc = None
        self._com_ready = False

    # 在采样线程内调用一次
    def attach_thread(self) -> None:
        if self._disabled or self._com_ready:
            return
        try:
            import pythoncom
            pythoncom.CoInitialize()
            self._com_ready = True
        except Exception:
            self._disabled = True

    def detach_thread(self) -> None:
        self._svc = None
        if self._com_ready:
            try:
                import pythoncom
                pythoncom.CoUninitialize()
            except Exception:
                pass
            self._com_ready = False

    def _ensure_service(self):
        if self._svc is not None:
            return self._svc
        import win32com.client
        loc = win32com.client.Dispatch("WbemScripting.SWbemLocator")
        self._svc = loc.ConnectServer(".", r"root\cimv2")
        return self._svc

    def read(self, force: bool = False) -> float | None:
        if self._disabled:
            return None
        now = time.monotonic()
        if not force and (now - self._last_t) < self.min_interval:
            return self._value
        self._last_t = now

        try:
            svc = self._ensure_service()
            best = None
            q = svc.ExecQuery(
                "SELECT HighPrecisionTemperature FROM "
                "Win32_PerfFormattedData_Counters_ThermalZoneInformation"
            )
            for it in q:
                try:
                    hp = int(it.HighPrecisionTemperature)
                except Exception:
                    continue
                if hp <= 0:
                    continue
                c = hp / 10.0 - 273.15
                if not (-40.0 < c < 150.0):
                    continue
                best = c if best is None else max(best, c)
            if best is not None:
                self._value = best
        except Exception:
            # WMI 不可用（精简系统 / 权限拒绝）：永久停用，不再重试
            self._disabled = True
            self._svc = None
        return self._value

    @property
    def available(self) -> bool:
        return not self._disabled


class Sampler:
    """采集器，内部保存上次读数用于算差分。"""

    def __init__(self) -> None:
        self.ncpu = psutil.cpu_count(logical=True) or 1
        self._t = time.monotonic()
        try:
            self._disk = psutil.disk_io_counters()
        except Exception:
            self._disk = None
        try:
            self._net = psutil.net_io_counters()
        except Exception:
            self._net = None
        psutil.cpu_percent(None)
        for p in psutil.process_iter(["cpu_percent"]):
            pass  # 预热进程 CPU 计数，首次读数才有意义

        self.temp = TempReader()

    def sample(self) -> Snapshot:
        now = time.monotonic()
        dt = max(now - self._t, 1e-3)
        self._t = now
        s = Snapshot()

        s.cpu = float(psutil.cpu_percent(None))
        vm = psutil.virtual_memory()
        s.mem_pct = float(vm.percent)
        s.mem_used = float(vm.total - vm.available)
        s.mem_total = float(vm.total)

        # 磁盘：优先用 busy_time 折算「活动时间占比」，与任务管理器口径一致
        try:
            dio = psutil.disk_io_counters()
        except Exception:
            dio = None
        if dio is not None:
            prev = self._disk
            if prev is not None:
                busy = getattr(dio, "busy_time", None)
                pbusy = getattr(prev, "busy_time", None)
                if busy is not None and pbusy is not None:
                    s.disk_pct = min(100.0, max(0.0, (busy - pbusy) / (dt * 1000.0) * 100.0))
                else:
                    rt = getattr(dio, "read_time", 0) - getattr(prev, "read_time", 0)
                    wt = getattr(dio, "write_time", 0) - getattr(prev, "write_time", 0)
                    s.disk_pct = min(100.0, max(0.0, max(rt, wt) / (dt * 1000.0) * 100.0))
                s.disk_read = max(0.0, (dio.read_bytes - prev.read_bytes) / dt)
                s.disk_write = max(0.0, (dio.write_bytes - prev.write_bytes) / dt)
            self._disk = dio

        try:
            nio = psutil.net_io_counters()
        except Exception:
            nio = None
        if nio is not None:
            prev = self._net
            if prev is not None:
                s.net_down = max(0.0, (nio.bytes_recv - prev.bytes_recv) / dt)
                s.net_up = max(0.0, (nio.bytes_sent - prev.bytes_sent) / dt)
            self._net = nio

        s.procs_cpu, s.procs_mem = self._processes()
        s.temp_c = self.temp.read()
        return s

    def _processes(self):
        agg: dict = {}
        # 用 attrs 批量取，比逐进程三次系统调用快一倍
        for p in psutil.process_iter(["name", "cpu_percent", "memory_info"]):
            info = p.info
            name = info.get("name") or ""
            if not name or name in SKIP_PROCS:
                continue
            cpu = info.get("cpu_percent")
            mi = info.get("memory_info")
            if cpu is None or mi is None:
                continue
            rss = float(mi.rss)
            slot = agg.get(name)
            if slot is None:
                agg[name] = [1, cpu, rss]
            else:
                slot[0] += 1
                slot[1] += cpu
                slot[2] += rss

        rows = [
            ProcRow(name=n, count=v[0], cpu=v[1] / self.ncpu, mem=v[2])
            for n, v in agg.items()
        ]
        by_cpu = sorted(rows, key=lambda r: (r.cpu, r.mem), reverse=True)[:6]
        by_mem = sorted(rows, key=lambda r: (r.mem, r.cpu), reverse=True)[:6]
        return by_cpu, by_mem


# --------------------------------------------------------- 悬浮条本体 ----
class SamplerThread(QThread):
    """后台采样线程：Windows 上枚举 350+ 进程需 400~800ms，放主线程会卡界面。"""

    ready = Signal(object)

    def __init__(self, interval_ms: int, parent=None) -> None:
        super().__init__(parent)
        self.interval_ms = max(300, int(interval_ms))
        self.sampler = Sampler()
        self._stop = False
        self._lock = Lock()

    def set_interval(self, ms: int) -> None:
        with self._lock:
            self.interval_ms = max(300, int(ms))

    def run(self) -> None:
        # WMI/COM 必须在实际使用它的线程里初始化
        self.sampler.temp.attach_thread()
        try:
            while not self._stop:
                t0 = time.monotonic()
                try:
                    snap = self.sampler.sample()
                    self.ready.emit(snap)
                except Exception:
                    pass
                with self._lock:
                    wait = self.interval_ms / 1000.0
                spent = time.monotonic() - t0
                remain = wait - spent
                # 分片休眠，保证退出响应及时
                while remain > 0 and not self._stop:
                    step = min(0.05, remain)
                    time.sleep(step)
                    remain -= step
        finally:
            self.sampler.temp.detach_thread()

    def stop(self) -> None:
        self._stop = True
        self.wait(2000)


class Hud(QWidget):
    W = 742          # 卡面宽
    M = 16           # 阴影留白
    PAD = 16
    V_PAD = 14
    RING_D = 62
    RING_T = 6
    RING_GAP = 12
    RING_N = 5       # CPU / 内存 / 磁盘 / 网络 / 温度
    ROW_H = 21
    # 环形块实需高度：环 + 标签 + 明细（不含上下留白）
    RING_BLOCK = 93

    # 温度→环周长映射区间：20°C 起、100°C 满
    # 取 20 起步是为了冷机（30°C 上下）时仍有一小段弧，不至于看起来像没启用
    TEMP_LO = 20.0
    TEMP_HI = 100.0

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setWindowTitle(APP_NAME)

        self.cfg = self._load_cfg()
        self.theme = THEMES.get(self.cfg.get("theme", "dark"), DARK)
        self.rows = max(1, min(5, int(self.cfg.get("rows", 3))))
        self.sort = self.cfg.get("sort", "cpu")
        self.pinned = bool(self.cfg.get("pinned", True))
        self.interval = max(500, int(self.cfg.get("interval", 1000)))
        if not self.pinned:
            self.setWindowFlag(Qt.WindowStaysOnTopHint, False)

        self.snap = Snapshot()
        self.ncpu = psutil.cpu_count(logical=True) or 1
        self.disp = {"cpu": 0.0, "mem": 0.0, "disk": 0.0, "net": 0.0}
        self.disp_temp = 0.0          # 温度的独立插值，避免被百分制 clamp 影响读数
        self.show_temp = bool(self.cfg.get("show_temp", True))
        self.temp_ok = False          # 本机是否真的读到了温度
        self._targets = dict(self.disp)
        self.net_speed = 0.0
        self._target_speed = 0.0
        self._target_temp = 0.0
        self.net_scale = 1.0
        self.net_mbps = 0.0

        self._hover_row = -1
        self._hover_chip = False
        self._drag_off = QPoint()
        self._pressed_chip = False
        self._row_rects: list = []
        self._chip_rect = QRectF()

        self._f_pct = mkfont(15, QFont.Weight.DemiBold, "Segoe UI")
        self._f_label = mkfont(11, QFont.Weight.Medium)
        self._f_detail = mkfont(10)
        self._f_head = mkfont(10, QFont.Weight.Medium)
        self._f_chip = mkfont(9, QFont.Weight.Medium, "Segoe UI")
        self._f_row = mkfont(12)
        self._f_val = mkfont(11, QFont.Weight.DemiBold, "Segoe UI")

        self._anim = QTimer(self)
        self._anim.setInterval(33)
        self._anim.timeout.connect(self._tick_anim)

        # 后台采样线程，避免枚举进程阻塞 UI
        self.worker = SamplerThread(self.interval, self)
        self.worker.ready.connect(self._on_snapshot)

        self._apply_geometry()
        self._restore_pos()

    def start(self) -> None:
        self.worker.start()

    # ---------------------------------------------------------- 配置 --
    def _load_cfg(self) -> dict:
        try:
            return json.loads(CONF_PATH.read_text("utf-8"))
        except Exception:
            return {}

    def _save_cfg(self) -> None:
        pos = self.pos()
        data = {
            "x": pos.x(),
            "y": pos.y(),
            "theme": self.theme.name,
            "rows": self.rows,
            "sort": self.sort,
            "pinned": self.pinned,
            "show_temp": self.show_temp,
            "interval": self.interval,
        }
        try:
            CONF_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        except Exception:
            pass

    def _restore_pos(self) -> None:
        x, y = self.cfg.get("x"), self.cfg.get("y")
        geo = QApplication.primaryScreen().availableGeometry()
        if isinstance(x, int) and isinstance(y, int):
            for sc in QApplication.screens():
                g = sc.availableGeometry()
                if g.contains(QPoint(x + 60, y + 30)):
                    geo = g
                    break
            x = min(max(x, geo.left() - self.M), geo.right() - self.width() + self.M)
            y = min(max(y, geo.top() - self.M), geo.bottom() - self.height() + self.M)
            self.move(x, y)
        else:
            self.move(geo.right() - self.width() - 20, geo.top() + 80)

    # ---------------------------------------------------------- 尺寸 --
    def _proc_block_h(self) -> float:
        return 14 + 7 + self.rows * self.ROW_H

    def _card_h(self) -> float:
        return max(float(self.RING_BLOCK), self._proc_block_h()) + 2 * self.V_PAD

    def _apply_geometry(self) -> None:
        self.setFixedSize(self.W + self.M * 2, int(round(self._card_h())) + self.M * 2)
        self.update()

    def _card_rect(self) -> QRectF:
        return QRectF(self.M, self.M, self.W, self._card_h())

    def _ring_count(self) -> int:
        """实际显示的环数：温度可用且开启时是 5，否则 4。"""
        return 5 if (self.snap.temp_c is not None and self.show_temp) else 4

    def _proc_x(self) -> float:
        n = self._ring_count()
        left = self.M + self.PAD + n * self.RING_D + (n - 1) * self.RING_GAP
        return left + 44

    def _proc_w(self) -> float:
        return self.M + self.W - self.PAD - self._proc_x()

    # ---------------------------------------------------------- 数据 --
    def _on_snapshot(self, snap) -> None:
        self.snap = snap

        mbps = max(snap.net_down, snap.net_up) * 8 / 1_000_000
        # 刻度只升不降地缓慢回落，避免环来回跳
        need = nice_ceiling(mbps)
        if need > self.net_scale:
            self.net_scale = need
        else:
            self.net_scale = max(need, self.net_scale * 0.995)
        self.net_mbps = mbps

        self._targets = {
            "cpu": snap.cpu,
            "mem": snap.mem_pct,
            "disk": snap.disk_pct,
            "net": min(100.0, mbps / self.net_scale * 100.0),
        }
        self._target_temp = snap.temp_c if snap.temp_c is not None else 0.0
        self._target_speed = snap.net_down * 8 / 1_000_000

        if not self._anim.isActive():
            self._anim.start()

    def _tick_anim(self) -> None:
        done = True
        for k, tgt in self._targets.items():
            cur = self.disp[k]
            nxt = cur + (tgt - cur) * 0.30
            if abs(tgt - nxt) < 0.12:
                nxt = tgt
            else:
                done = False
            self.disp[k] = nxt
        sp = self.net_speed + (self._target_speed - self.net_speed) * 0.30
        if abs(self._target_speed - sp) < 0.02:
            sp = self._target_speed
        else:
            done = False
        self.net_speed = sp

        # 温度单独插值：目标是摄氏度，收敛阈值 0.05°C
        tp = self.disp_temp + (self._target_temp - self.disp_temp) * 0.30
        if abs(self._target_temp - tp) < 0.05:
            tp = self._target_temp
        else:
            done = False
        self.disp_temp = tp

        self.update()
        if done:
            self._anim.stop()

    def _temp_pct(self, temp: float | None) -> float:
        """把温度线性映射到环周长，只看趋势，不代表真实负载。"""
        if temp is None:
            return 0.0
        span = self.TEMP_HI - self.TEMP_LO
        return max(0.0, min(100.0, (temp - self.TEMP_LO) / span * 100.0))

    def _metric_rows(self):
        s = self.snap
        rows = [
            ("CPU", f"{self.disp['cpu']:.0f}%", f"{self.ncpu} 核", self.disp["cpu"], self.theme.cpu),
            ("内存", f"{self.disp['mem']:.0f}%",
             f"{human_bytes(s.mem_used)}/{human_bytes(s.mem_total)}", self.disp["mem"], self.theme.mem),
            ("磁盘", f"{self.disp['disk']:.0f}%",
             f"R {human_bytes(s.disk_read)}/s", self.disp["disk"], self.theme.disk),
            ("网络", human_speed(self.net_speed * 1_000_000 / 8),
             f"↑ {human_speed(s.net_up)} Mbps", self.disp["net"], self.theme.net),
        ]
        # 温度读不到时（部分机器无 ACPI 热区）就不占位，保持四环布局
        t = s.temp_c
        self.temp_ok = t is not None
        if t is not None and self.show_temp:
            # 数值本身走插值以免跳动，但不经过百分制 clamp，保证读数真实
            shown = self.disp_temp
            hot = shown >= 85.0
            rows.append((
                "温度",
                f"{shown:.0f}°",
                "过热" if hot else "CPU 热区",
                self._temp_pct(shown),
                self.theme.warn_hi if hot else self.theme.temp,
            ))
        return rows

    def _proc_rows(self):
        src = self.snap.procs_cpu if self.sort == "cpu" else self.snap.procs_mem
        return src[: self.rows]

    # ---------------------------------------------------------- 绘制 --
    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)

        card = self._card_rect()
        radius = 18.0

        # 外层柔和投影（多层叠加模拟高斯）
        p.setPen(Qt.NoPen)
        for i in range(9, 0, -1):
            a = int(self.theme.shadow_a * (1 - i / 10.0) ** 2.4)
            if a <= 0:
                continue
            p.setBrush(QColor(8, 12, 22, a))
            p.drawRoundedRect(card.adjusted(-i, -i + 1, i, i + 3), radius + i, radius + i)

        # 卡面渐变
        g = QLinearGradient(card.topLeft(), card.bottomLeft())
        g.setColorAt(0, QColor(self.theme.bg_top))
        g.setColorAt(1, QColor(self.theme.bg_bottom))
        p.setBrush(g)
        p.setPen(QPen(QColor(self.theme.border), 1))
        p.drawRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)

        # 顶部内高光
        if self.theme.name == "dark":
            hl = QColor(self.theme.highlight)
            hl.setAlpha(22)
            path = QPainterPath()
            path.addRoundedRect(
                QRectF(card.left() + 20, card.top() + 0.9, card.width() - 40, 1.0), 1, 1
            )
            p.setPen(Qt.NoPen)
            p.setBrush(hl)
            p.drawPath(path)

        self._paint_rings(p, card)
        self._paint_divider(p, card)
        self._paint_procs(p, card)
        p.end()

    def _ring_top(self) -> float:
        """环形组在卡面内容区垂直居中，避免行数变化时底部留白过多。"""
        content_h = self._card_h() - 2 * self.V_PAD
        offset = max(0.0, (content_h - self.RING_BLOCK) / 2.0)
        return self.M + self.V_PAD + offset

    def _proc_top(self) -> float:
        """进程块同样在内容区垂直居中，行数少时不会挤在顶部。"""
        content_h = self._card_h() - 2 * self.V_PAD
        offset = max(0.0, (content_h - self._proc_block_h()) / 2.0)
        return self.M + self.V_PAD + offset

    def _paint_rings(self, p: QPainter, card: QRectF) -> None:
        cy = self._ring_top() + self.RING_D / 2
        x0 = card.left() + self.PAD
        for i, (label, value, detail, pct, accent) in enumerate(self._metric_rows()):
            cx = x0 + i * (self.RING_D + self.RING_GAP) + self.RING_D / 2
            r = (self.RING_D - self.RING_T) / 2
            rect = QRectF(cx - r, cy - r, r * 2, r * 2)

            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(self.theme.track), self.RING_T, Qt.SolidLine, Qt.RoundCap))
            p.drawEllipse(rect)

            color = QColor(accent)
            if label in ("CPU", "内存", "磁盘"):
                if pct >= 90:
                    color = QColor(self.theme.warn_hi)
                elif pct >= 75:
                    color = QColor(self.theme.warn_mid)
            elif label == "温度":
                # 温度按摄氏度分档，而不是百分比
                if self.snap.temp_c is not None:
                    if self.snap.temp_c >= 85.0:
                        color = QColor(self.theme.warn_hi)
                    elif self.snap.temp_c >= 75.0:
                        color = QColor(self.theme.warn_mid)

            pct = max(0.0, min(100.0, pct))
            if pct > 0.4:
                span = -max(int(pct / 100.0 * 360 * 16), 34)
                # 光晕用平头线帽，避免起点处鼓出一个圆球
                glow = QColor(color)
                glow.setAlpha(40)
                p.setPen(QPen(glow, self.RING_T + 5, Qt.SolidLine, Qt.FlatCap))
                p.drawArc(rect, 90 * 16, span)
                p.setPen(QPen(color, self.RING_T, Qt.SolidLine, Qt.RoundCap))
                p.drawArc(rect, 90 * 16, span)

            p.setPen(QColor(self.theme.text))
            p.setFont(self._f_pct)
            p.drawText(QRectF(cx - self.RING_D / 2, cy - 12, self.RING_D, 23), Qt.AlignCenter, value)

            p.setPen(QColor(self.theme.text2))
            p.setFont(self._f_label)
            p.drawText(QRectF(cx - 34, cy + self.RING_D / 2 + 4, 68, 14), Qt.AlignCenter, label)

            p.setPen(QColor(self.theme.text3))
            p.setFont(self._f_detail)
            fm = QFontMetrics(self._f_detail)
            txt = fm.elidedText(detail, Qt.ElideRight, 88)
            p.drawText(QRectF(cx - 44, cy + self.RING_D / 2 + 18, 88, 13), Qt.AlignCenter, txt)

    def _paint_divider(self, p: QPainter, card: QRectF) -> None:
        x = self._proc_x() - 22
        p.setPen(QPen(QColor(self.theme.border), 1))
        p.drawLine(
            QPointF(x, card.top() + self.V_PAD + 6),
            QPointF(x, card.bottom() - self.V_PAD - 6),
        )

    def _paint_procs(self, p: QPainter, card: QRectF) -> None:
        x = self._proc_x()
        w = self._proc_w()
        top = self._proc_top()

        # 标题
        p.setPen(QColor(self.theme.text3))
        p.setFont(self._f_head)
        p.drawText(QRectF(x, top, 120, 14), Qt.AlignLeft | Qt.AlignVCenter, "占用最高")

        # 排序切换小胶囊
        chip_txt = "按 CPU" if self.sort == "cpu" else "按内存"
        p.setFont(self._f_chip)
        fm = QFontMetrics(self._f_chip)
        cw = fm.horizontalAdvance(chip_txt) + 18
        chip = QRectF(x + w - cw, top - 3, cw, 19)
        self._chip_rect = chip
        base = QColor(self.theme.chip)
        if self._hover_chip:
            base = base.lighter(115) if self.theme.name == "dark" else base.darker(105)
        p.setPen(Qt.NoPen)
        p.setBrush(base)
        p.drawRoundedRect(chip, 9.5, 9.5)
        p.setPen(QColor(self.theme.text2))
        p.drawText(chip, Qt.AlignCenter, chip_txt)

        # 进程行
        rows = self._proc_rows()
        self._row_rects = []
        row_top = top + 21
        accent = QColor(self.theme.cpu if self.sort == "cpu" else self.theme.mem)
        val_color = QColor(self.theme.text2)

        for i, row in enumerate(rows):
            rr = QRectF(x - 7, row_top + i * self.ROW_H, w + 7, self.ROW_H - 2)
            self._row_rects.append(rr)
            hovered = i == self._hover_row
            if hovered:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(self.theme.hover))
                p.drawRoundedRect(rr, 6, 6)

            bar = QColor(accent)
            bar.setAlpha(230)
            p.setPen(Qt.NoPen)
            p.setBrush(bar)
            p.drawRoundedRect(QRectF(rr.left() + 3, rr.center().y() - 5, 3, 10), 1.5, 1.5)

            # 右侧数值
            if self.sort == "cpu":
                val_txt = f"{row.cpu:.1f}%" if row.cpu >= 0.05 else "—"
            else:
                val_txt = human_bytes(row.mem)
            p.setFont(self._f_val)
            vfm = QFontMetrics(self._f_val)
            vw = vfm.horizontalAdvance(val_txt)
            p.setPen(val_color)
            p.drawText(
                QRectF(rr.right() - vw - 2, rr.top(), vw + 2, rr.height()),
                Qt.AlignRight | Qt.AlignVCenter,
                val_txt,
            )

            # 名称
            name = row.name
            if row.count > 1:
                name = f"{name} ({row.count})"
            p.setFont(self._f_row)
            fm2 = QFontMetrics(self._f_row)
            avail = rr.width() - 14 - vw - 10
            p.setPen(QColor(self.theme.text))
            p.drawText(
                QRectF(rr.left() + 12, rr.top(), max(avail, 20), rr.height()),
                Qt.AlignLeft | Qt.AlignVCenter,
                fm2.elidedText(name, Qt.ElideRight, int(max(avail, 20))),
            )

        if not rows:
            p.setPen(QColor(self.theme.text3))
            p.setFont(self._f_row)
            p.drawText(QRectF(x, row_top, w, 20), Qt.AlignLeft | Qt.AlignVCenter, "正在采样…")

    # ---------------------------------------------------------- 交互 --
    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton:
            if self._chip_rect.contains(ev.position()):
                self._pressed_chip = True
                return
            self._pressed_chip = False
            self._drag_off = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, ev) -> None:
        pos = ev.position()

        if ev.buttons() & Qt.LeftButton and not self._pressed_chip:
            self.move(ev.globalPosition().toPoint() - self._drag_off)
            return

        # hover 反馈
        chip_hover = self._chip_rect.contains(pos)
        row_hover = -1
        for i, rr in enumerate(self._row_rects):
            if rr.contains(pos):
                row_hover = i
                break

        changed = False
        if chip_hover != self._hover_chip:
            self._hover_chip = chip_hover
            changed = True
        if row_hover != self._hover_row:
            self._hover_row = row_hover
            changed = True
            if row_hover >= 0:
                rows = self._proc_rows()
                if row_hover < len(rows):
                    r = rows[row_hover]
                    self.setToolTip(
                        f"{r.name}\n进程数：{r.count}\n"
                        f"CPU：{r.cpu:.1f}%\n内存：{r.mem / 1024 ** 2:.1f} MB"
                    )
            else:
                self.setToolTip("")

        cur = Qt.ArrowCursor if not (chip_hover or row_hover >= 0) else Qt.PointingHandCursor
        if self.cursor().shape() != cur:
            self.setCursor(cur)
        if changed:
            self.update()

    def mouseReleaseEvent(self, ev) -> None:
        if ev.button() != Qt.LeftButton:
            return
        if self._pressed_chip and self._chip_rect.contains(ev.position()):
            self.toggle_sort()
        else:
            self._snap_to_edges()
            self._save_cfg()
        self._pressed_chip = False

    def leaveEvent(self, _ev) -> None:
        if self._hover_row != -1 or self._hover_chip:
            self._hover_row = -1
            self._hover_chip = False
            self.setToolTip("")
            self.update()

    def wheelEvent(self, ev) -> None:
        """在进程区滚轮：直接切换排序方式。"""
        if not self._proc_area_rect().contains(ev.position()):
            ev.ignore()
            return
        self.toggle_sort()
        ev.accept()

    def _proc_area_rect(self) -> QRectF:
        card = self._card_rect()
        return QRectF(self._proc_x() - 8, card.top() + self.V_PAD,
                      self._proc_w() + 8, card.height() - 2 * self.V_PAD)

    def toggle_sort(self) -> None:
        self.sort = "mem" if self.sort == "cpu" else "cpu"
        self._hover_row = -1
        self.setToolTip("")
        self._save_cfg()
        self.update()

    def _snap_to_edges(self) -> None:
        geo = QApplication.screenAt(self.frameGeometry().center())
        if geo is None:
            geo = QApplication.primaryScreen().availableGeometry()
        else:
            geo = geo.availableGeometry()
        x, y = self.x(), self.y()
        th = 18
        if abs(x + self.M - geo.left()) < th:
            x = geo.left() - self.M
        elif abs(x + self.width() - self.M - geo.right()) < th:
            x = geo.right() - self.width() + self.M
        if abs(y + self.M - geo.top()) < th:
            y = geo.top() - self.M
        elif abs(y + self.height() - self.M - geo.bottom()) < th:
            y = geo.bottom() - self.height() + self.M
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def contextMenuEvent(self, ev) -> None:
        self.build_menu().exec(ev.globalPos())

    def build_menu(self) -> QMenu:
        m = QMenu(self)
        m.setStyleSheet(self._menu_qss())

        sub = m.addMenu("外观主题")
        for key, label in (("dark", "深色"), ("light", "浅色")):
            act = sub.addAction(label)
            act.setCheckable(True)
            act.setChecked(self.theme.name == key)
            act.triggered.connect(lambda _c=False, k=key: self.set_theme(k))

        sub = m.addMenu("显示条数")
        for n in range(1, 6):
            act = sub.addAction(f"{n} 条")
            act.setCheckable(True)
            act.setChecked(self.rows == n)
            act.triggered.connect(lambda _c=False, k=n: self.set_rows(k))

        sub = m.addMenu("刷新频率")
        for ms, label in ((500, "0.5 秒"), (1000, "1 秒"), (2000, "2 秒"), (5000, "5 秒")):
            act = sub.addAction(label)
            act.setCheckable(True)
            act.setChecked(self.interval == ms)
            act.triggered.connect(lambda _c=False, k=ms: self.set_interval(k))

        # 温度：读不到时标记出来，并允许手动隐藏
        act = m.addAction("显示温度")
        act.setCheckable(True)
        act.setChecked(self.show_temp)
        if not self.snap.temp_c and not self.temp_ok:
            act.setText("显示温度（本机读不到）")
            act.setEnabled(False)
        else:
            act.triggered.connect(self.set_show_temp)

        m.addSeparator()
        act = m.addAction("始终置顶")
        act.setCheckable(True)
        act.setChecked(self.pinned)
        act.triggered.connect(self.set_pinned)

        act = m.addAction("回到默认位置")
        act.triggered.connect(self.reset_pos)

        m.addSeparator()
        act = m.addAction("退出")
        act.triggered.connect(QApplication.instance().quit)
        return m

    def _menu_qss(self) -> str:
        if self.theme.name == "dark":
            return (
                "QMenu{background:#1B2029;color:#E7EBF3;border:1px solid #2C3242;"
                "border-radius:10px;padding:6px;font-family:'Microsoft YaHei UI';font-size:12px;}"
                "QMenu::item{padding:6px 22px 6px 14px;border-radius:6px;}"
                "QMenu::item:selected{background:#2A3241;}"
                "QMenu::separator{height:1px;background:#2C3242;margin:5px 8px;}"
                "QMenu::indicator{width:12px;height:12px;}"
            )
        return (
            "QMenu{background:#FFFFFF;color:#141922;border:1px solid #DCE2EC;"
            "border-radius:10px;padding:6px;font-family:'Microsoft YaHei UI';font-size:12px;}"
            "QMenu::item{padding:6px 22px 6px 14px;border-radius:6px;}"
            "QMenu::item:selected{background:#EAF0FA;}"
            "QMenu::separator{height:1px;background:#E4E9F2;margin:5px 8px;}"
            "QMenu::indicator{width:12px;height:12px;}"
        )

    # ---------------------------------------------------------- 命令 --
    def set_theme(self, name: str) -> None:
        self.theme = THEMES.get(name, DARK)
        icon_owner = getattr(self, "_icon_owner", None)
        if icon_owner is not None:
            icon_owner.setIcon(make_icon(self.theme))
        refresh = getattr(self, "_tray_refresh", None)
        if refresh is not None:
            refresh()
        self._save_cfg()
        self.update()

    def set_rows(self, n: int) -> None:
        self.rows = max(1, min(5, n))
        self._apply_geometry()
        self._save_cfg()

    def set_show_temp(self) -> None:
        self.show_temp = not self.show_temp
        self._hover_row = -1
        self.setToolTip("")
        self._save_cfg()
        self.update()

    def set_interval(self, ms: int) -> None:
        self.interval = ms
        self.worker.set_interval(ms)
        self._save_cfg()

    def set_pinned(self, on: bool) -> None:
        self.pinned = on
        pos = self.pos()
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, on)
        self.move(pos)
        if visible:
            self.show()
        self._save_cfg()

    def reset_pos(self) -> None:
        geo = QApplication.primaryScreen().availableGeometry()
        self.move(geo.right() - self.width() - 20, geo.top() + 80)
        self._save_cfg()


# --------------------------------------------------------------- 图标 ----
def make_icon(theme: Theme) -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(theme.bg_top))
    p.setPen(QPen(QColor(theme.border), 2))
    p.drawRoundedRect(QRectF(2, 2, 60, 60), 14, 14)
    rect = QRectF(14, 14, 36, 36)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(theme.track), 6, Qt.SolidLine, Qt.RoundCap))
    p.drawEllipse(rect)
    p.setPen(QPen(QColor(theme.cpu), 6, Qt.SolidLine, Qt.RoundCap))
    p.drawArc(rect, 90 * 16, -int(0.68 * 360 * 16))
    p.end()
    return QIcon(pm)


def already_running() -> bool:
    """单实例：若已有实例在跑，通知它把面板亮出来，本进程直接退出。

    用本地套接字而非 pid 文件——进程被强杀也不会留下脏锁。
    """
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if sock.waitForConnected(400):
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(400)
        sock.disconnectFromServer()
        return True
    return False


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)

    if already_running():
        return 0

    # 没有活着的实例，清掉上次可能残留的套接字后自己当主实例
    QLocalServer.removeServer(SERVER_NAME)
    server = QLocalServer()
    if not server.listen(SERVER_NAME):
        server = None  # 监听失败也照常运行，只是失去单实例保护

    hud = Hud()
    hud.show()
    hud.start()

    def raise_hud() -> None:
        hud.show()
        hud.setWindowState(hud.windowState() & ~Qt.WindowMinimized)
        hud.raise_()
        hud.activateWindow()

    def on_peer() -> None:
        if server is None:
            return
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()
            conn.readyRead.connect(lambda c=conn: (c.readAll(), raise_hud()))
            conn.disconnected.connect(conn.deleteLater)
            # 连接建立即唤醒，避免对端不发数据的边界情况
            raise_hud()

    if server is not None:
        server.newConnection.connect(on_peer)

    tray = QSystemTrayIcon(make_icon(hud.theme), app)
    tray.setToolTip(APP_NAME)

    def refresh_tray_menu() -> None:
        menu = hud.build_menu()
        act_show = menu.addAction("显示 / 隐藏")
        act_show.setCheckable(True)
        act_show.setChecked(hud.isVisible())
        act_show.triggered.connect(lambda: hud.setVisible(not hud.isVisible()))
        menu.insertAction(menu.actions()[0], act_show)
        menu.insertSeparator(menu.actions()[1])
        tray.setContextMenu(menu)

    def on_tray(reason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            hud.setVisible(not hud.isVisible())

    tray.activated.connect(on_tray)
    tray.show()
    hud._tray_refresh = refresh_tray_menu   # 主题切换时刷新菜单样式
    hud._icon_owner = tray                  # 主题切换时刷新托盘图标
    refresh_tray_menu()

    def bye() -> None:
        hud.worker.stop()
        hud._save_cfg()
        tray.hide()
        if server is not None:
            server.close()
            QLocalServer.removeServer(SERVER_NAME)

    app.aboutToQuit.connect(bye)
    return app.exec()


def run() -> int:
    """带兜底的入口：任何异常都留下日志并弹窗，绝不静默退出。"""
    try:
        return main()
    except Exception:
        import traceback
        tb = traceback.format_exc()
        try:
            LOG_PATH.write_text(tb, "utf-8")
        except Exception:
            pass
        fatal_dialog(
            f"{APP_NAME} 启动失败",
            "程序启动时出错，详细信息已写入：\n"
            f"{LOG_PATH}\n\n{tb[-900:]}",
        )
        return 1


if __name__ == "__main__":
    sys.exit(run())
