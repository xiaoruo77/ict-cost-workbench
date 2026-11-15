# 通用成本分析工作台

把业务明细变成**算得出、查得回的成本报告**。同一个核算引擎，通过行业配置确定成本对象、字段和计量口径。导入数据后，按成本对象归集、分摊和计算，结果可以追溯到原始行，报告可以保存为独立修订。

**从 [V1.0.0](https://github.com/xiaoruo77/ict-cost-workbench/tree/v1.0.0-github) 开始：** 仓库自带制药、餐饮、医疗器械、物流四套合成样例。核算和报告导出无需模型 API Key。

## 在工作台里做什么

1. **选行业和数据。** 从「数据接入」载入样例，或按字段映射导入 CSV，确定要分析的成本对象和计量单位。
2. **跑核算并读结果。** 查看成本构成、分摊结果和单位成本；有预算数据时可做预算差异分析。
3. **查依据、存报告。** 从结果回查参与计算的明细行，保存报告后可重开同一修订。支持 Word、JSON 和 Markdown；PDF 导出需安装 Pandoc、XeLaTeX 及 CJK 字体。

**行业目录用来选分类，行业配置用来算账。** 目录覆盖 20 个门类、97 个大类、473 个中类、1,382 个小类。本版附四套可运行样例；新增行业时，按业务定义成本对象、必填数据、计量单位和分摊规则。

## 本机运行

建议新建 Python 环境。依赖锁定以 Linux x86_64 / CPython 3.13 为基准。

```bash
python -m pip install -r requirements.txt
python scripts/setup_industry_catalog.py
python -m uvicorn --app-dir server app:app --host 127.0.0.1 --port 18778
```

打开 [本机工作台](http://127.0.0.1:18778/#home)，用测试账号进入「数据接入」，任选一套合成样例，走一遍核算、追溯和导出。需要指定数据位置时，让 `LLM_WIKI_DATA` 指向独立运行目录。

行业分类脚本从[国家统计局发布页](https://www.stats.gov.cn/sj/tjbz/gjtjbz/202302/t20230213_1902779.html)对应的附件 2 安装数据。已有附件时，可用 `python scripts/setup_industry_catalog.py --from-file <附件路径>` 在本地安装。

## 源码与样例

GitHub 版包含核算代码、合成样例和运行说明。样例用于复现计算流程；旧资料、内部报告模板和本机运行数据留在原工作区。代码来源和依赖见 [DATA_NOTICE.md](DATA_NOTICE.md) 与 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)；其中部分段落描述历史本地环境，以本页列出的 GitHub 版内容为准。
