# Solve2Ship FAQ / 常见问题

[Home](../README.md) · [中文入口](../README.zh-CN.md) · [Usage](USAGE.md) · [Related work](COMPARISONS.md)

## What is Solve2Ship?

Solve2Ship (STS) is an open-source AI-native solution engineering project exploring **Goal-to-Service**. A user provides input samples, a natural-language expected outcome and optional constraints. The system proposes executable approaches, evaluates them on shared cases, makes bounded revisions and packages supported winners as independent local services.

中文：STS 从样本与自然语言目标出发，探索实现方案、真实评测并交付本机服务。完整产品仍在建设中，当前能力以版本说明为准。

## Who is it for?

The preview is for developers and researchers investigating which approach fits a measurable task, and business users collaborating with someone who can configure local resources. The companion [sts-skills](https://github.com/runzhiyyds/sts-skills) is being prepared for natural-language use through local agents. It has its own installation and behavioral limits; the web preview should not be described as a finished no-code production product.

中文：当前适合开发者、研究者及有环境配置支持的业务用户。配套技能项目面向本地 Agent 使用方式，不能把“自然语言输入”理解为所有技术准备都已自动解决。

## What does “cross-paradigm” mean?

The candidates can use different methods: rules, deterministic code, trained ML, prompts, hybrids, workflows or bounded tool-use agents. The current registry implements those families within explicit limits. General multi-agent systems and harnesses remain planned. Candidate names and IDs are extensible; a candidate is not necessarily an agent.

中文：不同技术范式共同竞争，规则和脚本也可以胜出。通用多 Agent 与 Harness 尚未完成。

## How is this different from a workflow builder or prompt optimizer?

STS starts with the desired outcome and explores how to achieve it. A workflow or prompt is one possible candidate. Existing workflow builders, optimizers and ML libraries can be useful implementation components when an adapter and its resource/license review are available. The [related-work comparison](COMPARISONS.md) separates this product direction from demonstrated capabilities and avoids unsupported performance rankings.

中文：工作流、Prompt 和 ML 库都可以是实现材料，用户无需先给出初始工作流。差异化是跨技术路线的选型与交付目标，不是声称其他项目不能做相关事情。

## Are labels required? Can the system infer business truth from a goal?

Labels are optional for starting exploration. When labels exist, the preview reports agreement with those provided labels and does not certify them. Without labels, model-judge scores are uncalibrated proxies. Independent final delivery currently requires supported quality checks; a free-text goal does not automatically provide trustworthy ground truth. The v0.13.0 preview adds bounded structural checks with explicit unreviewed provenance. v0.14 adds self-attested output reviews and an explicit once-only final review path. Descriptive judge agreement is not calibrated pruning; authenticated truth, broad semantic checks and general executable criteria remain unfinished.

中文：没有标签可以开始探索，但自然语言目标不会自动变成真值。没有可靠评价时，不能把裁判分包装为准确率，也不能跳过最终质量门禁。

## Does an agent always win? Does STS guarantee self-improvement?

No. A real synthetic acceptance task retained an agent's incorrect output shape after an edit and selected Python instead. Iteration can fail or regress; previous artifacts and evidence remain available. “Self-improving AI” is a research direction, not a guarantee of monotonic gains or a global optimum. See the [observed cases](USE_CASES.md#observed-acceptance-cases).

中文：不会预设 Agent 更好，也不保证修改有收益。保留失败和退化是评测的一部分。

## What is actually delivered?

A supported candidate is frozen, checked once against sealed final inputs and independently tested as a local HTTP source bundle. Code/ML services require the reviewed Docker runtime. Model-backed services additionally require their own credentials, persistent usage state and explicit budget. They do not require the STS experiment database or controller. Production cloud hosting, public authentication and multi-tenancy are not accepted capabilities.

中文：交付物应可独立调用，不只是 Prompt、流程图或分数。它仍有明确的运行环境和凭据要求，不代表已完成云部署。

## How are generated code and spending controlled?

Generated Python/model loading stays in accepted resource-limited Docker containers, with no host-execution fallback. Model requests reserve budget before submission. Root/global ceilings persist, unknown charges remain reserved and paid operations are not automatically replayed. Model-service request IDs return saved successful results and fence failed/interrupted operations. Token ceilings exclude compute and hosting charges. Review [security](SECURITY.md) and [usage](USAGE.md) before execution.

中文：代码隔离、累计预算和请求防重放分别控制执行与费用风险；它们不是对恶意本机所有者的防篡改保证，也不等于生产安全认证。

## Does this integrate AIDE, AutoGluon, LangGraph, Dify or other frameworks?

The accepted release does not claim those integrations. It has its own bounded adapter registry, a scikit-learn adapter and reviewed Docker execution. Related projects are compared or considered as potential components, not silently presented as installed dependencies or tested adapters. New adoption requires exact version, license, provenance and runtime acceptance.

中文：讨论或列出一个开源项目，不等于已经接入。是否可用请查看实际依赖与版本说明。

## How should I cite or share STS?

Use [CITATION.cff](../CITATION.cff), include the version/commit and preserve license notices. Link to the [project definition](../README.md), [case scope](USE_CASES.md) and [comparison sources](COMPARISONS.md) when explaining claims. The [media kit](MEDIA_KIT.md) provides reusable copy and a diagram. No search ranking, AI-engine citation or endorsement is guaranteed.

中文：引用具体版本和能力范围，传播时保留证据限制；本项目不保证在 Google、Bing 或 AI 搜索中的排名和引用。


### What do the executable structure checks establish?

The optional jsonschema adapter checks actual outputs against a model-proposed,
frozen structural specification using training inputs only. Schemas, dependency
versions, hashes, coverage/gaps and per-output violations are inspectable. Checks
are unreviewed and cannot certify semantic correctness, replace quality evidence,
change labels/judge rankings or unlock unsupported final delivery. Invalid proposals
and interrupted attempts are preserved rather than silently regenerated.

中文：可执行结构检查可验证字段、类型、枚举和数值范围，并显示来源与遗漏；模型提出的
规范仍未审阅，不能证明语义正确、替代质量证据或绕过最终交付门禁。


### Can my local agent use STS without opening Studio?

Yes. The separately released sts-skills v0.2 bridge connects to an installed loopback
STS engine for task/evidence/review/service operations. It defaults to read-only,
requires existing budgets and explicit paid commands, and never invents user approval.
It does not bundle the engine, model or isolated runtime. [Headless guide](https://github.com/runzhiyyds/sts-skills/blob/main/docs/HEADLESS.md).

中文：本地 Agent 可通过配套技能的无前端连接工具调用 STS；仍需已有引擎、凭据、预算与
安全执行环境。付费操作须明确授权，真实用户判断不能由 Agent 自动生成。
