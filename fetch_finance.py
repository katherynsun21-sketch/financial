"""财经新闻多源抓取、五大赛道分类和营销洞察生成脚本。"""

import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

DATA_DIR = Path(__file__).parent / "data"
CUSTOM_TZ = timezone(timedelta(hours=8))

CONNECT_TIMEOUT = 5
READ_TIMEOUT = 10
MAX_RETRIES = 2
TOPHUB_TOP_PER_BOARD = 15
MAX_GENERIC_ITEMS_PER_SOURCE = 30
MAX_TOTAL_ITEMS = 220
LLM_BATCH_SIZE = 20
LLM_TIMEOUT = 45

LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").strip() or "https://ark.cn-beijing.volces.com/api/v3"
LLM_MODEL = os.getenv("LLM_MODEL", "").strip()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

SECTORS = ["保险", "非金", "信贷", "财富", "银行"]

SECTOR_KEYWORDS = {
    "保险": [
        "保险", "险企", "寿险", "财险", "车险", "健康险", "养老险", "再保险", "保费",
        "赔付", "赔款", "理赔", "保险资管", "中国人寿", "中国平安", "中国太保",
        "新华保险", "人保", "众安", "保险公司", "银保", "精算", "万能险", "分红险",
    ],
    "银行": [
        "银行", "商业银行", "股份行", "城商行", "农商行", "村镇银行", "银行业", "存款",
        "息差", "净息差", "不良率", "拨备", "资本充足", "理财子", "招行", "工行",
        "建行", "农行", "中行", "交行", "邮储", "浦发", "兴业", "平安银行", "中信银行",
    ],
    "信贷": [
        "信贷", "贷款", "房贷", "按揭", "消费贷", "经营贷", "小微", "普惠", "融资",
        "授信", "征信", "债务", "逾期", "违约", "展期", "延期还本", "降息", "LPR",
        "社融", "信用", "按揭贷款", "房地产融资", "融资租赁", "保理", "助贷",
    ],
    "财富": [
        "财富", "理财", "基金", "公募", "私募", "信托", "资管", "券商资管", "净值",
        "养老金融", "养老金", "个人养老金", "FOF", "ETF", "投顾", "资产配置", "高净值",
        "家族信托", "固收", "权益", "债基", "货基", "财富管理", "代销", "客户资产",
    ],
    "非金": [
        "证券", "券商", "投行", "经纪", "两融", "融资融券", "期货", "交易所", "A股",
        "港股", "北交所", "IPO", "并购", "重组", "创投", "租赁", "担保", "典当",
        "小贷", "支付", "金融科技", "互金", "消金", "消费金融", "AMC", "资产管理公司",
    ],
}

POSITIVE_KEYWORDS = [
    "增长", "上涨", "提升", "改善", "回暖", "利好", "支持", "鼓励", "加快", "扩大", "创新",
    "降准", "降息", "减费", "增持", "获批", "突破", "修复", "复苏", "盈利", "放宽",
]
NEGATIVE_KEYWORDS = [
    "下滑", "下降", "亏损", "风险", "处罚", "罚款", "收紧", "违约", "逾期", "暴雷", "承压",
    "不良", "退市", "调查", "叫停", "减少", "裁员", "整改", "问责", "低迷", "拖累",
]

NEWS_KEYWORDS = sorted(set(sum(SECTOR_KEYWORDS.values(), [])) | set(POSITIVE_KEYWORDS) | set(NEGATIVE_KEYWORDS))

TOPHUB_URL = "https://tophub.today/c/finance"

