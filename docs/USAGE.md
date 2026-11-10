# Usage / 使用说明

## Local Studio

Run make run, open http://127.0.0.1:8000, paste/import JSON/JSONL input samples and
write a natural-language expected outcome. Examples are under examples/. Labels
are optional. Review/save the task before starting paid exploration. Settings →
Language explicitly selects 简体中文; the default is English.

Configure a model with python3 -m solve2ship configure-model under PYTHONPATH=src.
Use the hidden prompt; no key in arguments or browser. authorize-budget --cny
sets a cumulative authorized ceiling, never a usage reset. The current reviewed
endpoint is https://api.deepseek.com and model deepseek-flash. Date-limited pricing
fails closed after its review date until the policy is reviewed. Provider invoices
are authoritative; token estimates do not monetize local compute/hosting.

Results show actual outputs, common-case comparisons, bounded edit history, per-step
calls/costs and errors. Label agreement is conditional on unverified labels; judges
are uncalibrated. Re-test/evolution preserves earlier evidence. Cancellation stops
future work, not already-submitted provider charges. Unknown charges are not retried.

## Optional isolated execution

Rules/Prompt/Hybrid need no Docker. Generated Python and numeric ML require Docker
Desktop/Engine on this development machine, a reviewed immutable Linux image and
fresh local activation. The catalog's currently reviewed images are linux/arm64;
other platforms need their own digest/dependency acceptance. No runtime binary/image
is included in the repository.

Pull the exact Python image reference from dependencies.json using your installed
Docker CLI, then run:

    python3 scripts/verify_docker.py --activate

The verifier performs actual UID/network/readonly/resource/timeout/OOM/output checks.
It stores owner-only local reports under .sts/runtime-acceptance and a profile under
.sts/sandbox.json. Adapter/runner/image/Engine changes require fresh acceptance.
Missing Docker or failed checks never fall back to host execution.

Numeric ML additionally needs an image built from runtimes/sklearn/Dockerfile with
its exact reviewed wheels and requirements.lock. The catalog records the developer's
local immutable ML image ID; it is not a publicly pullable image. Build/review a local
image, register that immutable ID in the local catalog without changing dependency
pins, then run python3 scripts/verify_sklearn.py --activate. The review verifies real
fit/predict and dependency versions before enabling the adapter. This experimental
setup is manual; a portable image/bootstrap installer is not implemented yet.

## Supported delivery

A supported leader with independent final labels can be frozen once. Failed checks
retain their report. Exposed final inputs cannot be used for further search, even
reordered/relabelled. Source export additionally requires independent startup,
license and file-integrity checks. The trusted independent verifier is
python3 scripts/verify_service.py DELIVERY_ID; export verification is
python3 scripts/verify_export.py DELIVERY_ID. Local reports remain in .sts/.

Rules/Python/numeric classifiers, Prompt/Hybrid and bounded workflows are supported.
Model-backed final checks consume the original root/global authorization. Standalone
serving requires separate credentials and a persistent budget; it starts with zero
authorization. Exported code/ML/workflow serving still requires the reviewed Engine/
image. Keep the development HTTP API local.

## Independent model-backed service

Unpack the accepted source ZIP and enter its service directory. With Python 3.13+:

```sh
python3 -m runtime.service_model configure-model --state-dir /absolute/private/service-state
python3 -m runtime.service_model authorize-budget --cny YOUR_APPROVED_LIMIT --state-dir /absolute/private/service-state
python3 -m runtime.service_model status --state-dir /absolute/private/service-state
python3 -m runtime.service_runtime --state-dir /absolute/private/service-state
```

The model setup uses a hidden key prompt. Use the reviewed endpoint/model and a
date-valid pricing policy. Keep this owner-only state outside the source bundle and
version control; it contains a plaintext profile, usage.sqlite3 and cached responses.
Never delete/recreate it to reset authorization. The STS controller and experiment DB
are unnecessary. This local service has no public authentication; bind to loopback.

Paid POST /predict accepts exactly input plus request_id (32 lowercase hex characters).
Create one ID per intended operation, persist it before submission, and reuse it when
checking that operation. Successful repeats return the saved result without another
model call; changed input or failed/interrupted IDs return 409. A new ID is a new paid
operation, not a safe automatic retry. Unknown charges remain reserved across restarts.

For maintainer verification funded by an existing STS authorization, explicitly run
python3 scripts/verify_model_service.py DELIVERY_ID --allowance-cny APPROVED_ALLOWANCE,
then python3 scripts/verify_export.py DELIVERY_ID --service-grant GRANT_ID. These
commands make real paid calls. The immutable grant reserves headroom against the
original root/global limit; unused headroom is not automatically refunded. Both
verifiers share one persistent standalone ledger per grant. Independently authorizing
a new operator budget is a separate funding decision, not extra original-project
authorization. Model checks and local compute estimates are not provider invoices.

中文：粘贴样本、描述目标，先审阅保存，再在授权预算内探索。界面默认英文，可在设置
选择简体中文。结果提供真实输出、费用、步骤和版本；标签/裁判都不能直接当作可信真值。
预算与未知费用不会因重启清零。Python/ML 需要经过本机实际验收的 Docker；运行报告、
数据和密钥均留在 .sts/，不进入 Git。ML 镜像当前是手动本机构建与验收路径，不随源码
分发。受支持方案经过一次性最终检查和独立服务验证后才可导出。含模型调用的服务在
解压后按上面的命令设置独立凭据、累计预算和状态目录；默认额度为零。服务状态包含
明文凭据、用量和缓存，请放在源码之外并保留，不能靠删除状态清零。每项付费操作必须
保存一个 32 位小写十六进制 request_id；成功重复请求不再计费，失败/中断编号返回 409，
不会自动重试。项目内划拨额度继续占用原全局/整树预算，未用部分不自动退回。不要将
本机开发 HTTP 服务当作生产托管。


