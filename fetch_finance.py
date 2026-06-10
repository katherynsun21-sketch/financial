"""
财经热榜数据抓取脚本
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

TARGET_URL = "https://tophub.today/c/finance"
DATA_DIR = Path(__file__).parent / "data"
CUSTOM_TZ = timezone(timedelta(hours=8))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3

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

    board_link_elem = card_html.select_one(".cc-cd-is a")
    board_url = board_link_elem.get("href", "") if board_link_elem else ""
    if board_url and board_url.startswith("/"):
        board_url = "https://tophub.today" + board_url

    card_id = card_html.get("id", "")
    node_id = card_id[5:] if card_id.startswith("node-") else ""

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
            heat = parse_heat_value(extra_text)
            item_url = a_tag.get("href", "")
            item_id = a_tag.get("itemid", "")
            items.append({
                "rank": int(rank_text) if rank_text.isdigit() else idx,
                "title": item_title,
                "extra": extra_text,
                "heat": heat,
                "url": item_url,
                "item_id": item_id,
            })

    full_text = card_html.get_text(" ", strip=True)
    time_match = re.search(
        r"(\d+\s*(?:分钟前|小时前|天前|刚刚)|\d{1,2}:\d{2})", full_text
    )
    update_time = time_match.group(1) if time_match else ""

    return {
        "board_name": title,
        "board_subtitle": subtitle,
        "board_url": board_url,
        "node_id": node_id,
        "update_time_text": update_time,
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
            logger.info(
                f"  [{idx}] {board['board_name']} | {board['board_subtitle']} "
                f"| {board['items_count']} 条 | {board['update_time_text']}"
            )
        except Exception as e:
            logger.error(f"  [{idx}] 解析失败: {e}")
    total_items = sum(b["items_count"] for b in boards)
    return {
        "source_url": TARGET_URL,
        "fetched_at": datetime.now(CUSTOM_TZ).isoformat(),
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "date": datetime.now(CUSTOM_TZ).strftime("%Y-%m-%d"),
        "time": datetime.now(CUSTOM_TZ).strftime("%H:%M:%S"),
        "boards_count": len(boards),
        "total_items": total_items,
        "boards": boards,
    }


def save_data(data: dict) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    date_str = data["date"]
    daily_file = DATA_DIR / f"{date_str}.json"
    latest_file = DATA_DIR / "latest.json"
    with open(daily_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"已保存: {daily_file}")
    with open(latest_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"已更新: {latest_file}")
    return latest_file


def main() -> int:
    logger.info("=== 财经热榜数据抓取开始 ===")
    logger.info(f"目标 URL: {TARGET_URL}")
    try:
        html = fetch_html(TARGET_URL)
        data = parse_all_boards(html)
        save_data(data)
        logger.info(
            f"=== 抓取完成: {data['boards_count']} 个榜单, "
            f"{data['total_items']} 条数据 ==="
        )
        return 0
    except Exception as e:
        logger.error(f"=== 抓取失败: {e} ===")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
