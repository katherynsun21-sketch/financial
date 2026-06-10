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
BATCH_SIZE = 20
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


def fetch_html(url, referer=""):
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
            logger.info("抓取成功 %d 字: %s", len(response.text), url)
            return response.text
        except Exception as e:
            logger.warning("抓取失败: %s", e)
            time.sleep(attempt * 3)
    raise RuntimeError("重试 %d 次后仍失败" % MAX_RETRIES)


def parse_heat_value(extra_text):
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
                v *= 10000
            elif unit == "亿":
                v *= 100000000
            elif unit in ("k", "K", "千"):
                v *= 1000
            result["value"] = int(v) if v == int(v) else v
        except ValueError:
            pass
    return result


# 关键词兜底分类器（LLM 失败时保证页面不空）
SECTOR_KEYWORDS = {
    "保险": ["保险", "保险公司", "寿险", "健康险", "财险", "车险", "重疾",
             "保费", "理赔", "代理人", "银保", "保协", "年金险"],
    "银行": ["银行", "商业银行", "工行", "建行", "农行", "中行", "交行", "招行",
             "兴业", "浦发", "中信银行", "存款", "储蓄", "银行卡", "信用卡",
             "银行间", "LPR", "存款利率"],
    "非银金融": ["券商", "证券公司", "中信证券", "中金", "华泰", "国泰君安", "海通",
               "基金", "公募", "私募", "信托", "期货", "IPO", "上市", "证券",
               "A股", "沪指", "创业板", "科创板", "北交所", "资管"],
    "信贷": ["信贷", "贷款", "房贷", "按揭", "经营贷", "消费贷", "普惠", "不良率",
            "贷款利率", "首套", "二套", "首付", "MLF", "再贷款"],
    "财富管理": ["理财", "财富管理", "理财产品", "净值型", "私行", "私人银行", "理财子",
               "信托产品", "代销", "高净值", "家族信托"],
    "监管动态": ["监管总局", "金融监管总局", "银保监会", "证监会", "央行", "人民银行",
               "外管局", "外汇局", "国务院", "政策", "通知", "办法",
               "征求意见", "处罚", "罚款", "罚单", "立案", "调查", "修订",
               "发布公告", "答记者问", "国常会", "金融稳定"],
}

POSITIVE_KW = ["增长", "上涨", "利好", "突破", "超额", "创新高", "扩大", "收益",
               "盈利", "净利润", "回暖", "改善", "修复", "大增"]
NEGATIVE_KW = ["下跌", "亏损", "下降", "下滑", "利空", "风险", "违约", "暴雷",
               "逾期", "爆雷", "罚单", "罚款", "处罚", "调查", "警告",
               "破产", "承压", "收紧", "缩表", "加息", "放缓"]


def classify_by_keyword(title):
    t = title or ""
    hits = {}
    for sector, kws in SECTOR_KEYWORDS.items():
        count = sum(1 for k in kws if k in t)
        if count > 0:
            hits[sector] = count

    if hits:
        primary = max(hits.keys(), key=lambda s: (hits[s], -list(SECTOR_KEYWORDS).index(s)))
    else:
        primary = "其他热门"

    pos = sum(1 for k in POSITIVE_KW if k in t)
    neg = sum(1 for k in NEGATIVE_KW if k in t)
    if pos > neg and pos >= 1:
        score = 1
        level = "利好"
    elif neg > pos and neg >= 1:
        score = -1
        level = "利空"
    else:
        score = 0
        level = "中性"

    impact = {}
    for s in SECTORS:
        impact[s] = {"score": 0, "level": "中性", "reason": ""}
    for sector, kws in SECTOR_KEYWORDS.items():
        sub_hits = sum(1 for k in kws if k in t)
        if sector == primary:
            impact[sector] = {
                "score": score,
                "level": level,
                "reason": "关键词命中 %d 个，情绪%s" % (hits.get(sector, 0), level),
            }
        elif sub_hits > 0:
            impact[sector] = {
                "score": 0,
                "level": "中性",
                "reason": "间接提及(%d个词)" % sub_hits,
            }
    if primary == "其他热门":
        impact["其他热门"] = {"score": 0, "level": "中性", "reason": "未命中赛道关键词"}
    return {"primary_sector": primary, "sector_impact": impact, "summary": title[:30]}


# ---- 数据源 1: tophub.today ----
def parse_tophub_card(card_html):
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
        "board_name": "[TopHub] " + title,
        "board_subtitle": subtitle,
        "update_time": "",
        "items_count": len(items),
        "items": items,
    }


def fetch_tophub():
    boards = []
    for url in ["https://tophub.today/", "https://tophub.today/c/finance"]:
        try:
            html = fetch_html(url)
            soup = BeautifulSoup(html, "html.parser")
            cards = soup.find_all(class_="cc-cd")
            logger.info("tophub 抓到 %d 个榜单", len(cards))
            for c in cards:
                b = parse_tophub_card(c)
                if b["items_count"] > 0:
                    boards.append(b)
        except Exception as e:
            logger.warning("tophub 失败: %s", e)
    return boards