## Experimental tool-use candidates / 实验性工具调用方案

When the reviewed Docker runtime is available, the planner may propose agent.tools.v1
alongside simpler families. It defines 1–3 frozen Rules/Python tools and task
instructions; for each input the model selects a tool with JSON arguments, observes
its output and chooses the next tool or final result. At most four metered decisions
include the final answer. The last decision cannot launch another unused tool. No
shell, network, files, credentials, dynamic tool imports, subagents or recursion are
available. Generated Python stays in the accepted Docker policy.

Expand a sample to see selected tool, observed output, final action, per-decision
calls/costs and stop errors. A tool can succeed while the final model answer is wrong;
selection still checks actual output. Step exhaustion/failure/cancellation never
triggers a hidden final call or transport retry. Agent delivery uses the same
standalone profile, persistent usage state and request fencing; the current live
acceptance selected/exported Python. Agent final/package/HTTP coverage is protocol
fixture evidence, not a live agent final/export claim.

中文：运行环境可用时，系统可自主生成工具调用候选，逐条输入选择冻结工具、读取观察
并返回最终输出；最多四次付费决策。展开样本可看所选工具、观察、最终回答和逐步费用。
工具成功并不代表最终答案正确。无任意网络、文件、Shell、子代理或动态导入权限，
Python 只在受限容器内执行。达到上限或失败不触发隐藏重试。本轮真实胜出并交付的是
Python；Agent 最终检查/服务打包/HTTP 的测试属于明确的协议夹具证据。


## Optional executable structure checks / 可选可执行结构检查

The base app still starts without extra packages. To enable reviewed structure checks
with CPython 3.13, create/activate a local virtual environment and install exact hashes:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --only-binary=:all: --require-hashes -r requirements/schema.lock
make check-schema
make run
```

New Studio searches use the M8 protocol: one metered proposal uses training inputs
and the goal, then freezes an output schema and its stated coverage/gaps for the
whole search tree. Child edits/retests inherit it; validation/final outputs cannot
silently rewrite evaluation criteria. An uncertain/interrupted proposal is not
replayed. Missing optional packages produce explicit unavailable evidence; package
version changes require review. The adapter supports a bounded JSON Schema subset,
not arbitrary code, references, regex, formats or network access.

Expand Executable structure checks and sample evidence to inspect schema, provenance,
actual passes/errors and semantic requirements left uncovered. Proposed criteria are
unreviewed: they neither override labels/judges nor unlock unsupported final delivery.
A structural pass can still be semantically wrong. No-label final quality still needs
independent executable quality criteria/human calibration, not just a model judge.

中文：基础启动无需新增依赖；若启用结构检查，按上述命令在独立环境中安装已锁定的五项
依赖。新探索只向标准生成器提供训练输入和目标，整个搜索树冻结同一份检查；检查不随
验证或最终反馈偷偷变化。可展开查看来源、覆盖/遗漏、逐条通过与错误。模型提出的标准
仍未审阅，结构通过不能当作真实准确率，也不会绕过尚未支持的最终质量门禁。


## Quality review without labeling a dataset / 无需逐条标注数据集的质量复核

Review example quality opens actual validation outputs, your saved judgments and a
strictly descriptive comparison with model-judge scores. Provide a local reviewer
identifier and personally confirm the displayed outputs. Acceptable / Needs
correction / Cannot judge are separate from labels and measured accuracy. Changing
a saved opinion requires a reason; its previous record remains inspectable.

For an unlabeled task, explicitly choose Freeze this leader and prepare final examples
for my review. This is a real one-time final execution, not a preview that can be
repeated. The frozen artifact, criterion/policy and final input quarantine remain
fixed; paid final scope closes as soon as execution ends. Review each displayed final
output and complete the decision once. Every successful output must be explicitly
accepted. Missing/uncertain/rejected opinions or runtime errors cannot pass. A closed
acceptance/rejection cannot be changed or reopened; exposed inputs cannot tune a
new candidate. The default label-based gate and unsupported hard constraints remain.

Operator identity is self-declared on this owner-local development tool, not verified
human truth. Accepted examples do not establish general business accuracy. Independent
startup/file/license checks still gate source export. Exported provenance records
acceptance source/count/hash, not reviewer identity/comments. Never enter keys or
sensitive data in review notes. Installing a skill does not grant money or resources.

The companion sts-skills v0.2 supplies a headless local-engine client: status and
results are read-only by default; paid exploration/final execution require explicit
commands and existing authorization. Review packets are owner-only files; submitted
answers must come from actual user judgments, never agent-generated approvals.
See the [headless guide](https://github.com/runzhiyyds/sts-skills/blob/main/docs/HEADLESS.md).

中文：展开“复核样例”即可用业务语言判断实际输出，不用先逐条标注全部数据。填写本机
复核标识并确认已亲自检查；更正旧判断须说明原因并保留历史。无标签任务可明确选择
“冻结此方案，准备最终样例交给我复核”；这会真实使用一次最终输入，不能反复试跑。
每条结果须成功并被明确接受，才能完成质量决定并继续独立服务/许可/导出检查；拒绝、
缺评、无法判断或执行错误不能通过。决定关闭后不再改票，也不能用已暴露样例继续优化。
这只是操作者自行声明的业务判断，不是身份认证或普遍准确率。复核评论不要填写密钥或
敏感信息；源码导出只保留最小接受来源/数量/哈希，不带身份或评论。
