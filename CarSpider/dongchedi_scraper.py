"""
懂车帝爬虫 - 三部分数据采集
  1. 车系列表      → dongchedi_series.csv
  2. 车款及价格    → dongchedi_variants.csv
  3. 车款配置信息  → dongchedi_config.csv

依赖：
    pip install requests pandas openpyxl

用法：
    python3 dongchedi_scraper.py

断点续传：中断后直接重跑，已完成项自动跳过。
重新全量：删除 checkpoint.json 及三个 CSV 文件后重跑。
"""

import re
import time
import random
import json
import os
from datetime import datetime
import requests
import pandas as pd

# ══════════════════════════════════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════════════════════════════════

PROXY_KEY = os.environ.get("QG_PROXY_KEY", "你的key")   # 建议改用环境变量传入

COOKIE = (
    "__ac_signature=_02B4Z6wo00f01DkpuIwAAIDCCrxOzRtdPPA5CbwAAGcJ46; "
    "ttwid=7586480978921227801; "
    "msToken=bxD5awB9wtUnw101ZOSgSkMZymPCh05xsrBamWA_vnu3xD6w4Vk_fCE6pjMhzBDI5s0v9zWd9SzD6dy70eJl9_4NoJ74Bd2JyQnB3nfO5VPnsu7-HCKAXDNDEmaCZPcmnAS82LiA953gILXiNeY7OorVxAomT5g5YFHa_g0PsH5G8KdlQ36aig==; "
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)

CITY = "北京"
CITY_ENCODED = "%E5%8C%97%E4%BA%AC"

# 抓取范围（全量）
MAX_SERIES_PAGES = 157   # 车系列表总页数（4706条 / 30，多留余量）
MAX_SERIES_DETAIL = 9999 # 所有车系都抓车款
MAX_CONFIG_PER_SERIES = 99  # 每个车系所有在售车款配置

DELAY_MIN = 2
DELAY_MAX = 4

BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Origin": "https://www.dongchedi.com",
    "Cookie": COOKIE,
}


# ══════════════════════════════════════════════════════════════════════════════
#  代理池（青果网络 按量提取）
# ══════════════════════════════════════════════════════════════════════════════

