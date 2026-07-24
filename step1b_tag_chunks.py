#!/usr/bin/env python3
"""为 chunks 新增 topic 和 entities 字段。

Topic 枚举（稳定，每个 chunk ≤3 个）：
  地理概况     — 地理位置、地形、DEM、土地覆盖
  区划指标     — 区划因子、分级阈值、适宜性等级
  灾害风险     — 旱涝/冷害/霜冻/病虫害风险等级
  品质评价     — 品质指标（蛋白质、容重、湿面筋等）
  品种资源     — 作物品种名称、主栽/示范品种
  越冬条件     — 越冬期、积雪、冻害、极端低温
  气象要素     — 气温/降水/积温/日照/辐射/蒸发
  生育期       — 发育阶段、物候期、播种-成熟
  方法与算法   — 公式、模型、统计方法、数据来源
  产量分级     — 产量区划、光温生产潜力、等级

Entities 原则：
  - 不重复 metadata 已有字段（省份/作物）
  - chunk 中具体出现的可检索术语，用于克服语义鸿沟
  - 例：topic=品种资源 → entities=[新冬18号, 新冬22号, ...]
  - 每个 chunk ≤5 个 entities

用法：python3 step1b_tag_chunks.py
"""

import json
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CHUNKS_PATH = BASE_DIR / "data" / "chunks.json"
OUT_PATH = CHUNKS_PATH

# ── Topic 判定规则 ──────────────────────────────────────────────

def _check_geo(text: str) -> bool:
    kw = ['东经', '北纬', '东西长', '南北宽', '总面积', 'km2', '地处', '地形', '地貌',
          '海拔', 'DEM', '土地覆盖', '土地利用', '平原', '丘陵', '盆地', '山区',
          '位于.*边陲', '欧亚大陆']
    return any(re.search(k, text) for k in kw)

def _check_variety(text: str) -> bool:
    kw = ['品种', '新冬\\d+', '九圣', '金石农', '石冬', '新粮', '主栽品种', '示范品种',
          '中强筋', '超高产品种', '品种类型']
    return any(re.search(k, text) for k in kw)

def _check_winter(text: str) -> bool:
    kw = ['越冬', '积雪', '冻害', '安全越冬', '积雪深度', '越冬期', '极端最低气温',
          '冻不坏', '抗寒', '耐寒']
    return any(re.search(k, text) for k in kw)

def _check_indicator(text: str) -> bool:
    kw = ['区划因子', '区划指标', '分级标准', '分级阈值', '适宜区', '次适宜', '不适宜',
          '适宜性', '指标体系', '等级划分', '阈值', '区划指数', '种植区划',
          '最适宜', '不可种植']
    return any(re.search(k, text) for k in kw)

def _check_method(text: str) -> bool:
    kw = ['公式', '回归', '统计方法', '权重', '模型', '数据来源', '数据处理',
          '普查', '气象观测站', '多元回归', '层次分析', 'AHP', 'GIS', '归一化',
          '标准化', '指数', '计算.*方法']
    return any(re.search(k, text) for k in kw)

def _check_disaster(text: str) -> bool:
    kw = ['风险区划', '灾害', '渍涝', '干旱', '冷害', '霜冻', '病虫害',
          '条锈病', '赤霉病', '蚜虫', '干热风', '风险指数', '风险等级',
          '致灾因子', '承灾体', '防灾减灾', '孕灾环境']
    return any(re.search(k, text) for k in kw)

def _check_weather(text: str) -> bool:
    kw = ['气温', '降水量', '积温', '日照', '辐射', '蒸发', '风速', '相对湿度',
          '平均气温', '活动积温', '有效积温', '无霜期', '生长季', '年降水']
    return any(re.search(k, text) for k in kw)

def _check_growth(text: str) -> bool:
    kw = ['生育期', '发育期', '物候', '播种', '出苗', '开花', '成熟', '鼓粒',
          '黄熟', '拔节', '抽穗', '灌浆', '发育阶段', '生长期', '生物学下限']
    return any(re.search(k, text) for k in kw)

