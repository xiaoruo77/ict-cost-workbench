"""Pharma labels, units and source-column conventions; no calculations."""
PLUGIN_ID = "pharma"
PLUGIN_NAME = "药企成本分析"
PLUGIN_VERSION = "1.0.0"
DEFAULT_DATASET_ID = "pharma-sample-v1"
SELF_ENTITY = "中药一厂"
PEER_ENTITY = "中药二厂"
QUANTITY_UNIT = "box"
QUANTITY_LABEL = "盒"
CURRENCY = "CNY"
DIFF_BASE = "一厂"
DIFF_FORMULA = "(二厂 − 一厂) ÷ 一厂 × 100%"
DIFF_NOTE = "差异金额 = 二厂 − 一厂；差异率 = (二厂 − 一厂) ÷ 一厂 × 100%；正数表示二厂更高。一厂为0时差异率不可计算。"
LEGACY_DIRECTIONS = {
    "peer_higher": "二厂高", "self_higher": "一厂高",
    "equal": "持平", "unavailable": "不可比",
}
PRODUCT_SPECS = {
    "银黄口服液": "10ml×10支/盒",
    "板蓝根颗粒": "10g×20袋/盒",
    "六味地黄胶囊": "0.3g×60粒/盒",
}
CATEGORY_COLUMNS = {
    "materials": "直接材料(元/盒)",
    "labor": "直接人工(元/盒)",
    "manufacturing_overhead": "制造费用(元/盒)",
}
CATEGORY_LABELS = {
    "materials": "直接材料",
    "labor": "直接人工",
    "manufacturing_overhead": "制造费用",
}
ACTUAL_FILES = {
    ("中药一厂", 2026): "csv/cost_data/中药一厂_成本汇总_2026年1-6月.csv",
    ("中药一厂", 2025): "csv/cost_data/中药一厂_成本汇总_2025年1-6月.csv",
    ("中药二厂", 2026): "csv/cost_data/中药二厂_成本汇总_2026年1-6月.csv",
    ("中药二厂", 2025): "csv/cost_data/中药二厂_成本汇总_2025年1-6月.csv",
}
BUDGET_FILES = {
    ("中药一厂", 2026): "csv/cost_data/中药一厂_预算数据_2026年.csv",
}
