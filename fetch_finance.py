"""
财经热榜抓取 + 五大赛道（保险/银行/非银/信贷/财富管理/其他热门）影响评估
数据来源: tophub.today（首页全部分类 + finance 分类页）
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

HOME_URL = "https://tophub.today/"
FINANCE_URL = "https://tophub.today/c/finance"
DATA_DIR = Path(__file__).parent / "data"
CUSTOM_TZ = timezone(timedelta(hours=8))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
MAX_ITEMS_FOR_LLM = 40   # 扩大到 40 条送进模型
LLM_RETRY_TIMES = 2
TOP_PER_BOARD = 10        # 每个榜单取前 10 条（原来是 5）

LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
LLM_MODEL = os.environ.get("LLM_MODEL", "")

# 6 个赛道（新增"其他热门"作为兜底分类）
SECTORS = ["保险", "银行", "非银金融", "信贷", "财富管理", "其他热门"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("finance_fetcher")


# ============================================================
# 1. 抓取 HTML（多页支持：首页 + finance 分类页）
# ============================================================
def fetch_html(url: str) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(f"抓取中 (第 {attempt}/{MAX_RETRIES} 次): {url}")
            response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            response.encoding = response.apparent_encoding or "utf-8"
            logger.info(f"抓取成功，长度: {len(response.text)} 字符")
            return response.text
        except Exception as e:
            wait_time = attempt * 3
            logger.warning(f"抓取失败: {e}，等待 {wait_time} 秒后重试...")
            time.sleep(wait_time)
    raise RuntimeError(f"重试 {MAX_RETRIES} 次后仍无法抓取")


# ============================================================
# 2. 解析榜单卡片
# ============================================================
def parse_heat_value(extra_text: str) -> dict:
    result = {"value": 0, "raw": extra_text}
    if not extra_text:
        return result
    text = extra_text.strip()
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


def parse_board_card(card_html, board_hint: str = "") -> dict:
    title_elem = card_html.find(class_="cc-cd-lb")
    title = title_elem.get_text(strip=True) if title_elem else (board_hint or "未知榜单")
    sub_elem = card_html.find(class_="cc-cd-sb-st")
    subtitle = sub_elem.get_text(strip=True) if sub_elem else ""

    items = []
    item_container = card_html.find(class_="cc-cd-cb")
    if item_container:
        for idx, a_tag in enumerate(item_container.find_all("a")[:TOP_PER_BOARD], 1):
            rank_elem = a_tag.find(class_="s")
            title_elem_item = a_tag.find(class_="t")
            extra_elem = a_tag.find(class_="e")
            rank_text = rank_elem.get_text(strip=True) if rank_elem else str(idx)
            item_title = title_elem_item.get_text(strip=True) if title_elem_item else ""
            extra_text = extra_elem.get_text(strip=True) if extra_elem else ""
            hv = parse_heat_value(extra_text)
            items.append({
                "rank": int(rank_text) if rank_text.isdigit() else idx,
                "title": item_title,
                "heat_raw": extra_text,
                "heat_value": hv["value"],
                "url": a_tag.get("href", ""),
            })

    full_text = card_html.get_text(" ", strip=True)
    time_match = re.search(
        r"(\d+\s*(?:分钟前|小时前|天前|刚刚)|\d{1,2}:\d{2})", full_text
    )
    update_time = time_match.group(1) if time_match else ""

    return {
        "board_name": title,
        "board_subtitle": subtitle,
        "update_time": update_time,
        "items_count": len(items),
        "items": items,
    }


def parse_page_boards(html_text: str, source_label: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    cards = soup.find_all(class_="cc-cd")
    logger.info(f"[{source_label}] 发现 {len(cards)} 个榜单卡片")
    boards = []
    for idx, card in enumerate(cards, 1):
        try:
            board = parse_board_card(card)
            boards.append(board)
            logger.info(f"  [{idx}] {board['board_name']} | {board['items_count']} 条")
        except Exception as e:
            logger.error(f"  [{idx}] 解析失败: {e}")
    return boards


# ============================================================
# 3. 汇总新闻（首页 + finance 页，去重）
# ============================================================
def collect_items_for_llm(all_boards: list[dict]) -> list[dict]:
    seen = set()
    merged = []
    for board in all_boards:
        for item in board["items"]:
            key = item["title"][:30]
            if key in seen or not item["title"]:
                continue
            seen.add(key)
            merged.append({
                "id": len(merged) + 1,
                "title": item["title"],
                "source_board": board["board_name"],
            })
            if len(merged) >= MAX_ITEMS_FOR_LLM:
                return merged
    return merged


# ============================================================
# 4. Prompt（这次特别强调：每条都要归类）
# ============================================================
def build_classification_prompt(items: list[dict]) -> str:
    bullet_list = "\n".join(
        f"  [{i['id']}] ({i['source_board']}) {i['title']}"
        for i in items
    )
    return f"""请对下面每条财经相关新闻标题，从六个赛道（保险 / 银行 / 非银金融 / 信贷 / 财富管理 / 其他热门）判断影响。

【赛道说明】
- 保险：涉及保险公司、保费、保险产品、监管新规、理赔、健康险/寿险/财险
- 银行：涉及银行、存贷、利率、存款、信用卡、理财代销、LPR、银行财报
- 非银金融：涉及券商/基金/信托/期货/AMC、A股券商板块、资管、公募/私募基金发行
- 信贷：涉及贷款利率、个人消费贷、经营贷、房贷、LPR、普惠金融、不良率
- 财富管理：涉及理财、基金、信托、财富管理业务、私人银行、净值化转型
- 其他热门：新闻明确是财经/金融话题，但不属于以上五个（如宏观经济、汇率、贸易、行业监管）；纯娱乐/体育/纯科技产品请也归此类，若完全不相关则 primary_sector 留空字符串