def _check_quality(text: str) -> bool:
    kw = ['品质', '蛋白质', '湿面筋', '容重', '稳定时间', '品质指标', '品质区划',
          '品质评分', '中强筋', '强筋', '弱筋']
    return any(re.search(k, text) for k in kw)

def _check_yield(text: str) -> bool:
    kw = ['产量', '光温生产潜力', '单产', '公斤', '产量区划', '产量分级',
          '产量等级', '生产力']
    return any(re.search(k, text) for k in kw)

TOPIC_CHECKS = [
    ('地理概况', _check_geo),
    ('品种资源', _check_variety),
    ('越冬条件', _check_winter),
    ('区划指标', _check_indicator),
    ('方法与算法', _check_method),
    ('灾害风险', _check_disaster),
    ('气象要素', _check_weather),
    ('生育期', _check_growth),
    ('品质评价', _check_quality),
    ('产量分级', _check_yield),
]

def tag_topics(text: str) -> list[str]:
    topics = []
    for name, check in TOPIC_CHECKS:
        if check(text):
            topics.append(name)
    return topics[:3] or ['(未分类)']


# ── Entity 提取规则（按 topic 驱动，提取具体可检索术语）─────────

# 品种名称正则：匹配 新冬18号、九圣D1508、金石农1号、石冬0358 等
_VARIETY_RE = re.compile(
    r'(?:新冬\d+号?|九圣\w+|金石农\d+号?|石冬\d+号?|新粮\d+号?|石冬\d+号?)'
)

# 地理特征术语
_GEO_ENTITY_RE = re.compile(
    r'(?:东经|北纬|经度|纬度|海拔|DEM|数字高程|平原|丘陵|盆地|山区|高原|'
    r'土地覆盖|土地利用|地貌|欧亚大陆|西北边陲)'
)

# 区划指标名称
_INDICATOR_ENTITY_RE = re.compile(
    r'(?:活动积温|有效积温|稳定≥?10℃.*?积温|≥?10℃.*?积温|'
    r'5～?9\s*月.*?降水|生长季.*?降水|年降水量|'
    r'平均气温|极端最低气温|最高气温|最低气温|'
    r'最适宜区|适宜区|次适宜区|不适宜区|'
    r'日照时数|太阳辐射|光合有效辐射|'
    r'无霜期|≥0℃积温|活动积温|'
    r'土壤质地|土壤类型|坡度|坡向)'
)

# 方法与算法名称
_METHOD_ENTITY_RE = re.compile(
    r'(?:AHP|GIS|ET0|NDVI|DEM|层次分析[法]?|'
    r'多元回归|逐步回归|归一化|标准化|加权求和|'
    r'主成分分析|聚类分析|模糊数学|专家打分|'
    r'Penman.*?公式|FAO.*?公式|'
    r'Thornthwaite|Hargreaves|'
    r'作物系数|Kc|'
    r'克里金插值|反距离权重|空间插值)'
)

# 灾害类型
_DISASTER_ENTITY_RE = re.compile(
    r'(?:干旱|渍涝|冷害|霜冻|冻害|冰雹|大风|干热风|'
    r'条锈病|赤霉病|蚜虫|螟虫|稻飞虱|'
    r'致灾因子危险性|承灾体暴露度|承灾体脆弱性|孕灾环境|防灾减灾能力|'
    r'风险指数|风险等级|轻[度]?风险|中[度]?风险|重[度]?风险)'
)

# 气象要素名称
_WEATHER_ENTITY_RE = re.compile(
    r'(?:≥?10℃活动积温|≥?0℃积温|有效积温|活动积温|'
    r'年降水量|生长季降水|日照时数|太阳辐射|光合有效辐射|'
    r'年均?气温|极端最低气温|极端最高气温|'
    r'无霜期|初霜日|终霜日|'
    r'相对湿度|平均风速|蒸发量)'
)

