"""
财经新闻抓取 + 五大赛道（保险/银行/非银/信贷/财富管理/其他热门/监管动态）
数据源:
  1. tophub.today （首页 + 财经分类页，综合榜单）
  2. 东方财富 （快讯 / 要闻）
  3. 财联社 （电报，带深度标签）
  4. 国家金融监督管理总局 （原银保监会，政策/发文）
  5. 中国证监会 （要闻/监管公告）
  6. 中国人民银行 （货币政策/公告）
大模型: 火山引擎 Ark（兼容 OpenAI API，带 JSON 容错解析）
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
MAX_ITEMS_FOR_LLM = 60      # 现在数据源多，一次送 60 条进模型
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
# 工具：通用 HTTP 请求（自带重试 + 错误隔离）
# ============================================================
def fetch_html(url: str, referer: str = "") -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
    }
    if referer:
        headers["Referer"] = referer
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(f"抓取中 (第{attempt}/{MAX_RETRIES}次): {url}")
            response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            # 部分中文站点编码需要显式指定
            if not response.encoding or response.encoding.lower() == "iso-8859-1":
                response.encoding = response.apparent_encoding or "utf-8"
            logger.info(f"抓取成功: {len(response.text)} 字符")
            return response.text
        except Exception as e:
            wait_time = attempt * 3
            logger.warning(f"失败: {e}，{wait_time}秒后重试...")
            time.sleep(wait_time)
    raise RuntimeError(f"重试 {MAX_RETRIES} 次后仍无法抓取")


def fetch_json(url: str, referer: str = "") -> dict:
    """抓 JSON API 用。"""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(f"抓取JSON (第{attempt}/{MAX_RETRIES}次): {url}")
            response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            wait_time = attempt * 3
            logger.warning(f"失败: {e}，{wait_time}秒后重试...")
            time.sleep(wait_time)
    raise RuntimeError(f"重试 {MAX_RETRIES} 次后仍无法抓取 JSON")


# ============================================================
# 工具：解析热度字符串
# ============================================================
def parse_heat_value(extra_text: str) -> dict:
    result = {"value": 0, "raw": extra_text}
    if not extra_text:
        return result
    text = str(extra_text).strip()
    m = re.match(r"([\d,]+(?:\.\d+)?)\s*(万|w|W|亿|k|K|千)?", text)
    if m:
        num_str = m.group(1).replace(",", "")
        unit = m.group(2) or ""
        try:
            value = float(num_str)
            if unit == "万":
                value *= 10_000
            elif unit == "亿":
                value *= 100_000_000
            elif unit in ("k", "K", "千"):
                value *= 1_000
            result["value"] = int(value) if value == int(value) else value
        except ValueError:
            pass
    return result


# ============================================================
# 数据源 1：tophub.today（首页 + 财经分类页）
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
        "board_name": title,
        "board_subtitle": subtitle,
        "update_time": "",
        "items_count": len(items),
        "items": items,
    }


def fetch_tophub() -> list[dict]:
    boards = []
    for label, url in [("首页", "https://tophub.today/"), ("财经分类", "https://tophub.today/c/finance")]:
        try:
            html = fetch_html(url)
            soup = BeautifulSoup(html, "html.parser")
            cards = soup.find_all(class_="cc-cd")
            logger.info(f"[{label}] 抓到 {len(cards)} 个榜单")
            for i, c in enumerate(cards, 1):
                try:
                    b = parse_tophub_card(c)
                    if b["items_count"] > 0:
                        b["board_name"] = f"[TopHub·{label}] {b['board_name']}"
                        boards.append(b)
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"tophub {label} 抓取失败: {e}")
    return boards


# ============================================================
# 数据源 2：东方财富 （快讯 + 要闻列表）
# ============================================================
def fetch_eastmoney() -> list[dict]:
    items_all = []

    # 2a. 东方财富 快讯页面（最稳定）
    try:
        html = fetch_html("https://kuaixun.eastmoney.com/", referer="https://kuaixun.eastmoney.com/")
        soup = BeautifulSoup(html, "html.parser")
        # 快讯项一般是带时间的列表块
        cards = soup.select("ul.livenews-list li, .media-list li, .article-item, [class*='news'] [class*='item']")
        if not cards:
            cards = soup.find_all("li")
        for li in cards[:40]:
            a = li.find("a")
            title_text = ""
            url = ""
            if a:
                title_text = a.get_text(strip=True)
                url = a.get("href", "")
                if url.startswith("/"):
                    url = "https://kuaixun.eastmoney.com" + url
            else:
                title_text = li.get_text(strip=True)
            if title_text and len(title_text) > 8 and len(title_text) < 140:
                items_all.append({"title": title_text, "url": url, "heat_raw": ""})
        logger.info(f"东方财富快讯: {len(items_all)} 条")
    except Exception as e:
        logger.warning(f"东方财富快讯抓取失败: {e}")

    # 2b. 东方财富首页 要闻摘要 (作为补充)
    try:
        html = fetch_html("https://www.eastmoney.com/", referer="https://www.eastmoney.com/")
        soup = BeautifulSoup(html, "html.parser")
        # 抓首页所有链接标题，去重补入
        seen_now = {i["title"][:30] for i in items_all}
        for a in soup.find_all("a")[:200]:
            t = a.get_text(strip=True)
            if not t or len(t) < 8 or len(t) > 120:
                continue
            key = t[:30]
            if key in seen_now:
                continue
            # 只保留含有中文且看起来像新闻标题的
            if not re.search(r"[\u4e00-\u9fa5]", t):
                continue
            if re.search(r"^更多|^查看|^[A-Za-z0-9]+$|首页|资讯|财经|股票", t):
                continue
            url = a.get("href", "")
            if url.startswith("//"):
                url = "https:" + url
            elif url.startswith("/"):
                url = "https://www.eastmoney.com" + url
            items_all.append({"title": t, "url": url, "heat_raw": ""})
            seen_now.add(key)
            if len(items_all) >= 80:
                break
    except Exception as e:
        logger.warning(f"东方财富首页抓取失败: {e}")

    if not items_all:
        return []
    return [{
        "board_name": "[东方财富] 快讯/要闻",
        "board_subtitle": "东方财富网 - 财经快讯汇总",
        "update_time": "",
        "items_count": len(items_all),
        "items": [
            {
                "rank": idx + 1,
                "title": it["title"],
                "heat_raw": it["heat_raw"],
                "heat_value": 0,
                "url": it["url"],
            }
            for idx, it in enumerate(items_all)
        ],
    }]


# ============================================================
# 数据源 3：财联社 （电报页）
# ============================================================
def fetch_cailianpress() -> list[dict]:
    items_all = []
    try:
        # 财联社电报列表页
        html = fetch_html("https://www.cls.cn/telegraph", referer="https://www.cls.cn/")
        soup = BeautifulSoup(html, "html.parser")
        # 电报项是一个一个的卡片
        cards = soup.select("[class*='telegraph'][class*='list'] [class*='item'], [class*='telegram'] [class*='item'], .cl-article-list-item")
        if not cards:
            # fallback: 抓整页所有链接文本
            for a in soup.find_all("a"):
                t = a.get_text(strip=True)
                if 10 <= len(t) <= 140 and re.search(r"[\u4e00-\u9fa5]", t):
                    url = a.get("href", "")
                    if url.startswith("/"):
                        url = "https://www.cls.cn" + url
                    items_all.append({"title": t, "url": url})
        else:
            for card in cards[:40]:
                title = card.get_text(" ", strip=True)
                if title and 10 <= len(title) <= 200:
                    link = card.find("a")
                    url = link.get("href", "") if link else ""
                    if url.startswith("/"):
                        url = "https://www.cls.cn" + url
                    items_all.append({"title": title[:140], "url": url})
        logger.info(f"财联社电报: {len(items_all)} 条")
    except Exception as e:
        logger.warning(f"财联社抓取失败: {e}")

    if not items_all:
        return []
    return [{
        "board_name": "[财联社] 电报",
        "board_subtitle": "财联社 - 财经快讯与深度",
        "update_time": "",
        "items_count": len(items_all),
        "items": [
            {
                "rank": idx + 1,
                "title": it["title"],
                "heat_raw": "",
                "heat_value": 0,
                "url": it.get("url", ""),
            }
            for idx, it in enumerate(items_all)
        ],
    }]


# ============================================================
# 数据源 4：国家金融监督管理总局（原银保监会）- 政策与公告
# ============================================================
def fetch_regulator() -> list[dict]:
    all_titles = []

    # 4a. 金融监管总局 - 新闻列表
    try:
        html = fetch_html(
            "http://www.cbirc.gov.cn/cn/view/pages/ItemList.html?itemPId=922",
            referer="http://www.cbirc.gov.cn/",
        )
        soup = BeautifulSoup(html, "html.parser")
        # 抓取页面内所有 href 指向政策文章的链接
        for a in soup.find_all("a")[:150]:
            t = a.get_text(strip=True)
            href = a.get("href", "")
            if not t or len(t) < 10 or len(t) > 120:
                continue
            if not re.search(r"[\u4e00-\u9fa5]", t):
                continue
            # 只要政策/公告/通知/新闻类链接
            if not re.search(r"(notice|news|gonggao|ItemDetail|article|view/pages/Item)", href, re.IGNORECASE):
                continue
            if href.startswith("/"):
                href = "http://www.cbirc.gov.cn" + href
            all_titles.append({"title": t, "url": href, "source": "国家金融监管总局"})
        logger.info(f"金融监管总局: {len(all_titles)} 条")
    except Exception as e:
        logger.warning(f"金融监管总局抓取失败: {e}")

    # 4b. 证监会 - 要闻
    try:
        html = fetch_html("http://www.csrc.gov.cn/csrc/c100028/zfxxgk.shtml", referer="http://www.csrc.gov.cn/")
        soup = BeautifulSoup(html, "html.parser")
        before = len(all_titles)
        for a in soup.find_all("a")[:150]:
            t = a.get_text(strip=True)
            href = a.get("href", "")
            if not t or len(t) < 10 or len(t) > 120:
                continue
            if not re.search(r"[\u4e00-\u9fa5]", t):
                continue
            # 证监会链接通常是 .htm/html
            if not re.search(r"\.(htm|html)", href, re.IGNORECASE):
                continue
            if href.startswith("/"):
                href = "http://www.csrc.gov.cn" + href
            elif href.startswith("./"):
                href = "http://www.csrc.gov.cn/csrc/c100028/" + href[2:]
            all_titles.append({"title": t, "url": href, "source": "证监会"})
        logger.info(f"证监会: {len(all_titles) - before} 条")
    except Exception as e:
        logger.warning(f"证监会抓取失败: {e}")

    # 4c. 央行 - 货币政策司新闻
    try:
        html = fetch_html(
            "http://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html",
            referer="http://www.pbc.gov.cn/",
        )
        soup = BeautifulSoup(html, "html.parser")
        before = len(all_titles)
        for a in soup.find_all("a")[:150]:
            t = a.get_text(strip=True)
            href = a.get("href", "")
            if not t or len(t) < 10 or len(t) > 140:
                continue
            if not re.search(r"[\u4e00-\u9fa5]", t):
                continue
            if not re.search(r"\.(htm|html)", href, re.IGNORECASE):
                continue
            if href.startswith("/"):
                href = "http://www.pbc.gov.cn" + href
            all_titles.append({"title": t, "url": href, "source": "央行"})
        logger.info(f"央行: {len(all_titles) - before} 条")
    except Exception as e:
        logger.warning(f"央行抓取失败: {e}")

    if not all_titles:
        return []
    return [{
        "board_name": "[监管动态] 三部门发文",
        "board_subtitle": "金融监管总局 / 证监会 / 央行 官方公告",
        "update_time": "",
        "items_count": len(all_titles),
        "items": [
            {
                "rank": idx + 1,
                "title": it["title"],
                "heat_raw": "",
                "heat_value": 0,
                "url": it["url"],
            }
            for idx, it in enumerate(all_titles)
        ],
    }]


# ============================================================
# 汇总：所有数据源合并 + 去重
# ============================================================
def fetch_all_sources() -> list[dict]:
    boards = []
    # 顺序执行，每个源独立失败不影响
    try:
        boards += fetch_tophub()
    except Exception as e:
        logger.warning(f"tophub 整体失败: {e}")
    try:
        boards += fetch_eastmoney()
    except Exception as e:
        logger.warning(f"eastmoney 整体失败: {e}")
    try:
        boards += fetch_cailianpress()
    except Exception as e:
        logger.warning(f"cailianpress 整体失败: {e}")
    try:
        boards += fetch_regulator()
    except Exception as e:
        logger.warning(f"regulator 整体失败: {e}")

    # 总去重：所有 items 合并，按标题前 30 字去重
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
    # 移除空榜单
    boards = [b for b in boards if b.get("items_count", 0) > 0]
    logger.info(f"=== 合并后共 {len(boards)} 个榜单，{sum(b['items_count'] for b in boards)} 条新闻 ===")
    return boards


# ============================================================
# 为 LLM 准备输入列表（统一 id/title/source_board）
# ============================================================
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
            if len(merged) >= MAX_ITEMS_FOR_LLM:
                return merged
    return merged


# ============================================================
# 给大模型的 Prompt
# ============================================================
def build_classification_prompt(items: list[dict]) -> str:
    bullet_list = "\n".join(
        f"  [{i['id']}] ({i['source_board']}) {i['title']}"
        for i in items
    )
    return f"""请对下面每条财经相关新闻标题，从七个赛道判断影响。

