# -*- coding: utf-8 -*-
"""ZeroNAS 桌面注册"风扇调速"图标 + 快捷方式

用法:
    python register_icon.py http://192.168.18.233:9700

要求:
    1. 目标 NAS 的 fanctl.json 中 enable_exec 为 true（模板配置已带）
    2. fanctl 容器已正常运行
功能:
    - 在应用中心注册 fanctl 应用（state=started, port=9700）
    - 为桌面用户添加快捷方式（自动探测已有 user_id，默认 1000）
    - 每次执行前自动备份 appstore.db -> appstore.db.bak-fanctl
    - 可重复执行（INSERT OR REPLACE）
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

CODE = r'''
import sqlite3, json, time, shutil

DB = "/host/userdata/db/appstore.db"
shutil.copyfile(DB, DB + ".bak-fanctl")
print("backup ok -> appstore.db.bak-fanctl")

cfg = {
    "appId": "com.centerm.fanctl",
    "serviceName": "fanctl",
    "version": {"lowVersion": "1.0.0", "version": "1.0.0"},
    "languageList": ["zh-CN", "en-US"],
    "i18n": [
        {"name": "风扇调速", "description": "NAS 风扇转速监控与温度联动调速",
         "author": "fanctl", "langName": "zh-CN", "versionContent": ""},
        {"name": "Fan Control", "description": "NAS fan speed control",
         "author": "fanctl", "langName": "en-US", "versionContent": ""},
    ],
    "accessCtrl": {
        "urlAppAccesses": [
            {"httpsEnable": False, "httpsPort": "", "port": "9700", "urlPath": "/"}
        ],
        "supports": ["pc", "app"],
    },
}
cfg_str = json.dumps(cfg, ensure_ascii=False)
ICON = "http://127.0.0.1:9700/icon.svg"
NOW = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()) + ".000000000+08:00"

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
    "package_size": 1,
    "latest_package_size": 1,
    "carousel_img_urls": "",
    "latest_carousel_img_urls": "",
    "version": "1.0.0",
    "latest_version": "1.0.0",
    "release_date": time.strftime("%Y-%m-%d"),
    "latest_release_date": time.strftime("%Y-%m-%d"),
    "latest_version_content": "",
    "sort": 99,
    "need_update": 0,
    "is_shortcut": 1,
    "start_time": None,
    "create_time": NOW,
    "update_time": NOW,
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

print("目标:", HOST)
# shell 层先备份一次
print(ex("cp /host/userdata/db/appstore.db /host/userdata/db/appstore.db.bak-fanctl && echo backup shell ok").get("out", ""))
run_remote_py(CODE)
print("完成！刷新 ZeroNAS 桌面即可看到「风扇调速」图标")