GENERIC_HTML_SOURCES = [
    {"name": "东方财富-财经", "url": "https://finance.eastmoney.com/", "type": "财经媒体"},
    {"name": "东方财富-银行", "url": "https://bank.eastmoney.com/", "type": "财经媒体"},
    {"name": "东方财富-保险", "url": "https://insurance.eastmoney.com/", "type": "财经媒体"},
    {"name": "东方财富-证券", "url": "https://stock.eastmoney.com/a/cgsxw.html", "type": "财经媒体"},
    {"name": "财联社-电报", "url": "https://www.cls.cn/telegraph", "type": "财经媒体"},
    {"name": "财联社-金融", "url": "https://www.cls.cn/finance", "type": "财经媒体"},
    {"name": "第一财经", "url": "https://www.yicai.com/news/", "type": "财经媒体"},
    {"name": "21财经", "url": "https://www.21jingji.com/channel/readnumber/", "type": "财经媒体"},
    {"name": "证券时报", "url": "https://www.stcn.com/", "type": "财经媒体"},
    {"name": "每日经济新闻", "url": "https://www.nbd.com.cn/", "type": "财经媒体"},
    {"name": "新浪财经", "url": "https://finance.sina.com.cn/", "type": "财经媒体"},
    {"name": "央行", "url": "https://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html", "type": "监管机构"},
    {"name": "国家金融监督管理总局", "url": "https://www.nfra.gov.cn/cn/view/pages/index/index.html", "type": "监管机构"},
    {"name": "证监会", "url": "https://www.csrc.gov.cn/csrc/c100028/zfxxgk_zdgk.shtml", "type": "监管机构"},
    {"name": "财政部", "url": "https://www.mof.gov.cn/zhengwuxinxi/caizhengxinwen/", "type": "监管机构"},
    {"name": "外汇局", "url": "https://www.safe.gov.cn/", "type": "监管机构"},
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("finance_fetcher")


def now_info() -> dict:
    now_bj = datetime.now(CUSTOM_TZ)
    now_utc = datetime.now(timezone.utc)
    return {
        "fetched_at": now_bj.isoformat(),
        "fetched_at_utc": now_utc.isoformat(),
        "date": now_bj.strftime("%Y-%m-%d"),
        "time": now_bj.strftime("%H:%M:%S"),
    }


def fetch_html(url: str, referer: str = "") -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Connection": "close",
    }
    if referer:
        headers["Referer"] = referer

    last_exception = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info("抓取中 第 %s/%s 次: %s", attempt, MAX_RETRIES, url)
            resp = requests.get(url, headers=headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            resp.raise_for_status()
            if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
                resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text
        except Exception as exc:
            last_exception = exc
            if attempt < MAX_RETRIES:
                time.sleep(attempt * 2)
            else:
                logger.warning("抓取失败，已跳过: %s | %s", url, exc)
    logger.warning("重试失败，返回空内容: %s", last_exception)
    return ""


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def item_id(title: str, url: str, source: str) -> str:
    raw = "%s|%s|%s" % (title, url, source)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def absolute_url(base_url: str, href: str) -> str:
    if not href:
        return ""
    if href.startswith("javascript:") or href.startswith("#"):
        return ""
    return urljoin(base_url, href)


def parse_heat_value(extra_text: str) -> dict:
    result = {"value": 0, "unit": "", "raw": extra_text or ""}
    text = normalize_text(extra_text)
    if not text:
        return result
    m = re.match(r"([\d,]+(?:\.\d+)?)\s*(万|w|W|亿|k|K|千)?", text)
    if not m:
        return result
    try:
        value = float(m.group(1).replace(",", ""))
        unit = m.group(2) or ""
        if unit in ("万", "w", "W"):
            value *= 10000
        elif unit == "亿":
            value *= 100000000
        elif unit in ("k", "K", "千"):
            value *= 1000
        result["value"] = int(value) if value == int(value) else value
        result["unit"] = unit
    except ValueError:
        pass
    return result


def classify_by_keyword(title: str, source: str = "") -> dict:
    text = "%s %s" % (title, source)
    sector_scores = {}
    for sector, keywords in SECTOR_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw and kw in text)
        sector_scores[sector] = score

    sector = max(sector_scores, key=sector_scores.get)
    if sector_scores[sector] == 0:
        sector = guess_sector_from_source(source)

    pos = sum(1 for kw in POSITIVE_KEYWORDS if kw in text)
    neg = sum(1 for kw in NEGATIVE_KEYWORDS if kw in text)
    impact_score = max(-2, min(2, pos - neg))
    if impact_score > 0:
        score_label = "利好"
    elif impact_score < 0:
        score_label = "利空"
    else:
        score_label = "中性"

    return {
        "sector": sector,
        "score_label": score_label,
        "impact_score": impact_score,
        "reason": "关键词兜底分类：%s；情绪判断：%s。" % (sector, score_label),
        "marketing_angle": build_fallback_angle(sector, score_label),
        "customer_segments": fallback_segments(sector),
        "actions": fallback_actions(sector, score_label),
    }


