# 亚马逊 ABA「热门搜索词」接口说明

> 站点：sellercentral.amazon.com（美国站）｜版本：2026-10-19 实测
> 环境：紫鸟浏览器（agent 模式）+ Chrome DevTools Protocol

---

## 一、怎么拿到可调试的浏览器

紫鸟内核默认不开放调试端口。用官方 CLI 以 **agent 模式**启动店铺环境，会自动开一个本地 CDP 端口：

```bash
# CLI 位置
C:\Users\<用户>\AppData\Roaming\ziniaobrowser\cli\ziniao.exe

# 列出店铺
ziniao.exe browser list --json

# 以 agent 模式启动（storeId 从上面拿）
ziniao.exe browser start --id 26932126933854 --mode agent --json
```

启动后查内核监听的端口：

```powershell
$ids = (Get-Process ziniaobrowser).Id
Get-NetTCPConnection -State Listen | Where-Object { $ids -contains $_.OwningProcess }
```

实测端口是随机的（如 `38979`），验证方式：

```bash
curl http://127.0.0.1:38979/json/version
# 返回 {"Browser":"Chrome/129.0.6668.75", ...} 即为 CDP 端点
```

> 注意：本机 `9222` 是独立 Chrome 的调试端口，与紫鸟无关，别连错。

---

## 二、接口清单

| 用途 | 方法 | 路径 |
|---|---|---|
| 仪表盘清单 + 账户上下文 | POST | `/api/brand-analytics/v1/dashboards` |
| 筛选项 / 列定义 / 可选周 | POST | `/api/brand-analytics/v1/dashboard/top-search-terms/metadata` |
| **榜单数据（核心）** | POST | `/api/brand-analytics/v1/dashboard/top-search-terms/reports` |
| 下载配置项 | POST | `/api/brand-analytics/v1/dashboard/top-search-terms/download/metadata` |
| 创建下载任务 | POST | `/api/brand-analytics/v1/dashboard/top-search-terms/download` |

所有请求都需要：

```
Content-Type: application/json
Accept: application/json
X-Requested-With: XMLHttpRequest
Cookie（登录态）
```

---

## 三、数据接口请求体

```json
{
  "viewId": "top-search-terms-default-view",
  "filterSelections": [
    { "id": "reporting-range", "value": "weekly", "valueType": null },
    { "id": "weekly-week", "value": "2026-10-17", "valueType": "weekly" },
    { "id": "search-term-freeform", "value": "dumbbell", "valueType": null }
  ],
  "reportOperations": [
    {
      "reportId": "top-search-terms-report-table",
      "reportType": "TABLE",
      "pageNumber": 1,
      "pageSize": 100,
      "sortByColumnId": "st-search-frequency",
      "ascending": true
    }
  ],
  "selectedCountries": ["us"],
  "reportId": "top-search-terms-report-table"
}
```

### 关键点：分页藏在 `reportOperations` 里

`reportOperations` 是**数组**（不是对象），每项对应一个报表的操作状态：

| 字段 | 说明 |
|---|---|
| `pageNumber` | 页码，从 1 开始 |
| `pageSize` | 每页条数，只接受 `10 / 25 / 50 / 100` |
| `sortByColumnId` | 排序列，默认 `st-search-frequency` |
| `ascending` | `true` 升序（排名数字小的在前） |

顶层直接写 `pageNumber` / `pageSize` **无效**，服务端会忽略并固定返回第 1 页 25 条。

### 可用筛选项

| filter id | 类型 | 取值 |
|---|---|---|
| `reporting-range` | DROPDOWN | `daily` / `weekly` / `monthly` / `quarterly` |
| `weekly-week` | 周（`valueType: "weekly"`） | 该周周六日期，如 `2026-10-17` |
| `search-term-freeform` | 文本框（模糊匹配） | 任意关键词，如 `dumbbell` |
| `asins` | 文本框 | 最多 100 个 ASIN，逗号分隔 |
| `brand-freeform` | 文本框 | 品牌名 |
| `category-dropdown` | DROPDOWN | 亚马逊分类 ID |

---

## 四、响应结构

