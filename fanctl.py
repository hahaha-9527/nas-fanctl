#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fanctl — NAS 风扇调速守护进程（纯 Python 标准库，无第三方依赖）

功能:
  * 读取 /sys/class/hwmon 下的温度传感器与风扇转速
  * 按温度-PWM 曲线自动调速（升温立即响应、降温缓降防抖、紧急全速保护）
  * 内置 Web 配置页（默认 http://NAS_IP:9700），可拖拽编辑曲线、手动模式

用法:
  python3 fanctl.py --probe               # 检测硬件: 有哪些温度/风扇/PWM
  python3 fanctl.py --probe --gen-config  # 生成初始配置 fanctl.json
  python3 fanctl.py                       # 前台运行（读同目录 fanctl.json）
  python3 fanctl.py --simulate            # 模拟模式（无硬件演示/联调用）

注意: 需要 root 权限写 PWM；建议用 systemd 常驻，见 README.md
"""

import argparse
import glob
import json
import math
import os
import subprocess
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_CURVE = [[35, 15], [45, 30], [55, 45], [65, 65], [75, 85], [82, 100]]

# 用户确认的默认模板（Zero 1 Pro 实测调优版，页面"恢复默认设置"使用）
DEFAULT_TEMPLATE = {
    "port": 9700,
    "mode": "auto",
    "interval": 2,
    "failsafe_temp": 85,
    "down_step": 5,
    "min_change": 2,
    "fans": [
        {"id": "hwmon2/pwm2", "name": "CPU 风扇", "group": "cpu",
         "curve": [[40, 30], [46, 40], [55, 52], [65, 68], [75, 85], [82, 100]]},
        {"id": "hwmon2/pwm3", "name": "硬盘风扇", "group": "hdd",
         "curve": [[38, 20], [50, 25], [55, 40], [60, 55], [68, 75], [75, 100]]},
    ],
}
CPU_CHIP_HINTS = ("coretemp", "k10temp", "zenpower", "cpu")
DEFAULT_PORT = 9700
# 版本号：v1.<迭代次数>。2026-09-13 项目创建当日完成 70 次已部署迭代
VERSION = "v1.71"


def log(msg):
    print(time.strftime("[%Y-%m-%d %H:%M:%S] ") + str(msg), flush=True)


# ---------------------------------------------------------------- 后端：真实 sysfs / 模拟

class SysfsBackend:
    """真实 Linux sysfs 硬件监控接口"""

    def __init__(self, root="/sys"):
        self.root = root

    def hwmon_dirs(self):
        return sorted(glob.glob(os.path.join(self.root, "class", "hwmon", "hwmon*")), key=natural_key)

    def _max_state(self, cur_path):
        """读 cooling_device 的 max_state（档位上限）"""
        try:
            with open(os.path.join(os.path.dirname(cur_path), "max_state")) as f:
                return int(float(f.read().strip()))
        except (OSError, ValueError):
            return 0

    @staticmethod
    def _is_hwmon_pwm(path):
        return _re.search(r"/(pwm\d+)$", path) is not None

    def read(self, path):
        path = path.replace("\\", "/")
        try:
            with open(path, "r") as f:
                v = f.read().strip()
        except OSError:
            return None
        # hwmon pwmN: 芯片刻度 0-255 → 归一化为 0-100 百分比
        if self._is_hwmon_pwm(path):
            try:
                return "%.1f" % (float(v) * 100.0 / 255.0)
            except ValueError:
                return v
        # ACPI cooling_device: 把档位值归一化为 0-100 百分比
        if path.endswith("/cur_state"):
            mx = self._max_state(path)
            if mx:
                try:
                    return "%.1f" % (float(v) * 100.0 / mx)
                except ValueError:
                    return v
        return v

    def write(self, path, val):
        path = path.replace("\\", "/")
        try:
            # hwmon pwmN: 百分比 0-100 → 芯片刻度 0-255
            if self._is_hwmon_pwm(path):
                val = int(round(float(val) * 255.0 / 100.0))
            elif path.endswith("/cur_state"):
                mx = self._max_state(path)
                if mx:
                    val = int(round(float(val) * mx / 100.0))
            with open(path, "w") as f:
                f.write(str(val))
            return True
        except OSError as e:
            log("写入失败 %s = %s : %s" % (path, val, e))
            return False

    def list_files(self, d):
        return os.listdir(d)

    @property
    def simulated(self):
        return False


class SimBackend:
    """模拟模式：内存中虚拟一套 coretemp + nct6798，温度随 PWM 响应变化"""

    def __init__(self):
        self.t = 0.0
        self.cpu_temp = 45.0
        self.sys_temp = 38.0
        self.pwm = {1: 64, 2: 64, 3: 64}
        self.lock = threading.Lock()

    @property
    def simulated(self):
        return True

    def hwmon_dirs(self):
        return ["hwmon1(coretemp)", "hwmon2(nct6798)"]

    def list_files(self, d):
        if d.startswith("hwmon1"):
            return ["name", "temp1_input", "temp1_label", "temp2_input", "temp2_label",
                    "temp3_input", "temp3_label"]
        return ["name", "temp1_input", "temp1_label", "temp2_input", "temp2_label",
                "fan1_input", "fan2_input", "fan3_input",
                "pwm1", "pwm1_enable", "pwm2", "pwm2_enable", "pwm3", "pwm3_enable"]

    def _tick(self):
        with self.lock:
            self.t += 1
            # CPU 温度趋向目标：PWM 越低越热，并叠加缓慢的正弦扰动
            target = 40 + (100 - self.pwm[1]) * 0.42 + 6 * math.sin(self.t / 45)
            self.cpu_temp += (target - self.cpu_temp) * 0.18
            target2 = 34 + (100 - self.pwm[2]) * 0.22 + 3 * math.sin(self.t / 60 + 2)
            self.sys_temp += (target2 - self.sys_temp) * 0.15
        return self.cpu_temp, self.sys_temp

    def read(self, path):
        cpu, sys_t = self._tick()
        path = path.replace("\\", "/")
        noise = 0.3 * math.sin(self.t * 3.7)
        table = {
            "hwmon1(coretemp)/name": "coretemp",
            "hwmon1(coretemp)/temp1_input": (cpu + noise) * 1000,
            "hwmon1(coretemp)/temp1_label": "Package id 0",
            "hwmon1(coretemp)/temp2_input": (cpu + 2.5 + noise) * 1000,
            "hwmon1(coretemp)/temp2_label": "Core 0",
            "hwmon1(coretemp)/temp3_input": (cpu + 1.8 + noise) * 1000,
            "hwmon1(coretemp)/temp3_label": "Core 1",
            "hwmon2(nct6798)/name": "nct6798",
            "hwmon2(nct6798)/temp1_input": (sys_t + noise) * 1000,
            "hwmon2(nct6798)/temp1_label": "SYSTIN",
            "hwmon2(nct6798)/temp2_input": (cpu - 3 + noise) * 1000,
            "hwmon2(nct6798)/temp2_label": "CPUTIN",
            "hwmon2(nct6798)/fan1_input": 300 + self.pwm[1] * 11.5,
            "hwmon2(nct6798)/fan2_input": 250 + self.pwm[2] * 9.8,
            "hwmon2(nct6798)/fan3_input": 200 + self.pwm[3] * 8.2,
            "hwmon2(nct6798)/pwm1": self.pwm[1],
            "hwmon2(nct6798)/pwm1_enable": 1,
            "hwmon2(nct6798)/pwm2": self.pwm[2],
            "hwmon2(nct6798)/pwm2_enable": 1,
            "hwmon2(nct6798)/pwm3": self.pwm[3],
            "hwmon2(nct6798)/pwm3_enable": 1,
        }
        return table.get(path)

    def write(self, path, val):
        path = path.replace("\\", "/")
        for i in (1, 2, 3):
            if path.endswith("/pwm%d" % i):
                with self.lock:
                    self.pwm[i] = int(val)
                return True
            if path.endswith("/pwm%d_enable" % i):
                return True
        return False


def natural_key(s):
    return [int(t) if t.isdigit() else t for t in re_split_digits(str(s))]


import re as _re


def re_split_digits(s):
    return _re.split(r"(\d+)", s)


# ---------------------------------------------------------------- 硬件发现

def _ensure_fan_chip(backend):
    """自愈：宿主机重启后 it87 驱动可能未自动加载（ZeroNAS 未启用
    systemd-modules-load），容器启动时检测不到风扇芯片就经宿主机
    根目录加载一次。"""
    if getattr(backend, "simulated", False) or not os.path.isdir("/host/proc"):
        return
    try:
        for d in backend.hwmon_dirs():
            name = (backend.read(os.path.join(d, "name")) or "").lower()
            if "it86" in name or "nct" in name:
                return                          # 风扇芯片已在，无需处理
    except Exception:
        return
    for mp in ("/usr/sbin/modprobe", "/sbin/modprobe"):
        if os.path.exists(mp):
            try:
                r = subprocess.run(["chroot", "/host", mp, "it87",
                                    "force_id=0x8620"],
                                   capture_output=True, text=True, timeout=20)
                time.sleep(3)
                log("自愈: 加载 it87 驱动 rc=%s %s"
                    % (r.returncode, (r.stderr or "").strip()[:120]))
            except Exception as e:
                log("自愈: 加载 it87 失败 %s" % e)
            break


def discover(backend):
    """扫描 hwmon，返回 temps / fans / pwms 列表"""
    _ensure_fan_chip(backend)
    temps, fans, pwms = [], [], []
    for d in backend.hwmon_dirs():
        chip_name = backend.read(os.path.join(d, "name")) or "unknown"
        chip_id = d.rstrip("/").split("/")[-1]
        files = backend.list_files(d)
        # 温度
        for f in sorted(files, key=natural_key):
            m = _re.match(r"^(temp\d+)_input$", f)
            if not m:
                continue
            tid = m.group(1)
            label = backend.read(os.path.join(d, tid + "_label")) or tid
            temps.append({"id": "%s/%s" % (chip_id, tid), "chip": chip_name,
                          "label": label, "path": os.path.join(d, f)})
        # 风扇转速
        for f in sorted(files, key=natural_key):
            m = _re.match(r"^(fan\d+)_input$", f)
            if m:
                fans.append({"id": "%s/%s" % (chip_id, m.group(1)), "chip": chip_name,
                             "path": os.path.join(d, f)})
        # PWM 输出（精确匹配 pwmN，排除 pwmN_enable/pwmN_mode）
        for f in sorted(files, key=natural_key):
            m = _re.match(r"^(pwm\d+)$", f)
            if m:
                p = m.group(1)
                pwms.append({"id": "%s/%s" % (chip_id, p), "chip": chip_name,
                             "path": os.path.join(d, f),
                             "enable_path": os.path.join(d, p + "_enable")})
    # ACPI 风扇 cooling device（/sys/class/thermal/cooling_deviceN，type 含 fan）
    root = getattr(backend, "root", None)
    if root:
        for d in sorted(glob.glob(os.path.join(root, "class", "thermal", "cooling_device*")),
                        key=natural_key):
            t = backend.read(os.path.join(d, "type")) or ""
            if "fan" not in t.lower():
                continue
            pwms.append({"id": "thermal/%s" % os.path.basename(d),
                         "chip": "acpi-fan(%s)" % t,
                         "path": os.path.join(d, "cur_state"),
                         "max_state": backend.read(os.path.join(d, "max_state"))})
    # 硬盘温度（SMART）：非模拟环境且有 smartctl 可用时探测。
    # 盘位预设：4 个 SATA 位（sda~sdd）+ 2 个 M.2 NVMe 位（nvme0/nvme1），
    # 空位也生成通道（absent）→ 界面灰显"空位"、联动自动排除，插上即自动读温。
    if not getattr(backend, "simulated", False) and _find_smartctl():
        present_sd = {os.path.basename(d) for d in glob.glob("/dev/sd?")}
        for name in ("sda", "sdb", "sdc", "sdd"):
            temps.append({"id": "hdd/%s" % name, "chip": "SMART",
                          "label": name, "path": None,
                          "absent": name not in present_sd})
        # M.2 NVMe：控制器节点 /dev/nvmeN（命名空间 nvmeNn1 存在也算在位）
        present_nv = set()
        for d in glob.glob("/dev/nvme*"):
            m = _re.match(r"^(nvme\d+)", os.path.basename(d))
            if m:
                present_nv.add(m.group(1))
        for name in ("nvme0", "nvme1"):
            temps.append({"id": "hdd/%s" % name, "chip": "SMART",
                          "label": name, "path": None,
                          "absent": name not in present_nv})
        # 预设之外的盘（如 M.2 SATA 走 sdX、USB 盘），动态补通道
        for name in sorted(present_sd - {"sda", "sdb", "sdc", "sdd"}):
            temps.append({"id": "hdd/%s" % name, "chip": "SMART",
                          "label": name, "path": None, "absent": False})
    return temps, fans, pwms


# ---- 硬盘温度（SMART，经 smartctl 读取，带缓存避免频繁查询） ----

_smart_bin = None            # smartctl 可执行文件路径（懒加载）
_hdd_temp_cache = {}         # 盘名 -> (时间戳, 温度或 None)
HDD_TEMP_TTL = 30            # 硬盘温度缓存秒数（SMART 查询不宜过频）


def _find_smartctl():
    """寻找可用的 smartctl：优先宿主机挂载点，其次 PATH"""
    global _smart_bin
    if _smart_bin is None:
        for p in ("/host/usr/sbin/smartctl", "smartctl"):
            try:
                r = subprocess.run([p, "--version"], capture_output=True, timeout=5)
                if r.returncode == 0:
                    _smart_bin = p
                    break
            except Exception:
                continue
    return _smart_bin


def read_hdd_temp(dev_name):
    """读单块盘的温度（°C）；休眠盘返回 None（-n standby 不唤醒）"""
    now = time.time()
    hit = _hdd_temp_cache.get(dev_name)
    if hit and now - hit[0] < HDD_TEMP_TTL:
        return hit[1]
    binp = _find_smartctl()
    val = None
    if binp:
        try:
            r = subprocess.run([binp, "-A", "-n", "standby", "/dev/%s" % dev_name],
                               capture_output=True, text=True, timeout=15)
            if dev_name.startswith("nvme"):
                # NVMe 没有 194/190 属性，温度在 SMART/Health Information 段：
                # "Temperature:    45 Celsius"（毫开尔文单位行 Accurate 需跳过）
                for line in r.stdout.splitlines():
                    m = _re.match(r"\s*Temperature:\s+(\d+)\s+Celsius", line)
                    if m:
                        val = float(m.group(1))
                        break
            else:
                for line in r.stdout.splitlines():
                    parts = line.split()
                    if len(parts) >= 10 and parts[0] in ("194", "190"):
                        m = _re.match(r"(\d+)", parts[9])   # 原始值即温度
                        if m:
                            val = float(m.group(1))
                        break
        except Exception:
            val = None
    _hdd_temp_cache[dev_name] = (now, val)
    return val


def temp_zh(chip, label):
    """把传感器的 chip/label 翻译成中文显示名"""
    c = (chip or "").lower()
    m = _re.match(r"temp(\d+)$", label or "")
    n = m.group(1) if m else (label or "")
    if c == "smart":
        return "硬盘 %s" % (label or "?")
    if c == "coretemp":
        if (label or "").lower().startswith("package"):
            return "CPU 整体温度"
        mc = _re.match(r"Core (\d+)$", label or "", _re.I)
        if mc:
            return "CPU 核心 %s" % mc.group(1)
        return "CPU 温度 (%s)" % label
    if c == "acpitz":
        return "主板环境温度" if n == "1" else "ACPI 温度 %s" % n
    if c.startswith("it86") or c.startswith("it87"):
        return "主板温度 %s" % n
    return "%s/%s" % (chip, label)


def pwm_zh(p):
    """给 PWM 通道生成中文默认名"""
    chip = (p.get("chip") or "").lower()
    if chip.startswith("acpi-fan"):
        tail = (p["id"].rsplit("cooling_device", 1)[-1] or "?")
        return "ACPI 风扇通道 %s（固件无效，建议删除）" % tail
    if chip.startswith("it86") or chip.startswith("it87"):
        return "主板风扇接口 %s" % p["id"].split("/")[-1]
    return "%s %s" % (p.get("chip"), p["id"].split("/")[-1])


def read_temp(backend, t):
    if t.get("chip") == "SMART":
        if t.get("absent"):
            return None                     # 空盘位：不查询，直接无读数
        return read_hdd_temp(t["label"])   # 硬盘走 SMART，path 为 None
    v = backend.read(t["path"])
    if v is None:
        return None
    try:
        return float(v) / 1000.0
    except ValueError:
        return None


def fan_sensor_ids(temps, fc):
    """按联动分组解析风扇的传感器：cpu=全部 CPU 温度，hdd=全部在位 SATA 硬盘
    （空盘位自动排除；M.2/NVMe 温度不可靠——部分杂牌盘恒报固定值，不参与联动）。
    未设分组时回退到配置里的 sensors 列表。"""
    g = fc.get("group")
    if g == "cpu":
        ids = [t["id"] for t in temps
               if any(h in t["chip"].lower() or h in t["label"].lower()
                      for h in CPU_CHIP_HINTS)]
        if ids:
            return ids
    elif g == "hdd":
        ids = [t["id"] for t in temps
               if t["chip"] == "SMART" and not t.get("absent")
               and not (t.get("label") or "").startswith("nvme")]
        if ids:
            return ids
    return fc.get("sensors") or [t["id"] for t in temps]


def read_rpm(backend, fan):
    v = backend.read(fan["path"])
    try:
        return int(float(v)) if v is not None else None
    except ValueError:
        return None


def curve_pwm(curve, temp):
    pts = sorted([[float(p[0]), float(p[1])] for p in curve])
    if temp <= pts[0][0]:
        return pts[0][1]
    if temp >= pts[-1][0]:
        return pts[-1][1]
    for i in range(len(pts) - 1):
        t0, p0 = pts[i]
        t1, p1 = pts[i + 1]
        if t0 <= temp <= t1:
            return p0 + (p1 - p0) * (temp - t0) / (t1 - t0)
    return pts[-1][1]


def transform_curve(curve, mul, lo=None, hi=None):
    """按系数缩放曲线的 PWM 轴（用于派生默认的静音/均衡曲线）"""
    out = []
    for pt in curve:
        v = float(pt[1]) * mul
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        out.append([pt[0], int(round(v))])
    return out


def fan_curve_for(fc, mode):
    """取风扇在某功率档位下的曲线：
    auto=fc.curve；silent/balance=fc.curves[mode]，未单独配置时由自动曲线派生
    （静音=×0.6 下限 20%，均衡=×1.25 上限 95%）"""
    if mode in ("silent", "balance"):
        c = (fc.get("curves") or {}).get(mode)
        if isinstance(c, list) and len(c) >= 2:
            return c
        base = fc.get("curve", DEFAULT_CURVE)
        if mode == "silent":
            return transform_curve(base, 0.6, lo=20)
        return transform_curve(base, 1.25, hi=95)
    return fc.get("curve", DEFAULT_CURVE)


# ---------------------------------------------------------------- 控制器

class FanController:
    def __init__(self, backend, config_path):
        self.backend = backend
        self.config_path = config_path
        self.cfg = self.load_config()
        self.temps, self.fans, self.pwms = discover(backend)
        self.lock = threading.Lock()
        self.status = {"temps": [], "fans": []}
        self.ema = {}           # 温度平滑缓存 id -> value
        self.cur_pwm = {}       # pwm id -> 上次写入值
        self.fail_cnt = {}      # pwm id -> 传感器连续失败次数
        self.orig_enable = {}   # pwm id -> 原始 pwm_enable 值（退出时恢复）
        self.manual = {}        # pwm id -> None 或 0-100
        self.running = True
        self._map_pwn_entries()
        self._takeover_manual_mode()

    # ----- 配置 -----
    def default_config(self):
        return {
            "port": DEFAULT_PORT,
            "mode": "auto",
            "interval": 2,
            "failsafe_temp": 85,
            "down_step": 1,
            "min_change": 2,
            "fans": [],
        }

    def load_config(self):
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except FileNotFoundError:
            log("配置文件不存在: %s（将以默认参数运行，无风扇条目）" % self.config_path)
            return self.default_config()
        except (ValueError, OSError) as e:
            log("配置文件读取失败: %s" % e)
            return self.default_config()
        base = self.default_config()
        base.update({k: v for k, v in cfg.items() if k != "fans"})
        base["fans"] = cfg.get("fans", [])
        return base

    def save_config(self):
        # 先写临时文件再原子替换，旧配置保留为 .bak（防止写坏/误清后无法恢复）
        tmp = self.config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        if os.path.exists(self.config_path):
            os.replace(self.config_path, self.config_path + ".bak")
        os.replace(tmp, self.config_path)
        log("配置已保存: %s" % self.config_path)

    def _map_pwn_entries(self):
        """把配置里的 pwm id 映射到实际发现的 pwm 条目。
        重启后 hwmon 编号可能变化（如 hwmon2→hwmon3），精确 id 找不到时
        按「pwm 编号」兜底重匹配，优先选有对应转速计（fanN_input）的芯片。"""
        by_id = {p["id"]: p for p in self.pwms}
        self.entries = []
        for fc in self.cfg.get("fans", []):
            p = by_id.get(fc.get("id"))
            if p is None and fc.get("id") and "/" in fc["id"]:
                suffix = fc["id"].split("/", 1)[1]          # 如 pwm2
                cands = [c for c in self.pwms
                         if c["id"].split("/", 1)[1] == suffix]
                if len(cands) > 1:
                    def _has_tach(c):
                        fid = c["id"].replace("/pwm", "/fan")
                        return any(f["id"] == fid for f in self.fans)
                    cands.sort(key=lambda c: not _has_tach(c))
                if cands:
                    p = cands[0]
                    log("风扇 %s 未找到，按编号重匹配为 %s" % (fc["id"], p["id"]))
                    fc["id"] = p["id"]                      # 回写，避免每次都重匹配
            self.entries.append({"cfg": fc, "dev": p})

    # ----- 手动接管 pwm_enable -----
    def _takeover_manual_mode(self):
        for e in self.entries:
            dev = e["dev"]
            if not dev or not dev.get("enable_path"):
                continue
            cur = self.backend.read(dev["enable_path"])
            if cur is not None:
                try:
                    self.orig_enable[dev["id"]] = int(float(cur))
                except ValueError:
                    pass
                if str(cur) != "1":
                    if self.backend.write(dev["enable_path"], 1):
                        log("pwm_enable %s: %s -> 1 (手动接管)" % (dev["id"], cur))
            cur_pwm = self.backend.read(dev["path"])
            if cur_pwm is not None:
                try:
                    self.cur_pwm[dev["id"]] = int(float(cur_pwm))
                except ValueError:
                    pass

    def restore(self):
        for pid, val in self.orig_enable.items():
            for e in self.entries:
                if e["dev"] and e["dev"]["id"] == pid:
                    self.backend.write(e["dev"]["enable_path"], val)
                    log("恢复 pwm_enable %s = %s" % (pid, val))

    # ----- 主循环 -----
    def loop(self):
        interval = max(1, int(self.cfg.get("interval", 2)))
        while self.running:
            try:
                self.tick()
            except Exception as e:
                log("控制循环异常: %r" % e)
            time.sleep(interval)

    def tick(self):
        cfg = self.cfg
        failsafe_temp = float(cfg.get("failsafe_temp", 85))
        down_step = float(cfg.get("down_step", 1))
        min_change = float(cfg.get("min_change", 2))
        mode = cfg.get("mode", "auto")
        if mode not in ("auto", "silent", "balance", "full"):
            mode = "auto"

        # 1) 读取全部温度 + EMA 平滑
        temp_vals = {}
        now = time.time()
        for t in self.temps:
            v = read_temp(self.backend, t)
            old = self.ema.get(t["id"])
            if v is None:
                if old is not None:
                    temp_vals[t["id"]] = old
                continue
            self.ema[t["id"]] = v if old is None else old * 0.5 + v * 0.5
            temp_vals[t["id"]] = self.ema[t["id"]]

        temp_view = []
        for t in self.temps:
            c = round(temp_vals[t["id"]], 1) if t["id"] in temp_vals else None
            # 明显超出物理合理范围的读数 = 该通道没接传感器（悬空引脚），界面不显示
            invalid = c is not None and not (-30 <= c <= 120)
            temp_view.append({"id": t["id"], "chip": t["chip"], "label": t["label"],
                              "zh": temp_zh(t["chip"], t["label"]),
                              "c": None if invalid else c, "invalid": invalid,
                              "absent": bool(t.get("absent"))})

        # 2) 逐风扇计算目标 PWM
        fan_view = []
        rpm_by_id = {}
        for f in self.fans:
            rpm_by_id[f["id"]] = read_rpm(self.backend, f)

        for i, e in enumerate(self.entries):
            dev, fc = e["dev"], e["cfg"]
            if not dev:
                fan_view.append({"id": fc.get("id"), "name": fc.get("name", "?"),
                                 "missing": True, "pwm": None, "rpm": None,
                                 "target": None, "manual": self.manual.get(fc.get("id"))})
                continue
            pid = dev["id"]
            sensor_ids = fan_sensor_ids(self.temps, fc)
            vals = [temp_vals[s] for s in sensor_ids if s in temp_vals
                    and temp_vals[s] is not None]
            if vals:
                tmax = max(vals)
                self.fail_cnt[pid] = 0
            else:
                tmax = None
                self.fail_cnt[pid] = self.fail_cnt.get(pid, 0) + 1

            manual = self.manual.get(pid)
            curve_target = None
            failsafe = False
            if mode == "full":
                curve_target = 100.0  # 全速：临时档，恒 100%
            elif tmax is not None:
                curve_target = curve_pwm(fan_curve_for(fc, mode), tmax)
            target = curve_target
            if manual is not None:
                target = float(manual)
            # 保护: 温度超限 或 传感器连续读不到 -> 全速
            if (tmax is not None and tmax >= failsafe_temp) or \
               (tmax is None and self.fail_cnt[pid] >= 5):
                target = 100.0
                failsafe = True

            cur = self.cur_pwm.get(pid, 64)
            new = cur
            if failsafe:
                new = 100.0
            elif manual is not None:
                new = max(0, min(100, float(manual)))  # 手动：精确立即写入，不走缓降/最小变化量
            elif target is not None:
                if target >= cur:
                    new = target            # 升速立即响应
                else:
                    new = max(target, cur - down_step)  # 降速缓降防抖
                new = max(0, min(100, new))
            descending = (not failsafe and manual is None
                          and target is not None and target < cur)
            if failsafe or manual is not None or descending \
               or abs(new - cur) >= min_change:
                iv = int(round(new))
                # enable 自检：BIOS/驱动重载可能把 pwmN_enable 重置回 2（自动），
                # 此时硬件会忽略 pwm 写入，发现不是 1 就重新接管
                en_path = dev.get("enable_path")
                if en_path:
                    en = self.backend.read(en_path)
                    if en is not None and str(en).strip() != "1":
                        if self.backend.write(en_path, 1):
                            log("pwm_enable %s: %s -> 1 (重新接管)"
                                % (pid, str(en).strip()))
                if self.backend.write(dev["path"], iv):
                    self.cur_pwm[pid] = new
                    if failsafe or manual is not None or descending \
                       or abs(new - cur) >= 3:
                        log("%s: %s -> PWM %d%s" % (
                            fc.get("name", pid),
                            ("手动 %.0f%%" % manual) if manual is not None
                            else "%.0f°C" % (tmax or -1),
                            iv, "  [紧急全速]" if failsafe else ""))
            fan_view.append({
                "id": pid, "name": fc.get("name", pid), "missing": False,
                "pwm": int(round(self.cur_pwm.get(pid, 0))),
                "rpm": rpm_by_id.get(pid.replace("/pwm", "/fan")),
                "target": int(round(target)) if target is not None else None,
                "curve_target": int(round(curve_target)) if curve_target is not None else None,
                "temp": round(tmax, 1) if tmax is not None else None,
                "manual": manual, "failsafe": failsafe,
                "sensors": sensor_ids,
                "curve": fc.get("curve", DEFAULT_CURVE),
            })

        with self.lock:
            self.status = {
                "sim": self.backend.simulated,
                "time": now,
                "version": VERSION,
                "mode": mode,
                "interval": cfg.get("interval", 2),
                "failsafe_temp": cfg.get("failsafe_temp", 85),
                "temps": temp_view,
                "fans": fan_view,
            }

    # ----- 自动检测 -----
    def autodetect_fans(self):
        """重新扫描硬件，为每个 PWM 通道生成风扇条目（CPU 传感器优先联动）"""
        self.temps, self.fans, self.pwms = discover(self.backend)
        # 跳过固件无效的 ACPI 通道（thermal/cooling_deviceN）
        usable = [p for p in self.pwms if not p["id"].startswith("thermal/")]

        def _has_fan(p):
            # hwmon 通道：对应 fanN 转速有读数才算接了风扇
            m = _re.match(r"^pwm(\d+)$", p["id"].split("/")[-1])
            if not m:
                return True
            fan_id = "%s/fan%s" % (p["id"].split("/")[0], m.group(1))
            for f in self.fans:
                if f["id"] == fan_id:
                    return bool(read_rpm(self.backend, f))
            return False

        filtered = [p for p in usable if _has_fan(p)]
        if filtered:
            usable = filtered
        # 保护：一个可用通道都没有（多为驱动未加载），绝不能清空现有配置
        if not usable:
            log("自动检测中止: 未发现可用 PWM 通道（疑似驱动未加载），保留原配置")
            raise RuntimeError("未发现可用的 PWM 通道（疑似主板风扇驱动未加载），已保留现有配置")
        cpu_temps = [t["id"] for t in self.temps
                     if any(h in t["chip"].lower() or h in t["label"].lower()
                            for h in CPU_CHIP_HINTS)]
        sensor_ids = cpu_temps or [t["id"] for t in self.temps]
        # 保留已有条目的自定义设置（名字/曲线/联动分组/传感器），按通道 id 匹配
        old = {f.get("id"): f for f in self.cfg.get("fans", [])}
        self.cfg["fans"] = [
            {"id": p["id"],
             "name": old.get(p["id"], {}).get("name") or pwm_zh(p),
             "group": old.get(p["id"], {}).get("group"),
             "sensors": old.get(p["id"], {}).get("sensors") or sensor_ids,
             "curve": old.get(p["id"], {}).get("curve") or
                      [list(pt) for pt in DEFAULT_CURVE]}
            for p in usable
        ]
        self._map_pwn_entries()
        self._takeover_manual_mode()
        self.save_config()
        log("自动检测: 发现 %d 个 PWM 通道，已生成风扇配置" % len(usable))

    # ----- 配置热更新 -----
    def apply_config(self, new_cfg):
        base = self.default_config()
        base.update({k: v for k, v in new_cfg.items() if k != "fans"})
        # mode 持久档只允许 auto/silent/balance（full 是临时档，不落盘）
        if base.get("mode") not in ("auto", "silent", "balance"):
            base["mode"] = "auto"
        if not isinstance(new_cfg.get("fans"), list):
            raise ValueError("fans 必须是数组")
        for fc in new_cfg["fans"]:
            if not fc.get("id"):
                raise ValueError("风扇缺少 id")
            curve = fc.get("curve", DEFAULT_CURVE)
            if not isinstance(curve, list) or len(curve) < 2:
                raise ValueError("曲线至少需要 2 个点")
            for pt in curve:
                if not (isinstance(pt, list) and len(pt) == 2):
                    raise ValueError("曲线点格式应为 [温度, 转速%]")
        base["fans"] = new_cfg["fans"]
        self.cfg = base
        self._map_pwn_entries()
        self.save_config()

    def stop(self, *_):
        self.running = False


# ---------------------------------------------------------------- Web UI

# ZeroNAS 桌面图标（应用商店快捷方式引用 /icon.svg），页面标题也复用同一图标
FAN_ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">'
    '<defs>'
    '<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0" stop-color="#1b3d4d"/><stop offset="1" stop-color="#091e28"/>'
    '</linearGradient>'
    '<linearGradient id="bl" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0" stop-color="#7ceaff"/><stop offset="1" stop-color="#1f9db2"/>'
    '</linearGradient>'
    '</defs>'
    '<rect width="128" height="128" rx="30" fill="url(#bg)"/>'
    '<rect x="3.5" y="3.5" width="121" height="121" rx="27" fill="none" stroke="#35c3d6" stroke-opacity=".35" stroke-width="3"/>'
    '<g fill="url(#bl)">'
    '<path d="M64 55 C64 36 55 27 43 27 C37 27 33 31 33 37 C33 47 47 53 64 55 Z"/>'
    '<path d="M64 55 C64 36 55 27 43 27 C37 27 33 31 33 37 C33 47 47 53 64 55 Z" transform="rotate(90 64 64)"/>'
    '<path d="M64 55 C64 36 55 27 43 27 C37 27 33 31 33 37 C33 47 47 53 64 55 Z" transform="rotate(180 64 64)"/>'
    '<path d="M64 55 C64 36 55 27 43 27 C37 27 33 31 33 37 C33 47 47 53 64 55 Z" transform="rotate(270 64 64)"/>'
    '</g>'
    '<circle cx="64" cy="64" r="9" fill="#eafcff"/>'
    '<circle cx="64" cy="64" r="4" fill="#0d2833"/>'
    '</svg>'
)

WEB_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>fanctl · 风扇调速</title>
<style>
  :root{
    --bg:#000000; --panel:#0d0d0d; --panel2:#181818; --line:#000000;
    --bartrack:#232323; --barline:#000000;
    --txt:#f3ece1; --dim:#9c9c9c; --accent:#35c3d6; --warn:#f0a020; --ok:#4cc38a; --bad:#e5484d;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--txt);font:14px/1.6 "Segoe UI","Microsoft YaHei",sans-serif;padding:24px;zoom:1.5}
  .wrap{max-width:1080px;margin:0 auto}
  h1{font-size:20px;font-weight:600;display:flex;align-items:center;gap:10px}
  .badge{font-size:11px;padding:2px 8px;border-radius:10px;background:var(--panel2);color:var(--accent);border:1px solid var(--line)}
  .grid{display:grid;grid-template-columns:1fr 380px;gap:16px;margin-top:16px}
  @media(max-width:900px){.grid{grid-template-columns:1fr}}
  /* 手机端：取消 150% 缩放，单列布局自然撑满，间距收紧 */
  @media(max-width:700px){
    body{zoom:1;padding:12px}
    .grid{gap:12px;margin-top:12px}
    h1{font-size:17px;flex-wrap:wrap;row-gap:6px}
    /* 档位组与右侧按钮并排一行：档位收紧 + 恢复默认缩为图标 */
    .presets{margin-left:0}
    .presets button{padding:4px 10px;font-size:12px}
    #resetCfg .rst-txt{display:none}
    #themeBtn .theme-txt{display:none}
    .card{padding:12px}
  }
  .rightcol{display:flex;flex-direction:column;gap:16px}
  .rightcol>.card{margin-top:0}
  .rc-rpm{flex:1;display:flex;flex-direction:column}
  .leftcol{display:flex;flex-direction:column}
  .leftcol .manual{flex:1}   /* 左栏弹性卡：右栏更高时吸收高度差，保证两栏底边对齐 */
  .rc-rpm .fanlist{flex:1;margin-top:10px;gap:12px}
  .rc-rpm .fan{flex:1;display:flex;flex-direction:column;justify-content:center}
  .rc-rpm .fan .name{font-size:14px}
  .rc-rpm .fan .meta{font-size:13px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}
  .card h2{font-size:13px;font-weight:600;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-bottom:10px}
  .temps{display:grid;grid-template-columns:1fr 1fr;gap:8px}
  .temps.dense{gap:6px}
  .temps.dense .chip{padding:4px 9px;font-size:11px}
  .temps.dense .chip b{font-size:13.5px}
  .chip{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:6px 12px;font-size:12px;white-space:nowrap}
  .chip b{font-size:15px;color:var(--accent)}
  .chip.off{background:transparent;border-style:dashed;color:var(--dim);display:flex;justify-content:space-between;align-items:center}
  .chip.off b{font-size:11px;color:var(--dim);font-weight:400;background:var(--panel2);border-radius:8px;padding:1px 8px}
  .fanlist{display:flex;flex-direction:column;gap:10px;margin-top:8px}
  .fan{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:10px 14px}
  .fan .row{display:flex;justify-content:space-between;align-items:center}
  .fan .name{font-weight:600}
  .fan .meta{font-size:12px;color:var(--dim)}
  .fan .sub{display:flex;justify-content:space-between;align-items:center;margin-top:8px;font-size:12px;color:var(--dim)}
  .fan .sub b{font-weight:600;font-size:12.5px;color:var(--txt)}
  .fan .sub b.tmp{color:var(--accent)}
  .bar{height:6px;background:var(--bartrack,#232323);border:1px solid var(--barline,transparent);border-radius:3px;margin-top:6px;overflow:hidden}
  .bar>i{display:block;height:100%;background:linear-gradient(90deg,#2a8fa0,var(--accent));border-radius:3px;transition:width .5s}
  .fs{color:var(--bad);font-size:11px;font-weight:600}
  select,input[type=number],input[type=text]{background:var(--panel2);color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:5px 8px;font:inherit}
  button{background:var(--accent);color:#04222a;border:0;border-radius:8px;padding:8px 18px;font-weight:600;cursor:pointer}
  button.ghost{background:var(--panel2);color:var(--txt);border:1px solid var(--line)}
  button:hover{filter:brightness(1.1)}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  #curve{width:100%;height:auto;touch-action:none;user-select:none}
  .hint{font-size:12px;color:var(--dim);margin-top:6px}
  .setgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:4px}
  .fld{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--dim)}
  .fld input{width:100%}
  .sechead{display:flex;justify-content:space-between;align-items:center;margin-top:10px;font-size:12px;color:var(--dim)}
  .sechead a{color:var(--accent);cursor:pointer}
  .sechead a:hover{text-decoration:underline}
  .sensors{display:grid;grid-template-columns:repeat(3,1fr);gap:5px;margin-top:5px}
  .sensors label{font-size:11.5px;background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:3px 6px;cursor:pointer;display:flex;gap:4px;align-items:center;white-space:nowrap}
  .manual{margin-top:12px;padding-top:12px;border-top:1px dashed var(--line)}
  .manual input[type=range]{width:100%;accent-color:var(--accent)}
  .num{width:70px}
  .btnrow{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}
  #toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:var(--ok);color:#03230f;padding:8px 20px;border-radius:20px;font-weight:600;opacity:0;transition:.3s;pointer-events:none}
  #toast.err{background:var(--bad);color:#fff}
  kbd{background:var(--panel2);border:1px solid var(--line);border-radius:4px;padding:0 5px;font-size:11px}
  .presets{display:inline-flex;margin-left:12px;border:1px solid var(--line);border-radius:16px;overflow:hidden;vertical-align:middle}
  .presets button{background:transparent;color:var(--dim);border:none;padding:4px 13px;font-size:12.5px;cursor:pointer;font-family:inherit;transition:.15s}
  .presets button+button{border-left:1px solid var(--line)}
  .presets button:hover{color:var(--txt)}
  .presets button.on{background:var(--accent);color:#fff;font-weight:600}
  /* 浅色主题：白底黑字，注释/辅助文字深灰 */
  body.light{
    --bg:#ffffff; --panel:#ffffff; --panel2:#f3f3f3; --line:#dcdcdc;
    --txt:#111111; --dim:#555555;
    --accent:#0b8fa5; --warn:#b96e00;
    --bartrack:#ffffff; --barline:#dcdcdc;
  }
  /* 深色模式：边框全黑，卡片靠阴影区分；标题类文字纯白 */
  body:not(.light) .card{box-shadow:0 0 0 1px rgba(255,255,255,.05),0 6px 22px rgba(0,0,0,.55)}
  body:not(.light) .fan{box-shadow:0 0 0 1px rgba(255,255,255,.04)}
  body:not(.light) h1{color:#ffffff}
  body:not(.light) .card h2{color:#ffffff}
  body:not(.light) .badge{color:var(--accent);border-color:rgba(255,255,255,.14)}
</style>
</head>
<body>
<div class="wrap">
  <h1><img src="/icon.svg" alt="" style="width:30px;height:30px">fanctl 风扇调速 <span class="badge" id="mode">—</span>
    <span class="presets" id="presets" title="一键功率场景：静音/均衡为独立曲线并持久保存，全速为临时档（重启恢复）">
      <button data-mode="auto" class="on">自动</button>
      <button data-mode="silent">静音</button>
      <button data-mode="balance">均衡</button>
      <button data-mode="full">全速</button>
    </span>
    <span style="margin-left:auto;display:flex;gap:8px">
      <button class="ghost" id="resetCfg" title="恢复出厂设置：重置三档曲线、档位与全局参数" style="padding:4px 10px;font-size:13px">↩️<span class="rst-txt"> 恢复默认</span></button>
      <button class="ghost" id="themeBtn" style="padding:4px 12px;font-size:13px">☀️<span class="theme-txt"> 浅色</span></button>
    </span></h1>

  <div class="grid">
    <div class="leftcol">
      <div class="card">
        <h2>温度 - 转速曲线</h2>
        <div class="row" style="margin-bottom:8px">
          <span style="color:var(--dim);font-size:12px">风扇</span>
          <select id="fanSel"></select>
          <input type="text" id="fanName" style="flex:1;min-width:120px" placeholder="风扇名称">
        </div>
        <svg id="curve" viewBox="0 0 640 330"></svg>
        <div class="hint">拖动圆点调整曲线 · 点击空白处加点 · 双击圆点删点 · 编辑的是当前档位的曲线，<kbd>保存</kbd> 后立即生效（热更新，无需重启）</div>
      </div>

      <div class="card manual">
        <h2>手动模式</h2>
        <div class="row">
          <label style="font-size:13px"><input type="checkbox" id="manOn"> 开启手动定速（当前风扇）</label>
          <input type="range" id="manVal" min="0" max="100" value="50" disabled>
          <b id="manShow" style="min-width:44px">50%</b>
          <button class="ghost" id="manApply" disabled>应用</button>
        </div>
        <div class="hint">手动模式优先于曲线；取消勾选立即恢复自动调速。0% 时部分主板风扇会停转。</div>
      </div>
    </div>

    <div class="rightcol">
      <div class="card">
        <h2>实时温度</h2>
        <div class="temps" id="temps"></div>
      </div>

      <div class="card rc-rpm">
        <h2>实时转速</h2>
        <div class="fanlist" id="fans"></div>
      </div>

      <div class="card">
        <h2>全局设置</h2>
        <div class="setgrid">
          <label class="fld"><span>紧急全速阈值 °C</span><input type="number" id="fsTemp" min="40" max="110"></label>
          <label class="fld"><span>采样间隔 秒</span><input type="number" id="interval" min="1" max="60"></label>
        </div>
        <div class="btnrow">
          <button id="save">💾 保存配置</button>
          <button class="ghost" id="autodetect">🔍 自动检测风扇</button>
        </div>
        <div class="hint" id="saveMsg"></div>
      </div>
    </div>
  </div>
</div>
<div id="toast"></div>

<script>
const W=640,H=330,L=46,R=18,T=14,B=30;
const TD0=20,TD1=95;
let cfg=null,status=null,sel=0,pts=[],drag=null,curMode='auto';
const $=id=>document.getElementById(id);
const svg=$('curve');
const X=t=>L+(t-TD0)/(TD1-TD0)*(W-L-R);
const Y=p=>T+(1-p/100)*(H-T-B);
const iX=x=>TD0+(x-L)/(W-L-R)*(TD1-TD0);
const iY=y=>(1-(y-T)/(H-T-B))*100;

function toast(msg,err){const t=$('toast');t.textContent=msg;t.className=err?'err':'';t.style.opacity=1;setTimeout(()=>t.style.opacity=0,1800);}

async function api(path,opt){const r=await fetch(path,opt);if(!r.ok)throw new Error(await r.text());return r.json();}

function drawAxes(){
  let s='';
  for(let t=20;t<=95;t+=15)s+=`<line x1="${X(t)}" y1="${T}" x2="${X(t)}" y2="${H-B}" stroke="var(--line)" stroke-width="1" opacity=".5"/><text x="${X(t)}" y="${H-B+16}" fill="var(--dim)" font-size="10" text-anchor="middle">${t}°</text>`;
  for(let p=0;p<=100;p+=25)s+=`<line x1="${L}" y1="${Y(p)}" x2="${W-R}" y2="${Y(p)}" stroke="var(--line)" stroke-width="1" opacity=".5"/><text x="${L-6}" y="${Y(p)+4}" fill="var(--dim)" font-size="10" text-anchor="end">${p}%</text>`;
  s+=`<rect x="${L}" y="${T}" width="${W-L-R}" height="${H-T-B}" fill="rgba(53,195,214,.04)" stroke="var(--line)"/>`;
  return s;
}

function drawCurve(){
  const f=status&&status.fans[sel];
  let s=drawAxes();
  if(pts.length>1){
    s+=`<polyline points="${pts.map(p=>X(p[0])+','+Y(p[1])).join(' ')}" fill="none" stroke="var(--accent)" stroke-width="2.5"/>`;
  }
  // 当前工作点
  if(f&&f.temp!=null&&f.pwm!=null){
    s+=`<circle cx="${X(Math.min(TD1,Math.max(TD0,f.temp)))}" cy="${Y(f.pwm)}" r="5" fill="var(--warn)" opacity=".9"><animate attributeName="r" values="4;6;4" dur="1.5s" repeatCount="indefinite"/></circle>`;
    s+=`<text x="${X(Math.min(TD1,Math.max(TD0,f.temp)))+9}" y="${(Y(f.pwm)-7<T+8)?Y(f.pwm)+16:Y(f.pwm)-7}" fill="var(--warn)" font-size="11">${f.temp}°C</text>`;
  }
  pts.forEach((p,i)=>{
    s+=`<circle class="pt" data-i="${i}" cx="${X(p[0])}" cy="${Y(p[1])}" r="7" fill="var(--panel2)" stroke="var(--accent)" stroke-width="2.5" style="cursor:grab"/>`;
    const ly=(Y(p[1])-11<T+8)?Y(p[1])+18:Y(p[1])-11;               // 顶部不裁剪：标签翻到点下方
    const lx=X(p[0]), anchor='middle';                              // 右缘不裁剪：改为右对齐
    const ax=(X(p[0])>W-R-34)?X(p[0])-8:lx, an=(X(p[0])>W-R-34)?'end':anchor;
    s+=`<text x="${ax}" y="${ly}" fill="var(--txt)" font-size="10" text-anchor="${an}">${Math.round(p[0])}°/${p[1]}%</text>`;
  });
  svg.innerHTML=s;
}

// 界面上只展示有实际参考价值的温度：硬盘（SMART）+ 主板环境温度 + CPU 整体温度
// 空盘位（absent）也显示，但灰显
function tempVisible(t, forSensors){
  if(t.invalid)return false;
  if(t.c==null&&!t.absent)return false;
  const zh=t.zh||'';
  if(t.chip==='SMART')return true;                       // 所有硬盘温度
  if(forSensors)return zh==='CPU 整体温度';               // 联动列表：仅 CPU 整体
  return zh==='主板环境温度'||zh==='CPU 整体温度';         // 实时状态：再加主板环境
}

function renderStatus(){
  if(!status)return;
  const mans=status.fans.filter(f=>f.manual!=null).length;
  curMode=status.mode||'auto';
  const MODE_ZH={silent:'静音曲线',balance:'均衡曲线',full:'全速曲线'};
  $('mode').textContent=status.sim?'模拟模式'
    :mans>0?`${mans}/${status.fans.length} 手动`
    :curMode!=='auto'?MODE_ZH[curMode]
    :'自动调速';
  renderPresets();
  const tv=$('temps');tv.innerHTML='';
  status.temps.filter(t=>tempVisible(t,false)).forEach(t=>{
    const d=document.createElement('div');d.className='chip';
    if(t.absent){
      d.classList.add('off');d.title='空盘位（未插入硬盘）';
      d.innerHTML=`${t.zh||t.chip+'/'+t.label} <b>空位</b>`;
    }else{
      d.innerHTML=`${t.zh||t.chip+'/'+t.label} <b>${t.c}°C</b>`;d.title=`${t.chip}/${t.label}`;
    }
    tv.appendChild(d);
  });
  // 盘数多时（>=7 块温度芯片，即 5 盘以上）自动紧凑化，防止右栏超过左栏高度
  tv.classList.toggle('dense', tv.children.length>=7);
  const fl=$('fans');fl.innerHTML='';
  status.fans.forEach((f,i)=>{
    const d=document.createElement('div');d.className='fan';
    const fsTag=f.failsafe?' <span class="fs">⚠ 紧急全速</span>':'';
    const manTag=f.manual!=null?` <span style="color:var(--warn);font-size:11px">手动 ${f.manual}%</span>`:'';
    // 目标转速估算：按当前转速与 PWM 占比线性推算（取整到 10 RPM）
    let tgtRpm='';
    if(f.rpm!=null&&f.target!=null&&f.pwm>0)
      tgtRpm=`（约 ${Math.round(f.rpm*f.target/f.pwm/10)*10} RPM）`;
    let sub='';
    if(!f.missing)
      sub=`<div class="sub"><span>联动温度 <b class="tmp">${f.temp!=null?f.temp+'°C':'—'}</b></span>
           <span>目标 <b>${f.target!=null?f.target+'%':'—'}</b>${tgtRpm}</span></div>`;
    d.innerHTML=`<div class="row"><span class="name">${f.name}${manTag}${fsTag}</span>
      <span class="meta">${f.rpm!=null?f.rpm+' RPM':'—'} · PWM ${f.pwm??'—'}%</span></div>
      <div class="bar"><i style="width:${f.pwm||0}%"></i></div>${sub}`;
    fl.appendChild(d);
  });
}

function curCurve(f){
  // 当前档位对应的曲线：auto=f.curve；silent/balance=f.curves[mode]，
  // 未单独配置时按系数派生（与后端 fan_curve_for 一致），不落盘直到用户保存
  if(curMode==='silent'||curMode==='balance'){
    const c=f.curves&&f.curves[curMode];
    if(c&&c.length>=2)return c;
    const mul=curMode==='silent'?0.6:1.25;
    return f.curve.map(p=>[p[0],Math.round(curMode==='silent'
      ?Math.max(20,p[1]*mul):Math.min(95,p[1]*mul))]);
  }
  return f.curve;
}
function loadSelected(){
  const f=cfg.fans[sel];if(!f)return;
  $('fanName').value=f.name||'';
  pts=curCurve(f).map(p=>[p[0],p[1]]);
  const m=status&&status.fans[sel];
  $('manOn').checked=m&&m.manual!=null;
  $('manVal').disabled=$('manApply').disabled=!$('manOn').checked;
  if(m&&m.manual!=null){$('manVal').value=m.manual;$('manShow').textContent=m.manual+'%';}
  drawCurve();
}

function selectFan(i){sel=i;loadSelected();}

function rebuildFans(){
  const s=$('fanSel');s.innerHTML='';
  cfg.fans.forEach((f,i)=>{const o=document.createElement('option');o.value=i;o.textContent=(f.name||f.id);s.appendChild(o)});
  sel=0;loadSelected();
}

async function refresh(){
  try{
    status=await api('/api/status');
    const first=!cfg;
    if(first){cfg=await api('/api/config');
      rebuildFans();
      $('fsTemp').value=cfg.failsafe_temp;$('interval').value=cfg.interval;
      loadSelected();
    }
    renderStatus();drawCurve();
  }catch(e){$('mode').textContent='连接中断，重试中…';}
}

// ---- 自动检测 ----
$('autodetect').addEventListener('click',async()=>{
  try{
    const r=await api('/api/autodetect',{method:'POST'});
    cfg=r.config;
    rebuildFans();
    toast(r.pwms?('✓ 检测到 '+r.pwms+' 个在转的风扇，已生成配置（现有名字和曲线已保留）'):'⚠ 未发现在转的风扇，可能需要加载驱动');
    if(!r.pwms)$('saveMsg').textContent='未发现 PWM：需要 nct6775/it87 驱动，见 README';
  }catch(e){toast('检测失败: '+e.message,true);}
});

$('resetCfg').addEventListener('click',async()=>{
  if(!confirm('确定恢复默认设置吗？\n\n将重置自动/静音/均衡三档曲线、档位和全局参数（手动绘制的曲线会清空），手动定速也会取消。'))return;
  try{
    const r=await api('/api/reset',{method:'POST'});
    cfg=r.config;
    $('fsTemp').value=cfg.failsafe_temp;$('interval').value=cfg.interval;
    rebuildFans();
    $('saveMsg').textContent='已恢复默认模板';
    toast('✓ 已恢复默认设置');
  }catch(e){toast('恢复失败: '+e.message,true);}
});

// ---- 一键功率场景（每档一条独立曲线；auto/silent/balance 持久，full 临时）----
const PRESET_NAME={silent:'静音曲线',balance:'均衡曲线',full:'全速曲线',auto:'自动'};
function renderPresets(){
  const cur=(status&&status.mode)||'auto';
  document.querySelectorAll('#presets button').forEach(b=>b.classList.toggle('on',b.dataset.mode===cur));
}
document.querySelectorAll('#presets button').forEach(b=>{
  b.addEventListener('click',async()=>{
    try{
      const mode=b.dataset.mode;
      await api('/api/preset',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({mode})});
      toast(mode==='auto'?'✓ 已恢复自动曲线':('✓ 已切换到'+PRESET_NAME[mode]+(mode==='full'?'（临时，重启后恢复）':'')));
      refresh();
    }catch(e){toast('失败: '+e.message,true);}
  });
});

// ---- 曲线编辑交互 ----
function svgPoint(ev){
  const r=svg.getBoundingClientRect();
  return [ (ev.clientX-r.left)/r.width*W, (ev.clientY-r.top)/r.height*H ];
}
svg.addEventListener('pointerdown',ev=>{
  const pt=ev.target.closest('.pt');
  if(pt){drag=+pt.dataset.i;svg.setPointerCapture(ev.pointerId);return;}
  if(ev.target.tagName==='rect'||ev.target.tagName==='svg'){
    const [x,y]=svgPoint(ev);
    if(x<L||x>W-R||y<T||y>H-B)return;
    pts.push([Math.round(iX(x)),Math.round(iY(y))]);
    pts.sort((a,b)=>a[0]-b[0]);drawCurve();
  }
});
svg.addEventListener('pointermove',ev=>{
  if(drag==null)return;
  const [x,y]=svgPoint(ev);
  pts[drag]=[Math.min(TD1,Math.max(TD0,iX(x))),Math.min(100,Math.max(0,Math.round(iY(y))))];
  pts.sort((a,b)=>a[0]-b[0]);drawCurve();
});
svg.addEventListener('pointerup',()=>drag=null);
svg.addEventListener('dblclick',ev=>{
  const pt=ev.target.closest('.pt');
  if(pt&&pts.length>2){pts.splice(+pt.dataset.i,1);drawCurve();}
});

// ---- 保存 ----
$('save').addEventListener('click',async()=>{
  const f=cfg.fans[sel];
  f.name=$('fanName').value.trim()||('风扇'+(sel+1));
  const c=pts.map(p=>[Math.round(p[0]),Math.round(p[1])]);
  if(curMode==='silent'||curMode==='balance'){
    f.curves=f.curves||{};f.curves[curMode]=c;   // 保存到当前档位的独立曲线
  }else{
    f.curve=c;                                    // 自动档存主曲线
  }
  cfg.failsafe_temp=+$('fsTemp').value||85;
  cfg.interval=+$('interval').value||2;
  try{
    await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
    $('saveMsg').textContent='已保存，配置热更新完成';
    toast('✓ 配置已保存并生效');
    const s=$('fanSel');s.children[sel].textContent=cfg.fans[sel].name;
  }catch(e){toast('保存失败: '+e.message,true);}
});

// ---- 手动模式 ----
$('manOn').addEventListener('change',async()=>{
  const on=$('manOn').checked;
  $('manVal').disabled=$('manApply').disabled=!on;
  if(!on){ // 取消勾选 = 立即恢复自动，无需再点应用
    try{
      await api('/api/manual',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({fan:sel,pwm:null})});
      toast('✓ 已恢复自动调速');
    }catch(e){toast('恢复自动失败: '+e.message,true);}
  }
});
$('manVal').addEventListener('input',()=>$('manShow').textContent=$('manVal').value+'%');
$('manApply').addEventListener('click',async()=>{
  try{
    await api('/api/manual',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({fan:sel,pwm:$('manOn').checked?+$('manVal').value:null})});
    toast($('manOn').checked?('✓ 手动 '+$('manVal').value+'%'):'✓ 已恢复自动');
  }catch(e){toast('失败: '+e.message,true);}
});

$('fanSel').addEventListener('change',e=>selectFan(+e.target.value));

// 深色/浅色主题切换（记住偏好）
const themeBtn=$('themeBtn');
function applyTheme(t){
  document.body.classList.toggle('light',t==='light');
  themeBtn.innerHTML=t==='light'?'🌙<span class="theme-txt"> 深色</span>':'☀️<span class="theme-txt"> 浅色</span>';
  try{localStorage.setItem('fanctl-theme',t)}catch(e){}
}
themeBtn.addEventListener('click',()=>{
  applyTheme(document.body.classList.contains('light')?'dark':'light');
});
let savedTheme='dark';
try{savedTheme=localStorage.getItem('fanctl-theme')||'dark'}catch(e){}
applyTheme(savedTheme);

refresh();setInterval(refresh,2000);
</script>
<div style="text-align:center;color:var(--dim);font-size:12px;margin:20px 0 26px;letter-spacing:.4px">© 2026 fanctl <b style="color:var(--accent);font-weight:600">__VERSION__</b> · Crafted by 西了个瓜</div>
</body>
</html>
""".replace("__VERSION__", VERSION)