def guess_sector_from_source(source: str) -> str:
    if "保险" in source:
        return "保险"
    if "银行" in source or "央行" in source:
        return "银行"
    if "证券" in source or "证监" in source or "财联社" in source:
        return "非金"
    return "财富"


def build_fallback_angle(sector: str, score_label: str) -> str:
    if score_label == "利好":
        return "%s赛道出现正向信号，可结合政策、产品或市场情绪做客户触达。" % sector
    if score_label == "利空":
        return "%s赛道存在风险信号，适合做风险提示、资产检视和稳健配置沟通。" % sector
    return "%s赛道信息偏中性，适合做资讯解读和客户教育。" % sector


def fallback_segments(sector: str) -> list:
    mapping = {
        "保险": ["家庭保障客户", "养老规划客户", "高净值保障客户"],
        "银行": ["存款客户", "按揭客户", "中小企业主"],
        "信贷": ["小微企业主", "房贷客户", "消费金融客户"],
        "财富": ["理财客户", "基金客户", "高净值客户"],
        "非金": ["证券客户", "活跃交易客户", "金融科技关注人群"],
    }
    return mapping.get(sector, ["泛金融客户"])


def fallback_actions(sector: str, score_label: str) -> list:
    if score_label == "利空":
        return ["推送风险提示", "安排客户资产检视", "提供稳健配置方案"]
    if score_label == "利好":
        return ["提炼政策利好解读", "匹配相关产品卖点", "筛选高意向客户触达"]
    return ["制作资讯解读", "用于客户教育", "观察后续政策和市场变化"]


def parse_tophub_card(card_html) -> dict:
    title_elem = card_html.find(class_="cc-cd-lb")
    board_name = normalize_text(title_elem.get_text(" ", strip=True)) if title_elem else "未知榜单"
    sub_elem = card_html.find(class_="cc-cd-sb-st")
    board_subtitle = normalize_text(sub_elem.get_text(" ", strip=True)) if sub_elem else ""
    items = []
    container = card_html.find(class_="cc-cd-cb")
    if not container:
        return {"board_name": board_name, "board_subtitle": board_subtitle, "items": []}

    for idx, a_tag in enumerate(container.find_all("a"), 1):
        if idx > TOPHUB_TOP_PER_BOARD:
            break
        title_elem = a_tag.find(class_="t")
        extra_elem = a_tag.find(class_="e")
        title = normalize_text(title_elem.get_text(" ", strip=True)) if title_elem else normalize_text(a_tag.get_text(" ", strip=True))
        if not title or len(title) < 5:
            continue
        url = absolute_url(TOPHUB_URL, a_tag.get("href", ""))
        extra = normalize_text(extra_elem.get_text(" ", strip=True)) if extra_elem else ""
        items.append({
            "id": item_id(title, url, board_name),
            "title": title,
            "url": url,
            "source": "今日热榜-%s" % board_name,
            "source_type": "热榜",
            "rank": idx,
            "extra": extra,
            "heat": parse_heat_value(extra),
            "published_at": "",
        })
    return {"board_name": board_name, "board_subtitle": board_subtitle, "items": items}


def fetch_tophub() -> tuple:
    html = fetch_html(TOPHUB_URL)
    if not html:
        return [], []
    soup = BeautifulSoup(html, "html.parser")
    boards = []
    items = []
    for card in soup.find_all(class_="cc-cd"):
        try:
            board = parse_tophub_card(card)
            boards.append(board)
            items.extend(board["items"])
        except Exception as exc:
            logger.warning("解析今日热榜卡片失败: %s", exc)
    logger.info("今日热榜抓取 %s 个榜单，%s 条", len(boards), len(items))
    return items, boards


