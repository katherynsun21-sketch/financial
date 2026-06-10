"""
财经新闻抓取 + 七赛道分类（保险/银行/非银/信贷/财富管理/监管动态/其他热门）
数据源: tophub.today / 东方财富 / 财联社 / 监管总局 / 证监会 / 央行
AI引擎: 火山引擎 Ark（兼容 OpenAI API，带 JSON 容错 + 关键词兜底）
"""

import os
import re
import sys
import json
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from openai import OpenAI

DATA_DIR = Path(__file__).parent / "data"
CUSTOM_TZ = timezone(timedelta(hours=8))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 25
MAX_RETRIES = 2
BATCH_SIZE = 20           # 每批送 20 条（之前 60 条太长导致模型输出为空）
LLM_RETRY_TIMES = 2
TOP_PER_BOARD = 10

LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
LLM_MODEL = os.environ.get("LLM_MODEL", "")

SECTORS = ["保险", "银行", "非银金融", "信贷", "财富管理", "监管动态", "其他热门"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("finance_fetcher")


# ============================================================
# 工具：HTTP
# ============================================================
def fetch_html(url: str, referer: str = "") -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            if not response.encoding or response.encoding.lower() == "iso-8859-1":
                response.encoding = response.apparent_encoding or "utf-8"
            logger.info(f"抓取成功 {len(response.text)} 字: {url}")
            return response.text
        except Exception as e:
            logger.warning(f"抓取失败: {e}")
            time.sleep(attempt * 3)
    raise RuntimeError(f"重试 {MAX_RETRIES} 次后仍失败")


# ============================================================
# 工具：热度
# ============================================================
def parse_heat_value(extra_text: str) -> dict:
    result = {"value": 0, "raw": extra_text}
    if not extra_text:
        return result
    m = re.match(r"([\d,]+(?:\.\d+)?)\s*(万|w|W|亿|k|K|千)?", str(extra_text).strip())
    if m:
        num_str = m.group(1).replace(",", "")
        unit = m.group(2) or ""
        try:
            v = float(num_str)
            if unit == "万":
                v *= 10_000
            elif unit == "亿":
                v *= 100_000_000
            elif unit in ("k", "K", "千"):
                v *= 1_000
            result["value"] = int(v) if v == int(v) else v
        except ValueError:
            pass
    return result


# ============================================================
# 关键词兜底分类器（LLM 失败时用，保证页面不空）
# 每个赛道给一组关键词，标题命中越多 → 归到该赛道
# ============================================================
SECTOR_KEYWORDS = {
    "保险": [
        "保险", "保监局", "保险公司", "寿险", "健康险", "财险", "车险", "重疾",
        "保费", "理赔", "代理人", "银保", "监管总局", "保协", "年金险",
    ],
    "银行": [
        "银行", "商业银行", "工行", "建行", "农行", "中行", "交行", "招行",
        "兴业", "浦发", "中信银行", "存款", "储蓄", "银行卡", "借记卡", "信用卡",
        "银行间", "LPR", "降准", "存款利率",
    ],
    "非银金融": [
        "券商", "证券公司", "中信证券", "中金", "华泰", "国泰君安", "海通",
        "基金", "公募", "私募", "信托", "期货", "IPO", "上市", "证券", "证监会",
        "A股", "沪指", "创业板", "科创板", "北交所", "资管",
    ],
    "信贷": [
        "信贷", "贷款", "房贷", "按揭", "经营贷", "消费贷", "普惠", "不良率",
        "LPR", "贷款利率", "首套", "二套", "首付", "MLF", "再贷款", "抵押",
    ],
    "财富管理": [
        "理财", "财富管理", "理财产品", "净值型", "私行", "私人银行", "理财子",
        "信托产品", "公募基金", "代销", "高净值", "家族信托",
    ],
    "监管动态": [
        "监管总局", "金融监管总局", "银保监会", "证监会", "央行", "人民银行",
        "PBC", "外管局", "外汇局", "国务院", "政策", "通知", "办法",
        "征求意见", "处罚", "罚款", "罚单", "立案", "调查", "修订",
        "发布公告", "答记者问", "国常会", "金融稳定", "金融委",
    ],
}

# 标题自带的"利好/利空"情绪关键词
POSITIVE_KW = ["增长", "上涨", "利好", "利好于", "突破", "超额", "创新高", "扩大",
               "增长", "大增", "上升", "收益", "盈利", "净利润", "释放", "回暖",
               "改善", "修复", "上涨", "爆涨", "增长", "繁荣"]
NEGATIVE_KW = ["下跌", "亏损", "下降", "下滑", "利空", "风险", "违约", "暴雷",
               "逾期", "爆雷", "罚单", "罚款", "处罚", "调查", "警告", "破产",
               "承压", "收紧", "缩表", "加息", "破产", "放缓", "下滑"]


def classify_by_keyword(title: str) -> dict:
    """纯关键词分类。标题命中关键词数组 → 选命中最多的那个赛道；都不命中 → 其他热门"""
    t = title or ""
    # 每个赛道算命中次数（关键词在标题里出现算 1 次）
    hits = {}
    for sector, kws in SECTOR_KEYWORDS.items():
        count = sum(1 for k in kws if k in t)
        if count > 0:
            hits[sector] = count

    # 选出命中最多的赛道（若并列，取前面的，因为 SECTOR_KEYWORDS 顺序有优先级）
    if hits:
        primary = max(hits.keys(), key=lambda s: (hits[s], -list(SECTOR_KEYWORDS).index(s)))
    else:
        # 完全没命中 → 其他热门。但只要标题含中文，给个中性
        primary = "其他热门"

    # 情绪打分（粗粒度）
    pos = sum(1 for k in POSITIVE_KW if k in t)
    neg = sum(1 for k in NEGATIVE_KW if k in t)
    score = 0
    level = "中性"
    if pos > neg and pos >= 1:
        score = 1
        level = "利好"
    elif neg > pos and neg >= 1:
        score = -1
        level = "利空"

    # 生成一个所有赛道的 impact 结构（只有 primary 有分数，其他中性）
    impact = {}
    for s in SECTORS:
        impact[s] = {"score": 0, "level": "中性", "reason": ""}
    # 标题里还可能带其他赛道小信号：每个赛道只看是否有相关关键词
    for sector, kws in SECTOR_KEYWORDS.items():
        sub_hits = sum(1 for k in kws if k in t)
        if sector == primary:
            impact[sector] = {
                "score": score,
                "level": level,
                "reason": f"关键词命中 ({hits.get(sector, 0)}个)，情绪{level}",
            }
        elif sub_hits > 0:
            impact[sector] = {
                "score": 0,
                "level": "中性",
                "reason": f"间接提及（{sub_hits}个关键词）",
            }
    # "其他热门"赛道特殊处理
    if primary == "其他热门":
        impact["其他热门"] = {"score": 0, "level": "中性", "reason": "未命中任何赛道关键词"}
    return {
        "primary_sector": primary,
        "sector_impact": impact,
        "summary": title[:30],
    }


# ============================================================
# 1. tophub.today
# ============================================================
def parse_tophub_card(card_html) -> dict:
    title_elem = card_html.find(class_="cc-cd-lb")
    title = title_elem.get_text(strip=True) if title_elem else "未知榜单"
    sub_elem = card_html.find(class_="cc-cd-sb-st")
    subtitle = sub_elem.get_text(strip=True) if sub_elem else ""
    items = []
    item_container = card_html.find(class_="cc-cd-cb")
    if item_container:
        for idx, a_tag in enumerate(item_container.find_all("a")[:TOP_PER_BOARD], 1):
            rank_elem = a_tag.find(class_="s")
            t_elem = a_tag.find(class_="t")
            e_elem = a_tag.find(class_="e")
            rank_text = rank_elem.get_text(strip=True) if rank_elem else str(idx)
            t = t_elem.get_text(strip=True) if t_elem else ""
            e = e_elem.get_text(strip=True) if e_elem else ""
            hv = parse_heat_value(e)
            items.append({
                "rank": int(rank_text) if rank_text.isdigit() else idx,
                "title": t,
                "heat_raw": e,
                "heat_value": hv["value"],
                "url": a_tag.get("href", ""),
            })
    return {
        "board_name": f"[TopHub] {title}",
        "board_subtitle": subtitle,
        "update_time": "",
        "items_count": len(items),
        "items": items,
    }


def fetch_tophub() -> list[dict]:
    boards = []
    for url in ["https://tophub.today/", "https://tophub.today/c/finance"]:
        try:
            html = fetch_html(url)
            soup = BeautifulSoup(html, "html.parser")
            cards = soup.find_all(class_="cc-cd")
            logger.info(f"tophub 抓到 {len(cards)} 个榜单")
            for c in cards:
                b = parse_tophub_card(c)
                if b["items_count"] > 0:
                    boards.append(b)
        except Exception as e:
            logger.warning(f"tophub 失败: {e}")
    return boards


# ============================================================
# 2. 东方财富
# ============================================================
def fetch_eastmoney() -> list[dict]:
    items_all = []
    try:
        html = fetch_html("https://kuaixun.eastmoney.com/", "https://kuaixun.eastmoney.com/")
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a")[:200]:
            t = a.get_text(strip=True)
            if not t or len(t) < 8 or len(t) > 140:
                continue
            if not re.search(r"[\u4e00-\u9fa5]", t):
                continue
            if re.search(r"^(更多|查看|首页|资讯|财经|股票|新股|数据)$", t):
                continue
            url = a.get("href", "")
            if url.startswith("//"):
                url = "https:" + url
            elif url.startswith("/"):
                url = "https://kuaixun.eastmoney.com" + url
            items_all.append({"title": t, "url": url})
        logger.info(f"东方财富: {len(items_all)} 条")
    except Exception as e:
        logger.warning(f"东方财富失败: {e}")

    if not items_all:
        return []
    return [{
        "board_name": "[东方财富] 快讯",
        "board_subtitle": "东方财富网 - 财经快讯",
        "update_time": "",
        "items_count": len(items_all),
        "items": [{
            "rank": idx + 1,
            "title": it["title"],
            "heat_raw": "",
            "heat_value": 0,
            "url": it["url"],
        } for idx, it in enumerate(items_all)],
    }]


# ============================================================
# 3. 财联社
# ============================================================
def fetch_cailianpress() -> list[dict]:
    items_all = []
    try:
        html = fetch_html("https://www.cls.cn/telegraph", "https://www.cls.cn/")
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a")[:200]:
            t = a.get_text(strip=True)
            if not t or len(t) < 10 or len(t) > 180:
                continue
            if not re.search(r"[\u4e00-\u9fa5]", t):
                continue
            if re.search(r"^(首页|电报|滚动|公司|券商|基金|更多)$", t):
                continue
            url = a.get("href", "")
            if url.startswith("/"):
                url = "https://www.cls.cn" + url
            items_all.append({"title": t, "url": url})
        logger.info(f"财联社: {len(items_all)} 条")
    except Exception as e:
        logger.warning(f"财联社失败: {e}")

    if not items_all:
        return []
    return [{
        "board_name": "[财联社] 电报",
        "board_subtitle": "财联社 - 财经快讯",
        "update_time": "",
        "items_count": len(items_all),
        "items": [{
            "rank": idx + 1,
            "title": it["title"][:140],
            "heat_raw": "",
            "heat_value": 0,
            "url": it.get("url", ""),
        } for idx, it in enumerate(items_all)],
    }]


# ============================================================
# 4. 三部门（监管总局 / 证监会 / 央行）
# ============================================================
def fetch_regulator() -> list[dict]:
    all_titles = []

    # 金融监管总局
    try:
        html = fetch_html("http://www.cbirc.gov.cn/cn/view/pages/ItemList.html?itemPId=922")
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a")[:150]:
            t = a.get_text(strip=True)
            href = a.get("href", "")
            if not t or len(t) < 10 or len(t) > 120:
                continue
            if not re.search(r"(notice|news|ItemDetail|ItemList|gonggao|article)", href, re.IGNORECASE):
                continue
            if not re.search(r"[\u4e00-\u9fa5]", t):
                continue
            if href.startswith("/"):
                href = "http://www.cbirc.gov.cn" + href
            all_titles.append({"title": t, "url": href, "source": "国家金融监管总局"})
    except Exception as e:
        logger.warning(f"监管总局失败: {e}")

    # 证监会
    try:
        html = fetch_html("http://www.csrc.gov.cn/csrc/c100028/zfxxgk.shtml", "http://www.csrc.gov.cn/")
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a")[:150]:
            t = a.get_text(strip=True)
            href = a.get("href", "")
            if not t or len(t) < 10 or len(t) > 120:
                continue
            if not re.search(r"\.(htm|html)", href, re.IGNORECASE):
                continue
            if not re.search(r"[\u4e00-\u9fa5]", t):
                continue
            if href.startswith("/"):
                href = "http://www.csrc.gov.cn" + href
            all_titles.append({"title": t, "url": href, "source": "证监会"})
    except Exception as e:
        logger.warning(f"证监会失败: {e}")

    # 央行
    try:
        html = fetch_html("http://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html", "http://www.pbc.gov.cn/")
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a")[:150]:
            t = a.get_text(strip=True)
            href = a.get("href", "")
            if not t or len(t) < 10 or len(t) > 140:
                continue
            if not re.search(r"\.(htm|html)", href, re.IGNORECASE):
                continue
            if not re.search(r"[\u4e00-\u9fa5]", t):
                continue
            if href.startswith("/"):
                href = "http://www.pbc.gov.cn" + href
            all_titles.append({"title": t, "url": href, "source": "央行"})
    except Exception as e:
        logger.warning(f"央行失败: {e}")

    if not all_titles:
        return []
    return [{
        "board_name": "[监管动态] 三部门发文",
        "board_subtitle": "金融监管总局 / 证监会 / 央行",
        "update_time": "",
        "items_count": len(all_titles),
        "items": [{
            "rank": idx + 1,
            "title": it["title"],
            "heat_raw": "",
            "heat_value": 0,
            "url": it["url"],
        } for idx, it in enumerate(all_titles)],
    }]


# ============================================================
# 汇总 + 去重
# ============================================================
def fetch_all_sources() -> list[dict]:
    boards = []
    for fn in [fetch_tophub, fetch_eastmoney, fetch_cailianpress, fetch_regulator]:
        try:
            boards += fn()
        except Exception as e:
            logger.warning(f"{fn.__name__} 整体失败: {e}")

    seen_titles = set()
    for b in boards:
        kept = []
        for it in b.get("items", []):
            key = it["title"][:30] if it.get("title") else ""
            if key and key not in seen_titles:
                seen_titles.add(key)
                kept.append(it)
        b["items"] = kept
        b["items_count"] = len(kept)
    boards = [b for b in boards if b.get("items_count", 0) > 0]
    logger.info(f"=== 合并后 {len(boards)} 个榜单，{sum(b['items_count'] for b in boards)} 条新闻 ===")
    return boards


def collect_items_for_llm(all_boards: list[dict]) -> list[dict]:
    merged = []
    seen = set()
    for board in all_boards:
        for item in board.get("items", []):
            key = item["title"][:30] if item.get("title") else ""
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append({
                "id": len(merged) + 1,
                "title": item["title"],
                "source_board": board.get("board_name", "未知"),
            })
    return merged


# ============================================================
# LLM 调用（简化版 prompt + 每批 20 条）
# ============================================================
def build_short_prompt(items_batch: list[dict]) -> str:
    """极简版 prompt：每条只输出 1 个赛道 + 分数，大幅降低输出量"""
    lines = "\n".join(
        f"[{i['id']}] {i['title']}"
        for i in items_batch
    )
    return f"""你是财经信息分类器。对下面每条新闻标题，输出 JSON。

【赛道】保险 / 银行 / 非银金融 / 信贷 / 财富管理 / 监管动态 / 其他热门
【评分】-2 强利空 / -1 利空 / 0 中性 / 1 利好 / 2 强利好

输出格式（只输出 JSON，不要任何解释和代码块）：
{{
  "items": [
    {{"id": 数字, "primary": "赛道名", "score": 数字, "reason": "不超过15字"}}
  ]
}}

新闻列表：
{lines}
"""


def parse_llm_json_strict(text: str) -> list[dict]:
    """比之前更激进的 JSON 修复：专门处理 items 为空的情况。"""
    if not text:
        return []

    # 找第一个 { 和 最后一个 }
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return []
    core = text[start:end + 1]

    # 尝试直接解析
    try:
        data = json.loads(core)
        items = data.get("items", [])
        if items:
            return items
    except json.JSONDecodeError:
        pass

    # 如果解析失败，用正则硬抓所有 "id":xx,"primary":"xx","score":xx 片段
    pattern = re.compile(
        r'"id"\s*:\s*(\d+)\s*,\s*"primary"\s*:\s*"([^"]+)"\s*,\s*"score"\s*:\s*(-?\d+)'
    )
    fallback = []
    for m in pattern.finditer(core):
        try:
            reason_m = re.search(
                r'"id"\s*:\s*' + m.group(1) +
                r'[^}]*"reason"\s*:\s*"([^"]{0,30})"',
                core,
            )
            reason = reason_m.group(1) if reason_m else ""
            fallback.append({
                "id": int(m.group(1)),
                "primary": m.group(2),
                "score": int(m.group(3)),
                "reason": reason,
            })
        except Exception:
            pass
    return fallback


def call_llm_batch(items_batch: list[dict], batch_no: int) -> list[dict]:
    """对一批新闻调用 LLM，返回字典列表。失败则返回空列表（走关键词兜底