class ProxyManager:
    """
    每个 IP 存活 5 分钟，到期前 30 秒自动换新 IP。
    请求失败（407/连接错误）时立即换 IP。
    """

    FETCH_URL = "https://share.proxy.qg.net/get"

    def __init__(self, key: str, rotate_every: int = 50):
        self._key = key
        self._rotate_every = rotate_every   # 最多复用几次后强制换 IP
        self._server: str | None = None
        self._deadline: datetime | None = None
        self._use_count: int = 0

    def _fetch_new(self) -> None:
        resp = requests.get(
            self.FETCH_URL,
            params={"key": self._key, "num": 1, "distinct": "true"},
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != "SUCCESS":
            raise RuntimeError(f"代理获取失败: {body.get('code')} {body.get('msg','')}")
        item = body["data"][0]
        self._server = item["server"]               # ip:port
        self._deadline = datetime.strptime(item["deadline"], "%Y-%m-%d %H:%M:%S")
        self._use_count = 0
        print(f"  [proxy] 新IP: {item['proxy_ip']} | 地区: {item['area']} | 截止: {item['deadline']}")

    def _need_rotate(self) -> bool:
        if self._server is None:
            return True
        if self._use_count >= self._rotate_every:
            return True
        # 到期前 30 秒换新
        if self._deadline and (self._deadline - datetime.now()).total_seconds() < 30:
            return True
        return False

    def get_proxies(self) -> dict:
        if self._need_rotate():
            self._fetch_new()
        self._use_count += 1
        return {"http": f"http://{self._server}", "https": f"http://{self._server}"}

    def mark_failed(self) -> None:
        """请求失败时强制换 IP"""
        print(f"  [proxy] IP {self._server} 失效，强制切换")
        self._server = None


proxy_mgr = ProxyManager(PROXY_KEY, rotate_every=50)


def request_with_proxy(method: str, url: str, max_retries: int = 3, **kwargs) -> requests.Response:
    """带代理和重试的请求封装"""
    for attempt in range(1, max_retries + 1):
        try:
            proxies = proxy_mgr.get_proxies()
            resp = requests.request(method, url, proxies=proxies, timeout=20, **kwargs)
            if resp.status_code == 407:         # 代理认证失败
                proxy_mgr.mark_failed()
                continue
            resp.raise_for_status()
            return resp
        except (requests.exceptions.ProxyError,
                requests.exceptions.ConnectionError) as e:
            proxy_mgr.mark_failed()
            if attempt == max_retries:
                raise
            time.sleep(2)
        except requests.exceptions.Timeout:
            if attempt == max_retries:
                raise
            time.sleep(2)
    raise RuntimeError(f"请求失败（{max_retries}次重试）: {url}")


# ══════════════════════════════════════════════════════════════════════════════
#  Part 1：车系列表
# ══════════════════════════════════════════════════════════════════════════════

def fetch_series_list(page: int, page_size: int = 30) -> dict:
    url = "https://www.dongchedi.com/motor/pc/car/brand/select_series_v2?aid=1839&app_name=auto_web_pc"
    headers = {
        **BASE_HEADERS,
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://www.dongchedi.com/auto/library-accurate/118x",
    }
    payload = {
        "sort_new": "hot_desc",
        "city_name": CITY,
        "limit": str(page_size),
        "page": str(page),
    }
    resp = request_with_proxy("POST", url, data=payload, headers=headers)
    return resp.json()


def extract_series_info(series_list: list) -> list:
    rows = []
    for s in series_list:
        rows.append({
            "series_id": s.get("id"),
            "concern_id": s.get("concern_id"),
            "series_name": s.get("outter_name"),
            "brand_id": s.get("brand_id"),
            "brand_name": s.get("brand_name"),
            "official_price": s.get("official_price"),  # 车系指导价区间，仅供参考
            "dcar_score": s.get("dcar_score"),
            "car_count": s.get("count"),
            "top_tag": (s.get("top_tag") or {}).get("text"),
            "cover_url": s.get("cover_url"),
        })
    return rows


# ══════════════════════════════════════════════════════════════════════════════
#  Part 2：车款及价格
# ══════════════════════════════════════════════════════════════════════════════

def fetch_car_list(series_id: int) -> dict:
    url = (
        f"https://www.dongchedi.com/motor/pc/car/series/car_list"
        f"?aid=1839&app_name=auto_web_pc&series_id={series_id}&city_name={CITY_ENCODED}"
    )
    headers = {**BASE_HEADERS, "Referer": f"https://www.dongchedi.com/auto/series/{series_id}"}
    resp = request_with_proxy("GET", url, headers=headers)
    return resp.json()


def extract_car_variants(car_data: dict, series_info: dict) -> list:
    """解析 tab_list 结构，取在售车款"""
    rows = []
    tab_list = car_data.get("data", {}).get("tab_list", [])
    online_tab = next(
        (t for t in tab_list if t.get("tab_key") == "online_all"),
        tab_list[0] if tab_list else None,
    )
    if not online_tab:
        return rows

    for item in online_tab.get("data", []):
        if item.get("type") != "1115":
            continue
        car = item.get("info", {})
        rows.append({
            "series_id": series_info.get("series_id"),
            "series_name": series_info.get("series_name"),
            "brand_name": series_info.get("brand_name"),
            "year": car.get("year"),
            "car_id": car.get("car_id") or car.get("id"),
            "car_name": car.get("name"),
            "price": car.get("price"),
            "official_price_num": car.get("official_price"),
            "dealer_price": car.get("dealer_price"),
            "engine": car.get("engine"),
            "gear_box": car.get("gear_box"),
            "fuel_type": car.get("fuel_type"),
            "pure_elec_range": car.get("pure_elec_range"),
            "fuel_consumption": car.get("fuel_consumption"),
        })
    return rows


# ══════════════════════════════════════════════════════════════════════════════
#  Part 3：车款配置信息
# ══════════════════════════════════════════════════════════════════════════════

def fetch_car_config(car_id: int) -> dict:
    """
    抓取 params-carIds-{car_id} 页面，从 SSR JSON 中提取完整配置。
    返回 {字段名: 值} 的扁平字典，含车型基础信息。
    """
    url = f"https://www.dongchedi.com/auto/params-carIds-{car_id}"
    headers = {**BASE_HEADERS, "Referer": "https://www.dongchedi.com"}
    resp = request_with_proxy("GET", url, headers=headers)

    # 优先匹配 Next.js 标准 SSR 数据节点
    next_data = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        resp.text, re.DOTALL
    )
    if next_data:
        ssr_script = next_data.group(1)
    else:
        # 兜底：找以 { 开头的最长 inline script
        scripts = [s for s in re.findall(r"<script[^>]*>(.*?)</script>", resp.text, re.DOTALL)
                   if s.strip().startswith("{")]
        if not scripts:
            raise ValueError(f"页面无有效SSR数据，该车型可能已下架（car_id={car_id}）")
        ssr_script = max(scripts, key=len)
    data = json.loads(ssr_script)
    raw = data["props"]["pageProps"]["rawData"]
    if not raw.get("car_info"):
        raise ValueError(f"car_info为空，该车型已下架（car_id={car_id}）")

    # properties: key → 中文标签
    key_to_text = {p["key"]: p["text"] for p in raw["properties"]}

    # 兜底手动映射（properties 里缺失的高频字段）
    FALLBACK = {
        "main_drive_pillow_adjustment":         "主驾头枕调节",
        "vice_drive_pillow_adjustment":         "副驾头枕调节",
        "external_mirror_heat":                 "外后视镜加热",
        "front_airbag":                         "前排气囊",
        "vice_airbag":                          "副驾气囊",
        "main_airbag":                          "主驾气囊",
        "vice_drive_backrest_adjustment":       "副驾座椅靠背调节",
        "vice_drive_back_and_forth_adjustment": "副驾座椅前后调节",
        "main_drive_back_and_forth_adjustment": "主驾座椅前后调节",
        "main_drive_backrest_adjustment":       "主驾座椅靠背调节",
        "main_drive_window_sunshade_mirror":    "主驾遮阳板化妆镜",
        "front_armrest":                        "前排中央扶手",
        "rear_electric_window":                 "后排电动车窗",
        "front_electric_window":                "前排电动车窗",
        "reversing_camera":                     "倒车影像",
        "cruise":                               "定速巡航",
        "multifunction_steer_wheel":            "多功能方向盘",
        "front_usb_typec_interface_count":      "前排USB-C接口数",
        "vice_drive_window_sunshade_mirror":    "副驾遮阳板化妆镜",
        "exter_mirror_elec_adjustment":         "外后视镜电动调节",
        "front_usb_typec_max_charging_power":   "前排USB-C最大充电功率(W)",
    }

    def to_label(k: str) -> str:
        """key → 中文标签，三级查找：精确匹配 → 去数字/字母后缀 → 兜底字典"""
        if k in key_to_text:
            return key_to_text[k]
        # 去掉末尾 _数字、_小数、_品牌名、尾部下划线 等后缀
        base = re.sub(r"[_\s][A-Za-z0-9 .]+$", "", k)
        base = re.sub(r"_\d+$", "", base)
        base = base.rstrip("_")
        if base in key_to_text:
            return key_to_text[base]
        # 兜底字典
        if base in FALLBACK:
            return FALLBACK[base]
        if k in FALLBACK:
            return FALLBACK[k]
        return k   # 真的找不到，保留英文，人工后续补充

    # type=3：父字段有 sub_list，子 key 不单独展开，聚合到父列
    child_to_parent: dict[str, str] = {}   # child_key → parent_text
    parent_children: dict[str, list] = {}  # parent_text → [child_key, ...]
    for p in raw["properties"]:
        if p.get("type") == 3 and p.get("sub_list"):
            parent_text = p["text"]
            children = [c["key"] for c in p["sub_list"]]
            parent_children[parent_text] = children
            for ck in children:
                child_to_parent[ck] = parent_text

    car = raw["car_info"][0]
    info = car.get("info", {})

    result = {
        "车型ID": car.get("car_id"),
        "车型名称": car.get("car_name"),
        "车系名称": car.get("series_name"),
        "车系ID": car.get("series_id"),
        "年款": car.get("car_year"),
        "官方指导价": car.get("official_price"),
        "经销商价": car.get("dealer_price"),
    }

    # 先写父列（保持 properties 顺序）
    for parent_text, children in parent_children.items():
        parts = []
        for ck in children:
            v = info.get(ck)
            val = (v.get("value", "") if isinstance(v, dict) else v) or ""
            if val:
                parts.append(val)
        result[parent_text] = "|".join(parts)

    # 再写普通字段（跳过已归入父列的子 key）
    for k, v in info.items():
        if k in child_to_parent:
            continue
        label = to_label(k)
        result[label] = v.get("value", "") if isinstance(v, dict) else v

    return result