```json
{
  "status": 200,
  "partial": false,
  "country": "us",
  "reportsV2": [{
    "type": "TABLE",
    "id": "top-search-terms-report-table",
    "totalItems": 362,
    "pageNumber": 1,
    "pageSize": 100,
    "rows": [
      {
        "st-search-term": "dumbbell set",
        "st-search-frequency": "828",
        "st-top-brand-1": "CAP",
        "st-top-brand-2": "FITPLAM",
        "st-top-brand-3": "SAUVOO",
        "st-top-category-1": "Sports",
        "st-top-category-2": "Major Appliances",
        "st-top-category-3": "",
        "st-top-asin-1": "B00INLFJ4P",
        "st-top-asin-title-1": "CAP 150lb Rubber Hex Dumbbell Set...",
        "st-top-asin-click-share-1": "10.29",
        "st-top-asin-conversion-share-1": "7.14",
        "st-top-asin-2": "...",
        "st-top-asin-3": "..."
      }
    ]
  }]
}
```

字段含义：

- `st-search-frequency`：**搜索频率排名**，数字越小越热门（不是搜索量绝对值）
- `st-top-asin-1/2/3`：点击量前三的 ASIN
- `st-top-asin-click-share-1/2/3`：对应 ASIN 的点击占比（%）
- `st-top-asin-conversion-share-1/2/3`：对应 ASIN 的转化率份额（%）
- `st-top-brand-1/2/3`、`st-top-category-1/2/3`：点击量前三的品牌 / 分类

---

## 五、抓取脚本

`aba_grab.js`：通过 CDP 在页面上下文调接口，支持多关键词 + 自动翻页，导出 CSV。

```bash
# 单个关键词
node aba_grab.js dumbbell

# 多个关键词（结果合并，带"查询词"列）
node aba_grab.js dumbbell "hex dumbbell set" "dumbbell rack"

# 指定周次 / 站点 / 输出文件
WEEK=2026-10-17 COUNTRY=us PAGE_SIZE=100 OUT=结果.csv node aba_grab.js dumbbell
```

输出 24 列：查询词、搜索词、搜索频率排名、前三品牌、前三分类、前三 ASIN / 商品名 / 点击占比 / 转化份额、商品链接。

---

## 六、可视化工作台（本地页面）

工具型界面，不做亚马逊后台复刻。左侧配置、右侧结果表，主线是「筛 → 判 → 标记 → 导出」。

### 启动

```bash
node aba_server.js
# 浏览器打开 http://127.0.0.1:3210
```

### 布局与能力

**左侧配置栏**

| 区域 | 说明 |
|---|---|
| 分析方式 | 按词根 / 按 ASIN 两种模式 |
| 词根 | 支持多个，逗号分隔，结果合并去重 |
| 竞对 ASIN | 最多 100 个，反查这些 ASIN 带来流量的搜索词（SOP 第四步） |
| 报告周期 | 每周 / 每月等，周次下拉 |
| 初筛 | 流量阈值（3w/15w/30w/不限）+ 排除词 + 排除纯规格词 |
| 参考产品 | 标题或 10 位 ASIN（失焦自动抓标题价格）+ 参考价 |
| 开始分析 | 分析后可按开关自动跑前台比对 |

**右侧结果区**

| 区域 | 说明 |
|---|---|
| 统计卡 | 词表总数 / 进入候选 / 各层级数量 / 保留与排除数 |
| 筛选 chips | 全部、各层级、高相关、已保留、已排除、未标记，可组合 |
| 表格 | 勾选 · ABA排名 · 关键词 · 层级 · 购买比 · 垄断 · 相关性 · 修饰词 · 首品牌 · 标记 |
| 行详情 | 点关键词展开：ASIN、类目、点击/转化/前3累计、词数、优先级、备注框、商品缩略图 |
| 标记 | 单行「保留/排除」，支持批量；标记按「词根+周次」存 localStorage，换词根自动切换 |
| 导出词库 | 按 SOP 第八步字段导出 CSV：项目/关键词/ASIN/ABA排名/流量层级/修饰词/购买比/购买型/垄断/点击占比/转化份额/相关性/首品牌/类目/投放优先级/标记/备注 |

### 网络不通时自动兜底

本机到 `sellercentral.amazon.com` 可能连不通（常见于代理环境：`http_proxy` 指向的代理能建 CONNECT 隧道但数据不通，Node 的 fetch 又不读这个环境变量，直连 CloudFront 会超时）。

服务端已内置双通道：

1. 启动后先探测直连（3.5 秒超时）；
2. 不通就自动切到**紫鸟通道**——在紫鸟页面的上下文里发请求，借用浏览器的出网线路和登录态；
3. 直连状态缓存 3 分钟，网络恢复后自动切回。

紫鸟的调试端口是随机分配的，服务端会按 `CDP_PORT` 环境变量 → `.aba_cdp_port` 缓存文件 → `38979` → netstat 扫描 的顺序自动找，并用 `Chrome/129` 版本号排除真实 Chrome。