【赛道说明】
- 保险：保险公司、保费、保险产品、监管新规、健康险/寿险/财险、理赔
- 银行：商业银行、存贷、利率、存款、LPR、银行财报、信用卡、金融市场业务
- 非银金融：券商、基金、信托、期货、资管、公募/私募基金发行、A股券商板块
- 信贷：个人消费贷、经营贷、房贷、普惠金融、不良率、信贷政策
- 财富管理：理财、基金代销、信托、私人银行、财富管理业务、净值化转型
- 监管动态：金融监管总局/证监会/央行/外管局 发文、监管政策、处罚公告、规章修订
- 其他热门：其他财经话题（宏观经济、汇率、贸易、商品等）；非财经新闻 primary_sector 留空

评分：-2 强利空 / -1 利空 / 0 中性 / +1 利好 / +2 强利好。

【输出要求】
1. 只输出一个合法 JSON 对象。不要解释文字，不要代码块。
2. 字符串用英文双引号，字符串中的英文双引号必须转义为 \\"。
3. 每个 score 字段是整数：-2 / -1 / 0 / 1 / 2。
4. 输出以 {{ 开头，以 }} 结尾。

JSON 结构：
{{
  "items": [
    {{
      "id": 数字id,
      "title": "原样复制新闻标题",
      "sector_impact": {{
        "保险": {{"score": 数字, "level": "强利空/利空/中性/利好/强利好", "reason": "不超过20字"}},
        "银行": {{"score": 数字, "level": "...", "reason": "..."}},
        "非银金融": {{"score": 数字, "level": "...", "reason": "..."}},
        "信贷": {{"score": 数字, "level": "...", "reason": "..."}},
        "财富管理": {{"score": 数字, "level": "...", "reason": "..."}},
        "监管动态": {{"score": 数字, "level": "...", "reason": "..."}},
        "其他热门": {{"score": 数字, "level": "...", "reason": "..."}}
      }},
      "primary_sector": "从上面七个中选一个最相关的，不可空",
      "summary": "一句话概述，不超过30字"
    }}
  ],
  "market_overview": {{
    "overall_sentiment": "乐观/中性/谨慎",
    "hot_sector": "当日最热门赛道",
    "cold_sector": "当日最承压赛道",
    "brief": "2-3句话总结今日整体盘面"
  }}
}}

