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
