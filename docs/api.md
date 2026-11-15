# 分析服务接口

资料服务在 8765 端口提供 `/releases`、`/releases/term-a` 和版本内的表分页。表页携带 revision；客户端必须读取同一清单版本并验证摘要，不能合并不同版本的页。

应用服务在 8766 端口提供 `GET /catalog`、`POST /analyses`、`GET /analyses`、`GET /analyses/{id}`。当前目录登记 overview 课程资料汇总与 support_fairness 支持分流公平性分析。分析实现登记在 `learning.domain.ANALYSES`，函数接收完整 tables 和 parameters，返回可序列化对象。

创建请求字段为 request_key、release、analysis 和可选的 parameters 对象。request_key 是调用方的幂等编号：相同完整请求返回同一分析，相同编号不同内容返回 409。分析返回 id、request、revision 和 result。不同 request_key 是不同留存记录。缺少字段、未知分析类型或无效资料返回 422；查询不存在的记录返回 404。JSON 不允许 NaN 或 Infinity，无法估计的量使用 null 并在结果中说明原因。

保存后的分析内容不因资料更新而重算。新分析可在 result 内保存统计对象、参数、诊断及所需的版本化结构；不可覆盖其他分析或跳过资料完整性校验。数据库只保存本地分析，不是资料服务的业务真源。

## support_fairness

`support_fairness` 用实际抽查机制评估现有困难评分在年龄组、上网条件组及交叉群体中的误差，并比较有限名额下的候选分流规则。结果只形成可复核建议，不修改原评分，也不改变任何人申请辅导的资格。

可选 parameters：

- `splits`：`selection`、`estimation`、`validation` 各自的入组批次数组，三者必须互不重叠且覆盖全部批次。默认 `{"selection":[0,1],"estimation":[2],"validation":[3]}`。
- `requirements`：公平要求阈值，键为 `min_verified_help_rate`、`min_age_selection_ratio`、`min_access_selection_ratio`、`max_unreviewed_fraction`、`min_design_coverage`、`min_capacity_utilization`。
- `rules`：候选规则数组，`family` 取 `score`、`threshold`、`reserve`、`priority`、`quota`；省略时使用内置候选集。
- `score_threshold`（默认 0.5）、`min_group_size`（默认 20）、`min_random_audits`（默认 10）、`min_design_audits`（默认 5）。

结果要点：

- `sampling` 与 `design_check`：随机复核、转介复核、未复核人数；登记入样概率与实际抽样框覆盖是否一致。
- `existing_score_error.by_role`：按 selection、estimation、validation 角色分别给出群体误差，每个角色只用自己批次的资料。每个群体含 AUC、加权帮助率、混淆矩阵与 `estimable`。无入样概率已知随机复核的群体为 `estimable=false`，数值为 null，绝不补成“不需要帮助”。
- `rules`：每条候选规则在 selection 批次的选中人数、校区名额、设计覆盖、帮助率区间、群体选择率与逐项达标状态。
- `feasibility`：是否存在同时达标的规则；否则给出每项要求的全候选集最好可达值、达到它的规则及剩余缺口，并区分单项不可达与联合不可行。
- `estimation`、`validation`：推荐规则在 estimation 与 validation 批次的独立结果；验证不参与选择或调参。
- `recommendation`：推荐规则与验证批次的可复核名单。没有规则同时达标时状态为 `candidate_only`；完全没有设计证据时不选规则、不生成个人名单。

帮助率以部分识别区间表达：`lower` 把未复核者视为不需要帮助，`upper` 把未复核者视为需要帮助，转介标签计入端点但不进入设计估计。设计检查不一致时，登记概率不能当作跨群体代表性依据。
