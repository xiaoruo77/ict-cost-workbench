# 通用成本分析工作台 V1.0.0 · GitHub 版

此私有仓库保存冻结 V1 的去敏副本。原冻结提交为 `8cd781a175875f6853788493c3d06d7dfeb14f85`，这里只记录其身份，不继承原 Git 历史。保留工作台、统一核算和合成样例；不附旧赛题原件、内部报告模板、真实资料、账户数据、本机验收日志或旧交付 ZIP。

## 运行

使用独立 Python 环境安装依赖。原 `requirements.lock` 为 Linux x86_64 / CPython 3.13 的部署版本记录，不代表各系统均已测试。Word 导出由 python-docx 生成；PDF 还需 Pandoc、XeLaTeX 与可用的 CJK 字体。核算、合成样例和报告导出不需要配置模型 API Key。

```bash
python -m pip install -r requirements.txt
python scripts/setup_industry_catalog.py
python -m uvicorn --app-dir server app:app --host 127.0.0.1 --port 18778
```

浏览器打开 `http://127.0.0.1:18778/#home`。用独立账号在“数据接入”载入制药、餐饮、医疗器械或物流合成样例；运行核算、核对源行、保存报告后可重新打开同一修订。可用 `LLM_WIKI_DATA` 指定新的独立运行目录；不要复用正式数据目录。

行业分类从[国家统计局发布页](https://www.stats.gov.cn/sj/tjbz/gjtjbz/202302/t20230213_1902779.html)附件 2 下载，不随仓库再分发。可自行下载并运行 `python scripts/setup_industry_catalog.py --from-file <已下载的附件>` 安装校验；分类共 20 个门类、97 个大类、473 个中类、1,382 个小类，并不代表每个行业已有完整核算模板。

旧药企资料、固定图表和旧专题生成需要在独立环境取得合法授权的本地原资料及模板，不属于本仓库的演示或验收范围。旧工作区部署脚本和整机环境不随仓库发布。来源与依赖见 [DATA_NOTICE.md](DATA_NOTICE.md) 和 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
