# 代码来源与第三方组件说明

本文件列出实际使用方向和许可核对入口，不给整个既有项目作统一授权结论。原项目源码、继承模板与本次新增改造需要分别追溯；未发现足以证明上游完整授权范围的材料时，不能添加“全部原创”或全仓库 Apache/MIT 声明。当前未执行公共仓库、镜像或资料发布。

## 1. 版本证据

Python 直接及传递依赖以 [requirements.lock](requirements.lock) 为准；安装是否一致以同一镜像中的 `pip check` 和包清单为准。旧文档手写版本不是当前锁定结果。系统工具和字体以 Dockerfile、镜像软件包清单及对应发行包的版权文件为准。入口：[Dockerfile.ict](Dockerfile.ict)、[前端 index](server/static/index.html)。

## 2. 主要依赖和来源

| 组件 / 生态 | 本项目用途 | 官方来源或许可入口 |
|---|---|---|
| FastAPI | HTTP 应用 | [项目 LICENSE（MIT）](https://github.com/fastapi/fastapi/blob/master/LICENSE) |
| Starlette、Uvicorn、HTTPX、Pydantic | ASGI、服务与请求/数据处理 | [Starlette](https://github.com/Kludex/starlette)、[Uvicorn](https://github.com/Kludex/uvicorn)、[HTTPX](https://github.com/encode/httpx)、[Pydantic](https://github.com/pydantic/pydantic) 的实际版本 LICENSE |
| LangChain / LangGraph / langchain-openai | 可选模型客户端、工具与 Agent 编排 | [LangChain](https://github.com/langchain-ai/langchain)、[LangGraph](https://github.com/langchain-ai/langgraph) 的实际分发包 LICENSE |
| python-docx、openpyxl、xlrd | Word、电子表格读写 | 对应锁定分发包中的 LICENSE 与项目元数据 |
| pandas、NumPy、Matplotlib | 表格处理与图表 | [pandas](https://github.com/pandas-dev/pandas)、[NumPy](https://github.com/numpy/numpy)、[Matplotlib](https://github.com/matplotlib/matplotlib) 的实际版本许可文件 |
| PyMuPDF / MuPDF | PDF 提取与检查 | [官方 License and Copyright](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright) |
| React / React DOM | 本地前端界面 | [React 官方 LICENSE（MIT）](https://github.com/react/react/blob/main/LICENSE)；保留对应 vendor 文件中的版权和许可声明 |
| htm | 前端模板绑定 | [官方项目](https://github.com/developit/htm) 的实际版本 LICENSE |
| Apache ECharts / zrender | 前端图表 | [ECharts](https://github.com/apache/echarts)、[zrender](https://github.com/ecomfe/zrender) 对应分发版本的 LICENSE/NOTICE |
| Pandoc、TeX Live / XeLaTeX、CJK 字体 | PDF 排版 | [Pandoc](https://github.com/jgm/pandoc)、[TeX Live copying](https://www.tug.org/texlive/copying.html) 及镜像包内版权清单 |

本表没有逐项宣称传递依赖已完成再分发审核，也没有把未核验版本的许可证名称写成已确认结论。部署或分发版本改变后应重新保存实际组件清单。

## 3. PyMuPDF 的许可边界

2026-11-12 核对的官方说明列出开源 AGPL 与商业许可两种路径，并要求按完整 AGPL 条款检查实际使用场景。不能把其影响简化为“只有修改并分发 PyMuPDF 自身才需要关注”，也不能因这里只做 PDF 文本提取就推断自动豁免。[官方说明](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright)

当前文件没有取得商业许可的证据，也不对实际服务/分发方案作法律结论。需要公开、分发或商业部署时，应核对对应版本完整条款和实际方案，并保存授权或替换实现的证据。其余组件同样应以真实分发包和上游许可文件为准。

## 4. 当前实现与第三方的边界

V2 Decimal 核算、成本组件流转、严格 CSV 映射及个人快照是当前代码可核对的改造。Agent 编排和模型请求依赖第三方框架；检索当前使用本地 BM25，不应继续把关闭的外部 embedding、向量存储或 RRF 路径作为已启用创新点。

继承来的检索、知识、报告和界面代码仍保留其上游来源；有代码差异不等于拥有整个原项目的版权。前端本地资源应保留原有声明，不修改作者署名。Windows 系统字体的本地使用也不等于可以重新打包公开分发这些字体。
