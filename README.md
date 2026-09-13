# fanctl — NAS 风扇调速安装包

纯标准库 Python 风扇调速守护进程 + Web 界面（端口 9700），支持温度曲线联动、手动调速、
SATA SMART / NVMe 温度采集、深浅色主题、手机自适应。已在 Centerm Zero 1 Pro（IT8620E）
上长期稳定运行，理论上适用于任何带 Docker 的 x86 NAS（ZeroNAS / 群晖 / 威联通 / 自建）。

## 包内容

| 文件 | 说明 |
|---|---|
| `fanctl.py` | 主程序（单文件，容器内运行） |
| `docker-compose.yml` | Compose 编排：自动装 kmod → 自动加载驱动 → 自动定位主程序 |
| `fanctl.json` | 初始配置（fans 为空，装好后用页面"自动检测"生成） |
| `fanctl.json.example` | Zero 1 Pro 实测配置示例（双风扇曲线，可参考） |
| `tools/register_icon.py` | 注册 ZeroNAS 桌面"风扇调速"图标 + 快捷方式 |
| `tools/verify.py` | 安装/重启后一键验证 |
| `optional/it87-load.service` | 可选：宿主机开机加载驱动 systemd 服务 |

## 安装步骤（ZeroNAS）

### 1. 创建 Compose 项目
ZeroNAS 网页 → Docker → Compose 项目 → 新建，项目名 `nas-fanctl`，
把 `docker-compose.yml` 的内容粘贴进去并启动。
> 首次启动日志出现 `===NOT-FOUND` 是正常的——此时还没有主程序文件，容器会原地等待。

### 2. 上传主程序
ZeroNAS 文件管理器，把 `fanctl.py` 和 `fanctl.json` 上传到
`docker-projects/nas-fanctl/` 目录（即 Compose 项目目录）。

### 3. 重建容器
在 Compose 项目里重建（或停止再启动）容器。看日志应出现：
```
===driver: it87 force_id=0x8620   （或其他 force_id / nct6775）
===FOUND: /host/volume1/.../fanctl.py
```
容器会自动创建网络并监听 9700 端口。

### 4. 页面配置
浏览器打开 `http://NAS_IP:9700`：
1. 点「自动检测风扇」——自动跳过未接线的通道（检测到 0 通道会拒绝执行并保留配置）；
2. 用「手动调速」拉高每个 PWM 听声音，确认哪个是 CPU 风扇、哪个是硬盘风扇；
3. 改名 + 勾选联动组（CPU 温度 / SATA 硬盘温度），调整曲线并保存；
4. 曲线参考：CPU 风扇 40°C→30%，82°C→100%；硬盘风扇 38°C→20%，78°C→100%。

### 5. 注册桌面图标（ZeroNAS）
在你自己的电脑上执行（需要 Python 3）：
```
python tools/register_icon.py http://NAS_IP:9700
```
自动备份 `appstore.db`（.bak-fanctl）、注册应用、为桌面用户添加快捷方式。
刷新 ZeroNAS 桌面即可看到「风扇调速」图标，点击直接打开控制页。

### 6. 验证
```
python tools/verify.py http://NAS_IP:9700
```
检查驱动加载、各风扇实时转速/占空比、配置完整性。以后每次重启完也可跑一遍。

### 7.（可选）安全加固
`enable_exec: true` 仅供注册图标和远程排障使用。全部弄完后可把配置中
`enable_exec` 改为 `false` 再重建容器（之后 register_icon.py 将不可用，但不影响调速功能）。

## 重启丢驱动？不会

容器 `restart: unless-stopped` 会随 Docker 自启动，启动脚本每次都会先确认驱动已加载；
另外 fanctl.py 启动时还有自愈兜底（检测不到风扇芯片时 chroot 宿主机加载 it87）。
三层保障下，重启 NAS 后驱动、配置、桌面图标都会原样恢复。

## 常见问题

**构建时报 `Get "https://registry-1.docker.io/v2/": ... Client.Timeout exceeded`** →
国内网络直连 Docker Hub 超时，基础镜像拉不下来。compose 里默认已走国内加速源
（`docker.m.daocloud.io/library/python:3.12-slim-bookworm`）；若该源也不可用，
把 `image:` 换成注释里的其他加速源（docker.1ms.run / dockerpull.cn / docker.1panel.live）
后重新构建。海外网络可换回官方 `python:3.12-slim-bookworm`。

**页面提示"未发现 PWM"** → 驱动没加载。看容器日志 `===driver:` 一行：
- 显示"未能加载"：说明主板芯片不在尝试列表里。it87 常用 force_id：
  `0x8620 0x8728 0x8665 0x8613 0x8712 0x8762 0x8603`，可在 compose 里增删；
  非 ITE 芯片试试 nct6775。可跑 `sensors-detect`（apt 装 lm-sensors）确认芯片型号。
- Zero 1 Pro 实测：IT8620E 在非标准地址 0xa40，必须 `force_id=0x8620`。

**不知道哪个 PWM 对应哪个风扇** → 页面手动拉到 60%+ 听声音辨认（本机：pwm2=CPU、pwm3=硬盘）。

**硬盘识别** → SATA 盘读 SMART attr 194/190；NVMe 盘读 `Temperature:` 字段，插上即识别、
不参与硬盘风扇联动（只展示温度），联动只取 SATA 盘最高温。

**风扇会停转吗** → 0% 时部分主板风扇会停转，曲线最低点别低于 20% 较稳妥。

**升级 fanctl.py** → 直接替换项目目录里的 fanctl.py，重建容器即可；配置不受影响
（配置写入为原子操作并自动留 .bak 备份）。