# ---- 数据源 2: 东方财富 ----
def fetch_eastmoney():
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
        logger.info("东方财富: %d 条", len(items_all))
    except Exception as e:
        logger.warning("东方财富失败: %s", e)

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


# ---- 数据源 3: 财联社 ----
def fetch_cailianpress():
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
        logger.info("财联社: %d 条", len(items_all))
    except Exception as e:
        logger.warning("财联社失败: %s", e)

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


# ---- 数据源 4: 三部门 ----
def fetch_regulator():
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
        logger.warning("监管总局失败: %s", e)
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
        logger.warning("证监会失败: %s", e)
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
        logger.warning("央行失败: %s", e)

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


# ---- 汇总 + 去重 ----
def fetch_all_sources():
    boards = []
    for fn in [fetch_tophub, fetch_eastmoney, fetch_cailianpress, fetch_regulator]:
        try:
            boards += fn()
        except Exception as e:
            logger.warning("%s 整体失败: %s", fn.__name__, e)

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
    logger.info("=== 合并后 %d 个榜单，%d 条新闻 ===",
                len(boards), sum(b["items_count"] for b in boards))
    return boards


def collect_items_for_llm(all_boards):
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


# ---- LLM 相关 ----
def build_short_prompt(items_batch):
    # 用单引号拼接主体，避免 body 中出现 """ 导致三引号误闭合
    head_lines = []
    head_lines.append("你是财经信息分类器。对下面每条新闻标题，输出 JSON。")
    head_lines.append("")
    head_lines.append("【赛道】保险 / 银行 / 非银金融 / 信贷 / 财富管理 / 监管动态 / 其他热门")
    head_lines.append("【评分】-2 强利空 / -1 利空 / 0 中性 / 1 利好 / 2 强利好")
    head_lines.append("")
    head_lines.append("输出格式（只输出 JSON，不要任何解释和代码块）：")
    head_lines.append('{"items":[{"id":数字,"primary":"赛道名","score":数字,"reason":"不超过15字"}]}')
    head_lines.append("")
    head_lines.append("新闻列表：")

    body_lines = []
    for i in items_batch:
        body_lines.append("[%d] %s" % (i["id"], i["title"]))

    return "\n".join(head_lines) + "\n" + "\n".join(body_lines)


def parse_llm_json_strict(text):
    if not text:
        return []
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return []
    core = text[start:end + 1]
    # 先尝试标准 JSON
    try:
        data = json.loads(core)
        items = data.get("items", [])
        if items:
            return items
    except json.JSONDecodeError:
        pass

    # 兜底：用正则硬抓 id / primary / score
    items = []
    for m in re.finditer(
        r'"id"\s*:\s*(\d+)\s*,\s*"primary"\s*:\s*"([^"]+)"\s*,\s*"score"\s*:\s*(-?\d+)',
        core,
    ):
        try:
            items.append({
                "id": int(m.group(1)),
                "primary": m.group(2),
                "score": int(m.group(3)),
                "reason": "",
            })
        except Exception:
            pass
    return items


def call_llm_batch(items_batch, batch_no):
    if not LLM_API_KEY or not LLM_MODEL:
        return []

    prompt = build_short_prompt(items_batch)
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    min_ok = max(1, int(len(items_batch) * 0.5))

    for attempt in range(1, LLM_RETRY_TIMES + 1):
        try:
            logger.info("[第%d批·第%d次] 调用 Ark，%d 条", batch_no, attempt, len(items_batch))
            response = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system", "content": "只输出 JSON，不要任何其他文字。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=3500,
            )
            content = response.choices[0].message.content.strip()
            parsed_items = parse_llm_json_strict(content)
            if len(parsed_items) >= min_ok:
                logger.info("[第%d批] LLM 成功，返回 %d 条", batch_no, len(parsed_items))
                return parsed_items
            logger.warning("[第%d批] items 不足 (%d < %d)，重试", batch_no, len(parsed_items), min_ok)
            time.sleep(2)
        except Exception as e:
            logger.warning("[第%d批·第%d次] 异常: %s", batch_no, attempt, e)
            time.sleep(2)
    return []


