# medlitflow 配置说明

本目录包含 medlitflow 的所有配置文件。这里说明每个文件的作用、怎么改、改动后有什么影响。

---

## 目录里有什么

| 文件 | 位置 | 作用 | 你多久改一次 |
|---|---|---|---|
| `default.yaml` | 程序包内 `config/` | 所有可调参数的默认值 | 很少改 |
| `user.yaml` | exe 同级 `config/` | 你的个性化配置（由「设置」页生成，一般无需手改） | 通过 UI 修改即可 |
| `README.md` | 程序包内 `config/` | 本文件，配置说明 | 不用改 |

`user.yaml` 不存在时视为「全部使用默认值」。保存时只更新你改过的 UI 字段，不会删除 `library` / `archive` / `enrichment` / `screening` / `fallback` / `rate_limit` 等非 UI 段。

`user.yaml` 与 `data/` 同在 `CathBiomed.exe` 同级目录，关闭程序后保留，重启后继续生效。程序保存时会尝试把 `user.yaml` 的文件权限收紧为「仅当前 Windows 用户可读写」；如果收紧失败，程序仍会正常保存，只是同机其他 Windows 用户可能读到该文件。

---

## 第一次使用怎么配

只需要做两件事。

### 第一步：填 API Key（可选，但推荐）

打开「设置」页，填入 NCBI / Semantic Scholar / OpenAlex 的 API Key 与联系邮箱。保存后写入 `config/user.yaml`，不修改 `default.yaml`。

### 第二步：输入检索式

检索式不写在配置文件里，有两种输入方式：

- **UI**：打开「检索」页，在输入框输入一个或多个检索式（多个用分号 `;` 或换行分隔）。
- **CLI**：`cathbiomed --query "lupus AND biomarker"`，或用 `--query-file queries.txt` 指定每行一个检索式的文件。

每次运行会把原始检索式写入 `data/runs/<run_name>/output/run_metadata.json`，供 PRISMA 追溯。

---

## 14 个配置段总表

| 配置段 | 作用 | 高频 |
|---|---|---|
| `project` | 输出目录、run 名、reviewer_id | ✅ |
| `api` | NCBI / S2 / OpenAlex API Key 与联系邮箱 | ✅ |
| `rate_limit` | 各数据源请求间隔（秒） | ⚠️ |
| `dedup` | 去重源优先级、摘要相似度阈值 | ✅ |
| `library` | 文献库根目录与 inbox | ⚠️ |
| `retrieval` | 全文获取、数据源开关、分页上限、付费墙关键词 | ✅ |
| `fallback` | 三级摘要回退开关与批量大小 | ⚠️ |
| `enrichment` | 引用 / JCR / 机构增强 | ✅ |
| `screening` | 保留策略、回收站天数、时间窗口 | ✅ |
| `zotero` | Zotero 库类型、附件模式、观察分类 | ✅ |
| `archive` | 策略快照策略、合并阈值 | ⚠️ |
| `pipeline` | 运行模式、总检索上限 | ✅ |
| `output` | 导出目录、导出模式、子目录模板 | ⚠️ |
| `query_translation` | 检索式跨源翻译、审计开关 | ⚠️ |

---

## 关键配置项说明
### retrieval.max_results_per_query

控制每个数据源每个查询的最大结果数。

| 值 | 含义 |
|---|---|
| 0 | 使用该数据源的 API 硬上限（不额外限制） |
| 100 | 快速探索（只想看 top 100） |
| 1000 | 常规检索（默认） |
| 10000 | 系统综述（接近 PubMed 单次上限） |

各数据源的分页能力与上限：

| 数据源 | 单页上限 | 总量安全上限 | 分页实现 | 用户需要做什么 |
|---|---|---|---|---|
| PubMed | 10,000 | 10,000 | 单次 `esearch retmax`，无 history 分页 | 超过 10,000 需拆分子查询或增量检索 |
| Europe PMC | 1,000 | 10,000 | `cursorMark` 自动分页 | 无需额外操作，达到总量上限自动停止 |
| Crossref | 1,000 | 10,000 | `offset` 自动分页，`offset + rows ≤ 10000` | 无需额外操作，达到上限自动停止 |
| OpenAlex | 200 | 10,000 | `cursor` 自动分页 | 无需额外操作，达到总量上限自动停止 |

**说明**：`retrieval.max_results_per_query` 控制每个源的总量上限。Europe PMC / Crossref / OpenAlex 会自动分页直至达到该值或 API 安全上限；PubMed 单次请求最多 10,000 条，超出需拆分子查询或使用增量检索。

**为什么会有上限？** 无上限的检索会消耗大量 API 配额、磁盘空间和采集时间。一篇文献筛选平均需要 30 秒，10,000 条需要约 83 小时。请根据实际需要设置。

---

## 当前行为说明（配置项与实际执行的差异）

以下配置项在当前版本存在“写入值”与“实际行为”不完全一致的情况，便于排查：

| 配置项 | 写入值 | 实际行为 |
|---|---|---|
| `enrichment.citation.max_records_per_run` | 默认 500 | 普通 pipeline 启动钩子受 500 限制；UI「更新指标」按钮忽略该值，处理全部 include/maybe 文献 |
| `query_translation.unsupported_action` | 默认 `ask` | CLI 模式生效；UI 模式强制非交互，不弹询问，直接按降级执行 |
| `output.export_mode` | 默认 `symlink` | Windows 下自动降级为 `copy` |
| `retrieval.file_format.markdown_converter` | 默认 `false` | v1.5 未实现 Markdown 转换，配置项保留但无功能 |
| `retrieval.max_results_per_query` | 默认 1000 | 作为总量安全上限，与各源 API 上限取较小值 |

---

## 转移与分享

`config/user.yaml` 含 API Key、联系邮箱、reviewer_id、自定义数据目录等个人配置。转移或分享时按场景处理：

| 场景 | 操作 |
|---|---|
| 分享给他人 | 关闭程序 → 删除 `config/user.yaml`（或整个 `config/` 文件夹）→ 再打包分享。接收方首次启动会使用默认配置 |
| 换机器自用 | 复制整个 `CathBiomed` 目录（含 `data/` 和 `config/user.yaml`）。如果 `user.yaml` 中 `project.output_root` 是绝对路径，需在新机器上确认该路径存在，或在设置页重新选择数据目录 |
| 备份 | 保留 `config/user.yaml`，但备份介质应加密；`user.yaml` 内含敏感信息 |

删除 `config/` 文件夹不会影响 `data/` 中的 run、library、archive 和 PRISMA 追溯数据。