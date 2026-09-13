# -*- coding: utf-8 -*-
"""fanctl 安装/重启后验证

用法:
    python verify.py http://192.168.18.233:9700

检查项:
    1. NAS 是否在线
    2. hwmon 风扇芯片（it86/it87/nct67）
    3. 内核模块加载状态
    4. /api/status 各风扇转速与 PWM
    5. fanctl.json 配置条目数
"""
import base64, json, sys, time, urllib.request

if len(sys.argv) < 2:
    print(__doc__)
    sys.exit(1)
HOST = sys.argv[1].rstrip("/")
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def api(path, data=None):
    if data is None:
        req = urllib.request.Request(HOST + path)
    else:
        req = urllib.request.Request(HOST + path, data=json.dumps(data).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(opener.open(req, timeout=15).read().decode())

def ex(cmd):
    return api("/api/exec", {"cmd": cmd})

# 1) 等待上线（最长 6 分钟，适配刚重启场景）
print("等待 NAS 上线 ...", flush=True)
up = False
for i in range(120):
    try:
        api("/api/status")
        up = True
        print("NAS 已上线 (等待 %d 秒)" % (i * 3))
        break
    except Exception:
        time.sleep(3)
if not up:
    print("!! 6 分钟内未等到 NAS 上线，请手动确认容器状态")
    sys.exit(1)

print("\n=== hwmon 芯片 ===")
r = ex("for h in /host/sys/class/hwmon/hwmon*/name; do echo \"$h: $(cat $h)\"; done")
print(r.get("out", ""))

print("=== 风扇相关内核模块 ===")
r = ex("grep -hE 'it87|nct67' /host/proc/modules || echo '(未加载 it87/nct6775 模块)'")
print(r.get("out", ""))

time.sleep(5)
print("=== 风扇实时状态 ===")
s = api("/api/status")
fans = s.get("fans", [])
if not fans:
    print("(无风扇条目 —— 请在页面点击「自动检测风扇」)")
for f in fans:
    print("%-10s manual=%s rpm=%s pwm=%s" % (f.get("name"), f.get("manual"), f.get("rpm"), f.get("pwm")))

print("\n=== 配置文件 ===")
r = ex("python3 -c \"import json,glob;p=glob.glob('/host/*/data/personal/1000/docker-projects/nas-fanctl/fanctl.json')+glob.glob('/host/volume*/data/personal/1000/docker-projects/nas-fanctl/fanctl.json');c=json.load(open(p[0])) if p else {};print('fans:',len(c.get('fans',[])),[f.get('name') for f in c.get('fans',[])])\"")
print(r.get("out", ""))
print("\n验证完成")