def is_useful_title(title: str) -> bool:
    title = normalize_text(title)
    if len(title) < 8 or len(title) > 90:
        return False
    bad_words = ["首页", "登录", "注册", "广告", "更多", "专题", "视频", "图片", "客户端", "版权", "联系我们"]
    if any(word in title for word in bad_words):
        return False
    return any(word in title for word in NEWS_KEYWORDS) or len(title) >= 14


def parse_generic_html(source: dict, html: str) -> list:
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen = set()

    for tag in soup.find_all("a"):
        title = normalize_text(tag.get("title") or tag.get_text(" ", strip=True))
        if not is_useful_title(title):
            continue
        url = absolute_url(source["url"], tag.get("href", ""))
        dedup_key = title[:40]
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        items.append({
            "id": item_id(title, url, source["name"]),
            "title": title,
            "url": url,
            "source": source["name"],
            "source_type": source.get("type", "网页"),
            "rank": len(items) + 1,
            "extra": "",
            "heat": {"value": 0, "unit": "", "raw": ""},
            "published_at": "",
        })
        if len(items) >= MAX_GENERIC_ITEMS_PER_SOURCE:
            break
    return items


def fetch_generic_sources() -> tuple:
    all_items = []
    source_status = []
    for source in GENERIC_HTML_SOURCES:
        html = fetch_html(source["url"])
        items = parse_generic_html(source, html)
        all_items.extend(items)
        source_status.append({
            "name": source["name"],
            "url": source["url"],
            "type": source.get("type", "网页"),
            "items_count": len(items),
            "ok": bool(html),
        })
        logger.info("%s 抓取 %s 条", source["name"], len(items))
    return all_items, source_status


def dedupe_items(items: list) -> list:
    result = []
    seen_title = set()
    seen_url = set()
    for item in items:
        title = normalize_text(item.get("title", ""))
        url = item.get("url", "")
        if not title:
            continue
        title_key = re.sub(r"[\s，。！？、：:,.!?]", "", title)[:42]
        if title_key in seen_title or (url and url in seen_url):
            continue
        seen_title.add(title_key)
        if url:
            seen_url.add(url)
        item["title"] = title
        result.append(item)
        if len(result) >= MAX_TOTAL_ITEMS:
            break
    return result


def build_classify_prompt(batch: list) -> str:
    lines = [
        "你是金融机构营销策略分析师。请把新闻归入五大赛道，并判断影响方向。",
        "五大赛道只能从以下选择：保险、非金、信贷、财富、银行。",
        "score_label只能是：利好、利空、中性。impact_score只能是-2,-1,0,1,2。",
        "请只输出JSON，不要Markdown，不要解释。格式：",
        '{"items":[{"id":"","sector":"保险/非金/信贷/财富/银行","score_label":"利好/利空/中性","impact_score":0,"reason":"20字内","marketing_angle":"40字内","customer_segments":["客群1","客群2"],"actions":["动作1","动作2"]}]}',
        "新闻列表：",
    ]
    for item in batch:
        lines.append("- id=%s | source=%s | title=%s" % (item["id"], item["source"], item["title"]))
    return "\n".join(lines)


