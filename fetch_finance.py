"""
财经热榜数据抓取 + 大模型分析脚本
来源: https://tophub.today/c/finance
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

# 从环境变量读大模型配置 —— 在 GitHub 里以 Secrets 方式存 Key
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")

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
    last_exception = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(f"抓取中 (第 {attempt}/{MAX_RETRIES} 次): {url}")
            response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            response.encoding = response.apparent_encoding or "utf-8"
            logger.info(f"抓取成功，长度: {len(response.text)} 字符")
            return response.text
        except Exception as e:
            last_exception = e
            wait_time = attempt * 3
            logger.warning(f"抓取失败: {e}，等待 {wait_time} 秒后重试...")
            time.sleep(wait_time)
    raise RuntimeError(f"重试 {MAX_RETRIES} 次后仍无法抓取: {last_exception}")


def parse_heat_value(extra_text: str) -> dict:
    result = {"value": 0, "unit": "", "raw": extra_text}
    if not extra_text:
        return result
    text = extra_text.strip()
    result["raw"] = text
    m = re.match(r"([\d,]+(?:\.\d+)?)\s*(万|w|W|亿|k|K|千)?", text)
    if m:
        num_str = m.group(1).replace(",", "")
        unit = m.group(2) or ""
        try:
            value = float(num_str)
            if unit in ("万", "w", "W"):
                value *= 10_000
            elif unit == "亿":
                value *= 100_000_000
            elif unit in ("k", "K", "千"):
                value *= 1_000
            result["value"] = int(value) if value == int(value) else value
            result["unit"] = unit
        except ValueError:
            pass
    return result


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


def build_prompt(raw_data: dict) -> str:
    """根据原始抓取数据生成给大模型的提示词"""
    summary_lines = []
    for board in raw_data["boards"][:5]:  # 取前 5 个榜单避免 token 过长
        lines = []
        for item in board["items"][:5]:  # 每个榜单取前 5 条
            heat = ""
            if item["heat_value"]:
                heat = f"（热度 {item['heat_raw']}）"
            lines.append(f"  {item['rank']}. {item['title']}{heat}")
        if lines:
            summary_lines.append(f"[{board['board_name']} - {board['board_subtitle']}]")
            summary_lines.extend(lines)

    return f"""
你是一个财经市场观察者。下面是今日财经热榜的 TOP 条目，请你：

1) 用 2-3 句话总结今天市场的总体情绪（乐观/谨慎/悲观，以及主要方向）
2) 挑出 3 条你认为最值得关注的新闻，用一句话说明为什么值得关注
3) 给出一个"今日关注点"，限 20 字以内

用中文输出，使用下面的 JSON 格式（不要包含任何 markdown 标记）：
{{
  "sentiment": "...",
  "summary": "...",
  "highlights": [
    {{"title": "...", "reason": "..."}},
    {{"title": "...", "reason": "..."}},
    {{"title": "...", "reason": "..."}}
  ],
  "today_focus": "..."
}}

榜单数据：
{chr(10).join(summary_lines)}
""".strip()


def call_llm(raw_data: dict) -> dict:
    """调用大模型生成分析结果。没有 Key 则返回占位数据。"""
    if not LLM_API_KEY:
        logger.warning("未设置 LLM_API_KEY，跳过大模型分析")
        return {
            "error": "LLM_API_KEY not configured",
            "sentiment": "—",
            "summary": "（未配置大模型 Key，跳过分析）",
            "highlights": [],
            "today_focus": "",
        }

    prompt = build_prompt(raw_data)
    try:
        client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": "你是一个严谨的财经信息分析助手，只输出合法 JSON。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=800,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content.strip()
        # 去掉可能的 ```json ... ``` 包裹
        content = re.sub(r"^```json\s*|\s*```$", "", content, flags=re.IGNORECASE)
        parsed = json.loads(content)
        logger.info("大模型分析完成")
        return parsed
    except Exception as e:
        logger.error(f"大模型调用失败: {e}")
        return {
            "error": str(e),
            "sentiment": "—",
            "summary": f"（大模型调用失败：{e}）",
            "highlights": [],
            "today_focus": "",
        }


def save_data(data: dict, suffix: str) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    date_str = data["date"] if "date" in data else datetime.now(CUSTOM_TZ).strftime("%Y-%m-%d")
    out_file = DATA_DIR / f"{date_str}_{suffix}.json"
    latest_file = DATA_DIR / f"latest_{suffix}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"已保存: {out_file}")
    with open(latest_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"已更新: {latest_file}")
    return latest_file


def main() -> int:
    logger.info("=== 财经热榜数据抓取开始 ===")
    try:
        # 1) 抓取
        html = fetch_html(TARGET_URL)
        raw_data = parse_all_boards(html)
        save_data(raw_data, "raw")

        # 2) 大模型分析
        analysis = call_llm(raw_data)
        analysis_payload = {
            "generated_at": datetime.now(CUSTOM_TZ).isoformat(),
            "date": datetime.now(CUSTOM_TZ).strftime("%Y-%m-%d"),
            "time": datetime.now(CUSTOM_TZ).strftime("%H:%M:%S"),
            "analysis": analysis,
        }
        save_data(analysis_payload, "analysis")

        # 3) 综合文件（latest.json = 原始数据 + 分析）
        combined = {**raw_data, "analysis": analysis}
        combined_file = DATA_DIR / "latest.json"
        with open(combined_file, "w", encoding="utf-8") as f:
            json.dump(combined, f, ensure_ascii=False, indent=2)
        logger.info(f"已更新: {combined_file}")

        logger.info("=== 全部完成 ===")
        return 0
    except Exception as e:
        logger.error(f"=== 抓取失败: {e} ===")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