> 兜底前提：紫鸟用 agent 模式启动，且有一个 `sellercentral.amazon.com` 标签页保持打开。

### 精准大词分析（对齐关键词库 SOP）

点「找精准大词」按钮，拿「搜索词」框里的词根做分析。判据全部取自《如何搭关键词库？——汇总与SOP》2.2.7 第七步：

| 项 | 公式 / 阈值 |
|---|---|
| 流量分层 | 一级词：ABA 搜索排名 3w 以内／二级词：3~15w／三级词：15~30w／四级词：30w 以外（与广告架构话术一致） |
| 购买型词 | 转化份额 ÷ 点击份额 **> 60%** |
| 垄断词 | top1 转化份额 **> 25%**，或前 3 名累计 **> 35%**（慎投） |
| 投放优先级 | P0＝一级 + 购买型 + 非垄断；P1＝一级 + 非垄断；P2＝二级 + 购买型 |

输出内容：

- 词表总数 + 各层数量
- **高频修饰词**：词根之外的词按出现次数排序，直接看出这条赛道的流量方向
- 按 ABA 排名排序的词表（排名 / 搜索词 / 层级 / 购买比 / 垄断 / 首品牌 / P0 标记）
- 品牌词列表（含竞品，作反向词和防御词参考）

#### 初筛：直接排除的词

有些词不用送前台看，直接排除更省事。面板默认开启两道初筛：

| 项 | 说明 | 示例 |
|---|---|---|
| 排除纯规格词（默认开） | 除词根外只有数字 / 重量单位 | `20lb dumbbell`、`35 lb dumbbell`、`50 pound dumbbell` |
| 排除词（默认填 `fixed weight`） | 命中即排除，逗号分隔可加多个 | `fixed weight dumbbell set` |

被排除的词收在「已被初筛排除的词」折叠区里，可复核。这两项都可随时关掉或改。

#### 初筛：直接排除的词

排除词不写死。侧栏输入框手输（逗号分隔，记住上次输入），下面还有**修饰词 chips**：分析后把词表里实际出现的高频修饰词（最多 30 个，`word ×次数`）渲染成可点标签，点一下就加入排除并自动重算（走缓存秒级），再点一下恢复。已排除的 chip 红色高亮。虚词（with/for…）和重量单位（lb/oz…）不进 chips，单复数归并（`sets` 并进 `set`）。

修饰词统计基于**排除前的全量词表**，所以点掉的词不会从 chips 里消失，随时可以反悔。

**规格词不自动排除**：`20lb dumbbell`、`30 lbs dumbbell set of 2` 这类默认进候选（这类词往往转化意图很强）。原始数据视图里会打黄色「规格」标，想排的话在排除词里加 `lb` 即可。

#### 前台相关性校验（对齐 SOP「看搜索结果判断相似度」）

填「参考产品标题」+ 参考价，跑前台比对，系统会把每个词拿到亚马逊前台搜一遍，抓首屏结果后比对（单次最多 20 个词）：

| 维度 | 算法 |
|---|---|
| 分词 | 参考标题小写切词，去 the/and/for/with 等停用词，复数统一去 s（`sets` → `set`） |
| 词头 | 参考标题的**前 2 个实词**（通常就是品名主体，如 `dumbbell set`），结果标题必须全部命中，否则直接不算相似 |
| 核心词 | 参考标题前 3 个实词，算核心覆盖率 |
| 全词覆盖 | 参考标题全部实词在结果标题里的覆盖率 |
| 相似判定 | **词头必中** 且（核心覆盖 ≥67% 或 全词覆盖 ≥60%） |
| 价格 | 与参考价偏差 ≤30% 记满档、≤60% 记半档，只作展示与排序参考，不参与相似判定 |
| 相关度 | 电脑端**横屏一页 48 个自然坑位**（竖屏 16 个，广告位不计），取其中**前 24 个**，相似占比 ≥50% → 高相关 |

> 为什么要有「词头」这道闸：只按全词覆盖率算，`dumbbell stand` / `dumbbell rack` 这类配件词会因为命中了 `dumbbell + adjustable` 被判成相似，直接污染选词。加了词头必中后，`dumbbell set` 判 75%（高相关）、`adjustable dumbbell` 88%（高相关）、`dumbbell stand` 0%、`dumbbell rack` 0%，配件词被干净地筛掉。

#### 比对过程留痕（可回看 / 可导出 / 重启不丢）

每次比对都把**判定依据**完整留下来，而不是只给一个百分比：

