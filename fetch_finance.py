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
        "第三方支付", "支付公司", "消费金融公司", "贷款服务", "普惠金融", "助农贷款",
    ],
    "财富": [
        "财富", "理财", "基金", "公募", "私募", "信托", "资管", "券商资管", "净值",
        "养老金融", "养老金", "个人养老金", "FOF", "ETF", "投顾", "资产配置", "高净值",
        "家族信托", "固收", "权益", "债基", "货基", "财富管理", "代销", "客户资产",
        "证券", "期货", "典当", "证券公司", "基金公司", "财富平台",
    ],
    "非金": [
        "金融科技", "互金", "AMC", "资产管理公司", "综合金融平台", "中介服务",
        "金融科技公司", "数字金融",
    ],
}


EVENT_RULES = {
    "保险": [
        {"event_type": "保费增长", "keywords": ["保费增长", "新单增长", "NBV", "新业务价值", "续期改善"]},
        {"event_type": "养老健康政策", "keywords": ["养老金融", "个人养老金", "养老保险", "健康险", "长期护理"]},
        {"event_type": "投资收益改善", "keywords": ["权益市场回暖", "投资收益", "浮盈", "资产端改善"]},
        {"event_type": "赔付压力", "keywords": ["赔付上升", "赔款增加", "赔付率上升", "巨灾", "理赔压力"]},
        {"event_type": "监管处罚", "keywords": ["处罚", "罚款", "违规", "整改", "通报", "问责"]},
        {"event_type": "退保或销售承压", "keywords": ["退保", "销售下滑", "代理人减少", "保费下滑"]},
    ],
    "银行": [
        {"event_type": "信贷扩张", "keywords": ["贷款增长", "信贷投放", "社融增长", "支持实体", "普惠金融"]},
        {"event_type": "负债成本改善", "keywords": ["存款利率下调", "负债成本下降", "息差企稳", "净息差企稳"]},
        {"event_type": "资本补充", "keywords": ["资本补充", "永续债", "二级资本债", "资本充足率提升"]},
        {"event_type": "息差压力", "keywords": ["净息差收窄", "息差收窄", "息差承压", "降息"]},
        {"event_type": "资产质量风险", "keywords": ["不良率上升", "不良贷款", "拨备下降", "逾期", "违约", "房地产风险"]},
        {"event_type": "监管处罚", "keywords": ["处罚", "罚款", "违规", "整改", "通报", "问责"]},
    ],
    "信贷": [
        {"event_type": "融资支持", "keywords": ["融资支持", "信贷支持", "延期还本", "小微贷款", "普惠贷款", "贷款增长"]},
        {"event_type": "利率下行", "keywords": ["LPR下降", "降息", "贷款利率下降", "融资成本下降"]},
        {"event_type": "地产融资改善", "keywords": ["房地产融资", "白名单", "保交楼", "房贷利率下调"]},
        {"event_type": "逾期违约", "keywords": ["逾期", "违约", "坏账", "催收", "不良贷款", "债务风险"]},
        {"event_type": "授信收紧", "keywords": ["收紧", "压降", "暂停放款", "风控收紧", "贷款下滑"]},
    ],
    "财富": [
        {"event_type": "市场回暖", "keywords": ["A股上涨", "港股上涨", "市场回暖", "权益回暖", "风险偏好回升"]},
        {"event_type": "产品规模增长", "keywords": ["理财规模增长", "基金发行回暖", "ETF增长", "净申购", "规模增长"]},
        {"event_type": "养老财富机会", "keywords": ["个人养老金", "养老理财", "养老基金", "长期资金"]},
        {"event_type": "净值回撤", "keywords": ["净值回撤", "破净", "亏损", "赎回", "收益下滑"]},
        {"event_type": "信托风险", "keywords": ["信托风险", "信托违约", "兑付风险", "延期兑付", "爆雷"]},
        {"event_type": "监管处罚", "keywords": ["处罚", "罚款", "违规", "整改", "通报"]},
    ],
    "非金": [
        {"event_type": "金融科技机会", "keywords": ["金融科技", "数字金融", "AI金融", "互金平台", "综合金融平台"]},
        {"event_type": "资产管理处置", "keywords": ["AMC", "不良资产", "资产处置", "资产管理公司"]},
        {"event_type": "中介服务动态", "keywords": ["中介服务", "管理咨询", "综合服务"]},
        {"event_type": "监管处罚", "keywords": ["处罚", "罚款", "违规", "整改", "通报"]},
    ],
}