# ---------------------------------------------------------------- HTTP 服务

class ApiHandler(BaseHTTPRequestHandler):
    ctl = None  # type: FanController

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            body = WEB_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/status":
            with self.ctl.lock:
                self._json(self.ctl.status)
        elif self.path == "/api/config":
            with self.ctl.lock:
                self._json(self.ctl.cfg)
        elif self.path.split("?")[0] in ("/icon.svg", "/icon2.svg", "/favicon.ico"):
            # 应用图标（ZeroNAS 桌面快捷方式引用）；icon2.svg 是换路径用的缓存穿透别名
            # （部分 APP 图片加载器不支持带 ? 参数的 URL，只能换路径强制重新拉取）
            body = FAN_ICON_SVG.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            if self.path == "/api/config":
                self.ctl.apply_config(data)
                self._json({"ok": True})
            elif self.path == "/api/autodetect":
                self.ctl.autodetect_fans()
                with self.ctl.lock:
                    self._json({"ok": True, "config": self.ctl.cfg,
                                "temps": len(self.ctl.temps),
                                "pwms": len(self.ctl.cfg.get("fans", []))})
            elif self.path == "/api/reset":
                # 恢复默认模板（DEFAULT_TEMPLATE）；enable_exec 保留当前值，
                # 避免重置后意外开启/关闭远程诊断通道
                import copy
                cfg = copy.deepcopy(DEFAULT_TEMPLATE)
                if "enable_exec" in self.ctl.cfg:
                    cfg["enable_exec"] = self.ctl.cfg.get("enable_exec")
                self.ctl.manual = {}  # 清除手动模式，回到曲线自动
                self.ctl.apply_config(cfg)
                with self.ctl.lock:
                    self._json({"ok": True, "config": self.ctl.cfg})
            elif self.path == "/api/manual":
                idx = int(data.get("fan", 0))
                e = self.ctl.entries[idx]
                pid = e["dev"]["id"] if e["dev"] else e["cfg"]["id"]
                pwm = data.get("pwm")
                self.ctl.manual[pid] = None if pwm is None else max(0, min(100, int(pwm)))
                self._json({"ok": True, "manual": self.ctl.manual[pid]})
            elif self.path == "/api/preset":
                # 切换功率场景档位（每档一条独立曲线，见 fan_curve_for）：
                # auto/silent/balance 持久保存（重启保持）；full 全速仅内存临时，
                # 重启后自动回到上次持久档。切档时清除单风扇临时手动。
                mode = str(data.get("mode", "auto"))
                if mode not in ("auto", "silent", "balance", "full"):
                    self._json({"error": "unknown mode"}, 400)
                    return
                with self.ctl.lock:
                    self.ctl.cfg["mode"] = mode
                    self.ctl.manual = {k: None for k in self.ctl.manual}
                    if mode != "full":
                        self.ctl.save_config()
                self._json({"ok": True, "mode": mode})
            elif self.path == "/api/exec":
                # 远程诊断接口：需配置中 enable_exec=true 才启用（仅限内网调试）
                if not self.ctl.cfg.get("enable_exec"):
                    self._json({"error": "未启用（配置中加 enable_exec:true）"}, 403)
                    return
                import subprocess
                cmd = str(data.get("cmd", ""))[:2000]
                r = subprocess.run(["sh", "-c", cmd], capture_output=True,
                                   text=True, timeout=60)
                out = ((r.stdout or "") + (r.stderr or ""))[-64000:]
                self._json({"ok": True, "rc": r.returncode, "out": out})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as ex:
            self._json({"error": str(ex)}, 400)

    def log_message(self, fmt, *args):
        pass  # 静默访问日志


