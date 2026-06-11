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
    angles = {
        "保险": "【投放切入点】\n• 养老险：绑定延迟退休、养老金缺口话题，突出稳健收益和终身领取\n• 健康险：结合医疗费用上涨、慢病管理，强调保障范围和理赔服务\n• 代理人增员：突出收入潜力、培训体系、数字化工具支持\n• 车险续保：抓住新车销量数据，结合出行场景做精准触达\n\n【内容角度】\n• 政策解读：养老金第三支柱、商业健康险个税优惠\n• 场景化内容：家庭保障规划、企业团险方案对比\n• 用户证言：理赔案例、服务体验故事",
        "银行": "【投放切入点】\n• 零售信贷：LPR下调窗口，突出利率优惠和审批效率\n• 信用卡：权益升级、新卡种发布、分期费率优惠\n• 私行/财富：高净值客户资产配置、家族传承需求\n• 存款产品：利率对比、安全性背书、专属服务\n\n【内容角度】\n• 产品对比：不同期限、不同银行产品差异\n• 场景营销：装修贷款、购车分期、旅游消费\n• 品牌故事：数字化转型成果、服务创新案例",
        "信贷": "【投放切入点】\n• 消费金融：场景嵌入（电商、出行、教育），突出便捷审批\n• 小微贷款：政策扶持窗口，低利率+政府贴息\n• 房贷：利率下行周期，置换贷款需求激活\n• 助贷平台：流量合作、数据驱动精准获客\n\n【内容角度】\n• 利率科普：LPR机制、浮动利率转换\n• 资质解读：如何提升贷款通过率\n• 案例分享：小微企业主融资故事",
        "财富": "【投放切入点】\n• 基金发行：市场回暖期首发/新发基金推广\n• 理财产品：净值波动解读，稳健型产品突出\n• 养老金开户：政策红利窗口期，强调税收优惠\n• 券商开户：佣金战、投顾服务升级\n\n【内容角度】\n• 市场解读：宏观经济、行业趋势分析\n• 投资教育：资产配置理念、风险管理\n• 产品测评：不同基金/理财对比分析",
        "非金": "【投放切入点】\n• 金融科技：API开放平台、SaaS解决方案\n• 支付结算：跨境支付、B2B支付场景\n• 担保/保理：供应链金融、应收账款融资\n• AMC：不良资产处置、特殊机会投资\n\n【内容角度】\n• 技术展示：AI风控、数字身份认证\n• 案例研究：行业解决方案落地案例\n• 趋势分析：金融数字化转型方向",
    }
    return angles.get(sector, "%s赛道相关新闻可作为客户教育内容和销售切入点，结合客户预算做选题与投放计划。" % sector)


def get_compliance_focus(sector: str) -> str:
    compliance = {
        "保险": "【合规关注点】\n• 广告素材：禁止承诺收益、夸大保障范围\n• 产品说明：必须明确免责条款和等待期\n• 代理人资质：广告中展示的代理人必须有执业资格\n• 数据合规：用户信息收集需符合个人信息保护法\n• 监管报备：金融广告需提前报备相关监管部门",
        "银行": "【合规关注点】\n• 利率宣传：必须明示年化利率，禁止模糊表述\n• 风险提示：理财产品需显著提示风险等级\n• 信用卡营销：禁止诱导办卡、强制捆绑销售\n• 个人信息：客户信息保护严格遵守监管要求\n• 反洗钱：大额交易监控和报告义务",
        "信贷": "【合规关注点】\n• 利率红线：不得超过法定利率上限\n• 催收规范：禁止暴力催收、骚扰式催收\n• 资质要求：放贷主体必须具备相应牌照\n• 信息披露：费用构成必须清晰透明\n• 数据合规：用户征信信息使用需合规",
        "财富": "【合规关注点】\n• 基金宣传：禁止预测收益、承诺保本\n• 适当性管理：产品需匹配投资者风险承受能力\n• 投顾资质：提供投资建议需具备相应资质\n• 信息隔离：防止内幕信息泄露\n• 广告合规：基金销售广告需符合证监会规定",
        "非金": "【合规关注点】\n• 支付牌照：开展支付业务必须具备牌照\n• 数据安全：金融数据需符合等保要求\n• 反不正当竞争：禁止虚假宣传、商业诋毁\n• 跨境业务：涉及跨境的需符合外汇管理规定\n• 行业资质：根据业务类型取得相应监管许可",
    }
    return compliance.get(sector, "【合规关注点】\n• 遵守金融广告监管规定\n• 保护用户个人信息\n• 如实披露产品信息")