- 页面顶部多一个「比对过程」视图：每个词一行，显示参考标题、抓了几张卡片、几个广告、几个自然位、判定样本数、命中数、结论、比对时间。
- 点关键词展开，是**逐位次明细**：位次 / 商品图 / 对比标题 / 价格 / 核心覆盖 / 全词覆盖 / 词头是否命中 / 命中了哪些词 / 缺哪些词 / 判定「相似」还是「不算」。看到某个词判错，能立刻知道是哪条规则卡的。
- 「分析结果」视图里展开某一行，同样嵌着这块过程，并带「重新比对」按钮（强制绕过缓存重抓）。
- 落盘两层：
  - 前端 `localStorage`（键 `aba_rel_<词根>_<周次>_<参考标题前40字>`）——关掉浏览器再打开、重跑同条件分析，过程自动恢复，不用重新搜。
  - 后端 `relevance/<md5>.json`，7 天有效——同样的「词 + 参考标题 + 参考价」直接命中缓存，秒回。
- 「导出比对过程」按钮：一行 = 一个自然位的判定，26 列（含核心/全词覆盖、命中词、缺词、是否计入相似、比对时间），可直接在 Excel 里复核。

**实现方式：直连优先，不打浏览器**

- 直接 `GET https://www.amazon.com/s?k=<keyword>`，解析 HTML 里的 `data-component-type="s-search-result"` 卡片，取 ASIN / 标题 / 价格 / 主图。
- 必须带 `Cookie: i18n-prefs=USD`，否则按本机 IP 返回人民币价格（`CNY 534.80`）。
- 5 个并发，单次超时 18 秒；实测 10 个词约 **8 秒**。
- 直连失败（限流 / 验证码 / 网络）才降级到紫鸟浏览器通道，走 CDP 开标签页解析。
- ASIN 抓商品标题价格同理：`GET /dp/<asin>`，直连优先。

> 之前用浏览器是因为 `sellercentral.amazon.com` 本机连不通，但前台 `www.amazon.com` 是另一个域名，直连是通的，没必要开浏览器。

实现细节：

- 只取排名前 **1000 条**分析，覆盖一级到三级流量，避免全量 80 页拖慢响应；
- 词根变体用「归并连续重复字母」匹配，`dumbbell` / `dumbell` 视为同族；
- 品牌词按 ABA 高频品牌自动识别，单独列出，不进入候选池；
- 点表格里的搜索词可直接回填筛选框并查询详情。

### 导出实操表（对齐《哑铃架-搭建关键词实操》模板）

顶部「导出实操表」按钮，一键生成模板「新版关键词库」格式的 xlsx：

- **元信息块自动预填**：词根＝输入的词根；一级词＝词根 + L1 词，取前 6；竞对ASIN＝词表里 TOP 点击 ASIN 按频次取前 9；竞·品牌词＝点击品牌频次第一（排除 Amazon）；卖点词根/场景属性/偏好属性＝修饰词按 spec/scene/other 分类预填（模板里这块本来就是人工判断，生成后可直接在 Excel 里改）。
- **来源表先计算**：ABA 正向、ABA 反向、卖家精灵正向各自在原始行后展示购买比、前三转化份额累计、购买性判断和垄断性判断，不先跨表汇总关键词，也不跨表拼接份额。卖家精灵反向缺少首位份额时明确显示待补数据。
- **最终明细行后汇总**＝第八步才把当前筛选（或勾选）的词汇总、标准化、去重并形成主记录：更新日期（2026年10月17号 格式）、流量层级（一级：3w以内…四级：30w以外）、卖点/场景/偏好词根包含（按元信息词自动匹配填入）、相关性（前台比对结果）、垄断性TOP3（前 3 转化累计）、购买性（转化份额÷点击份额）、投放优先级（P0-P3）、备注（页面里写的备注）。
- 7 组下拉校验原样保留：流量层级、使用情况、卖点/场景/偏好词根、当前状态、否词列表；冻结首列和表头，细边框、行高 40/27/30 对齐模板。

接口：`POST /api/export-kw`，body 传 `week + meta{root,coreWords,asins,sellWords,sceneWords,preferWords,compBrand} + rows[]`。

### 三方工具：卖家精灵（SOP 第五步）

侧栏「三方工具（卖家精灵）」两个开关（默认关，跑一次查一次点数）：

- **关键词挖掘**：对输入的每个词根跑卖家精灵拓展（`POST /v3/api/keyword-miner`，取前 100 条）；
- **关键词反查**：对混填的 ASIN 跑反查（`POST /v3/api/relation/reversing`，取前 100 条）。