def extract_json(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {}
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return {}
    return {}


def call_llm_classify(items: list) -> dict:
    if not LLM_API_KEY or not LLM_MODEL:
        logger.warning("未配置 LLM_API_KEY 或 LLM_MODEL，使用关键词兜底分类。")
        return {}

    try:
        from openai import OpenAI
    except Exception as exc:
        logger.warning("openai 依赖不可用，使用关键词兜底分类: %s", exc)
        return {}

    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL, timeout=LLM_TIMEOUT)
    result = {}
    for batch_no, start in enumerate(range(0, len(items), LLM_BATCH_SIZE), 1):
        batch = items[start:start + LLM_BATCH_SIZE]
        prompt = build_classify_prompt(batch)
        try:
            logger.info("调用大模型分类批次 %s，%s 条", batch_no, len(batch))
            resp = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": "你只输出合法JSON。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
            )
            content = resp.choices[0].message.content
            data = extract_json(content)
            for row in data.get("items", []):
                item_key = row.get("id")
                if item_key:
                    result[item_key] = row
        except Exception as exc:
            logger.warning("大模型分类批次失败，改用关键词兜底: %s", exc)
    logger.info("大模型分类覆盖 %s/%s 条", len(result), len(items))
    return result


def merge_classification(items: list, llm_map: dict) -> list:
    enriched = []
    for item in items:
        fallback = classify_by_keyword(item["title"], item.get("source", ""))
        llm_row = llm_map.get(item["id"], {}) if llm_map else {}
        sector = llm_row.get("sector") if llm_row.get("sector") in SECTORS else fallback["sector"]
        score_label = llm_row.get("score_label") if llm_row.get("score_label") in ["利好", "利空", "中性"] else fallback["score_label"]
        try:
            impact_score = int(llm_row.get("impact_score", fallback["impact_score"]))
        except Exception:
            impact_score = fallback["impact_score"]
        impact_score = max(-2, min(2, impact_score))
        merged = dict(item)
        merged.update({
            "sector": sector,
            "score_label": score_label,
            "impact_score": impact_score,
            "reason": llm_row.get("reason") or fallback["reason"],
            "marketing_angle": llm_row.get("marketing_angle") or fallback["marketing_angle"],
            "customer_segments": llm_row.get("customer_segments") or fallback["customer_segments"],
            "actions": llm_row.get("actions") or fallback["actions"],
            "classified_by": "llm" if item["id"] in llm_map else "keyword",
        })
        enriched.append(merged)
    return enriched


def summarize_by_sector(items: list) -> tuple:
    by_sector = {sector: [] for sector in SECTORS}
    for item in items:
        by_sector.setdefault(item["sector"], []).append(item)

    summary = {}
    for sector in SECTORS:
        rows = by_sector.get(sector, [])
        positive = sum(1 for x in rows if x.get("score_label") == "利好")
        negative = sum(1 for x in rows if x.get("score_label") == "利空")
        neutral = sum(1 for x in rows if x.get("score_label") == "中性")
        avg_score = round(sum(x.get("impact_score", 0) for x in rows) / len(rows), 2) if rows else 0
        summary[sector] = {
            "count": len(rows),
            "positive": positive,
            "negative": negative,
            "neutral": neutral,
            "avg_impact_score": avg_score,
            "top_titles": [x["title"] for x in rows[:5]],
        }
        by_sector[sector] = sorted(rows, key=lambda x: (abs(x.get("impact_score", 0)), x.get("rank", 999)), reverse=True)
    return by_sector, summary


def build_insight_prompt(sector_summary: dict, by_sector: dict) -> str:
    compact = {}
    for sector in SECTORS:
        compact[sector] = {
            "summary": sector_summary.get(sector, {}),
            "news": [
                {
                    "title": item["title"],
                    "score_label": item["score_label"],
                    "reason": item["reason"],
                }
                for item in by_sector.get(sector, [])[:8]
            ],
        }
    return "\n".join([
        "你是金融机构营销负责人，请基于新闻分类结果生成营销洞察。",
        "只输出JSON，不要Markdown。格式：",
        '{"overall":"总览100字内","sectors":{"保险":{"insight":"","opportunity":"","risk":"","suggested_campaigns":[""],"priority":"高/中/低"},"非金":{},"信贷":{},"财富":{},"银行":{}},"today_actions":[""]}',
        "输入数据：",
        json.dumps(compact, ensure_ascii=False),
    ])