def get_risk_factors(sector: str) -> str:
    risks = {
        "保险": "【投放风险提示】\n• 监管风险：广告内容被监管部门约谈整改\n• 品牌风险：理赔纠纷可能引发舆情危机\n• 合规风险：产品说明不当导致投诉\n• 市场风险：利率下行影响储蓄型产品吸引力",
        "银行": "【投放风险提示】\n• 声誉风险：客户投诉可能引发负面舆情\n• 合规风险：理财产品宣传不当被处罚\n• 利率风险：利率变动影响产品竞争力\n• 流动性风险：存款流失影响资金成本",
        "信贷": "【投放风险提示】\n• 信用风险：逾期率上升影响资产质量\n• 合规风险：催收不当引发监管关注\n• 利率风险：利率下行压缩利差空间\n• 政策风险：监管政策变化影响业务开展",
        "财富": "【投放风险提示】\n• 市场风险：净值波动可能引发客户投诉\n• 合规风险：宣传不当被监管处罚\n• 流动性风险：大额赎回可能影响产品运作\n• 声誉风险：基金经理变动可能影响投资者信心",
        "非金": "【投放风险提示】\n• 合规风险：业务资质不全被监管处罚\n• 技术风险：系统故障影响服务连续性\n• 竞争风险：行业竞争加剧压缩利润空间\n• 政策风险：监管政策变化影响业务模式",
    }
    return risks.get(sector, "【投放风险提示】\n• 监管政策变化风险\n• 市场竞争风险\n• 合规运营风险")


def fallback_segments(sector: str) -> list:
    mapping = {
        "保险": ["寿险/财险公司广告主", "保险代理人增员", "企业团险 HR 负责人", "高净值家庭保障客户", "车险续保人群"],
        "银行": ["银行零售信贷广告主", "信用卡/消费分期品牌", "私行/财富管理运营", "中小企业信贷部门", "存款/理财产品营销"],
        "信贷": ["消费金融公司", "小微企业信贷产品", "助贷/贷款服务平台", "融资租赁/保理广告主", "小微经营者"],
        "财富": ["公募/私募基金品牌", "信托/财富管理公司", "券商资管/投顾业务", "养老金/养老金融产品", "高净值客户服务"],
        "非金": ["金融科技/互金平台", "综合金融服务平台", "支付结算/担保类品牌", "AMC/资产管理公司", "金融行业解决方案厂商"],
    }
    return mapping.get(sector, ["金融行业泛客户"])