# ══════════════════════════════════════════════════════════════════════════════
#  断点续传
# ══════════════════════════════════════════════════════════════════════════════

CHECKPOINT_FILE = "checkpoint.json"
SERIES_CSV    = "dongchedi_series.csv"
VARIANTS_CSV  = "dongchedi_variants.csv"
CONFIG_CSV    = "dongchedi_config.csv"


def load_checkpoint() -> dict:
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE) as f:
            cp = json.load(f)
        print(f"[续传] 读取断点: 车系已完成{len(cp.get('series_pages_done',[]))}页 "
              f"| 车款已完成{len(cp.get('variants_done',[]))}个车系 "
              f"| 配置已完成{len(cp.get('config_done',[]))}款")
        return cp
    return {"series_pages_done": [], "variants_done": [], "config_done": []}


def save_checkpoint(cp: dict) -> None:
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(cp, f)


def append_csv(path: str, rows: list) -> None:
    """追加写入 CSV，首次写入带表头，后续按现有列头对齐再追加。
    发现新列时全量重写文件（保证 header 与数据始终对齐）。"""
    if not rows:
        return
    df = pd.DataFrame(rows)
    if not os.path.exists(path):
        df.to_csv(path, mode="w", index=False, header=True, encoding="utf-8-sig")
        return
    existing_cols = pd.read_csv(path, nrows=0, encoding="utf-8-sig").columns.tolist()
    new_cols = [c for c in df.columns if c not in existing_cols]
    if new_cols:
        # 有新列：读入全量数据，扩展 header，整体重写
        all_cols = existing_cols + new_cols
        existing_df = pd.read_csv(path, encoding="utf-8-sig", low_memory=False).reindex(columns=all_cols)
        df = df.reindex(columns=all_cols)
        pd.concat([existing_df, df], ignore_index=True).to_csv(
            path, mode="w", index=False, header=True, encoding="utf-8-sig"
        )
    else:
        df = df.reindex(columns=existing_cols)
        df.to_csv(path, mode="a", index=False, header=False, encoding="utf-8-sig")