GENERAL_EVENT_RULES = [
    {"event_type": "监管处罚", "keywords": ["处罚", "罚款", "违规", "立案", "调查", "整改", "问责", "通报"]},
    {"event_type": "风险暴露", "keywords": ["暴雷", "违约", "逾期", "兑付风险", "债务风险", "不良率上升"]},
    {"event_type": "经营承压", "keywords": ["下滑", "下降", "亏损", "承压", "低迷", "减少"]},
    {"event_type": "政策支持", "keywords": ["支持", "鼓励", "扩大", "优化", "放宽", "减费", "降准"]},
    {"event_type": "增长改善", "keywords": ["增长", "提升", "改善", "回暖", "复苏", "盈利", "修复"]},
]

RULE_KEYWORDS = []
for rules in list(EVENT_RULES.values()) + [GENERAL_EVENT_RULES]:
    for rule in rules:
        RULE_KEYWORDS.extend(rule["keywords"])

NEWS_KEYWORDS = sorted(
    set(sum(SECTOR_KEYWORDS.values(), []))
    | set(RULE_KEYWORDS)
)

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


PRIMARY_SECTORS = ["保险", "银行", "财富", "信贷"]


def pick_sector(text: str, source: str) -> tuple:
    sector_scores = {}
    for sector, keywords in SECTOR_KEYWORDS.items():
        sector_scores[sector] = sum(1 for kw in keywords if kw and kw in text)

    # 先看四个主赛道，优先排序
    primary_scores = {s: sector_scores.get(s, 0) for s in PRIMARY_SECTORS}
    best_primary = max(primary_scores, key=primary_scores.get)

    if primary_scores[best_primary] > 0:
        return best_primary, sector_scores

    # 四个主赛道都未命中时，再看非金
    if sector_scores.get("非金", 0) > 0:
        return "非金", sector_scores

    # 全部为 0，按来源兜底
    sector = guess_sector_from_source(source)
    return sector, sector_scores


def match_topic_rules(text: str, sector: str) -> tuple:
    matched = []
    rules = EVENT_RULES.get(sector, []) + GENERAL_EVENT_RULES
    for rule in rules:
        hits = [kw for kw in rule["keywords"] if kw in text]
        if not hits:
            continue
        matched.append({
            "topic_type": rule["event_type"],
            "keywords": hits[:4],
            "hit_count": len(hits),
        })

    if not matched:
        return "一般资讯", []

    primary = sorted(matched, key=lambda row: row["hit_count"], reverse=True)[0]
    return primary["topic_type"], matched[:5]


def calc_confidence(sector_scores: dict, matched_rules: list) -> float:
    best_sector_score = max(sector_scores.values()) if sector_scores else 0
    rule_hits = sum(len(row.get("keywords", [])) for row in matched_rules)
    confidence = 0.35 + min(best_sector_score, 3) * 0.12 + min(rule_hits, 4) * 0.08
    return round(min(confidence, 0.92), 2)


def build_relevance_reason(sector: str, topic_type: str, matched_rules: list) -> str:
    if not matched_rules:
        return "按来源和金融相关性归入%s赛道，未命中更细主题关键词。" % sector
    keywords = []
    for row in matched_rules:
        keywords.extend(row.get("keywords", []))
    keyword_text = "、".join(keywords[:6])
    return "归入%s赛道，主题为%s，命中关键词：%s。" % (
        sector,
        topic_type,
        keyword_text,
    )


def classify_by_keyword(title: str, source: str = "") -> dict:
    text = "%s %s" % (title, source)
    sector, sector_scores = pick_sector(text, source)
    topic_type, matched_rules = match_topic_rules(text, sector)
    confidence = calc_confidence(sector_scores, matched_rules)
    relevance_reason = build_relevance_reason(sector, topic_type, matched_rules)

    return {
        "sector": sector,
        "topic_type": topic_type,
        "confidence": confidence,
        "matched_rules": matched_rules,
        "relevance_reason": relevance_reason,
        "reason": relevance_reason,
        "sector_angle": build_fallback_angle(sector),
        "marketing_angle": build_fallback_angle(sector),
        "customer_segments": fallback_segments(sector),
        "actions": fallback_actions(sector),
    }


def guess_sector_from_source(source: str) -> str:
    if "保险" in source:
        return "保险"
    if "银行" in source or "央行" in source:
        return "银行"
    if "证券" in source or "证监" in source or "财联社" in source:
        # 按 SQL 口径：证券/期货优先归财富，不作为非金
        return "财富"
    # 默认放财富，非金仅作为兜底桶
    return "财富"


def build_fallback_angle(sector: str) -> str:
    return "%s赛道新闻可用于客户资讯解读、需求识别和后续话题跟进。" % sector


