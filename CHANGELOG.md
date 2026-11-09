# Changelog / 版本变动

## v0.13.0 — 2026-11-09 — Executable criteria / 可执行评测规范


### English

- Optional mature jsonschema adapter: a metered proposal sees training inputs only,
  freezes a bounded structural specification and coverage/gaps for the search tree,
  then records actual per-output passes/violations. Child edits inherit the same
  criteria. Invalid proposals, unknown charges and interrupted attempts persist;
  no silent regeneration or external-reference/regex/code execution.
- Structure checks are explicitly unreviewed. They do not establish semantic quality,
  override label/judge rankings or unlock unsupported final delivery. A no-label
  Chinese task exercised 42 executions and inherited the same schema across an
  automatic edit; its final quality gate correctly remained closed.
- 157 Python tests with optional dependencies, six Node tests; base checks explicitly
  skip 11 optional-engine tests. Exact versions, wheel hashes and upstream MIT texts
  were reviewed before install. No runtime binaries are distributed.
- New bilingual brand, FAQ, use cases, objective related-work comparison, citation,
  community templates and reusable media drafts. No discovery/ranking gains claimed;
  no social posts were sent. Companion sts-skills v0.1.0 is separately downloadable.
- Nov-9 official DeepSeek pricing review retains existing conservative rates and all
  historical usage. Local serving remains a developer preview, not production hosting.

### 简体中文

- 可选 jsonschema 成熟校验器接入：付费标准生成只看训练输入，冻结整树的结构规范及
  覆盖/遗漏，再逐条记录真实通过与错误。子迭代继承同一规范；无效提案、未知费用和中断
  均留痕，不偷偷重生成。禁止外部引用、正则或代码执行。
- 结构规范仍未审阅，不证明语义质量，不覆盖标签/裁判排序，也不绕过最终交付门禁。
  无标签中文任务实际完成 42 次执行与一次自动修改，规范哈希保持不变，最终质量门禁
  正确保持关闭。
- 安装可选依赖后通过 157 项 Python、6 项前端测试；基础环境明确跳过 11 项引擎测试。
  五项依赖版本、包哈希及 MIT 许可在安装前核验，不随源码分发二进制。
- 中英品牌、FAQ、场景、客观相关项目比较、引用规范、社区模板和可复用传播草稿齐备；
  不宣称检索排名收益，未向社交账号发帖。配套 sts-skills v0.1.0 已单独提供下载。
- 重新核验 Nov-9 官方价格，保守费率未变、历史用量不清零。服务仍为本机开发预览。

## v0.12.0 — 2026-11-08 — Bounded tool use / 受限工具调用


### English

- Experimental agent.tools.v1 competes with simpler candidates. It defines frozen
  Rules/Python tools and dynamically chooses tools/arguments from observations.
- At most four metered decisions, including final output; cancellation before
  submission, explicit exhaustion, full call/container receipts, no hidden retries.
  No network/shell/file/subagent permissions or generated host execution.
- English/Chinese views show selected tools, observations and final actions. Serving
  reuses independent profiles, usage budgets and saved request IDs.
- 138 Python and five Node tests passed locally. Real synthetic Chinese acceptance
  exercised two tools; the agent failed output-shape labels even after one edit.
  Simpler Python won, passed 4/4 final labels and six independent private/ZIP HTTP
  cases. Agent serving/final coverage is explicitly protocol-only. No general agent
  superiority, business accuracy or optimization gain is claimed.
- GitHub hosted CI remains account-billing blocked; local checks are separate.
  No new application dependency or runtime image distribution.

### 简体中文

- 实验性 agent.tools.v1 与简单方案竞争：生成冻结的规则/Python 工具，按输入与工具
  观察动态选择工具及参数。
- 最多四次付费决策，含最终回答；提交前可取消，达到上限明确停止，保留完整调用/容器
  证据，不隐藏重试。无网络、Shell、文件、子代理权限，生成代码不进入宿主执行。
- 中英界面显示所选工具、观察与最终回答。服务复用独立凭据、预算和请求编号防重放。
- 本机通过 138 项 Python、5 项前端测试。真实合成中文任务执行了两类工具；Agent 经一次
  修改仍未满足输出结构。更简单的 Python 胜出，最终标签一致数 4/4，私有包与 ZIP 各通过
  六条独立 HTTP 用例。Agent 服务/最终检查仅有明确的协议测试，不宣称普遍优势、业务
  准确率或优化收益。