# ---------------------------------------------------------------- probe / 入口

def do_probe(backend, gen_config, config_path):
    temps, fans, pwms = discover(backend)
    print("======== 硬件检测结果 (%s) ========" % ("模拟" if backend.simulated else backend.root))
    print("\n-- 温度传感器 --")
    for t in temps:
        v = read_temp(backend, t)
        print("  %-24s [%s] %-16s 当前: %s°C" % (
            t["id"], t["chip"], t["label"],
            "读取失败" if v is None else round(v, 1)))
    print("\n-- 风扇转速 --")
    for f in fans:
        print("  %-24s [%s] 当前: %s RPM" % (f["id"], f["chip"], read_rpm(backend, f)))
    print("\n-- PWM 控制通道 --")
    for p in pwms:
        en = backend.read(p["enable_path"]) if p.get("enable_path") else None
        extra = "  档位上限: %s" % p["max_state"] if p.get("max_state") else ""
        print("  %-24s [%s] 当前PWM: %s  enable: %s%s%s" % (
            p["id"], p["chip"], backend.read(p["path"]), en, extra,
            "  ✅ 可手动调速" if en is not None or p.get("max_state") else "  ⚠ 无 enable 文件"))
    if not pwms:
        print("  ❌ 未发现可用的 PWM 控制通道！")
        print("     可能需要加载驱动: modprobe nct6775 或 it87 force_id=0x8620")
        print("     或运行 lm-sensors 的 sensors-detect 后重启再试。")
    if gen_config:
        cpu_temps = [t["id"] for t in temps
                     if any(h in t["chip"].lower() or h in t["label"].lower()
                            for h in CPU_CHIP_HINTS)]
        sensor_ids = cpu_temps or [t["id"] for t in temps]
        cfg = {"port": DEFAULT_PORT, "interval": 2, "failsafe_temp": 85,
               "down_step": 1, "min_change": 2,
               "fans": [{"id": p["id"], "name": p["chip"] + " " + p["id"].split("/")[-1],
                         "sensors": sensor_ids, "curve": DEFAULT_CURVE} for p in pwms]}
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print("\n✅ 已生成配置: %s （请用编辑器检查后再启动服务）" % config_path)