def merge_classification(items_for_llm, llm_results):
    llm_by_id = {}
    for r in llm_results:
        try:
            item_id = int(r.get("id", 0))
            if item_id > 0:
                llm_by_id[item_id] = r
        except Exception:
            pass

    final_items = []
    for src in items_for_llm:
        item_id = src.get("id", 0)
        title = src.get("title", "")
        llm_hit = llm_by_id.get(item_id)

        if llm_hit:
            primary = llm_hit.get("primary", "") or "其他热门"
            if primary not in SECTORS:
                primary = "其他热门"
            score = llm_hit.get("score", 0)
            if not isinstance(score, int):
                try:
                    score = int(score)
                except Exception:
                    score = 0
            score = max(-2, min(2, score))
            level = {2: "强利好", 1: "利好", -1: "利空", -2: "强利空"}.get(score, "中性")
            reason = llm_hit.get("reason", "")
            impact = {s: {"score": 0, "level": "中性", "reason": ""} for s in SECTORS}
            impact[primary] = {"score": score, "level": level, "reason": reason}
            final_items.append({
                "id": item_id,
                "title": title,
                "primary_sector": primary,
                "sector_impact": impact,
                "summary": title[:30],
            })
        else:
            kw = classify_by_keyword(title)
            final_items.append({
                "id": item_id,
                "title": title,
                "primary_sector": kw["primary_sector"],
                "sector_impact": kw["sector_impact"],
                "summary": kw["summary"],
                "_from_keyword": True,
            })
    return final_items


# ---- 保存 ----
def save_data(data, filename):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if "latest" in filename:
        out_file = DATA_DIR / ("%s.json" % filename)
    else:
        date_str = data.get("date", datetime.now(CUSTOM_TZ).strftime("%Y-%m-%d"))
        out_file = DATA_DIR / ("%s_%s.json" % (date_str, filename))
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("已保存: %s", out_file)
    return out_file


# ---- 主流程 ----
def main():
    logger.info("=== 财经新闻多源抓取开始 ===")
    try:
        all_boards = fetch_all_sources()
        if not all_boards:
            raise RuntimeError("所有数据源都没抓到数据")

        total_items = sum(b["items_count"] for b in all_boards)
        raw_data = {
            "fetched_at": datetime.now(CUSTOM_TZ).isoformat(),
            "date": datetime.now(CUSTOM_TZ).strftime("%Y-%m-%d"),
            "time": datetime.now(CUSTOM_TZ).strftime("%H:%M:%S"),
            "boards_count": len(all_boards),
            "total_items": total_items,
            "boards": all_boards,
        }

        items_for_llm = collect_items_for_llm(all_boards)
        logger.info("候选新闻 %d 条，分批送进 LLM", len(items_for_llm))

        llm_results_all = []
        batch_no = 1
        for i in range(0, len(items_for_llm), BATCH_SIZE):
            batch = items_for_llm[i:i + BATCH_SIZE]
            results = call_llm_batch(batch, batch_no)
            llm_results_all.extend(results)
            batch_no += 1
            time.sleep(1)

        logger.info("LLM 共覆盖 %d 条；其余走关键词兜底", len(llm_results_all))

        final_items = merge_classification(items_for_llm, llm_results_all)
        llm_cnt = sum(1 for x in final_items if not x.get("_from_keyword"))
        kw_cnt = sum(1 for x in final_items if x.get("_from_keyword"))

        hot_sector = max(SECTORS, key=lambda s: sum(
            1 for x in final_items if x.get("primary_sector") == s
        ))
        classification = {
            "items": final_items,
            "llm_count": llm_cnt,
            "keyword_count": kw_cnt,
            "market_overview": {
                "overall_sentiment": "中性",
                "hot_sector": hot_sector,
                "cold_sector": "其他热门",
                "brief": "当日共处理 %d 条财经新闻，其中 LLM 分类 %d 条，关键词兜底 %d 条。" % (
                    len(final_items), llm_cnt, kw_cnt),
            },
        }
        raw_data["classification"] = classification

        by_sector = {s: [] for s in SECTORS}
        for item in final_items:
            primary = item.get("primary_sector", "其他热门")
            if primary not in by_sector:
                primary = "其他热门"
            imp = item.get("sector_impact", {}).get(primary, {}) or {}
            score = imp.get("score", 0)
            by_sector[primary].append({
                "id": item.get("id"),
                "title": item.get("title", ""),
                "primary_sector": primary,
                "this_sector_score": score if isinstance(score, int) else 0,
                "this_sector_level": imp.get("level", "中性"),
                "this_sector_reason": imp.get("reason", ""),
                "url": "",
            })
        for s in SECTORS:
            by_sector[s].sort(key=lambda x: x.get("this_sector_score", 0), reverse=True)
        raw_data["by_sector"] = by_sector

        sector_summary = []
        for s in SECTORS:
            scores = [it.get("this_sector_score", 0) for it in by_sector[s]]
            positive = sum(1 for x in scores if x > 0)
            negative = sum(1 for x in scores if x < 0)
            sector_summary.append({
                "sector": s,
                "total": len(by_sector[s]),
                "positive": positive,
                "negative": negative,
                "neutral": len(scores) - positive - negative,
            })
        raw_data["sector_summary"] = sector_summary
        logger.info("赛道分配: %s", [(x["sector"], x["total"]) for x in sector_summary])

        save_data(raw_data, "latest")
        save_data(raw_data, "raw")
        save_data(classification, "classification")

        logger.info("=== 全部完成 ===")
        return 0
    except Exception as e:
        logger.error("=== 失败: %s ===", e)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
