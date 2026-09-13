# -*- coding: utf-8 -*-
"""ZeroNAS 桌面注册"风扇调速"图标 + 快捷方式

用法:
    python register_icon.py http://NAS_IP:9700

要求:
    1. 目标 NAS 的 fanctl.json 中 enable_exec 为 true（模板配置已带）
    2. fanctl 容器已正常运行
功能:
    - 在应用中心注册 fanctl 应用（state=started, port=9700）
    - 为桌面用户添加快捷方式（自动探测已有 user_id，默认 1000）
    - 图标部署到 TOS 网页根目录 /usr/local/pc/fanctl-icon.svg，
      应用中心登记为相对路径 /pc/fanctl-icon.svg —— 本地局域网和铁牛link
      远程访问（https://xxx.tieniu-link.com/pc/index.html）都能正常加载，
      不再依赖内网 IP 直连（内网 IP 远程加载不到，图标会裂图）
    - 每次执行前自动备份 appstore.db -> appstore.db.bak-fanctl
    - 可重复执行（INSERT OR REPLACE）
注意:
    TOS 系统大版本更新可能清空 /usr/local/pc 下的自定义文件，
    若更新后远程桌面图标消失，重新执行一次本脚本即可。
"""
import base64, json, sys, urllib.request

if len(sys.argv) < 2:
    print(__doc__)
    sys.exit(1)