评分：-2 强利空 / -1 利空 / 0 中性 / +1 利好 / +2 强利好。对每条新闻，**至少选择一个非零分数的赛道**。

【严格输出要求】
1. 只输出一个合法 JSON 对象，不要任何解释文字、开场白、代码块标记。
2. 所有字符串内容用英文双引号，字符串中的英文双引号必须转义为 \\"。
3. 每个 score 字段必须是整数：-2 / -1 / 0 / 1 / 2。
4. 输出必须以左花括号 {{ 开头，以右花括号 }} 结尾。

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
        "其他热门": {{"score": 数字, "level": "...", "reason": "..."}}
      }},
      "primary_sector": "从上面六个中选一个最相关的，不可空",
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
# 5. JSON 容错解析
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
        logger.warning(f"保存模型原始输出失败（不影响主流程）: {e}")


# ============================================================
# 6. 调用火山引擎 Ark（多次重试）
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
            logger.info(f"[第 {attempt}/{LLM_RETRY_TIMES} 次] 调用火山引擎 Ark: model={LLM_MODEL}")
            response = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {"role": "system",
                     "content": "你是一个只输出 JSON 的程序。任何情况下都只输出一个合法的 JSON 对象，完全不要输出 JSON 以外的文字，不要写解释，不要加 markdown。字符串中的中文引号必须转义。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.15,
                max_tokens=4000,
            )
            content = response.choices[0].message.content.strip()
            logger.info(f"模型原始输出长度: {len(content)} 字符")
            if attempt == 1:
                save_raw_llm_output(content)
            parsed = parse_llm_json(content)
            if isinstance(parsed, dict) and "market_overview" in parsed and "items" in parsed and len(parsed["items"]) >= len(items) * 0.6:
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
# 7. 兜底归类：若 primary_sector 为空，则根据 sector_impact 分数自动选一个
# ============================================================
def fallback_assign_sector(item: dict) -> dict:
    primary = item.get("primary_sector", "")
    if primary and primary in SECTORS:
        return item
    # primary 为空或无效 → 从 sector_impact 找分数最高的
    imp = item.get("sector_impact", {}) or {}
    best_sector = "其他热门"
    best_score = -999
    for sector in ["保险", "银行", "非银金融", "信贷", "财富管理", "其他热门"]:
        sc = imp.get(sector, {}).get("score", 0)
        if isinstance(sc, int) and sc > best_score:
            best_score = sc
            best_sector = sector
    # 如果全是 0 分，也归到"其他热门"，不让它丢失
    item["primary_sector"] = best_sector
    return item


# ============================================================
# 8. 保存 JSON
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
# 9. 主流程（首页 + finance 页同时抓）
# ============================================================
def main() -> int:
    logger.info("=== 财经热榜数据抓取开始 ===")
    try:
        # 1) 抓两页：首页 + finance 页，合并去重
        all_boards = []
        try:
            home_html = fetch_html(HOME_URL)
            all_boards.extend(parse_page_boards(home_html, "首页"))
        except Exception as e:
            logger.warning(f"首页抓取失败（不致命，继续抓 finance 页）: {e}")

        try:
            fin_html = fetch_html(FINANCE_URL)
            all_boards.extend(parse_page_boards(fin_html, "财经分类"))
        except Exception as e:
            logger.error(f"finance 页抓取失败: {e}")

        if not all_boards:
            raise RuntimeError("两个页面都没抓到数据")

        total_items = sum(b["items_count"] for b in all_boards)
        raw_data = {
            "fetched_at": datetime.now(CUSTOM_TZ).isoformat(),
            "date": datetime.now(CUSTOM_TZ).strftime("%Y-%m-%d"),
            "time": datetime.now(CUSTOM_TZ).strftime("%H:%M:%S"),
            "boards_count": len(all_boards),
            "total_items": total_items,
            "boards": all_boards,
        }

        # 2) 汇总 → 调用火山引擎 Ark
        items_for_llm = collect_items_for_llm(all_boards)
        logger.info(f"送入大模型分类: {len(items_for_llm)} 条新闻")
        classification = call_llm_for_classification(items_for_llm)

        # 3) 兜底归类：确保每条新闻都落到某个赛道
        valid_items = []
        for it in classification.get("items", []):
            if "primary_sector" in it or "sector_impact" in it:
                valid_items.append(fallback_assign_sector(it))
        classification["items"] = valid_items
        raw_data["classification"] = classification

        # 4) 按赛道聚合
        by_sector = {s: [] for s in SECTORS}
        for item in valid_items:
            primary = item.get("primary_sector", "其他热门")
            if primary not in by_sector:
                primary = "其他热门"
            imp = item.get("sector_impact", {}).get(primary, {}) or {}
            by_sector[primary].append({
                **item,
                "this_sector_score": imp.get("score", 0) if isinstance(imp.get("score"), int) else 0,
                "this_sector_level": imp.get("level", "中性"),
                "this_sector_reason": imp.get("reason", ""),
            })
        # 每个赛道按分数从高到低排序（利好优先展示）
        for s in SECTORS:
            by_sector[s].sort(key=lambda x: x.get("this_sector_score", 0), reverse=True)
        raw_data["by_sector"] = by_sector

        # 5) 按赛道统计摘要
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
        logger.info(f"赛道分配：{[(s['sector'], s['total']) for s in sector_summary]}")

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