def fallback_actions(sector: str) -> list:
    actions = {
        "保险": ["圈选寿险/财险/养老险广告主做专项跟进", "准备代理人增员、健康险、车险续保等话题素材", "检查保险合规文案和产品授权状态"],
        "银行": ["盘点零售信贷、信用卡、私行/财富客户营销机会", "准备品牌故事+效果转化素材组合", "联动银行客户做投放方案沟通"],
        "信贷": ["筛选消费金融、助贷平台、小微企业信贷客户", "准备 LPR 降息、融资成本变化相关的销售话术", "优化信贷产品获客落地页与运营活动"],
        "财富": ["联系基金、理财、信托、券商资管客户沟通推广计划", "准备市场回暖、基金发行、养老金开户等素材", "安排财富客户内容种草+产品跳转组合投放"],
        "非金": ["跟进金融科技、综合金融平台客户的品牌预算", "准备行业解决方案、标杆客户案例素材", "梳理支付/担保/AMC 等客户的投放需求"],
    }
    return actions.get(sector, ["整理当前赛道客户清单", "准备相关话题素材", "安排本周客户沟通计划"])


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
        "你是商业化金融行业广告销售和运营团队的行业研究员。请把新闻归入五大赛道，并给出销售/运营可执行的判断。",
        "五大赛道只能从以下选择：保险、非金、信贷、财富、银行。",
        "归类规则（必须遵守，和业务口径一致）：",
        "1) 标题或来源中出现保险、寿险、财险、养老险、保费、赔付等，归保险。",
        "2) 出现银行、商业银行、存款、息差、按揭、银联等，归银行。",
        "3) 出现基金、证券、期货、理财、信托、资管、私募、公募、ETF、高净值、典当、蚂蚁财富等，归财富（这是重点优先级）。",
        "4) 出现信贷、贷款、房贷、消费贷、经营贷、小微、普惠、融资、支付、消费金融、第三方支付、贷款服务等，归信贷（优先级高于财富和非金）。",
        "5) 非金只作为兜底：只有当新闻主要讲金融科技、互金平台、综合金融平台、AMC且不满足上面任何一条时，才归非金。",
        "基金、证券、期货一定要归财富，不要归非金；支付、消费金融一定要归信贷，不要归非金。",
        "输出要求：",
        "- topic_type 从：政策监管、产品机会、市场机会、经营变化、客户需求、行业竞争、监管处罚、一般资讯 中选择。",
        "- sector_angle 描述这条新闻对销售/运营的启示（例如：哪些类型客户可能有投放需求、适合什么投放场景）。",
        "- customer_segments 列出可能感兴趣的广告主/运营客群，2-3 个即可。",
        "- actions 给出销售/运营层面可执行动作，2-3 条即可。",
        "- relevance_reason 简要说明为什么归入该赛道（30 字内）。",
        "confidence 为 0-1 的小数。请只输出合法 JSON，不要 Markdown，不要解释。格式：",
        '{"items":[{"id":"","sector":"保险/非金/信贷/财富/银行","topic_type":"主题类型","confidence":0.75,"relevance_reason":"30字内","sector_angle":"销售/运营启示","customer_segments":["广告主/客群1","广告主/客群2"],"actions":["销售/运营动作1","销售/运营动作2"]}]}',
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
        # 为每条新闻生成面向销售/运营的可执行建议
        item_suggestion = _per_item_suggestion(merged)
        merged["item_suggestions"] = item_suggestion
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
            "news_titles": [item["title"] for item in by_sector.get(sector, [])[:8]],
        }
    return "\n".join([
        "你是商业化金融行业广告销售和运营团队的行业研究员。请基于五大赛道的当日新闻，生成对销售和运营可直接使用的整体局势分析。",
        "核心关注三个问题：",
        "1) 今天金融行业整体商业化机会在哪里？",
        "2) 每个赛道客户的客户画像、典型产品、可能的投放需求分别是什么？",
        "3) 销售/运营层面可以做哪些具体动作？",
        "请只输出 JSON，不要 Markdown，不要解释。格式：",
        '{"overall_situation":"今天整体局面 150 字内，聚焦销售/运营视角的概括","core_themes":["当前商业主题 1","商业主题 2","商业主题 3","商业主题 4"],"sectors":{"保险":{"insight":"30 字内销售/运营洞察","advertisers":["客户类型 1","客户类型 2"],"products":["主推产品/服务 1","主推产品/服务 2"],"suggested_landing":["建议运营动作 1","建议运营动作 2"]},"银行":{},"财富":{},"信贷":{},"非金":{}},"sales_today_actions":["今日销售动作 1","今日销售动作 2","今日销售动作 3"]}',
        "输入数据：",
        json.dumps(compact, ensure_ascii=False),
    ])


def _dynamic_content_angles(sector: str, top_titles: list, top_topics: list) -> list:
    head = [(t[:24] + u"…") if len(t) > 24 else t for t in (top_titles or [])][:2]
    topics = [tp for tp, _ in (top_topics or []) if tp and tp != u"一般资讯"][:2]
    base = {
        "保险": [
            u"结合「养老金融 / 商业健康险个税优惠」等话题，面向寿险/财险客户推送政策解读型内容",
            u"围绕代理人增员和服务升级，产出「理赔案例 + 行业对比」组合素材，作为品牌 + 效果投放的承接",
            u"关注新车销量、车险综改等动态，为车险续保类客户准备差异化投放话术和落地页",
        ],
        "银行": [
            u"围绕零售信贷利率、信用卡权益升级等话题，产出「产品对比 + 场景案例」型内容",
            u"面向私行/财富客户，准备「资产配置 + 财富传承」主题内容，承接高净值人群预算",
            u"结合存款利率变动和储蓄趋势，为零售存款/结构性产品客户准备差异化营销素材",
        ],
        "财富": [
            u"围绕基金发行、市场回暖、ETF规模变化，产出「新发基金解读 + 定投教育」组合内容",
            u"结合个人养老金、养老理财、信托产品动态，面向高净值客户推送「长期资产配置」话题",
            u"关注券商佣金战和投顾升级，为券商资管/投顾客户准备品牌 + 效果投放方案",
        ],
        "信贷": [
            u"围绕 LPR 下调、消费贷利率下行，面向消费金融/助贷平台客户准备「低利率 + 审批快」素材",
            u"结合小微融资支持政策，为中小微企业信贷客户制作「政策红利 + 成功案例」内容",
            u"关注房贷/经营贷置换需求，准备「利率科普 + 资质对比」型落地页与内容种草",
        ],
        "非金": [
            u"围绕金融科技升级、AI风控、数字金融等动态，为金融科技/综合金融平台客户准备技术展示型内容",
            u"关注支付场景创新、跨境支付合规，面向支付结算/担保类品牌推送场景解决方案素材",
            u"结合 AMC/不良资产处置动态，为资产管理公司、行业解决方案客户准备案例研究内容",
        ],
    }
    result = list(base.get(sector, []))
    if head:
        result.append(u"当日可直接复用的话题锚点：" + u"；".join(head))
    if topics:
        result.append(u"内容关键词建议叠加：" + u"、".join(topics))
    return result