# 生育期名称
_GROWTH_ENTITY_RE = re.compile(
    r'(?:播种期|出苗期|三叶期|分枝期|开花期|结荚期|鼓粒期|成熟期|'
    r'拔节期|抽穗期|灌浆期|黄熟期|乳熟期|'
    r'越冬期|返青期|分蘖期|'
    r'生物学下限温度|有效积温|'
    r'播种.*?出苗|出苗.*?开花|开花.*?成熟|'
    r'营养生长期|生殖生长期)'
)

# 品质指标
_QUALITY_ENTITY_RE = re.compile(
    r'(?:蛋白质|湿面筋|容重|稳定时间|'
    r'沉降值|降落值|'
    r'强筋|中强筋|弱筋|'
    r'粗蛋白|氨基酸|含油量|糖度|酸度|'
    r'出粉率|出米率)'
)

# 产量相关
_YIELD_ENTITY_RE = re.compile(
    r'(?:光温生产潜力|气候生产潜力|产量潜力|'
    r'kg/?ha|kg/?hm²|公斤/?亩|'
    r'单产|总产|生产力|'
    r'高产[区]?|中产[区]?|低产[区]?)'
)

# 越冬条件
_WINTER_ENTITY_RE = re.compile(
    r'(?:越冬期|安全越冬|积雪深度|积雪覆盖|'
    r'极端最低气温|越冬条件|冻害|'
    r'抗寒性|越冬死亡率|返青率)'
)

TOPIC_ENTITY_RES = {
    '地理概况': _GEO_ENTITY_RE,
    '品种资源': _VARIETY_RE,
    '越冬条件': _WINTER_ENTITY_RE,
    '区划指标': _INDICATOR_ENTITY_RE,
    '方法与算法': _METHOD_ENTITY_RE,
    '灾害风险': _DISASTER_ENTITY_RE,
    '气象要素': _WEATHER_ENTITY_RE,
    '生育期': _GROWTH_ENTITY_RE,
    '品质评价': _QUALITY_ENTITY_RE,
    '产量分级': _YIELD_ENTITY_RE,
}

# 全局兜底：非常见但重要的专业缩写/符号
_UNIVERSAL_RE = re.compile(
    r'(?:DEM|NDVI|GIS|AHP|ET0|Kc|FCV|'
    r'≥\d+℃|≥\d+mm|≥\d+cm|'
    r'℃·d|mm·d|hm²|km²|m/s)'
)


def tag_entities(topics: list[str], content: str) -> list[str]:
    entities = []
    seen = set()

    for topic in topics:
        pattern = TOPIC_ENTITY_RES.get(topic)
        if not pattern:
            continue
        for m in pattern.finditer(content):
            term = m.group(0)
            if term not in seen:
                seen.add(term)
                entities.append(term)
            if len(entities) >= 5:
                return entities[:5]

    # 兜底：通用专业术语
    for m in _UNIVERSAL_RE.finditer(content):
        term = m.group(0)
        if term not in seen:
            seen.add(term)
            entities.append(term)
        if len(entities) >= 5:
            break

    return entities[:5]


# ── 主流程 ───────────────────────────────────────────────────────

def main():
    with open(CHUNKS_PATH, encoding="utf-8") as f:
        chunks = json.load(f)

    n_updated = 0
    topic_stats = {}
    empty_entities = 0
    for c in chunks:
        m = c.setdefault("metadata", {})
        text = (m.get("section_title", "") + " "
                + " ".join(m.get("heading_path", [])) + " "
                + c["content"])
        topics = tag_topics(text)
        m["topic"] = topics
        m["entities"] = tag_entities(topics, text)
        if not m["entities"]:
            empty_entities += 1

        for t in topics:
            topic_stats[t] = topic_stats.get(t, 0) + 1
        n_updated += 1

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(chunks, f, ensure_ascii=False, indent=2)

    print(f"已更新 {n_updated} 个 chunk → {OUT_PATH}")
    print(f"无 entity 的 chunk: {empty_entities}")
    print(f"\nTopic 分布:")
    for k, v in sorted(topic_stats.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