def load_csv(path: str) -> list:
    if os.path.exists(path):
        return pd.read_csv(path, encoding="utf-8-sig").to_dict("records")
    return []


# ══════════════════════════════════════════════════════════════════════════════
#  主程序
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("懂车帝爬虫")
    print(f"  车系: {MAX_SERIES_PAGES}页 | 车款详情: {MAX_SERIES_DETAIL}个车系 | 配置: 每系{MAX_CONFIG_PER_SERIES}款")
    print("=" * 60)

    cp = load_checkpoint()
    series_pages_done: set = set(cp["series_pages_done"])
    variants_done:     set = set(cp["variants_done"])
    config_done:       set = set(cp["config_done"])

    # ── Part 1: 车系 ────────────────────────────────────────────
    print("\n[Part 1] 车系列表")
    for page in range(1, MAX_SERIES_PAGES + 1):
        if page in series_pages_done:
            print(f"  第 {page}/{MAX_SERIES_PAGES} 页... [已完成，跳过]")
            continue
        print(f"  第 {page}/{MAX_SERIES_PAGES} 页...", end=" ")
        try:
            data = fetch_series_list(page)
            if data.get("status") != 0:
                print(f"⚠️ {data.get('message')}")
                continue
            series = extract_series_info(data["data"]["series"])
            append_csv(SERIES_CSV, series)
            series_pages_done.add(page)
            cp["series_pages_done"] = list(series_pages_done)
            save_checkpoint(cp)
            print(f"✅ {len(series)} 条")
        except Exception as e:
            print(f"❌ {e}")
        if page < MAX_SERIES_PAGES:
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    all_series = load_csv(SERIES_CSV)
    if not all_series:
        print("未获取到车系数据，退出")
        return
    print(f"  → 车系共 {len(all_series)} 条")

    # ── Part 2: 车款及价格 ──────────────────────────────────────
    print(f"\n[Part 2] 车款及价格（前 {MAX_SERIES_DETAIL} 个车系）")
    target_series = all_series[:MAX_SERIES_DETAIL]

    for idx, series in enumerate(target_series, 1):
        series_id = int(series.get("concern_id") or series.get("series_id"))
        if series_id in variants_done:
            print(f"  [{idx}/{len(target_series)}] {series['series_name']} ... [已完成，跳过]")
            continue
        print(f"  [{idx}/{len(target_series)}] {series['series_name']} (ID:{series_id})...", end=" ")
        try:
            car_data = fetch_car_list(series_id)
            variants = extract_car_variants(car_data, series)
            append_csv(VARIANTS_CSV, variants)
            variants_done.add(series_id)
            cp["variants_done"] = list(variants_done)
            save_checkpoint(cp)
            print(f"✅ {len(variants)} 款")
        except Exception as e:
            print(f"❌ {e}")
        if idx < len(target_series):
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    all_variants = load_csv(VARIANTS_CSV)
    print(f"  → 车款共 {len(all_variants)} 条")

    # ── Part 3: 车款配置 ────────────────────────────────────────
    print(f"\n[Part 3] 车款配置信息（每系前 {MAX_CONFIG_PER_SERIES} 款）")

    series_groups: dict[int, list] = {}
    for v in all_variants:
        sid = int(v["series_id"])
        series_groups.setdefault(sid, [])
        if len(series_groups[sid]) < MAX_CONFIG_PER_SERIES:
            series_groups[sid].append(v)

    config_tasks = [v for variants in series_groups.values() for v in variants]
    total = len(config_tasks)

    for idx, variant in enumerate(config_tasks, 1):
        car_id = int(variant["car_id"])
        if car_id in config_done:
            print(f"  [{idx}/{total}] {str(variant['car_name'])[:35]} ... [已完成，跳过]")
            continue
        print(f"  [{idx}/{total}] {str(variant['car_name'])[:35]}...", end=" ")
        try:
            config = fetch_car_config(car_id)
            append_csv(CONFIG_CSV, [config])
            config_done.add(car_id)
            cp["config_done"] = list(config_done)
            save_checkpoint(cp)
            print(f"✅ {len(config)} 个字段")
        except Exception as e:
            print(f"❌ car_id={car_id} {type(e).__name__}: {e}")
        if idx < total:
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    # ── 汇总 ────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("完成")
    print(f"  车系:  {len(all_series)} 条 → {SERIES_CSV}")
    print(f"  车款:  {len(all_variants)} 条 → {VARIANTS_CSV}")
    print(f"  配置:  {len(config_done)} 条 → {CONFIG_CSV}")
    print("=" * 60)