def _dynamic_compliance(sector: str, top_topics: list) -> list:
    topics = [tp for tp, _ in (top_topics or []) if tp][:3]
    base = {
        "保险": [
            u"广告素材禁止承诺收益、夸大保障范围；产品页面必须明确免责条款和等待期",
            u"如涉及代理人展示，必须校验执业资格；金融广告需完成内部合规+监管报备",
            u"涉及健康险/养老险等税收优惠表述时，不得使用绝对化措辞（如「必涨」「稳赚」）",
        ],
        "银行": [
            u"必须明示年化利率（APR），禁止使用「最低」「最高」等模糊或诱导性措辞",
            u"理财产品需在首屏显著位置展示风险等级和投资者适当性提示",
            u"信用卡营销禁止诱导过度借贷、强制捆绑；客户信息收集需符合个保法最小必要原则",
        ],
        "财富": [
            u"基金/理财宣传不得预测收益、承诺保本、使用历史业绩暗示未来表现",
            u"涉及投顾、资产管理，必须标注持牌主体和资质备案号",
            u"涉及养老金/养老理财等长期产品，需提示流动性风险和税收政策变动风险",
        ],
        "信贷": [
            u"贷款利率不得超过法定上限，费用构成必须清晰透明并在合同/落地页显著位置披露",
            u"禁止暴力催收、骚扰式营销，催收/提醒短信文案需提前合规审查",
            u"征信信息查询/使用需用户明示授权，不得滥用数据驱动营销",
        ],
        "非金": [
            u"开展支付/担保/资管等业务必须具备相应牌照，广告需体现持牌信息",
            u"金融数据/用户信息处理需符合数据安全法和个人信息保护法",
            u"跨境业务/支付需符合外汇管理规定，不得暗示绕过监管",
        ],
    }
    result = list(base.get(sector, []))
    if u"监管处罚" in topics or any(u"罚" in t or u"监管" in t for t in topics):
        result.insert(0, u"【今日特别提醒】当日出现监管处罚类新闻，客户广告文案需重新走内部合规审查")
    if u"赔付" in topics or u"风险" in topics or any(u"风险" in t or u"赔付" in t for t in topics):
        result.append(u"【今日特别提醒】涉及赔付/负面舆情相关新闻，广告主需暂缓投放敏感性素材")
    return result