- GitHub 托管 CI 仍受账户计费限制，本机验收单列。不新增应用依赖或分发运行环境镜像。

## v0.11.0 — 2026-11-08 — Independent model services / 独立模型服务


### English

Model-backed Prompt, Hybrid and bounded workflows gain once-only final checks and
independent source delivery. Services use their own owner-only model profile and
persistent usage ledger; no key or positive authorization is embedded in a bundle.
Paid requests require IDs: successful repeats return cached results, while failed/
interrupted requests are fenced instead of automatically replayed. Delegated service
allowances remain committed in the original root/global project budget. Unknown
charges survive restarts. English/Chinese controls explain these budget boundaries.

This is still loopback developer serving, without encrypted production vault,
public tenancy or cloud hosting. A service usage DB is required; the STS experiment
DB/controller is not. Runtime image requirements and third-party notices still apply.
Validation: 127 Python and five Node tests passed in both the workspace and exact
public source tree. A small synthetic Chinese workflow passed 4/4 final labels;
private and ZIP services each passed six HTTP cases and a new request after restart,
with 14 real model calls each. This is serving evidence, not business accuracy.
Hosted CI cannot start because of the GitHub account billing lock; this preview was
validated locally. Hosted tag-release automation remains configured but blocked.

### 简体中文

Prompt、混合与有界模型工作流新增一次性最终检查和独立源码交付。服务使用独立的本机
模型配置与持久化用量账本，包内不含密钥或正数预算授权。付费请求必须提供编号：成功
的重复请求返回缓存，失败/中断的请求不会自动重放。独立服务划拨额度继续占用原项目
整树/全局预算，未知费用在重启后保留；中英界面明确显示这些边界。

当前仍是本机开发服务，未实现生产加密保险库、公开多租户或云托管。服务需要独立用量
数据库，但不依赖 STS 实验数据库或控制进程。仍须满足运行环境与第三方许可条件。
工作区与实际公开源码均通过 127 项 Python、5 项前端测试。小型合成中文工作流最终标签
一致数为 4/4；私有包与 ZIP 各通过 6 条 HTTP 用例和重启后的新请求，各有 14 次真实
模型调用。这是服务链路证据，不是业务准确率。GitHub 账户计费锁导致托管 CI 无法启动，
本次预览经过本机验证；标签自动发布已配置，待账户限制解除后才能运行。

## v0.10.0 — 2026-11-08 — Engineering preview / 工程预览

### English

- Initial public source preview: samples and natural-language outcome → real
  cross-family candidate generation, evaluation, bounded evolution and local delivery.
- Rules/Prompt/Hybrid, isolated Python, train-only numeric scikit-learn and bounded
  model-backed workflows; all generated code/model loading stays container-only.
- Complete invocation/step call attribution, persistent root/global CNY ceilings,
  unknown-charge retention and no automatic replay.
- English/Chinese Studio, readable step outputs, costs, errors and raw evidence.
- Apache-2.0, Chenyuan Guo / 郭辰远 attribution, pinned dependency provenance,
  separate acceptance evidence and scoped source-export checks.
- Limitations: model-backed standalone serving, general agents/harness, mature
  optimizers, calibrated evaluation and production hosting/security remain incomplete.
  Synthetic label matches and judge proxies are not verified business accuracy.

### 简体中文

- 首次公开源码预览：样本与自然语言目标 → 真实跨方案生成、评测、有界迭代和本机交付。
- 支持规则/Prompt/混合、隔离 Python、仅用训练集拟合的数值分类器及有界模型工作流；
  生成代码和模型加载始终留在隔离容器中。
- 完整的步骤/调用费用归因、持续保留的整树/全局预算、未知费用预留和禁止自动重放。
- 中英双语界面，可读的步骤输出、费用、错误与原始证据。
- Apache-2.0、郭辰远（Chenyuan Guo）署名、锁定依赖来源、分阶段验收与源码导出检查。
- 边界：含模型调用方案的独立交付、通用 Agent/Harness、成熟优化器、可信评价和生产托管
  仍未完成。合成样本标签一致性与裁判代理分不代表真实业务准确率。
