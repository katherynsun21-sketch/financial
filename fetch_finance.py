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
    datefmt="%Y-%m