def _dynamic_risks(sector: str, top_topics: list) -> list:
    topics = [tp for tp, _ in (top_topics or []) if tp][:3]
    base = {
        "保险": [
            u"利率下行环境下储蓄型产品吸引力可能减弱，需注意退保和销售承压风险",
            u"理赔纠纷/巨灾赔付类新闻可能引发舆情，需配合品牌安全策略",
            u"代理人队伍波动可能影响增员类投放 ROI，需持续监控转化数据",
        ],
        "银行": [
            u"净息差持续收窄，银行零售/信贷预算可能缩减，需调整客户沟通策略",
            u"存款/理财搬家趋势下，品牌型投放短期转化可能偏低，需配合活动位运营",
            u"房地产风险传导至不良率，涉房类素材需避开敏感时期",
        ],
        "财富": [
            u"市场波动期，净值型产品宣传需做好客户预期管理和投资者适当性",
            u"大额赎回、信托违约等负面事件可能影响品牌安全，需临时调整投放词包",
            u"基金经理变动/管理人负面新闻，需暂停相关品牌广告，避免品牌风险",
        ],
        "信贷": [
            u"逾期率上升期，消费金融/助贷客户的风险模型与投放节奏需要重新校准",
            u"降息周期利差压缩，客户预算可能收紧，需以「效果 + 品牌安全」方案承接",
            u"催收/数据合规监管升级，需提醒客户调整提醒类短信和外呼策略",
        ],
        "非金": [
            u"金融科技监管环境变化，需提醒客户关注牌照和业务边界调整",
            u"系统故障/安全事件会严重影响品牌信任，相关客户投放需有应急预案",
            u"行业竞争加剧+监管收紧，客户预算可能转向合规可控的效果类投放",
        ],
    }
    result = list(base.get(sector, []))
    if any(u"罚" in t or u"违规" in t for t in topics):
        result.insert(0, u"【今日风险提示】当日含监管处罚/违规类新闻，建议客户暂停敏感关键词投放 24-48 小时")
    if any(u"违约" in t or u"逾期" in t or u"风险" in t for t in topics):
        result.append(u"【今日风险提示】负面风险类新闻集中，建议调低「承诺型/收益型」创意比例")
    return result


def _per_item_suggestion(item: dict) -> dict:
    title = item.get("title", u"")
    sector = item.get("sector", u"")
    topic = item.get("topic_type", u"一般资讯")

    advertisers_map = {
        "保险": u"寿险/财险公司、健康险品牌、企业团险 HR、车险续保客户、代理人增员相关预算方",
        "银行": u"银行零售信贷、信用卡/消费分期品牌、私行/财富运营、中小企业信贷部、存款/理财营销团队",
        "财富": u"公募/私募基金品牌、信托/财富管理公司、券商资管/投顾业务、养老金/养老金融产品方、高净值客户服务机构",
        "信贷": u"消费金融公司、助贷/贷款服务平台、小微企业信贷产品方、融资租赁/保理客户、支付场景信贷合作方",
        "非金": u"金融科技/互金平台、综合金融服务平台、支付结算/担保类品牌、AMC/资产管理公司、行业解决方案客户",
    }

    # 依据标题/主题拼接更具体的内容角度
    actions = []
    if u"保费" in title or u"增长" in title or u"NBV" in title:
        actions.append(u"以「保费增长、新业务价值改善」为话题，向寿险/财险品牌客户沟通品牌 + 投放计划")
    if u"养老" in title or u"养老金" in title or u"养老险" in title:
        actions.append(u"准备养老金融专题内容，面向养老险、养老理财、个人养老金开户类客户推送")
    if u"健康" in title or u"医疗" in title or u"重疾" in title:
        actions.append(u"结合健康险/医疗险话题，面向健康险品牌、团险 HR 客户沟通投放方案")
    if u"车险" in title or u"新车" in title or u"汽车" in title or u"新能源" in title:
        actions.append(u"面向车险、新车分期等客户准备续保/场景信贷类投放素材和落地页")
    if u"利率" in title or u"LPR" in title or u"降息" in title or u"存款" in title:
        actions.append(u"围绕利率变动准备「产品对比 + 场景案例」型内容，面向零售信贷/存款/信用卡客户沟通")
    if u"贷款" in title or u"信贷" in title or u"小微" in title or u"普惠" in title:
        actions.append(u"面向消费金融、助贷平台、小微企业信贷客户准备「政策红利 + 审批效率」素材")
    if u"房贷" in title or u"按揭" in title or u"房地产" in title or u"保交楼" in title:
        actions.append(u"面向房贷/经营贷置换需求客户，准备「利率科普 + 资质对比」型落地页")
    if u"基金" in title or u"ETF" in title or u"理财" in title or u"发行" in title:
        actions.append(u"面向基金公司/理财子客户准备「新发基金解读 + 定投教育」内容和投放方案")
    if u"券商" in title or u"证券" in title or u"佣金" in title or u"投顾" in title:
        actions.append(u"面向券商资管/投顾业务客户沟通「佣金战 + 投顾升级」品牌 + 效果投放组合")
    if u"信托" in title or u"高净值" in title or u"家族" in title or u"传承" in title:
        actions.append(u"面向信托/财富管理公司准备「长期资产配置 + 财富传承」内容")
    if u"监管" in title or u"处罚" in title or u"罚款" in title or u"违规" in title or u"问责" in title:
        actions.append(u"【合规提醒】当日监管处罚/违规类新闻，建议客户审查敏感素材，必要时暂停关键词投放")
    if u"违约" in title or u"逾期" in title or u"暴雷" in title or u"兑付" in title or u"风险" in title:
        actions.append(u"【风险提示】负面风险/违约类新闻集中，建议客户调低「承诺型/收益型」创意比例，增加品牌安全词包")
    if u"科技" in title or u"AI" in title or u"数字" in title or u"互金" in title:
        actions.append(u"面向金融科技/综合金融平台客户沟通「AI风控、数字金融」技术展示和案例内容")
    if u"支付" in title or u"跨境" in title or u"担保" in title or u"AMC" in title or u"资产处置" in title:
        actions.append(u"面向支付结算/担保/AMC 等客户准备场景解决方案、案例研究型内容")
    if u"信用卡" in title or u"分期" in title or u"消费" in title:
        actions.append(u"面向信用卡/消费分期品牌沟通「权益升级 + 场景营销」投放计划")
    if not actions:
        # 兜底：结合主题给出建议
        actions.append(u"以该条新闻为话题，面向" + (advertisers_map.get(sector, u"金融行业广告主")) + u"沟通定制化投放方案")
        actions.append(u"准备「行业趋势 + 产品机会」型内容，用作品牌投放/内容种草的承接")

    compliance_extra = []
    if u"监管" in topic or u"罚" in topic or u"处罚" in topic:
        compliance_extra.append(u"该条含监管/处罚信号，客户广告文案需完成内部合规审查后再上线")
    if u"收益" in title or u"涨幅" in title or u"回报" in title:
        compliance_extra.append(u"涉及收益/回报型说法，广告不得使用绝对化措辞，需披露投资风险")

    risk_extra = []
    if u"违约" in title or u"逾期" in title or u"暴雷" in title or u"风险" in title:
        risk_extra.append(u"该条含负面风险信号，建议客户临时暂停高风险创意并关注品牌舆情")
    if u"投诉" in title or u"纠纷" in title or u"举报" in title:
        risk_extra.append(u"该条含客户投诉/纠纷信号，建议客户关注社媒舆情，必要时调整投放节奏")

    return {
        "suggested_advertisers": advertisers_map.get(sector, u"金融行业广告主"),
        "suggested_actions": actions[:3],
        "compliance_notes": compliance_extra or [u"常规审查：年化利率、风险提示、资质披露、个保法合规"],
        "risk_notes": risk_extra or [u"常规提醒：关注市场/监管/舆情三方面风险对投放 ROI 的影响"],
        "topic_type": topic,
    }


