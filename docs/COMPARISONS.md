# Related work: where Solve2Ship fits

[Home](../README.md) · [FAQ](FAQ.md) · [Use cases](USE_CASES.md)

**Scope reviewed: 2026-11-09.** This is a comparison of documented objectives and interfaces, not a benchmark, integration announcement or legal review. Upstream products change. Claims about other systems below come from their official repositories, documentation or papers; the STS interpretation is identified separately.

## A shared question, different search spaces

How can software explore multiple solutions and use evaluations to improve them? These projects address meaningful parts of that question. STS explores a **Goal-to-Service** direction: start from samples and an outcome, compare technical paradigms and carry the supported selection through independent service checks. Its accepted implementation is much narrower than that full vision.

| Project | Documented focus and user input | Search or construction unit | STS interpretation and relevance |
| --- | --- | --- | --- |
| [AIDE ML](https://github.com/WecoAI/aideml) | Open-source research reference for ML engineering; dataset, task and evaluation metric | LLM-guided tree search over scripts, including drafting, debugging and metric feedback | A relevant foundation for code-space search. STS intends to compare code with rules, ML, prompts and workflows, then validate a separate service artifact. This broader intention does not establish stronger ML performance. |
| [AutoML-Agent](https://deepauto-ai.github.io/automl-agent/) | Research framework taking task descriptions through ML planning, execution and verification toward deployable models | Specialized LLM agents and retrieved alternative plans across the ML pipeline | Closely related natural-language full-pipeline work. STS gives non-agent rules/code equal standing and targets a common service-delivery contract across paradigms. Public research code is available; verify applicable code/data licenses before reuse. |
| [Weco](https://docs.weco.ai/) | Product for iterative code optimization; existing code and a numerical evaluation script | Tree of proposed, executed and measured code variants; steering through natural language and local agents | Relevant experiment/search product and a distinct product from AIDE ML's MIT reference repository. Its documented scope extends beyond ML to prompts, agent scaffolds and general code. STS explores outcome-to-implementation-family selection; this is an architectural emphasis, not evidence that Weco lacks equivalent capability. |
| [AFlow](https://github.com/FoundationAgents/AFlow) | Research framework generating and optimizing agentic workflows against task evaluations | Code-represented workflows, LLM nodes/operators and a Monte Carlo tree-search variant | A relevant possible workflow-search component. STS aims to evaluate a workflow alongside simpler non-workflow candidates. AFlow's reported research results are not STS results. |
| [AutoGluon](https://auto.gluon.ai/stable/index.html) | AutoML library with tabular, time-series and multimodal interfaces | Model fitting, selection/ensembling and prediction using task-specific APIs | A mature candidate-building component to consider for supported ML tasks. STS currently uses a bounded numeric scikit-learn adapter, not AutoGluon, and makes no comparative model-quality claim. |

AIDE ML explicitly distinguishes its algorithm, reference repository and the Weco product. Do not treat all three as one open-source package or infer product rights from the reference repository's license. [Official AIDE ML description](https://github.com/WecoAI/aideml#what-is-aide-ml), [Weco overview](https://docs.weco.ai/).

AutoML-Agent's authors describe task-level planning, specialized agents and multi-stage verification from retrieval through deployment. Its paper reports experiments within its own setup; those numbers are not reproduced here. [Official paper](https://arxiv.org/abs/2410.02958), [official implementation](https://github.com/DeepAuto-AI/automl-agent).

AFlow describes its workflow search space and components in the official implementation; AutoGluon documents task-specific fitting/prediction and deployment options. [AFlow components](https://github.com/FoundationAgents/AFlow#framework-components), [AutoGluon documentation](https://auto.gluon.ai/stable/index.html).

## What STS is testing

STS's working design separates four concerns: define a task, construct executable alternatives, evaluate evidence and deliver a supported artifact. Candidate families remain extensible. Current local constraints include a reviewed Docker execution policy, cumulative budgets, sealed final data and explicitly limited serving adapters. General multi-agent/harness search and mature optimizer integrations remain unfinished.

The useful question is **which search space and evaluator fit this task?** A numeric prediction problem may suit a mature AutoML library. A supplied program and clear scalar metric may suit code optimization. A workflow task may benefit from workflow search. STS is investigating how such choices can compete under one task contract rather than assuming a single paradigm in advance.

## What this comparison does not show

No shared dataset, model, budget, hardware and evaluation protocol has been run across these projects by STS. There is no evidence here of STS outperforming them in quality, cost, latency, autonomy or deployment reliability. This page does not imply affiliation, endorsement or an implemented adapter. Dependency adoption needs its own pinned-version and license review.

## 中文说明

这些项目与 STS 具有真实交集，不应为了包装差异而贬低或缩小它们的能力：AIDE ML 研究代码空间的树搜索；AutoML-Agent 覆盖从任务描述到可部署 ML 模型的多阶段流程；Weco 的代码优化产品已覆盖更广泛场景，也支持本地 Agent；AFlow 搜索 Agentic Workflow；AutoGluon 提供成熟的任务型 AutoML 能力。上述描述以表格中官方来源为准。

STS 的差异化技术主张是 **让实现范式本身参与竞争，并把服务交付纳入闭环**。目前完成的是受约束的本机实现，不能把完整愿景当作现成优势。尚未开展同数据、同模型、同预算的横向实验；“跨范式”不意味着已经超越专门系统，也不意味着这些上游库已经接入。