HOST = sys.argv[1].rstrip("/")

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def ex(cmd):
    req = urllib.request.Request(HOST + "/api/exec",
        data=json.dumps({"cmd": cmd}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(opener.open(req, timeout=70).read().decode())

def run_remote_py(code):
    b = base64.b64encode(code.encode()).decode()
    ex("rm -f /tmp/q.b64 /tmp/q.py")
    CH = 1800
    for i in range(0, len(b), CH):
        op = ">" if i == 0 else ">>"
        r = ex("echo '%s' %s /tmp/q.b64" % (b[i:i+CH], op))
        assert r.get("ok"), r
    r = ex("base64 -d /tmp/q.b64 > /tmp/q.py && python3 /tmp/q.py")
    print(r.get("out", ""))
    if r.get("rc"):
        print("[rc=%s]" % r.get("rc"))
        sys.exit(1)

DESC_ZH = ("NAS 风扇转速监控与温度联动调速工具。支持 CPU 与 SATA 硬盘温度实时联动、"
           "三档独立功率曲线（自动/静音/均衡/全速）、单风扇手动定速、曲线可视化拖拽编辑；"
           "内置 it87/nct6775 驱动自动加载与开机自愈，深浅色主题，手机端自适应，"
           "可注册 ZeroNAS 桌面快捷图标。")
DESC_EN = ("NAS fan speed monitoring with temperature-linked control. "
           "CPU & SATA HDD temp tracking, three independent power curves "
           "(auto/silent/balance/full), per-fan manual override, visual curve editor, "
           "it87/nct6775 auto-loading with boot self-healing, light/dark themes, "
           "mobile-friendly UI, and a ZeroNAS desktop shortcut.")
FEATURES_ZH = ("v1.71 重点功能：\n"
               "· 三档独立功率曲线：自动/静音/均衡持久保存，全速为临时档，曲线可视化拖拽编辑\n"
               "· 一键调速：页头分段按钮，档位随各风扇曲线等比适配\n"
               "· 一键恢复默认：重置三档曲线、档位与全局参数\n"
               "· 温度联动：CPU 温度 + 4×SATA 硬盘最高温（支持 6 盘位，M.2 展示不参与联动）\n"
               "· 驱动自愈：it87/nct6775 自动加载、开机自愈、pwm 手动接管周期守护\n"
               "· 深/浅色主题一键切换，页面 150% 缩放，手机端自适应\n"
               "· 安全保护：0 通道检测拒绝执行，配置原子写入并自动备份")
FEATURES_EN = ("v1.71 highlights:\n"
               "- Three independent power curves (auto/silent/balance persistent, full temporary)\n"
               "- One-tap presets that scale with each fan curve\n"
               "- Factory reset for curves & settings\n"
               "- Temp linking: CPU + hottest of 4 SATA disks (6-bay ready, M.2 display-only)\n"
               "- Driver self-healing: it87/nct6775 auto-load & boot recovery\n"
               "- Light/dark themes, 150% zoom, mobile responsive\n"
               "- Safety: zero-channel detection guard, atomic config writes with backup")

CODE = r'''
import sqlite3, json, time, shutil

DB = "/host/userdata/db/appstore.db"
shutil.copyfile(DB, DB + ".bak-fanctl")
print("backup ok -> appstore.db.bak-fanctl")

cfg = {
    "appId": "com.centerm.fanctl",
    "serviceName": "fanctl",
    "version": {"lowVersion": "1.0.0", "version": "__APPVER__"},
    "languageList": ["zh-CN", "en-US"],
    "i18n": [
        {"name": "风扇调速", "description": "__DESC_ZH__",
         "author": "西了个瓜", "langName": "zh-CN", "versionContent": "__FEAT_ZH__"},
        {"name": "Fan Control", "description": "__DESC_EN__",
         "author": "西了个瓜", "langName": "en-US", "versionContent": "__FEAT_EN__"},
    ],
    "accessCtrl": {
        "urlAppAccesses": [
            {"httpsEnable": False, "httpsPort": "", "port": "9700", "urlPath": "/"}
        ],
        "supports": ["pc", "app"],
    },
}
cfg_str = json.dumps(cfg, ensure_ascii=False)
NOW = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()) + ".000000000+08:00"

# 图标地址：优先部署到 TOS 网页根目录并用相对路径（本地/铁牛link远程都能加载）；
# 部署失败（目录不存在等）则回退为直连 URL
ICON_REL = "/pc/fanctl-icon.svg"
try:
    import urllib.request as _u
    _svg = _u.urlopen("http://127.0.0.1:9700/icon2.svg", timeout=10).read()
    with open("/host/usr/local/pc/fanctl-icon.svg", "wb") as _f:
        _f.write(_svg)
    ICON = ICON_REL
    print("icon deployed -> /usr/local/pc/fanctl-icon.svg (%d bytes)" % len(_svg))
except Exception as _e:
    ICON = "__HOSTICON__"
    print("icon deploy failed (%s), fallback to direct URL" % _e)

c = sqlite3.connect(DB, timeout=15)
c.row_factory = sqlite3.Row
cols = [r[1] for r in c.execute("PRAGMA table_info(appstore_app)")]

# 模板行优先 autobangumi，没有则取任意一行，再不行就全 None
tpl = c.execute("select * from appstore_app where code='autobangumi'").fetchone()
if tpl is None:
    tpl = c.execute("select * from appstore_app limit 1").fetchone()
if tpl is not None:
    row = {k: None for k in cols}
    row.update(dict(tpl))
else:
    row = {k: None for k in cols}
row.update({
    "code": "fanctl",
    "icon_url": ICON,
    "state": "started",
    "shelf_state": 1,
    "service_name": "fanctl",
    "type": 1,
    "config": cfg_str,
    "latest_config": cfg_str,
    "latest_icon_url": ICON,
    "install_location": "",
    "download_url": "",
    "download_progress": 0,
    "package_size": 31,          # 整数！Go 后端 int64 扫描，写小数会让整个应用列表接口崩掉
    "latest_package_size": 31,   # 单位 KB，UI 显示 31.00 KB（安装包 zip 约 31KB）
    "carousel_img_urls": "",
    "latest_carousel_img_urls": "",
    "version": "__APPVER__",
    "latest_version": "__APPVER__",
    "release_date": time.strftime("%Y-%m-%d"),
    "latest_release_date": time.strftime("%Y-%m-%d"),
    "latest_version_content": "",
    "sort": 99,
    "need_update": 0,
    "is_shortcut": 1,
    "start_time": None,
    "create_time": NOW,
    "update_time": NOW,
    "licence_agreement_link": "",   # 不显示许可协议/源码链接两行
    "source_code_link": "",
    "image_size": 165888,           # 单位 KB，UI 显示 162.00 MB
    "latest_image_size": 165888,
    "documentation": "",
})
row = {k: row[k] for k in cols if k in row}
sql = "INSERT OR REPLACE INTO appstore_app (%s) VALUES (%s)" % (
    ",".join(row.keys()), ",".join("?" * len(row)))
c.execute(sql, [row[k] for k in row])

# 桌面快捷方式：复用已有 user_id，没有则用 1000
ur = c.execute("select user_id from appstore_app_shortcut limit 1").fetchone()
uid = str(ur["user_id"]) if ur else "1000"
c.execute("INSERT OR REPLACE INTO appstore_app_shortcut "
          "(user_id, app_code, operate_type, create_time, update_time) "
          "VALUES (?, 'fanctl', 1, ?, ?)", (uid, NOW, NOW))
c.commit()

r = c.execute("select code,state,is_shortcut from appstore_app where code='fanctl'").fetchone()
print("app row:", dict(r) if r else None)
r = c.execute("select user_id,app_code from appstore_app_shortcut where app_code='fanctl'").fetchone()
print("shortcut:", dict(r) if r else None)
print("DONE")
'''

CODE = (CODE
        .replace("__APPVER__", "1.71")
        .replace("__DESC_ZH__", DESC_ZH)
        .replace("__DESC_EN__", DESC_EN)
        .replace("__FEAT_ZH__", FEATURES_ZH)
        .replace("__FEAT_EN__", FEATURES_EN)
        # 兜底直连 URL：用用户传入的 NAS 地址（不能写 127.0.0.1）
        .replace("__HOSTICON__", HOST + "/icon2.svg"))

print("目标:", HOST)
# shell 层先备份一次
print(ex("cp /host/userdata/db/appstore.db /host/userdata/db/appstore.db.bak-fanctl && echo backup shell ok").get("out", ""))
run_remote_py(CODE)
print("完成！刷新 ZeroNAS 桌面即可看到「风扇调速」图标")
