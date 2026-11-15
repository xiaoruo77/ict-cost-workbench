# 餐饮隔离合成验收操作 v1

本文件只说明本次声明范围，完整字段及未覆盖项见field_dictionary.md。仅用合成账号和独立验收端口。正式8765不变更。

1. 以同一候选源码运行 `python -B fixtures/industry_triad/generate.py` 复现三个最小样例；加 `--large` 生成各自large子目录的大样本。
2. 同一登录会话上传本目录finance.csv、shared.csv、events.csv、raw_materials.csv、settlements.csv、stock.csv及rules.json。文件键分别为finance/shared/events/raw_materials/settlements/stock。CSV保留原始凭证和数量。
3. 运行2026-11 actual，核对发生成本、成本组、对象、预算差异、资产/损益分区、缺项notice和原始行。中间bridge及shared中心余额应清零。
4. 保存analysis_id/revision，展开财务与事件来源，重开同一修订。JSON/Markdown/Word/PDF应包含同一身份、金额和规则边界。
5. 对照expected.json独立预期；运行 `python -B -m unittest discover -s tests -p test_industry_triad.py -v`。真实HTTP和Edge操作证据另记，单元测试不能替代。
6. 大样本与失败样例分别留证；同会话顺序制药→餐饮→医疗器械→制药，重开最早制药版本核对行业/金额/来源/导出。

大样本seed=20261114073，六组不同物量倍率及单位成本倍率、40份变额原始凭证；包含不同对象、成本组、现金排除、退回和损耗，不是复制同一行。预期仅用整数分与Fraction，完全不导入成本引擎。

财务发生成本不是当期利润成本：production部分拆在制/成品/净COGS/报废；period直接期间费用；service为实际服务费用。现金备查不计成本。行业完整性始终受声明边界限制，不宣称全行业已覆盖。
