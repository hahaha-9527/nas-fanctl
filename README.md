# fanctl — NAS 风扇温度调速

> 单文件、零依赖的 NAS 风扇调速守护进程 + Web 控制台，按温度曲线自动调速。

纯 Python 标准库实现（无需 pip 安装任何包），提供一个可视化 Web 界面（默认 9700 端口），
把机箱风扇的转速与你 NAS 的 CPU / 硬盘温度联动起来。已在 Centerm Zero 1 Pro（ITE IT8620E）
上长期稳定运行，理论上适用于任何带 Docker 的 x86 NAS —— **铁牛OS（铁牛NAS / ZeroNAS）**、
群晖 DSM、威联通 QTS、TrueNAS、UNRAID、PVE 及自建 x86 NAS 均可使用。

## 功能特性

- **温度联动调速**：CPU 温度与全部在位 SATA 硬盘最高温双通道采集，升温立即响应、降温缓降防抖、超温紧急全速保护。
- **三档独立功率曲线**：自动 / 静音 / 均衡三档各自独立曲线并持久保存，全速为临时档；页头一键切换。
- **曲线可视化编辑**：网页端直接拖拽调整 温度→PWM 曲线拐点，所见即所得。
- **联动温度可选**：每个风扇可独立选择按 CPU 或硬盘温度调速，适配各种装机布局。
- **单风扇手动定速**：临时接管某个风扇，切档自动释放。
- **一键恢复默认**：随时重置曲线与全局参数，配置原子写入并自动备份。
- **驱动自愈**：`it87` / `nct6775` 自动加载，compose 启动 + 宿主机 systemd + 容器内自愈三层保障，重启不丢驱动。
- **安全保护**：检测到 0 个可用 PWM 通道时拒绝写入并保留原配置。
- **体验细节**：深浅色主题、手机端自适应、铁牛OS / ZeroNAS 桌面图标一键注册。

## 适用机型与系统

在 **Centerm Zero 1 Pro** 上开发并长期实机运行，已验证 / 可用的平台：

| 平台 / 系统 | 支持情况 |
|---|---|
| **铁牛OS · 铁牛NAS · ZeroNAS**（Centerm Zero 系列） | ⭐ 原生支持：Docker Compose 一键部署、`tools/register_icon.py` 一键注册桌面图标、应用中心信息登记，且已适配**铁牛link 远程访问**（图标与页面内外网均可正常加载） |
| Centerm Zero 1 Pro（ITE IT8620E） | ✅ 实测机型，`fanctl.json.example` 即本机调优配置 |
| 群晖 DSM / 威联通 QTS | ✅ 有 Docker + ITE `it87` / Nuvoton `nct6775` 风扇芯片即可，按本文步骤自行适配 |
| TrueNAS / UNRAID / PVE / 自建 x86 NAS | ✅ 同上，只需 root 与 Docker |

> 搜索关键词：铁牛 NAS、铁牛NAS、铁牛OS、tieniu nas、ZeroNAS 风扇调速、NAS 风扇温控、
> it87 风扇控制、群晖风扇调速、NAS 风扇自动调速、机箱风扇 PWM 调速。

## 环境要求

- x86 NAS，已安装 Docker / Docker Compose
- 主板风扇控制芯片为 ITE `it87` 或 Nuvoton `nct6775` 系列（大多数消费级 x86 主板都属这两类）
- 需要 **root / privileged** 权限（写 PWM 与加载内核模块）

## 包内容

| 文件 | 说明 |
|---|---|
| `fanctl.py` | 主程序（单文件，容器内运行） |
| `docker-compose.yml` | Compose 编排：自动装 kmod → 自动加载驱动 → 自动定位主程序 |
| `fanctl.json` | 初始配置（fans 为空，装好后用页面"自动检测"生成） |
| `fanctl.json.example` | Zero 1 Pro 实测配置示例（双风扇曲线，可参考） |
| `tools/register_icon.py` | 注册铁牛OS / ZeroNAS 桌面"风扇调速"图标 + 快捷方式 |
| `tools/verify.py` | 安装/重启后一键验证 |
| `optional/it87-load.service` | 可选：宿主机开机加载驱动 systemd 服务 |
| `CHANGELOG.md` | 版本更新记录 |
| `LICENSE` | MIT 开源许可 |

## 安装步骤（铁牛OS / ZeroNAS）

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

### 5. 注册桌面图标（铁牛OS / ZeroNAS）
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

## 版本记录

各版本的新增与修复详见 [CHANGELOG.md](CHANGELOG.md)。

## 许可

[MIT](LICENSE) © 2026 西了个瓜

> 本项目在真实硬件上反复调试而成，曲线默认值是针对 Centerm Zero 1 Pro（4 盘位、后方 14cm 风扇）
> 的实测调优结果，其他机型请以实际听感和温度为准自行微调。