def fallback_segments(sector: str) -> list:
    mapping = {
        "保险": ["家庭保障客户", "养老规划客户", "高净值保障客户"],
        "银行": ["存款客户", "按揭客户", "中小企业主"],
        "信贷": ["小微企业主", "房贷客户", "消费金融客户"],
        "财富": ["理财客户", "基金客户", "高净值客户"],
        "非金": ["证券客户", "活跃交易客户", "金融科技关注人群"],
    }
    return mapping.get(sector, ["泛金融客户"])


def fallback_actions(sector: str) -> list:
    return ["制作赛道资讯解读", "筛选相关客群触达", "观察后续政策和市场变化"]


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
        "你是金融机构行业研究员。请把新闻归入五大赛道，用于资讯披露和局势观察。",
        "五大赛道只能从以下选择：保险、非金、信贷、财富、银行。",
        "归类规则（必须遵守，和业务口径一致）：",
        "1) 标题或来源中出现保险、寿险、财险、养老险、保费、赔付等，归保险。",
        "2) 出现银行、商业银行、存款、息差、按揭、银联等，归银行。",
        "3) 出现基金、证券、期货、理财、信托、资管、私募、公募、ETF、高净值、典当、蚂蚁财富等，归财富（这是重点优先级）。",
        "4) 出现信贷、贷款、房贷、消费贷、经营贷、小微、普惠、融资、支付、消费金融、第三方支付、贷款服务等，归信贷（优先级高于财富和非金）。",
        "5) 非金只作为兜底：只有当新闻主要讲金融科技、互金平台、综合金融平台、AMC且不满足上面任何一条时，才归非金。",
        "不要判断利好、利空、中性，不要输出影响分。只判断新闻和哪个赛道最相关。",
        "基金、证券、期货一定要归财富，不要归非金；支付、消费金融一定要归信贷，不要归非金。",
        "topic_type请归纳为：政策监管、产品动态、市场环境、机构经营、客户需求、风险事件、宏观经济、一般资讯等。",
        "confidence为0到1的小数，表示赛道归属置信度。",
        "请只输出JSON，不要Markdown，不要解释。格式：",
        '{"items":[{"id":"","sector":"保险/非金/信贷/财富/银行","topic_type":"主题类型","confidence":0.75,"relevance_reason":"30字内，说明归类依据","sector_angle":"该赛道可关注的角度","customer_segments":["客群1","客群2"],"actions":["动作1","动作2"]}]}',
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
        try:
            confidence = float(llm_row.get("confidence", fallback["confidence"]))
        except Exception:
            confidence = fallback["confidence"]
        confidence = round(max(0, min(1, confidence)), 2)
        merged = dict(item)
        merged.update({
            "sector": sector,
            "topic_type": llm_row.get("topic_type") or fallback["topic_type"],
            "confidence": confidence,
            "matched_rules": fallback.get("matched_rules", []),
            "relevance_reason": llm_row.get("relevance_reason") or fallback["relevance_reason"],
            "reason": llm_row.get("relevance_reason") or llm_row.get("reason") or fallback["reason"],
            "sector_angle": llm_row.get("sector_angle") or fallback["sector_angle"],
            "marketing_angle": llm_row.get("sector_angle") or fallback["marketing_angle"],
            "customer_segments": llm_row.get("customer_segments") or fallback["customer_segments"],
            "actions": llm_row.get("actions") or fallback["actions"],
            "classified_by": "llm" if item["id"] in llm_map else "keyword",
        })
        enriched.append(merged)
    return enriched


def improve_sector_coverage(items: list, min_per_sector: int = 8, min_non_jin: int = 4) -> list:
    if not items:
        return items

    macro_sources = ["央行", "财政部", "国家金融监督管理总局", "外汇局", "东方财富-财经", "新浪财经"]
    counts = {sector: 0 for sector in SECTORS}
    for item in items:
        counts[item["sector"]] = counts.get(item["sector"], 0) + 1

    # 四个主赛道补到 min_per_sector；非金只补到 min_non_jin（非金是兜底，不强行塞）
    sector_target = {}
    for s in SECTORS:
        sector_target[s] = min_non_jin if s == "非金" else min_per_sector

    for target_sector in SECTORS:
        target = sector_target.get(target_sector, min_per_sector)
        while counts.get(target_sector, 0) < target:
            candidate = None
            candidate_score = 0
            for item in items:
                current_sector = item.get("sector")
                if current_sector == target_sector:
                    continue
                # 不把当前赛道低于目标值的新闻抢走
                if counts.get(current_sector, 0) <= sector_target.get(current_sector, min_per_sector):
                    # 但是允许从非金里抢，因为非金是兜底桶
                    if current_sector != "非金":
                        continue
                text = "%s %s" % (item.get("title", ""), item.get("source", ""))
                score = sum(1 for kw in SECTOR_KEYWORDS[target_sector] if kw in text)
                if target_sector == guess_sector_from_source(item.get("source", "")):
                    score += 2
                if any(name in item.get("source", "") for name in macro_sources):
                    score += 1
                if score > candidate_score:
                    candidate = item
                    candidate_score = score
            if not candidate:
                break
            old_sector = candidate["sector"]
            candidate["sector"] = target_sector
            candidate["classified_by"] = candidate.get("classified_by", "keyword") + "_coverage_adjusted"
            candidate["relevance_reason"] = "为增强%s赛道新闻覆盖，按来源或关键词相关性归入该赛道。" % target_sector
            candidate["reason"] = candidate["relevance_reason"]
            counts[old_sector] -= 1
            counts[target_sector] += 1

    return items


