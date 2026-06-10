"""
财经热榜抓取 + 五大赛道（保险/银行/非银/信贷/财富）影响评估
数据来源: tophub.today/c/finance
大模型: 火山引擎 Ark（兼容 OpenAI API）
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

TARGET_URL = "https://tophub.today/c/finance"
DATA_DIR = Path(__file__).parent / "data"
CUSTOM_TZ = timezone(timedelta(hours=8))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
MAX_ITEMS_FOR_LLM = 25

# ====== 火山引擎 Ark 配置（通过环境变量注入，不需要在这里写 Key）======
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
LLM_MODEL = os.environ.get("LLM_MODEL", "")

SECTORS = ["保险", "银行", "非银金融", "信贷", "财富管理"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("finance_fetcher")


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


def parse_board_card(card_html) -> dict:
    title_elem = card_html.find(class_="cc-cd-lb")
    title = title_elem.get_text(strip=True) if title_elem else "未知榜单"
    sub_elem = card_html.find(class_="cc-cd-sb-st")
    subtitle = sub_elem.get_text(strip=True) if sub_elem else ""

    items = []
    item_container = card_html.find(class_="cc-cd-cb")
    if item_container:
        for idx, a_tag in enumerate(item_container.find_all("a"), 1):
            rank_elem = a_tag.find(class_="s")
            title_elem_item = a_tag.find(class_="t")
            extra_elem = a_tag.find(class_="e")
            rank_text = rank_elem.get_text(strip=True) if rank_elem else str(idx)
            item_title = title_elem_item.get_text(strip=True) if title_elem_item else ""
            extra_text = extra_elem.get_text(strip=True) if extra_elem else ""
            items.append({
                "rank": int(rank_text) if rank_text.isdigit() else idx,
                "title": item_title,
                "heat_raw": extra_text,
                "heat_value": parse_heat_value(extra_text)["value"],
                "url": a_tag.get("href", ""),
            })

    full_text = card_html.get_text(" ", strip=True)
    time_match = re.search(r"(\d+\s*(?:分钟前|小时前|天前|刚刚)|\d{1,2}:\d{2})", full_text)
    update_time = time_match.group(1) if time_match else ""

    return {
        "board_name": title,
        "board_subtitle": subtitle,
        "update_time": update_time,
        "items_count": len(items),
        "items": items,
    }


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
            if unit in ("万",):
                value *= 10_000
            elif unit == "亿":
                value *= 100_000_000
            elif unit in ("k", "K", "千"):
                value *= 1_000
            result["value"] = int(value) if value == int(value) else value
        except ValueError:
            pass
    return result


def parse_all_boards(html_text: str) -> dict:
    soup = BeautifulSoup(html_text, "html.parser")
    cards = soup.find_all(class_="cc-cd")
    logger.info(f"发现 {len(cards)} 个榜单卡片")
    boards = []
    for idx, card in enumerate(cards, 1):
        try:
            board = parse_board_card(card)
            boards.append(board)
            logger.info(f"  [{idx}] {board['board_name']} | {board['items_count']} 条")
        except Exception as e:
            logger.error(f"  [{idx}] 解析失败: {e}")
    total_items = sum(b["items_count"] for b in boards)
    return {
        "fetched_at": datetime.now(CUSTOM_TZ).isoformat(),
        "date": datetime.now(CUSTOM_TZ).strftime("%Y-%m-%d"),
        "time": datetime.now(CUSTOM_TZ).strftime("%H:%M:%S"),
        "boards_count": len(boards),
        "total_items": total_items,
        "boards": boards,
    }


def collect_items_for_llm(raw_data: dict) -> list[dict]:
    seen = set()
    merged = []
    for board in raw_data["boards"]:
        for item in board["items"][:5]:
            key = item["title"][:30]
            if key in seen:
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


def build_classification_prompt(items: list[dict]) -> str:
    bullet_list = "\n".join(
        f"  [{i['id']}] ({i['source_board']}) {i['title']}"
        for i in items
    )
    return f"""你是一个金融行业研究员。请对下面每条财经新闻标题，从五大赛道（保险 / 银行 / 非银金融 / 信贷 / 财富管理）的角度判断影响。

评分规则：
- -2 = 强利空（行业整体明显承压）
- -1 = 利空（部分机构/业务受负面影响）
-  0 = 中性或影响不明确
- +1 = 利好（部分机构/业务受益）
- +2 = 强利好（行业整体明显受益）

输出严格 JSON，不要任何 markdown 标记：
{{
  "items": [
    {{
      "id": 新闻的数字 id,
      "title": "原样复制新闻标题",
      "sector_impact": {{
        "保险": {{ "score": 数字, "level": "强利空/利空/中性/利好/强利好", "reason": "20字以内" }},
        "银行": {{ "score": 数字, "level": "...", "reason": "..." }},
        "非银金融": {{ "score": 数字, "level": "...", "reason": "..." }},
        "信贷": {{ "score": 数字, "level": "...", "reason": "..." }},
        "财富管理": {{ "score": 数字, "level": "...", "reason": "..." }}
      }},
      "primary_sector": "五选一（或空字符串）",
      "summary": "一句话概述，30字以内"
    }}
  ],
  "market_overview": {{
    "overall_sentiment": "乐观/中性/谨慎",
    "hot_sector": "当日最热门赛道",
    "cold_sector": "当日最承压赛道",
    "brief": "2-3句话总结今日整体盘面与方向"
  }}
}}

新闻列表：
{bullet_list}
"""


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
    try:
        logger.info(f"调用火山引擎 Ark: model={LLM_MODEL}")
        client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": "你是严谨的财经信息分析助手，只输出合法 JSON。确保每个 score 是 -2/-1/0/1/2 整数。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=3500,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content.strip()
        content = re.sub(r"^```json\s*|\s*```$", "", content, flags=re.IGNORECASE)
        parsed = json.loads(content)
        logger.info("大模型分类完成")
        return parsed
    except Exception as e:
        logger.error(f"大模型调用失败: {e}")
        return {
            "error": str(e),
            "items": [],
            "market_overview": {
                "overall_sentiment": "—",
                "hot_sector": "—",
                "cold_sector": "—",
                "brief": f"大模型调用失败: {e}",
            },
        }


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


def main() -> int:
    logger.info("=== 财经热榜数据抓取开始 ===")
    try:
        # 1) 抓取 + 解析
        html = fetch_html(TARGET_URL)
        raw_data = parse_all_boards(html)

        # 2) 汇总 → 调用火山引擎 Ark 做五大赛道分类
        items_for_llm = collect_items_for_llm(raw_data)
        logger.info(f"送入大模型分类: {len(items_for_llm)} 条新闻")
        classification = call_llm_for_classification(items_for_llm)
        raw_data["classification"] = classification

        # 3) 按赛道聚合
        by_sector = {s: [] for s in SECTORS}
        for item in classification.get("items", []):
            primary = item.get("primary_sector", "")
            if primary and primary in by_sector:
                imp = item.get("sector_impact", {}).get(primary, {})
                by_sector[primary].append({
                    **item,
                    "this_sector_score": imp.get("score", 0),
                    "this_sector_level": imp.get("level", "中性"),
                    "this_sector_reason": imp.get("reason", ""),
                })
        raw_data["by_sector"] = by_sector

        # 4) 落盘
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
