# 懂车帝爬虫

抓取懂车帝全量车系、车款价格及配置参数，输出三张 CSV 表。

---

## 文件结构

```
CarSpider/
├── dongchedi_scraper.py   # 主程序（唯一需要编辑的文件）
├── checkpoint.json        # 断点记录（自动生成，无需手动维护）
├── dongchedi_series.csv   # 输出：车系列表
├── dongchedi_variants.csv # 输出：车款及价格
└── dongchedi_config.csv   # 输出：车款配置参数（2700+ 列）
```

---

## 依赖安装

```bash
pip install requests pandas openpyxl
```

---

## 快速开始

**第一步：配置代理和 Cookie**（见下方详细说明）

**第二步：运行**

```bash
cd CarSpider
python3 dongchedi_scraper.py
```

**断点续传**：中断后直接重跑，已完成项自动跳过。

**重新全量采集**：

```bash
rm checkpoint.json dongchedi_series.csv dongchedi_variants.csv dongchedi_config.csv
python3 dongchedi_scraper.py
```

---

## 配置说明

编辑 `dongchedi_scraper.py` 顶部配置区（第 30~58 行）。

### Cookie

```python
COOKIE = (
    "__ac_signature=...; "
    "ttwid=...; "
    "msToken=...; "
)
```

- 用浏览器登录 [dongchedi.com](https://www.dongchedi.com)，F12 → Network → 随便一个请求 → 复制 Request Headers 里的 `Cookie` 字段
- Cookie 有效期约数天，失效后重新获取替换即可
- **关键字段**：`msToken` 是鉴权核心，缺失或过期会返回空页面

### 抓取范围

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `MAX_SERIES_PAGES` | 车系列表页数，每页 30 条 | `157`（全量约 4700 条） |
| `MAX_SERIES_DETAIL` | 抓取车款的车系数量上限 | `9999`（全量） |
| `MAX_CONFIG_PER_SERIES` | 每车系最多抓取几个车款配置 | `99`（全量） |
| `DELAY_MIN / DELAY_MAX` | 请求间隔（秒），降低被封风险 | `2 ~ 4` |

---

## 代理配置

### 当前代理商：青果网络

本项目使用[青果网络](https://www.qg.net)短效住宅代理（按量提取 API）。

**前置步骤**：登录青果控制台，将本机公网 IP 加入白名单，否则提取 API 返回 403。

**相关配置**（`dongchedi_scraper.py` 第 30 行 + 第 71 行）：

```python
# 第 30 行：代理 Key，建议通过环境变量传入
PROXY_KEY = os.environ.get("QG_PROXY_KEY", "你的Key")

# 第 71 行：代理提取 API 地址
FETCH_URL = "https://share.proxy.qg.net/get"
```

**运行时行为**：
- 每个 IP 最多复用 50 次（`rotate_every=50`），之后自动换新
- IP 到期前 30 秒提前切换，防止请求中途超时
- 请求失败（407 / 连接错误）时立即换 IP，自动重试 3 次

---

### 更换代理商指南

需要修改 `ProxyManager` 类（第 65~116 行），共三处：

#### 1. 提取 API 地址

```python
# 改为新代理商的 API 地址
FETCH_URL = "https://新代理商的提取接口"
```

#### 2. 请求参数

```python
def _fetch_new(self) -> None:
    resp = requests.get(
        self.FETCH_URL,
        params={
            "key": self._key,   # 认证 Key 的参数名（按新代理商文档改）
            "num": 1,
            "distinct": "true", # 去重参数（不同代理商名称不同）
        },
        timeout=10,
    )
```

#### 3. 响应解析

不同代理商返回的 JSON 结构不同，需对应修改：

```python
# 青果返回格式示例：
# {"code": "SUCCESS", "data": [{"server": "1.2.3.4:8080", "deadline": "2024-01-01 12:00:00", ...}]}

body = resp.json()
if body.get("code") != "SUCCESS":          # ← 改为新代理商的成功标识字段
    raise RuntimeError(...)
item = body["data"][0]                     # ← 改为新代理商的数据路径
self._server = item["server"]              # ← ip:port 字段名
self._deadline = datetime.strptime(item["deadline"], "%Y-%m-%d %H:%M:%S")  # ← 过期时间字段名和格式
```

#### 4. Key 变量

```python
# 顶部配置区，改为新代理商的认证 Key
PROXY_KEY = os.environ.get("新代理商_KEY_ENV", "你的新Key")
```

**如果新代理商不提供过期时间字段**，可以将 `_deadline` 改为按固定时长轮换：

```python
from datetime import timedelta
self._deadline = datetime.now() + timedelta(minutes=5)  # 固定 5 分钟轮换
```

---

## 输出说明

### dongchedi_series.csv — 车系列表

| 字段 | 说明 |
|------|------|
| series_id / concern_id | 车系 ID（concern_id 用于后续车款查询） |
| series_name | 车系名称 |
| brand_name | 品牌名称 |
| official_price | 官方指导价区间（文本，仅供参考） |
| dcar_score | 懂车帝评分 |
| car_count | 在售车款数 |

### dongchedi_variants.csv — 车款及价格

| 字段 | 说明 |
|------|------|
| car_id | 车款 ID（用于配置查询） |
| car_name | 车款完整名称 |
| year | 年款 |
| price | 官方指导价（万元） |
| dealer_price | 经销商成交价（万元） |
| engine / gear_box | 发动机 / 变速箱描述 |
| fuel_type | 能源类型 |
| pure_elec_range | 纯电续航（PHEV/EV 车型） |

### dongchedi_config.csv — 车款配置参数

- 车型基础信息 + 约 2700 个配置字段（全中文列名）
- 含动力、底盘、安全气囊、智能驾驶、舒适配置、选装包等完整参数
- 停产/下架车型无配置数据，抓取时会跳过并记录原因

---

## 全量规模参考

| 阶段 | 请求数 | 预计耗时（2~4s 间隔） |
|------|--------|----------------------|
| 车系列表 | ~160 次 | ~10 分钟 |
| 车款价格 | ~4700 次 | ~5 小时 |
| 配置参数 | ~20000 次 | ~20 小时 |

> 实际耗时受网络质量和代理响应速度影响较大，建议在服务器上后台运行。

---

## 调试

```bash
# 诊断单个车型的原始数据结构
python3 dongchedi_scraper.py debug <car_id>

# 示例
python3 dongchedi_scraper.py debug 5714
```
