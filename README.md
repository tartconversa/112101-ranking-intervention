# 推荐治理干预双时间数仓

评估一次内容分发干预**实际影响了哪些内容和用户**、恢复推荐后是否仍沿用有效决定的
本地数仓项目。核心原则：**政策允许的范围、配置计划、真实发生的分发行为是三类
不可互相代替的事实**，且系统必须保留真实发生过的曝光和干预边界，不能根据当前
配置重写历史，也不能把计划投放量视作实际触达。

## 三层事实分离

| 层次 | 表 | 事实归属 | 回答的问题 |
| --- | --- | --- | --- |
| 政策允许范围 | `dim_policy_scope` | 政策团队定义的干预依据、内容范围、生效期限 | 政策**允许**采取什么措施 |
| 配置计划 | `dim_binding` | 运营团队记录的策略版本、渠道授权绑定 | 系统**计划**让哪个策略在哪生效 |
| 真实分发 | `fact_deployment` / `fact_exposure` | 实际投放窗口与曝光遥测 | **真实**发生了哪些投放与曝光 |

- 政策范围不能证明投放发生过；投放记录也不能反推政策授权。两者只做**比对**，
  比对结果以合规缺口（`deployment_binding_gaps`）和逐条曝光合规结论
  （`exposure_scope_compliance`）单独呈现。
- 影响度量只来自 `fact_exposure` 的真实曝光数；部署窗口的计划量不进入影响汇总。

## 双时间模型（不重写历史的关键）

每条策展事实同时携带两类时间：

- **业务时间**：`valid_from/valid_to`、`started_at/ended_at`、`occurred_at`
  —— 事实在业务上何时生效或发生；
- **系统记录时间** `recorded_at`：系统何时获知该事实，**只来自**
  `sources/ingestion_log.csv` 这一只追加事件日志。

`dim_policy_scope` 采用**断言累积**建模：初始断言保留登记时认知的完整业务区间；
处置撤销/收窄不修改旧行，只追加一条业务区间从事件时点起的新断言。查询时先按
业务区间和认知截止时间过滤，再在每个范围中取 `recorded_at` 最大的断言。因此：

- 撤销登记后，撤销**之后**的业务时点结论变为 withdrawn；
- 撤销**之前**的业务时点即使使用最新认知，结论仍是 active（历史不被重写）；
- 认知截止早于撤销登记时，系统只能依据当时持有的 active 断言作答。

曝光事实只追加：回滚失败窗口内的 EXP-33 永久保留；迟到的 EXP-32 在核实事件
出现前始终 `is_verified = 0`，报表在数据血统中显式列出尚未核实的记录。

## 数据来源（本地合成事件）

`sources/` 下全部为虚构 UTF-8 CSV，时间为带时区的 ISO 8601 UTC：

- `policy_scopes.csv` —— 政策范围（当前快照，终态可能是 narrowed/withdrawn）；
- `policy_strategy_bindings.csv` —— 政策版本 × 策略版本 × 渠道的授权绑定；
- `deployments.csv` —— 真实投放窗口与结局（completed/partial/rollback）；
- `exposures.csv` —— 真实曝光遥测（confirmed/late）；
- `ingestion_log.csv` —— 只追加的系统事件日志，提供记录时间、处置变更、
  回滚启动/失败、曝光核对与迟到入库事件。

## 架构

```
sources/*.csv
   │  load_csv_sources()（INSERT OR IGNORE，落地层只追加）
   ▼
raw_*            原始落地层：与源 CSV 一一对应，永不更新/删除
   │  rebuild_curated()（幂等全量推导，可随时重建）
   ▼
dim_*/fact_*/governance_task   双时间策展层 + 治理任务
   │
   ▼
queries.py（时点查询/合规比对/影响/恢复）→ reports.py（中文报表，附数据血统）
```

项目仅使用 Python 标准库（sqlite3、csv、datetime、argparse、unittest），
无需网络或第三方包。

## 使用方式

需要把 `src/` 加入导入路径（或以可编辑方式安装 `pip install -e .`）。

```bash
# 建库：导入原始 CSV 并构建双时间策展层
python -c "import sys; sys.path.insert(0, 'src'); \
  from ranking_facts.cli import main; main(['rebuild'])"

# 端到端演示（三层视图、双时间对比、迟到配置、不可变曝光、合规与恢复报表）
python -c "import sys; sys.path.insert(0, 'src'); \
  from ranking_facts.cli import main; main(['demo'])"
```

历史时点查询（两个时间参数相互独立）：

```bash
# 业务时点 12:30、系统认知截止 13:00 时的政策允许范围
python -m ranking_facts.cli scopes --as-of 2026-05-04T12:30:00Z \
    --known-at 2026-05-04T13:00:00Z        # 需先设置 PYTHONPATH=src

# 截至某系统时点已入库、且已核实的真实曝光
python -m ranking_facts.cli exposures --known-at 2026-05-05T12:00:00Z --verified-only
```

可用报表：`report --name {impact,compliance,recovery,tasks,layers,scopes}`。
每份报表末尾固定附带**数据血统**：结论使用的政策版本、策略版本、实际生效区间
与尚未核实的曝光记录。

## 治理任务（处置变更自动触发核查）

`governance_task` 在每次重建时推导，覆盖五类情形：

1. `config_late` —— 投放启动时系统中没有任何授权配置（DEP-4 先于 B-2 运行约 23 小时）；
2. `exposure_unverified` —— 迟到且未核实的真实曝光（EXP-32）；
3. `partial_delivery_review` —— partial 投放的计划与实际触达缺口（DEP-4）；
4. `recovery_check` —— 回滚失败后的恢复核查（DEP-5，窗口内仍有已核实曝光 EXP-33）；
5. `scope_revision_check` —— 处置撤销/收窄后是否仍有曝光落在旧范围（S-12 核查通过）。

## 测试

```bash
python -m unittest discover -s tests -v
```

- `tests/test_source_files.py` —— 源文件格式、唯一键与事件日志引用完整性；
- `tests/test_time_utils.py` —— UTC 解析与半开区间语义；
- `tests/test_warehouse.py` —— 双时间不重写历史、迟到配置检测、曝光不可变性、
  合规比对、影响汇总、恢复核查与重建幂等性。
