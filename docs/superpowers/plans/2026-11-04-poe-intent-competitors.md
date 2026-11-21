# 商机探测器购买意图竞对筛选实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 executing-plans 逐任务实现此计划；所有行为变更严格执行 TDD。

**目标：** 将第三步方法三改为“核心词搜索购买意图 → 唯一意图选择 → 最近 30 天商品下载 → 均价预筛 → 四维相关度筛选”。

**架构：** Agent 新增购买意图候选采集和按稳定意图引用下载两个动作；服务器模型只选择候选，不操作浏览器。竞对服务先解析并按 30 天均价筛选，再仅对通过项读取详情和评分，最后持久化每个核心词的意图与数量证据。

**技术栈：** Python 3.12、FastAPI、Pydantic、Playwright、httpx、Vue 3、TypeScript、pytest、Node 测试。

**规格：** `docs/superpowers/specs/2026-11-04-poe-intent-competitors-design.md`

## 全局约束

- 每个核心大词只选一个购买意图；低匹配时仍选相对最接近项。
- 最近 30 天平均售价在参考价 ±15% 内（含边界）才进入详情与模型分析。
- 四维综合相关度严格大于 50，等于 50 不通过。
- 价格不可靠的商品不得进入相似度模型。
- 保留 Agent Token、操作 ID、幂等重放、Session 恢复和 ABA `rank_limit`。
- 不修改服务器；不提交或推送 Git。

## 审查重点（Review Focus）

- 购买意图模型返回候选之外的引用时必须拒绝，不能静默选第一项。
- 最近 30 天字段缺失时必须拒绝整份自动文件，不能降级到 90/360 天。
- 边界价格 ±15% 通过，相关度 50 不通过、50.01 通过。
- 同一 ASIN 跨核心词只分析一次，同时保留全部来源证据。
- 任一核心词失败时不能设置 `confirmed_at`，也不能把部分结果交给第四步。

---

### 任务 1：领域契约、30 天 CSV 与购买意图选择

**文件：**
- 修改：`backend/app/domain/competitors.py`
- 修改：`backend/app/adapters/competitor_csv.py`
- 修改：`backend/app/services/qwen.py`
- 测试：`backend/tests/test_poe_downloads.py`
- 测试：`backend/tests/test_qwen.py`

- [ ] 编写失败测试：30 天均价解析为 `average_30d`，自动方法拒绝只有 90/360 天均价的文件。
- [ ] 编写失败测试：购买意图选择必须返回候选内唯一引用、理由和置信度；低匹配也不得为空。
- [ ] 运行定向测试并确认因新契约和方法缺失而失败。
- [ ] 实现最小领域模型、解析模式和 Qwen 选择方法。
- [ ] 运行定向测试确认通过。

### 任务 2：紫鸟 Agent 候选采集、30 天下载与远程协议

**文件：**
- 修改：`backend/app/adapters/poe_downloads.py`
- 修改：`backend/agent/runtime.py`
- 修改：`backend/agent/ops.py`
- 修改：`backend/agent/main.py`
- 修改：`backend/app/adapters/remote_browser.py`
- 测试：`backend/tests/test_poe_downloads.py`
- 测试：`backend/tests/test_agent_auth.py`
- 测试：`backend/tests/test_remote_browser.py`

- [ ] 编写失败测试：候选提取只接受 Seller Central 详情引用并保留热门词和指标。
- [ ] 编写失败测试：下载必须携带已选意图引用、确认“最近 30 天”，返回业务文件名和原件。
- [ ] 编写失败测试：远程请求保留 Bearer Token、操作 ID，并完整传输候选与文件。
- [ ] 运行定向测试确认失败原因是接口缺失。
- [ ] 实现 Agent 与远程协议的最小改动。
- [ ] 运行定向测试确认通过。

### 任务 3：价格优先的自动筛选和完整批次门控

**文件：**
- 修改：`backend/app/services/competitors.py`
- 修改：`backend/app/api/v1/competitors.py`
- 测试：`backend/tests/test_poe_downloads.py`
- 测试：`backend/tests/test_competitors.py`
- 测试：`backend/tests/test_workflow_auto.py`

- [ ] 编写失败测试：每核心词先选意图再下载；价格不合格项不调用详情或模型。
- [ ] 编写失败测试：±15% 边界通过；币种/价格缺失排除；相关度 50 排除、50.01 通过。
- [ ] 编写失败测试：跨核心词 ASIN 去重且来源合并；单词失败不确认整批；最多 100 个稳定排序。
- [ ] 运行定向测试确认失败。
- [ ] 实现价格预筛、缓存、证据统计、完整门控和业务下载文件名。
- [ ] 运行定向测试确认通过。

### 任务 4：前端状态、文档与回归验证

**文件：**
- 修改：`frontend/src/api/competitors.ts`
- 修改：`frontend/src/components/CompetitorsPanel.vue`
- 修改：`docs/竞对ASIN筛选.md`
- 测试：`frontend/tests/competitors-intent-workflow.cjs`

- [ ] 编写失败测试：方法三区域展示所选意图、理由、置信度、文件和三段数量。
- [ ] 运行前端测试确认失败。
- [ ] 扩展运行时校验和页面状态文案，不改变方法一、方法二。
- [ ] 运行前端测试和构建确认通过。
- [ ] 运行后端完整测试、前端完整测试和格式/类型检查，记录所有失败。