def fallback_marketing_insights(sector_summary: dict, by_sector: dict) -> dict:
    sectors = {}
    sector_advertisers = {
        "保险": ["寿险/财险公司品牌广告", "代理人增员类客户", "健康险/养老险产品", "企业团险 HR 投放", "车险续保业务"],
        "银行": ["银行零售信贷获客", "信用卡/消费分期品牌", "私行/财富客户运营", "中小企业信贷", "存款/理财营销"],
        "财富": ["公募/私募基金品牌", "信托/财富管理公司", "券商资管/投顾业务", "养老金融产品", "高净值客户服务"],
        "信贷": ["消费金融公司", "助贷/贷款服务平台", "小微企业信贷产品", "融资租赁/保理客户", "支付场景信贷"],
        "非金": ["金融科技/互金平台", "综合金融服务平台", "支付结算/担保类品牌", "AMC/资产管理公司", "行业解决方案客户"],
    }
    sector_products = {
        "保险": ["养老险", "健康险", "代理人增员工具", "企业团险", "车险"],
        "银行": ["零售信贷产品", "信用卡/分期", "私行财富服务", "中小企业贷款", "存款/结构性产品"],
        "财富": ["基金发行与定投", "理财产品", "信托产品", "养老金开户", "券商资管"],
        "信贷": ["消费贷款", "小微贷款", "房贷/经营贷", "助贷平台获客", "支付信贷服务"],
        "非金": ["金融科技解决方案", "支付结算产品", "担保服务", "资产处置", "综合金融平台"],
    }

    # 聚合当日全局高频主题词，给整体局势使用
    global_topics = []
    for sector in SECTORS:
        for tp, cnt in (sector_summary.get(sector, {}).get("topic_counts", {}) or {}).items():
            if tp and tp != u"一般资讯" and cnt >= 2:
                global_topics.append((tp, cnt))
    global_topics.sort(key=lambda x: x[1], reverse=True)
    global_topic_names = [tp for tp, _ in global_topics[:5]] or [u"政策监管", u"产品机会"]

    for sector in SECTORS:
        summary = sector_summary.get(sector, {})
        count = summary.get("count", 0)
        top_topics = sorted(summary.get("topic_counts", {}).items(), key=lambda x: x[1], reverse=True)
        top_titles = summary.get("top_titles", [])

        if count == 0:
            insight = sector + u"赛道今日暂未抓取到直接相关新闻。建议主动关注" + sector + u"相关政策和行业动态，提前准备投放素材。"
        else:
            topic_text_list = [topic for topic, cnt in top_topics[:3] if topic != u"一般资讯"] or [u"行业动态"]
            headlines_short = []
            for t in top_titles[:2]:
                headlines_short.append((t[:26] + u"…") if len(t) > 26 else t)
            insight = (
                sector + u"赛道今日共 " + str(count) + u" 条相关新闻。核心主题：" +
                u"、".join(topic_text_list) +
                u"。代表性新闻：" + u"；".join(headlines_short or [u"行业热点"]) +
                u"。销售/运营建议：围绕上述主题重新梳理客户清单，优先沟通与" + u"、".join(topic_text_list[:1]) +
                u"直接相关的广告主，并检查广告素材是否满足合规和品牌安全要求。"
            )

        sectors[sector] = {
            "insight": insight,
            "advertisers": sector_advertisers.get(sector, []),
            "products": sector_products.get(sector, []),
            "content_angles": _dynamic_content_angles(sector, top_titles, top_topics),
            "compliance_points": _dynamic_compliance(sector, top_topics),
            "risk_factors": _dynamic_risks(sector, top_topics),
            "top_titles": top_titles[:5],
            "topic_counts": summary.get("topic_counts", {}),
        }

    total_items = sum([sector_summary.get(s, {}).get("count", 0) for s in SECTORS])
    active_sectors = [s for s in SECTORS if sector_summary.get(s, {}).get("count", 0) >= 5]
    hot_topic_text = u""
    if active_sectors:
        hot_topic_text = u"今日热点赛道（新闻量较多）：" + u"、".join(active_sectors) + u"。"

    # 挑出当日"最值得销售跟进"的 2-3 个赛道做重点建议
    lead_sectors = active_sectors[:3] or [s for s in SECTORS if sector_summary.get(s, {}).get("count", 0) > 0][:3]
    lead_text = u"今日建议销售优先沟通：" + u"、".join(lead_sectors) + u"类客户。" if lead_sectors else u""

    overall = (
        u"今日共抓取 " + str(total_items) + u" 条金融相关新闻，覆盖五大类金融赛道。" + hot_topic_text +
        u"当日全局高频主题：" + u"、".join(global_topic_names) + u"。" + lead_text +
        u"建议销售/运营团队：① 按赛道重排客户清单，优先沟通与当日主题高度相关的客户；"
        u"② 准备「政策解读 + 产品机会 + 成功案例」组合素材；"
        u"③ 提前进行合规与品牌安全审查，避免敏感词和绝对化措辞。"
    )

    return {
        "overall_situation": overall,
        "core_themes": global_topic_names + [u"品牌安全与合规", u"内容种草与活动运营配合"],
        "sectors": sectors,
        "sales_today_actions": [
            u"按赛道整理当前在跟客户清单，优先沟通与当日高频主题（" + u"、".join(global_topic_names[:2]) + u"）相关的客户",
            u"准备「政策解读 + 产品机会 + 成功案例」内容素材，用于客户拜访和投放落地页",
            u"检查广告素材是否显著披露年化利率/风险等级/持牌信息，避免绝对化措辞",
            u"联动运营同事准备相关活动位、内容种草、搜索词包调整计划",
            u"对包含「监管处罚、违约、负面舆情」信号的赛道，建议客户 24-48 小时内暂停敏感投放",
        ],
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
