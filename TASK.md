# 历史已交付归档 (Archived / Completed)

## [REQ-01] 商品词生成优先一个单词，最多两个单词

- owner: Codex
- business_dept: Amazon 关键词研究
- requester: 项目方
- priority: P1
- due_date: 2026-11-09
- current_phase: 已交付
- delivered_date: 2026-11-09
- tags: 商品词, 生成提示词

### 任务清单

- [x] 更新候选生成提示词，优先一个单词，准确表达需要时使用两个，禁止三个及以上单词（0.1d）
- [x] 保留完整商品类别含义，禁止机械截断或拼接规避长度限制（0.1d）
- [x] 在服务器使用实际 qwen3.7-flash 模型实测：dumbbell rack、weight rack，均两个单词（0.1d）
- [x] AST 对比确认仅生成提示词改变，验证、评分、选择和历史契约保持原样（0.1d）
- [x] 保留原镜像与源码备份，后端切换后健康检查及源码哈希通过（0.1d）

新规则用于后续新生成的候选。历史候选及缓存不自动覆盖。发布记录：outputs/product-term-short-20261109。


## [REQ-02] Agent 商品详情三路并发采集

- owner: Codex
- business_dept: Amazon 关键词研究
- requester: 项目方
- priority: P1
- due_date: 2026-11-09
- current_phase: 已交付
- delivered_date: 2026-11-09
- tags: Agent, 商品采集, 三路并发

### 任务清单

- [x] Agent 正式批量入口最多三路，连接与页面在各自线程内创建和使用（0.1d）
- [x] 移除独立商品页全局读取锁，以 targetId 保护用户已有标签（0.1d）
- [x] 原顺序返回、逐条持久进度、失败隔离，保留原校验与评分（0.1d）
- [x] 12 项定向测试本机及服务器隔离验证通过（0.1d）
- [x] 源码真实读取峰值 3，三个 ASIN 成功；安装包正式接口同时三个标签，三条成功（0.1d）
- [x] 本机安装 1.4.34，发布两处安装包并保留回退备份，业务服务无需重启（0.1d）

发布证据：outputs/agent-parallel-20261109/release-1.4.34。未重跑八步全流程。


## [REQ-03] 自动竞对合并候选超过 500 条时仍按价格及相关度筛选

- owner: Codex
- business_dept: Amazon 关键词研究
- requester: 项目方
- priority: P1
- due_date: 2026-11-09
- current_phase: 已交付
- delivered_date: 2026-11-09
- tags: 竞对, 自动筛选, 候选上限

### 任务清单

- [x] 定位本次 4 张报表、1341 行、727 个去重 ASIN 在价格预筛前触发 500 上限（0.1d）
- [x] 仅移除自动多词合并上限，保留人工 500、30 天均价 ±15%、相关度 >50、最终 100 上限（0.1d）
- [x] 3 项定向回归通过本机及服务器隔离验证（0.1d）
- [x] 本次真实原件只读重放，排除本品后 66 个价格合格候选进入详情阶段；未调用浏览器和模型、未写数据库（0.1d）
- [x] 空闲时仅更新后端，保留原镜像和源码回退备份，健康及代码哈希核验通过（0.1d）

记录：outputs/competitor-limit-20261109。未重新执行整条工作流，历史失败记录仍保留。