新闻列表：
{bullet_list}
"""


# ============================================================
# JSON 容错解析
# ============================================================
def find_balanced_json(text: str) -> str:
    best = ""
    best_len = 0
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        depth = 0
        in_str = False
        escape = False
        quote_char = ""
        for j in range(i, len(text)):
            c = text[j]
            if escape:
                escape = False
                continue
            if in_str:
                if c == "\\":
                    escape = True
                elif c == quote_char:
                    in_str = False
            else:
                if c == '"' or c == "'":
                    in_str = True
                    quote_char = c
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        block = text[i:j + 1]
                        if len(block) > best_len:
                            best = block
                            best_len = len(block)
                        break
    return best


def extract_json(text: str) -> str:
    if not text:
        return ""
    s = text.strip()
    m = re.search(r"```(?:json)?\s*(\{[\s\S]+?\})\s*```", s)
    if m:
        s = m.group(1).strip()
    else:
        m = re.search(r"```\s*(\{[\s\S]+?\})\s*```", s)
        if m:
            s = m.group(1).strip()
    balanced = find_balanced_json(s)
    if balanced:
        return balanced
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        return s[start:end + 1]
    return s


def repair_json(raw: str) -> str:
    fixed = raw
    fixed = fixed.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    fixed = fixed.replace("，", ",")
    fixed = fixed.replace("：", ":")
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
    fixed = re.sub(r"```", "", fixed)
    start_brace = fixed.find("{")
    if start_brace > 0:
        fixed = fixed[start_brace:]
    fixed = re.sub(r"//[^\n]*", "", fixed)
    return fixed


def parse_llm_json(text: str) -> dict:
    cleaned = extract_json(text)
    if not cleaned:
        raise ValueError("模型输出为空或不含 JSON")
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e1:
        logger.warning(f"方案1失败: {e1}")
    repaired = repair_json(cleaned)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError as e2:
        logger.warning(f"方案2失败: {e2}")
    candidate = find_balanced_json(text)
    if candidate and candidate != cleaned:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as e3:
            logger.warning(f"方案3失败: {e3}")
            repaired2 = repair_json(candidate)
            try:
                return json.loads(repaired2)
            except json.JSONDecodeError as e4:
                logger.warning(f"方案4失败: {e4}")
    sentiment = re.search(r'"overall_sentiment"\s*:\s*"([^"]{0,10})"', repaired)
    brief = re.search(r'"brief"\s*:\s*"([^"]{0,200})"', repaired)
    return {
        "error": "模型输出 JSON 不合法，已降级显示",
        "items": [],
        "market_overview": {
            "overall_sentiment": sentiment.group(1) if sentiment else "—",
            "hot_sector": "—",
            "cold_sector": "—",
            "brief": (brief.group(1) if brief else "模型返回格式异常，已跳过 AI 分类。"),
        },
    }


def save_raw_llm_output(text: str):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        out_file = DATA_DIR / "latest_llm_raw.txt"
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(text)
        logger.info(f"已保存模型原始输出: {out_file} ({len(text)} 字符)")
        date_file = DATA_DIR / f"{datetime.now(CUSTOM_TZ).strftime('%Y-%m-%d')}_llm_raw.txt"
        with open(date_file, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:
        logger.warning(f"保存原始输出失败: {e}")


# ============================================================
# 调用火山引擎 Ark
# ============================================================
def call_llm_for_classification(items: list[dict]) -> dict:
    if not LLM_API_KEY or not LLM_MODEL:
        logger.warning("未配置 LLM_API_KEY 或 LLM_MODEL，跳过大模型分类")
        return {
            "error": "LLM not configured (missing API Key or Model Endpoint)",
            "items": [],
            "market_overview": {
                "overall_sentiment": "—",
                "hot_sector": "—",
                "cold_sector": "—",
                "brief": "未配置大模型 Key，跳过 AI 分类。",
            },
        }

    prompt = build_classification_prompt(items)
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)

    last_error = None
    for attempt in range(1, LLM_RETRY_TIMES + 1):
        try:
            logger.info(f"[第{attempt}/{LLM_RETRY_TIMES}次] 调用火山引擎 Ark: model={LLM_MODEL}")
            response = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system",
                     "content": "你是一个只输出 JSON 的程序。任何情况下都只输出一个合法的 JSON 对象，完全不要输出 JSON 以外的文字，不要写解释，不要加 markdown。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.15,
                max_tokens=5000,
            )
            content = response.choices[0].message.content.strip()
            logger.info(f"模型原始输出长度: {len(content)} 字符")
            if attempt == 1:
                save_raw_llm_output(content)
            parsed = parse_llm_json(content)
            if (isinstance(parsed, dict)
                    and "market_overview" in parsed
                    and "items" in parsed
                    and len(parsed["items"]) >= len(items) * 0.5):
                logger.info(f"大模型分类完成：共 {len(parsed['items'])} 条")
                return parsed
            last_error = f"第{attempt}次解析成功但结构不完整，items={len(parsed.get('items', []))}"
            logger.warning(last_error)
        except Exception as e:
            last_error = str(e)
            logger.error(f"[第{attempt}次] 调用或解析失败: {e}")
        time.sleep(3)

    return {
        "error": f"LLM failed after {LLM_RETRY_TIMES} retries: {last_error}",
        "items": [],
        "market_overview": {
            "overall_sentiment": "—",
            "hot_sector": "—",
            "cold_sector": "—",
            "brief": f"大模型多次调用/解析失败: {last_error}",
        },
    }


# ============================================================
# 兜底归类（primary_sector 为空时自动补一个）
# ============================================================
def fallback_assign_sector(item: dict) -> dict:
    primary = item.get("primary_sector", "")
    if primary and primary in SECTORS:
        return item
    imp = item.get("sector_impact", {}) or {}
    best_sector = "其他热门"
    best_score = -999
    for sector in SECTORS:
        sc = imp.get(sector, {}).get("score", 0)
        if isinstance(sc, int) and sc > best_score:
            best_score = sc
            best_sector = sector
    item["primary_sector"] = best_sector
    return item


# ============================================================
# 保存
# ============================================================
def save_data(data: dict, filename: str) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if "latest" in filename:
        out_file = DATA_DIR / f"{filename}.json"
    else:
        date_str = data.get("date", datetime.now(CUSTOM_TZ).strftime("%Y-%m-%d"))
        out_file = DATA_DIR / f"{date_str}_{filename}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"已保存: {out_file}")
    return out_file


# ============================================================
# 主流程
# ============================================================
def main() -> int:
    logger.info("=== 财经新闻多源抓取开始 ===")
    try:
        # 1) 抓所有数据源 → 合并 → 去重
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

        # 2) 送进大模型做七赛道分类
        items_for_llm = collect_items_for_llm(all_boards)
        logger.info(f"送入大模型分类: {len(items_for_llm)} 条新闻")
        classification = call_llm_for_classification(items_for_llm)

        # 3) 兜底归类
        valid_items = []
        for it in classification.get("items", []):
            valid_items.append(fallback_assign_sector(it))
        classification["items"] = valid_items
        raw_data["classification"] = classification

        # 4) 按赛道聚合 + 按利好分数排序
        by_sector = {s: [] for s in SECTORS}
        for item in valid_items:
            primary = item.get("primary_sector", "其他热门")
            if primary not in by_sector:
                primary = "其他热门"
            imp = item.get("sector_impact", {}).get(primary, {}) or {}
            score = imp.get("score", 0)
            by_sector[primary].append({
                **item,
                "this_sector_score": score if isinstance(score, int) else 0,
                "this_sector_level": imp.get("level", "中性"),
                "this_sector_reason": imp.get("reason", ""),
            })
        for s in SECTORS:
            by_sector[s].sort(key=lambda x: x.get("this_sector_score", 0), reverse=True)
        raw_data["by_sector"] = by_sector

        # 5) 每赛道统计
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
        logger.info(f"赛道分配：{[(x['sector'], x['total']) for x in sector_summary]}")

        # 6) 落盘
        save_data(raw_data, "latest")
        save_data(raw_data, "raw")
        save_data(classification, "classification")

        logger.info("=== 全部完成 ===")
        return 0
    except Exception as e:
        logger.error(f"=== 失败: {e} ===")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