结果与 ABA 词表**合并去重**：

- ABA 里已有的词：保留 ABA 排名，只标记 `ss`（说明这个词在三方数据里也出现）；
- ABA 没有的词：进候选，排名列显示「三方」，流量层级标紫色「三方词」，记录月搜索量。

**实现：纯本地直连**。cookie 用 `node ss_cookie.js` 一次性从主力 Chrome 导出到 `ss_auth.json`（27 条 cookie + UA），之后所有调用都是普通 HTTP 请求，不依赖浏览器。登录态失效时报「卖家精灵登录态失效」，重跑 `ss_cookie.js` 即可。

### 服务端接口

| 路径 | 说明 |
|---|---|
| `GET /` | 页面 |
| `GET /api/bootstrap` | 精简后的筛选项 / 列定义 / 每页条数 |
| `POST /api/reports` | 单页数据，body 用页面的查询对象 |
| `POST /api/analyze` | 按词根/ASIN 拉全量后做分层 + 初筛；`includeRaw:true` 返回带 `status` 的原始行；`week` 必填 |
| `POST /api/check-relevance` | 前台比对，返回 `results[].trail` 完整判定过程；`fresh:true` 绕过缓存 |
| `POST /api/ss/miner` | 卖家精灵关键词挖掘（直连，需 ss_auth.json） |
| `POST /api/ss/reverse` | 卖家精灵 ASIN 反查（直连，需 ss_auth.json） |
| `POST /api/export-kw` | 生成「新版关键词库」格式 xlsx（实操表） |
| `GET /api/product-info?asin=` | 抓 ASIN 的标题 / 价格 / 主图，用于回填参考产品 |
| `POST /api/export` | 拉全量 ABA 词表并直接返回 CSV |

`aba_server.js` 会把页面的扁平查询对象翻译成接口需要的 `filterSelections` + `reportOperations`，前端不用关心底层结构。分析结果按 `词根|ASIN|周次|站点|行数` 缓存在 `cache/<md5>.json`（7 天），改阈值/排除词不用重拉。

---

## 七、纯 Node 直连（不开浏览器）

**实测可行**：只要拿到 Cookie，用普通 HTTP 请求就能直连，不需要 CDP、不需要紫鸟开着、也不需要代理。

### 实测结论

| 项 | 是否需要 |
|---|---|
| `Cookie`（含 httpOnly 的 session-id / at-main / x-main / session-token） | **必需** |
| `anti-csrftoken-a2z` | 不需要（不带也返回 200） |
| `X-Requested-With: XMLHttpRequest` | 带上更稳妥 |
| 特定出口 IP / 代理 | 不需要，本机直连可用 |
| 浏览器 / CDP | 不需要 |

对比验证：CDP 抓取与纯 Node 直连得到的 362 行数据，MD5 指纹完全一致（`188b5de8ca032858`）。

### 用法

```bash
# 1) 一次性导出凭证（需要紫鸟开着且已登录卖家后台）
node aba_cookie.js            # 默认端口 38979
node aba_cookie.js 38979      # 或显式指定 CDP 端口

# 2) 之后随便抓，不用开浏览器
node aba_api.js dumbbell
node aba_api.js "dumbbell set" "dumbbell rack" adjustable
node aba_api.js --week 2026-10-10 --out 上周.csv dumbbell
```

`aba_auth.json` 结构：

```json
{ "cookie": "session-id=...; at-main=...; ...", "csrf": "...", "ua": "...", "exportedAt": "2026-10-19T..." }
```

### Cookie 有效期与续期

- 亚马逊卖家后台的 session 一般能维持数小时到数天；
- 接口返回 401 / 403 或非 JSON 时，脚本会直接提示「登录态失效」，重新跑 `node aba_cookie.js` 即可；
- 若要做定时任务，建议在抓取前先探一次第 1 页（pageSize=10），失败再触发重新导出。

> `aba_auth.json` 等同于登录凭证，不要提交到代码仓库或发给别人。

---

## 八、注意事项

1. **总量上限 10000 条**：`totalItems` 不会超过这个数，是亚马逊的展示上限。
2. **2026 年第 14 周数据口径变更**：官方去掉了机器人流量，跨第 14 周做同比会看到断崖，不是真实需求下滑。
3. **`weekly-week` 取的是周六日期**（该周最后一天），不是周一。
4. 榜单只统计**该周有品牌备案账户**的搜索行为，不等于全站搜索量。
5. 频繁请求可能触发限流，脚本里已内置 300ms 间隔。
