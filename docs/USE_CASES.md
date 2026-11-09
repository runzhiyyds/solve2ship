# Use cases and observed cases / 应用与已观察案例

[Home](../README.md) · [Usage](USAGE.md) · [FAQ](FAQ.md) · [Related work](COMPARISONS.md)

## Candidate problems

These are starting points for task design, not deployed customer outcomes.

| Desired outcome | Alternatives worth testing | Evidence to bring |
| --- | --- | --- |
| Route requests into a small set of business categories | Rules, compact classifier, prompt or hybrid | Distinct examples, category definitions and reviewed labels where possible |
| Convert order notes into structured amounts | Parser/code, prompt plus exact arithmetic, bounded workflow | Currency/rounding rules, representative wording and edge cases |
| Predict a class from numeric records | Simple baselines and trained ML | Valid labels, feature definitions, leakage controls and independent data |
| Choose a local calculation tool from a request | Direct code, prompt or bounded tool-use agent | Tool contracts, forbidden operations and expected output format |

Examples should represent the real distribution and its failures. Start with non-sensitive data and a task whose outcomes you can inspect. Open-ended high-stakes decisions, unknown evaluation criteria, unreviewed external access and public production hosting exceed this preview's accepted scope.

中文：上表是可尝试的问题，不是已部署业务案例。先明确成功标准、代表性数据与边界；需要可靠标签或人工评价的任务，不应靠模型裁判分替代真值。

## Observed acceptance cases

These small synthetic engineering cases exercised actual model/runtime calls. They are **not a comparative benchmark**, customer result or business-accuracy estimate. Provided labels were not independently certified. Private datasets, databases and development logs are excluded from the public repository; these summaries are not a complete independently reproducible research package.

### A simpler program beat the tool-use candidate

**Question:** For Chinese requests containing both hotel prices and dates, can the system choose a way to return either integer-cent cost or a day difference?

Three actual candidates were built: Python, a bounded tool-use agent and Hybrid. On six common validation cases, Python matched 6/6 provided labels. The agent chose fee/date tools and executed them in isolated containers, but returned scalars instead of the required object, matching 0/6. One real edit was measured at 0/6 again. Failures were retained; the Python artifact remained the selection.

The frozen Python candidate then matched 4/4 once-only final labels. Private and actual ZIP services each passed six independent HTTP equivalence cases outside the repository. The final set was not used for editing. The lesson is about maintaining output contracts and allowing simple alternatives to win, not general agent inferiority. Scope: [v0.12.0 engineering preview](https://github.com/runzhiyyds/solve2ship/releases/tag/v0.12.0).

### Model and exact computation can form a service

A Chinese order-note task used two model steps followed by isolated Python computation. It matched six validation labels and four once-only final labels. The private bundle and source ZIP each passed six independent HTTP cases plus a new operation after restart, using fourteen actual model calls in each proof.

The serving process used its own credentials and persistent usage ledger, without the STS experiment database or controller. Saved successful requests did not create new model calls; restarts preserved committed usage. This demonstrates the bounded serving/budget chain, not general arithmetic correctness or verified provider billing. Scope: [v0.11.0 engineering preview](https://github.com/runzhiyyds/solve2ship/releases/tag/v0.11.0).

### Train-only numeric ML can compete

A separate 120-row synthetic numeric classification task fitted actual logistic regression and random forest candidates on 60 training rows. On ten shared validation cases, they matched 10/10 and 9/10 labels, while Rules matched 6/10. The selected frozen logistic model matched 24/24 once-only final labels and passed ten independent service cases from both private and exported bundles.

Fitting and model loading stayed inside the reviewed scikit-learn container. This is evidence of train/build/evaluate/ship plumbing on that synthetic task, not proof of performance on unseen business data or superiority over other AutoML libraries.

## Make a result worth citing

A stronger public experiment should include a redistributable dataset and license, fixed train/validation/final partitions, evaluator provenance, model/version/date, candidate and runtime hashes, budget accounting, failures, paired comparisons and independently repeatable commands. Compare alternatives using the same cases and include uncertainty. Clearly separate validation feedback from a once-only final check.

The current examples and automated tests help inspect software behavior; they do not replace that research protocol. Cite the release/commit using [CITATION.cff](../CITATION.cff). Preserve unsuccessful trials rather than publishing only the champion.

## 中文结论

已观察案例支持的是具体工程链路：真实方案可以执行、失败会保留、更简单的方案可以胜出、胜出产物可以脱离控制进程调用。它们不支持“业务准确率达到某值”“总能优化”或“优于某开源项目”的结论。

若用于技术文章或视频，请同时展示样本规模、标签来源、失败与边界。完整可复现实验仍需要公开且可合法再分发的数据、固定协议与重复命令；不要把本页案例摘要包装成完整基准材料。