def summarize_by_sector(items: list) -> tuple:
    by_sector = {sector: [] for sector in SECTORS}
    for item in items:
        by_sector.setdefault(item["sector"], []).append(item)

    summary = {}
    for sector in SECTORS:
        rows = by_sector.get(sector, [])
        topic_counts = {}
        for row in rows:
            topic = row.get("topic_type", "一般资讯")
            topic_counts[topic] = topic_counts.get(topic, 0) + 1
        summary[sector] = {
            "count": len(rows),
            "topic_counts": topic_counts,
            "coverage": "充足" if len(rows) >= 8 else "偏少",
            "top_titles": [x["title"] for x in rows[:5]],
        }
        by_sector[sector] = sorted(rows, key=lambda x: (x.get("confidence", 0), -x.get("rank", 999)), reverse=True)
    return by_sector, summary


def build_insight_prompt(sector_summary: dict, by_sector: dict) -> str:
    compact = {}
    for sector in SECTORS:
        compact[sector] = {
            "summary": sector_summary.get(sector, {}),
            "news": [
                {
                    "title": item["title"],
                    "topic_type": item.get("topic_type", "一般资讯"),
                    "reason": item["reason"],
                }
                for item in by_sector.get(sector, [])[:8]
            ],
        }
    return "\n".join([
        "你是金融机构行业研究和营销策略负责人，请基于五大赛道新闻生成整体局势分析。",
        "不要做利好/利空判断，不要给投资建议。重点分析当前局面、主要议题、客户沟通方向和后续观察点。",
        "只输出JSON，不要Markdown。格式：",
        '{"overall_situation":"整体局面150字内","key_themes":["主题1","主题2"],"sectors":{"保险":{"situation":"","client_focus":"","content_angle":"","watch_points":[""]},"非金":{},"信贷":{},"财富":{},"银行":{}},"today_actions":["动作1","动作2"],"generated_title":"一句话标题"}',
        "输入数据：",
        json.dumps(compact, ensure_ascii=False),
    ])


def fallback_marketing_insights(sector_summary: dict, by_sector: dict) -> dict:
    sectors = {}
    for sector in SECTORS:
        summary = sector_summary.get(sector, {})
        count = summary.get("count", 0)
        top_topics = sorted(summary.get("topic_counts", {}).items(), key=lambda x: x[1], reverse=True)
        topic_text = "、".join([topic for topic, _ in top_topics[:3]]) or "一般资讯"
        sectors[sector] = {
            "situation": "%s今日共捕捉%s条相关新闻，主要主题为%s。" % (sector, count, topic_text),
            "client_focus": "围绕%s赛道客户的资讯理解、配置需求和风险认知做沟通。" % sector,
            "content_angle": build_fallback_angle(sector),
            "watch_points": ["政策变化", "市场情绪", "机构经营动态"],
        }
    return {
        "overall_situation": "已基于多源财经新闻完成五大赛道归类，可用于每日晨会、客户触达和营销选题。当前更适合做局势观察和客户教育，不做利好利空判断。",
        "key_themes": ["政策监管", "市场环境", "机构经营", "客户需求"],
        "sectors": sectors,
        "today_actions": ["按赛道制作资讯摘要", "选择客户关心的主题做解读", "持续跟踪监管和市场变化"],
        "generated_title": "五大金融赛道每日局势观察",
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
    classified_items = improve_sector_coverage(classified_items)
    by_sector, sector_summary = summarize_by_sector(classified_items)
    situation_analysis = call_llm_insights(sector_summary, by_sector)

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
        "situation_analysis": situation_analysis,
        "marketing_insights": situation_analysis,
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
        "situation_analysis": situation_analysis,
        "marketing_insights": situation_analysis,
        "by_sector": by_sector,
        "items": classified_items,
    }
    save_json(classified_data, DATA_DIR / (meta["date"] + "_classification.json"))

    logger.info("=== 完成：%s 条新闻，LLM覆盖 %s 条 ===", len(classified_items), len(llm_map))
    return 0


if __name__ == "__main__":
    sys.exit(main())