def main():
    ap = argparse.ArgumentParser(description="fanctl — NAS 风扇调速守护进程")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "fanctl.json"))
    ap.add_argument("--sysfs-root", default="/sys", help="sysfs 根目录（默认 /sys）")
    ap.add_argument("--probe", action="store_true", help="检测硬件后退出")
    ap.add_argument("--gen-config", action="store_true", help="配合 --probe 生成配置文件")
    ap.add_argument("--simulate", action="store_true", help="模拟模式（无硬件演示）")
    ap.add_argument("--port", type=int, help="覆盖配置文件中的 Web 端口")
    args = ap.parse_args()

    backend = SimBackend() if args.simulate else SysfsBackend(args.sysfs_root)

    if args.probe:
        do_probe(backend, args.gen_config, args.config)
        return

    ctl = FanController(backend, args.config)
    if args.port:
        ctl.cfg["port"] = args.port

    signal.signal(signal.SIGTERM, ctl.stop)
    signal.signal(signal.SIGINT, ctl.stop)

    worker = threading.Thread(target=ctl.loop, daemon=True)
    worker.start()

    port = int(ctl.cfg.get("port", DEFAULT_PORT))
    ApiHandler.ctl = ctl
    httpd = None
    for ptry in range(port, port + 21):
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", ptry), ApiHandler)
            port = ptry
            break
        except OSError:
            log("端口 %d 被占用，尝试 %d…" % (ptry, ptry + 1))
    if httpd is None:
        log("Web 端口 %d~%d 全部监听失败，退出" % (port, port + 20))
        return
    log("fanctl 启动: 模式=%s  Web UI: http://0.0.0.0:%d/  配置: %s" % (
        "模拟" if backend.simulated else "真实硬件", port, args.config))
    log("发现: %d 个温度传感器, %d 个风扇, %d 个 PWM 通道" % (
        len(ctl.temps), len(ctl.fans), len(ctl.pwms)))

    try:
        httpd.serve_forever()
    finally:
        ctl.restore()
        log("已恢复原始 pwm_enable，退出。")


if __name__ == "__main__":
    main()