def fallback_marketing_insights(sector_summary: dict, by_sector: dict) -> dict:
    sectors = {}
    for sector in SECTORS:
        summary = sector_summary.get(sector, {})
        count = summary.get("count", 0)
        avg = summary.get("avg_impact_score", 0)
        if count == 0:
            priority = "低"
        elif avg > 0.3 or count >= 8:
            priority = "高"
        else:
            priority = "中"
        sectors[sector] = {
            "insight": "%s今日共捕捉%s条相关新闻，平均影响分%s。" % (sector, count, avg),
            "opportunity": build_fallback_angle(sector, "利好" if avg > 0 else "中性"),
            "risk": "关注政策、市场波动和客户预期变化。",
            "suggested_campaigns": fallback_actions(sector, "利好" if avg > 0 else "中性"),
            "priority": priority,
        }
    return {
        "overall": "已基于多源财经新闻完成五大赛道归类，可用于每日晨会、客户触达和营销选题。",
        "sectors": sectors,
        "today_actions": ["优先查看高优先级赛道", "挑选利好新闻制作客户话术", "对利空新闻补充风险提示"],
        "generated_by": "keyword_fallback",
    }


def call_llm_insights(sector_summary: dict, by_sector: dict) -> dict:
    fallback = fallback_marketing_insights(sector_summary, by_sector)
    if not LLM_API_KEY or not LLM_MODEL:
        return fallback
    try:
        from openai import OpenAI
        client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL, timeout=LLM_TIMEOUT)
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": "你只输出合法JSON。"},
                {"role": "user", "content": build_insight_prompt(sector_summary, by_sector)},
            ],
            temperature=0.3,
        )
        data = extract_json(resp.choices[0].message.content)
        if data and isinstance(data.get("sectors"), dict):
            data["generated_by"] = "llm"
            return data
    except Exception as exc:
        logger.warning("大模型洞察生成失败，使用兜底洞察: %s", exc)
    return fallback


def save_json(data: dict, filename: Path) -> None:
    filename.parent.mkdir(parents=True, exist_ok=True)
    with open(filename, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)
    logger.info("已保存: %s", filename)


def main() -> int:
    logger.info("=== 财经新闻抓取、分类和营销洞察生成开始 ===")
    meta = now_info()

    tophub_items, raw_boards = fetch_tophub()
    generic_items, source_status = fetch_generic_sources()
    all_items = dedupe_items(tophub_items + generic_items)

    llm_map = call_llm_classify(all_items)
    classified_items = merge_classification(all_items, llm_map)
    by_sector, sector_summary = summarize_by_sector(classified_items)
    marketing_insights = call_llm_insights(sector_summary, by_sector)

    data = {
        **meta,
        "status": "ok" if classified_items else "partial_failed",
        "error": "" if classified_items else "未抓取到有效新闻，可能是目标站点超时或页面结构变化。",
        "sectors": SECTORS,
        "sources_count": len(GENERIC_HTML_SOURCES) + 1,
        "total_items": len(classified_items),
        "llm_enabled": bool(LLM_API_KEY and LLM_MODEL),
        "llm_base_url": LLM_BASE_URL,
        "llm_model": LLM_MODEL,
        "source_status": source_status,
        "raw_boards": raw_boards,
        "sector_summary": sector_summary,
        "marketing_insights": marketing_insights,
        "by_sector": by_sector,
        "items": classified_items,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    save_json(data, DATA_DIR / "latest.json")
    save_json(data, DATA_DIR / (meta["date"] + ".json"))

    raw_data = {
        **meta,
        "source_status": source_status,
        "raw_boards": raw_boards,
        "items": all_items,
    }
    save_json(raw_data, DATA_DIR / (meta["date"] + "_raw.json"))

    classified_data = {
        **meta,
        "sector_summary": sector_summary,
        "marketing_insights": marketing_insights,
        "by_sector": by_sector,
        "items": classified_items,
    }
    save_json(classified_data, DATA_DIR / (meta["date"] + "_classification.json"))

    logger.info("=== 完成：%s 条新闻，LLM覆盖 %s 条 ===", len(classified_items), len(llm_map))
    return 0


if __name__ == "__main__":
    sys.exit(main())
