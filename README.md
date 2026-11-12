<!-- ============================================================ -->
<!-- CathBiomed v1.5 · 根 README.md                              -->
<!-- ============================================================ -->

<div align="center">

**[简体中文](README.md)** · [English](#english-version-planned)

![Version](https://img.shields.io/badge/version-1.5.1-blue)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-lightgrey)
![License](https://img.shields.io/badge/license-Apache--2.0-blue)

</div>

# CathBiomed

以用户为主导的生物医学科研文献工作台。单机运行，数据不出本机；覆盖**采集 → 去重 → 全文获取 → 筛选 → 归档 → Zotero 同步**全流程，输出可追溯的审计链。

---

## 核心特性

| 模块 | 能力 | 说明 |
|---|---|---|
| **五源采集** | PubMed / PMC / Europe PMC / Crossref / OpenAlex | NCBI E-utilities 分页、Europe PMC cursorMark、Crossref offset、OpenAlex cursor；统一 Schema，单源内去重 |
| **泛读初筛（scan）** | 独立轻量流程，不下载全文，2–5 分钟完成 | 固定滚动近 5 年（`[dp]` 已含则跳过）；只取元数据 + 摘要 + JCR；不跑 S2/OpenAlex；结果写 `data/library/scan_cache/`，不写 library、不进 PRISMA；只保留最近一次 |
| **级联去重** | DOI → PMID → PMCID → 标准化标题 | union-find 合并；摘要变体完整保留（single / identical / near_identical / divergent） |
| **三级全文回退** | 本地 raw 缓存 → PubMed EFetch → Europe PMC | 逐级降级，`fallback_level` 落盘；摘要超 100 KB 自动截断 |
| **付费墙识别** | 用户关键词 + 启发式评分 | IMRaD 章节缺失 / 体积异常 / 特征词缺失 → `paywall` / `paywall_suspect` 分类 |
| **用户主导筛选** | I / E / M / U 快捷键批量决策 | 草稿 5 秒自动保存，决策优先级合并（include > maybe > exclude > unscreened） |
| **Zotero 双向** | 推送 + 拉取 + 双向验证 | 支持 user / group library；`zotero_links.parquet` 记录同步链 |
| **增强层** | 引用数（S2）+ JCR 影响因子 + 机构（OpenAlex） | S2 → OpenAlex → PubMed 三源探测降级；本地 JCR 表 + 模糊匹配 |
| **归档与版本** | 策略快照 3 档（metadata_only / per_run / full） | date_range / query / filter 变更自动递增版本号 |
| **审计链** | HTTP / pipeline / screening / archive / abandon | 5 类 JSONL / CSV 日志，逐条可追溯 |

---

## 差异化特点

| 维度 | CathBiomed | 典型同类工具 |
|---|---|---|
| 采集能力 | ✅ 五源统一采集 + 三级全文回退 | ❌ Rayyan / Covidence 仅做筛选，不采集 |
| 泛读模式 | ✅ 独立轻量初筛，2–5 分钟完成，不下载全文 | ❌ 多数同类工具不区分快速初筛与精读 |
| 运行方式 | ✅ 单机本地，无云端账号，数据不出本机 | ❌ Covidence 云端托管，数据上传厂商 |
| 筛选模式 | ✅ 用户主导，工具只负责「精准抵达」 | ❌ 自动化黑箱排序，决策依据不可追溯 |
| 审计链 | ✅ 可追溯审计链 + 5 类审计日志 | ⚠️ 部分工具仅记录最终决策，不记录过程 |
| Zotero 集成 | ✅ 双向同步（推送 + 拉取 + 失效验证） | ❌ 多数工具需手动导出 RIS/BibTeX |
| 期刊指标 | ✅ JCR 影响因子 + 分区 + 引用数一体 | ⚠️ 需另装插件或手动查询 |
| 空间管理 | ✅ 3 档保留策略 + 回收站 + 空间估算 | ⚠️ 多数工具无归档压缩 |

---

## 能解决什么问题

| 场景 | 痛点 | CathBiomed 的解法 |
|---|---|---|
| 系统综述检索 | 5 个数据库分别检索，手动合并去重 1000+ 条 | 一次输入检索式，五源统一采集，级联去重 |
| 快速初筛 | 不确定检索式宽窄，想快速看有没有相关文献 | 泛读模式 2–5 分钟出结果，不下载全文，不占用 library |
| 全文获取 | 付费墙 / 失效链接导致大量文献无全文 | 三级回退 + 付费墙识别 + 二次追链 |
| 筛选记录 | PRISMA 流程图靠人工整理，易出错 | 决策自动记录 + 筛选审计 + 一键导出报告 |
| 多人复核 | 双人决策冲突无法追溯 | `decision_1 / decision_2 / conflict_status` 字段（v1.6 完整启用） |
| 空间管理 | 全文堆积占满磁盘 | 3 档保留策略 + 回收站 30 天 + 空间估算 |
| Zotero 往返 | 手动导出导入，链路断裂 | 双向同步 + 失效检测 + 一键撤回 |

---

## 系统要求

| 项 | 要求 |
|---|---|
| 操作系统 | Windows 10 22H2+ / Windows 11 23H2+（Linux / macOS 未验证） |
| Zotero | 6+ 桌面端，需开启「设置 → 高级 → 允许此计算机上的其他应用程序与 Zotero 通信」 |
| Python | 3.11+（仅开发者需要；终端用户用 exe） |
| 磁盘 | 建议 ≥ 20 GB（全文 PDF 占主要空间） |
| 网络 | 首次启动需联网探测数据源可用性 |

---

## Zotero 配置

CathBiomed 通过 Zotero 本地 API（`http://localhost:23119`）通信，不需要 Zotero 账号，数据不出本机。

1. 安装并启动 Zotero 6+ 桌面端
2. Zotero → 编辑 → 设置 → 高级 → 勾选「允许此计算机上的其他应用程序与 Zotero 通信」
3. CathBiomed「设置 → Zotero 连接」：个人库填 `user` / 库 ID `0`；群组库填 `group` / 群组 ID
4. 「Zotero」页点「刷新」，顶部显示绿点即连接成功
5. 如需「可引入」功能，点「⚙ 配置」勾选要扫描的 Zotero 分类

Zotero 未启动或未开启本地 API 时，采集和筛选不受影响，仅 Zotero 页显示「未运行」。

---

## 快速开始

### 终端用户（exe）

1. 从 [GitHub Releases](https://github.com/DrMikeSun/cathbiomed/releases) 下载 `CathBiomed_v1.5.zip`
2. 解压到**用户可写目录**（如 `D:\CathBiomed`）。**不要解压到 `C:\Program Files\`**——数据目录 `data/` 与用户配置 `config/user.yaml` 均位于 exe 同级，Program Files 下无写权限会导致首次启动或保存设置失败
3. 启动 Zotero 6+，确认本地 API `http://localhost:23119` 可访问
4. 双击 `CathBiomed.exe`，首启动自动创建 `data/` 目录
5. 在「设置」页填写 API Key（可选，但推荐；有 Key 时 S2 / NCBI / OpenAlex 限流更宽松）

### 开发者（源码）

```cmd
git clone https://github.com/DrMikeSun/cathbiomed.git
cd cathbiomed
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

---

## 5 分钟上手

| 步骤 | 页面 | 操作 |
|---|---|---|
| 1 | **检索** | 输入检索式（多个用 `;` 或换行分隔）→ 选模式（精读 / 泛读）→ 开始运行 |
| 1.1 | **文献库 → 扫描结果** | 泛读完成后，第三 Tab「🔍 扫描结果」查看命中列表（策略 / 标题 / 作者 / 期刊 / 年份 / IF / 来源） |
| 1.2 | **采集任务 → 已完成** | 扫描条目底部 3 按钮：查看报告（打开 `scan_summary.md`）/ 再次检索（跳检索页精读）/ 删除（清空缓存） |
| 2 | **筛选中心** | 左侧选策略 → 按 `I` / `E` / `M` / `U` 批量决策 → 保存决策 |
| 3 | **筛选中心** | 决策完整后 → 确认归档 → 文件移动到 `library/{included,excluded,maybe,unscreened}/` |
| 4 | **文献库** | 查看已归档文献 → 右键标记噪声 / 数据源删除 / 彻底删除 |
| 5 | **Zotero** | 刷新 → 同步所选 → 推送到 Zotero |

---

## 配置

所有参数在 `config/default.yaml`，用户覆盖写入 `config/user.yaml`（UI 设置页自动维护，与 `data/` 同在 exe 同级）。

**详细参数手册见 [`config/README.md`](config/README.md)**——高频配置项说明与调参建议。

---

## JCR 影响因子表

JCR 表用于补全期刊影响因子和分区。程序包 `reference/` 目录附带默认表 `JCR IF 2025.default.xlsx`，可直接使用。

如需更新或替换：

1. 准备 `JCR IF 2025.xlsx`，列名需含 `Journal Name`、`Abbreviation`、`Index`、`Major Subject`、`Impact Factor`、`Q category`
2. 放到 `CathBiomed.exe` 同级的 `reference/`。程序优先读 `JCR IF 2025.xlsx`，其次读 `JCR IF 2025.default.xlsx`
3. 「采集任务 → 已完成」选中策略 → 点「更新指标」

JCR 表缺失或期刊名匹配失败时，影响因子和分区显示为「—」，采集和筛选不受影响。年份在 `config/default.yaml` 的 `enrichment.journal_metrics.active_year` 调整。

---

## 升级

v1.5 是首个开源版本，后续小版本升级按以下两种方式任选，均不会丢失数据。

**方式 A：原地覆盖（推荐）**

1. 关闭 CathBiomed
2. 下载新版本 zip，解压到临时目录
3. 将临时目录中的程序文件复制到旧版本安装目录，覆盖同名文件
4. **不要删除** `data/` 目录和 `config/user.yaml`——所有 run、library、archive、Zotero 链接均在 `data/` 下，`config/user.yaml` 保存用户配置
5. 启动新版本

**方式 B：新目录 + 迁移数据**

1. 关闭 CathBiomed
2. 下载新版本 zip，解压到新目录（如 `D:\CathBiomed_v1.6`）
3. 将旧目录的 `data/` 和 `config/user.yaml` 复制到新目录
4. 启动新目录中的 `CathBiomed.exe`

**v1.5 → v1.5.1 升级说明**：

- 无数据结构变更，现有 library / archive / Zotero 链接完全兼容，无需迁移
- 新采集的策略（v1.5.1 起）的 `query_execution.json` 自动写入 `original_pubmed_query`，可用于「再次检索」
- v1.5.0 及之前采集的旧策略无此字段，点击「再次检索」仅日志提示（见「已知限制」第 13 条）
- `config/default.yaml` 中 `pipeline.scan_max_hits` 默认值 100 → 1000（**仅用于 intensive 超阈值截断**，scan 模式不读）
- 如需回滚，保留旧 zip 覆盖回去即可

---

## 转移

| 场景 | 操作 |
|---|---|
| 分享给他人 | 关闭程序 → 删除 `config/user.yaml`（或整个 `config/` 文件夹）→ 再打包分享。接收方首次启动使用默认配置 |
| 换机器自用 | 复制整个 `CathBiomed` 目录（含 `data/` 和 `config/user.yaml`）。若 `user.yaml` 中 `project.output_root` 为绝对路径，需在新机器上确认该路径存在，或在设置页重新选择数据目录 |
| 备份 | 保留 `config/user.yaml`，但备份介质应加密；该文件含 API Key 等敏感信息 |

`config/user.yaml` 含 API Key、联系邮箱、reviewer_id、自定义数据目录等个人配置，**分享前务必删除**。

---

## 项目结构

```text
cathbiomed/
├── src/medlitflow/        后端 19 模块
│   ├── collectors.py      五源采集
│   ├── dedup.py           级联去重
│   ├── retrieval.py       全文获取 + 付费墙识别
│   ├── fallback.py        三级摘要回退
│   ├── enrichment.py      引用 / JCR / 机构增强
│   ├── screening.py       筛选 + 文件生命周期
│   ├── archive.py         策略归档 + 版本管理
│   ├── scan_cache.py      scan 结果缓存 + 报告生成
│   ├── pipeline.py        6 阶段调度
│   ├── robustness.py      启动体检 + 锁 + 中断
│   └── ...
├── ui/                    PySide6 UI 层
│   ├── views/             8 视图（仪表盘 / 检索 / 文献库 / 筛选 / Zotero / 任务 / 日志 / 设置）
│   ├── controllers/       8 控制器
│   ├── components/        30+ 组件
│   ├── bridge/            QThread Worker + Interactor
│   ├── theme/             QSS + Palette
│   ├── animations/        动效
│   └── models/            表格模型
├── config/                default.yaml + README.md
├── reference/             JCR 表（用户自备）
├── main.py                启动入口
├── pyproject.toml         项目元数据
└── requirements.txt       依赖
```

---

## 已知限制（v1.5.1）

| # | 限制 | 影响 | 计划 |
|---|---|---|---|
| 1 | 智能检索未实现 | 「智能设计」按钮禁用 | v1.6（`smart_index_requested` 信号已预埋） |
| 2 | 归档同步执行 | 10–30 秒 UI 冻结（有 WaitCursor） | v1.6 异步化 |
| 3 | 双人复核未完整启用 | `decision_1/2` 字段已存在但 UI 未接入 | v1.6 |
| 4 | 单查询硬上限 | PubMed 单次 10k；Europe PMC / Crossref / OpenAlex 已实现自动分页，总量安全上限 10k | 拆分子查询或增量检索 |
| 5 | 仅 Windows 验证 | Linux / macOS 未测试 | 待社区反馈 |
| 6 | 排除原因未细分 | `prisma_report.md` 中排除原因仅为「未分类」 | 正式系统评价需人工补充排除原因 |
| 7 | PMC 命中数可能高于 PubMed | 同一检索式下 PMC 含更多预印本、会议摘要、病例报告 | 数据源覆盖差异，非检索错误；可关闭 PMC 通道 |
| 8 | Zotero 校验全部失败时中止 | 不写报告、不改 `zotero_links.parquet` | 保护机制，需先启动 Zotero 桌面端 |
| 9 | Zotero 与 pipeline 不并发 | 两者同时运行可能产生 `records_final.parquet` 写入竞态 | 当前请避免同时运行 |
| 10 | Zotero 导入个别文献可能不进入指定分类 | 个别文献可能落在 Zotero 默认分类而非观察分类 | 待排查；可在 Zotero 中手动归类 |
| 11 | 取消后各阶段中断响应时间不一 | 个别阶段需等待当前网络请求结束；个别阶段运行时间偏长；不推荐频繁中止 | v1.6 细化检查点 |
| 12 | 不同显示尺寸下部分内容可能重叠或显示不全 | 小窗口或高 DPI 下表格 / 卡片布局可能挤压 | v1.6 适配 |
| 13 | 旧策略「再次检索」无视觉反馈 | v1.5.0 及之前采集的策略，`query_execution.json` 无 `original_pubmed_query`；点击「再次检索」后仅日志页显示 `[WARN] 无原始检索式（该策略为旧版本采集），请手动输入`，不跳检索页 | 手动输入检索式；v1.5.1 起新采集的策略无此问题 |

---

## 常见问题

**Q：启动后闪退 / 无法创建 `data` 目录？**
A：检查是否解压到 `C:\Program Files\` 等无写权限目录；请解压到 `D:\CathBiomed` 等用户可写目录。

**Q：Zotero 页显示「未运行」？**
A：确认 Zotero 桌面端已启动且已勾选「允许此计算机上的其他应用程序与 Zotero 通信」；Zotero 刚启动时需等 10–20 秒再点「刷新」。

**Q：全文下载大量失败？**
A：付费墙、失效链接、出版商反爬均可能导致失败。查看 `data/runs/<run_name>/output/retrieval_log.csv` 的 `status_code` 与 `retrieval_reason`；`paywall` / `paywall_suspect` 属预期，`broken_link` 可稍后重试。

**Q：影响因子全是「—」？**
A：可能原因：
1. `reference/JCR IF 2025.xlsx` 或 `JCR IF 2025.default.xlsx` 被删除或未放置；
2. 期刊名在 JCR 表中匹配失败（可尝试降低 `enrichment.journal_metrics.fuzzy_match_threshold`，或检查期刊名拼写）；
3. `active_year` 与表文件名年份不一致。

详见「JCR 影响因子表」。

**Q：运行日志在哪里？**
A：`data/runs/<run_name>/audit/pipeline_audit.jsonl`（阶段审计）、`audit/http_audit.jsonl`（HTTP 审计）、`output/summary_report.md`（运行摘要）。UI「日志」页可实时查看。

**Q：如何卸载？**
A：关闭程序，删除整个 `CathBiomed` 目录即可；程序不写注册表、不写系统目录，`data/` 与 `config/user.yaml` 都在安装目录内。

**Q：升级后旧数据还能用吗？**
A：可以。两种方式任选：
- 原地覆盖：把新版本程序文件复制到旧安装目录，覆盖同名文件，保留 `data/` 和 `config/user.yaml` 不动；
- 新目录迁移：新版本解压到新目录，把旧目录的 `data/` 和 `config/user.yaml` 复制过去。

当前 v1.5 无数据结构变更，无需额外迁移步骤。详见「升级」。

---

## 未来路线

| 版本 | 内容 | 解决什么问题 |
|---|---|---|
| **v1.6** | 智能检索（四层架构 + LLM 辅助设计检索式 + 3 层幻觉防护） | 降低设计高质量检索式的门槛 |

---

## 文档

| 文档 | 内容 |
|---|---|
| [`config/README.md`](config/README.md) | 配置参数手册 |

---

## 联系与支持

欢迎通过以下渠道反馈 Bug、提出建议、讨论需求或合作：

- **邮箱**：`mikesunjade@foxmail.com`
- **GitHub Issues**：[提交 Issue](https://github.com/DrMikeSun/cathbiomed/issues)
- **B 站**：`一笑烦恼消`

反馈 Bug 时可附日志或 `data/runs/<run_name>/audit/pipeline_audit.jsonl` 数行，便于定位。

---

## 开源版与闭源定制版

CathBiomed 采用双轨模式：

| 版本 | 定位 | 功能 | 支持 |
|---|---|---|---|
| **开源版**（本仓库） | 完整基础版，持续迭代 | 五源采集、去重、全文获取、筛选、归档、Zotero 同步、审计链 | 社区支持（GitHub Issues） |
| **闭源定制版** | 面向个人研究者、课题组、实验室 | 在开源版基础上增加高级功能、按需开发、优先支持 | 作者直接支持 |

开源版采用 Apache 2.0 许可证，可自由使用、修改、分发，包括商业用途。闭源定制版由作者提供增值服务，按实际需求开发。如需了解详情，请通过上方邮箱联系。

---

## 赞助

如果 CathBiomed 对你的科研工作有帮助，欢迎赞助支持项目持续迭代。赞助完全自愿，不影响开源版的使用。

| 方式 | 说明 |
|---|---|
| **支付宝** | 扫码赞助；备注可写 `CathBiomed 支持` + 简短需求/idea 关键词（备注约 50 字以内） |
| **微信** | 扫码赞助（赞赏码）；留言可写简短需求/idea |

<div align="center">

| 支付宝 | 微信赞赏 |
|---|---|
| ![支付宝](assets/sponsor/alipay.png) | ![微信赞赏](assets/sponsor/wechat.png) |

</div>

说明：
- 需求优先通过 GitHub Issues 社媒或邮箱讨论；
- 如需闭源定制版或合作，请通过上方邮箱联系，不要通过赞助备注讨论复杂需求；

---

## 贡献

- **Bug 报告**：反馈 Bug 时可附日志或 `data/runs/<run_name>/audit/pipeline_audit.jsonl` 数行，便于定位。
- **功能建议**：请说明「解决什么问题」而非「加什么按钮」
- **Pull Request**：提交前跑 `ruff check src/ ui/`；后端改动请说明影响范围
- **英文文档**：欢迎提供 README 英文版 PR（见顶部语言切换）

---

## 许可证

本项目采用 [Apache License 2.0](LICENSE)。Copyright 2026 DrMikeSun。

完整条款见 [LICENSE](LICENSE)；第三方依赖许可证归属见 [NOTICE](NOTICE)。

---

<div align="center">

**CathBiomed v1.5.1** · 2026-11-12

[回到顶部](#cathbiomed) · [配置手册](config/README.md)

</div>

---

## English Version (planned)

English README is not yet available. Contributions are welcome — please open a PR with `README.en.md` following the same structure. For now, the core concepts are:

- **Five-source collection**: PubMed / PMC / Europe PMC / Crossref / OpenAlex
- **Cascading dedup**: DOI → PMID → PMCID → normalized title
- **Three-tier fulltext fallback**: local raw cache → PubMed EFetch → Europe PMC
- **User-driven screening**: I/E/M/U shortcuts with 5-second draft autosave
- **Zotero bidirectional sync**: push + pull + link verification
- **Full audit chain**: HTTP / pipeline / screening / archive / abandon logs

<!-- ============================================================ -->
<!-- README 结束                                                  -->
<!-- ============================================================ -->