def debug_car_info(car_id: int) -> None:
    """诊断用：打印某车型 info 字段的原始 JSON 结构，用于排查一对多字段"""
    url = f"https://www.dongchedi.com/auto/params-carIds-{car_id}"
    headers = {**BASE_HEADERS, "Referer": "https://www.dongchedi.com"}
    resp = request_with_proxy("GET", url, headers=headers)

    scripts = re.findall(r"<script[^>]*>(.*?)</script>", resp.text, re.DOTALL)
    ssr_script = max(scripts, key=len)
    data = json.loads(ssr_script)
    raw = data["props"]["pageProps"]["rawData"]
    car = raw["car_info"][0]

    print(f"\n=== car_id={car_id} info 字段类型统计 ===")
    type_stats: dict[str, list] = {"dict_with_value": [], "dict_without_value": [], "list": [], "other": []}
    for k, v in car.get("info", {}).items():
        if isinstance(v, dict):
            if "value" in v and len(v) == 1:
                type_stats["dict_with_value"].append(k)
            else:
                type_stats["dict_without_value"].append(k)
                print(f"  [特殊dict] {k}: {json.dumps(v, ensure_ascii=False)[:200]}")
        elif isinstance(v, list):
            type_stats["list"].append(k)
            print(f"  [list]     {k}: {json.dumps(v, ensure_ascii=False)[:200]}")
        else:
            type_stats["other"].append(k)
            print(f"  [other]    {k}: {repr(v)[:100]}")

    print(f"\n  普通dict(value): {len(type_stats['dict_with_value'])} 个")
    print(f"  特殊dict:        {len(type_stats['dict_without_value'])} 个")
    print(f"  list:            {len(type_stats['list'])} 个")
    print(f"  其他:            {len(type_stats['other'])} 个")

    # 打印 properties 完整结构（前5条 + 含目标标签的条目）
    print(f"\n=== properties 结构样例（前5条）===")
    for p in raw["properties"][:5]:
        print(f"  {json.dumps(p, ensure_ascii=False)}")

    print(f"\n=== properties 中含目标标签的条目 ===")
    targets = {"主动安全预警系统", "驾驶模式选择", "车道偏离预警", "前方碰撞预警"}
    for p in raw["properties"]:
        if p.get("text") in targets:
            print(f"  {json.dumps(p, ensure_ascii=False)}")


if __name__ == "__main__":
    # 诊断模式：python3 dongchedi_scraper.py debug <car_id>
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "debug":
        debug_car_info(int(sys.argv[2]))
    else:
        main()
