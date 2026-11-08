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

Rules/Python/numeric classifiers are supported. Deterministic workflows have local
serving support; model-backed workflows refuse before final exposure until their
standalone credential/budget lifecycle is ready. Exported code/ML serving still
requires the explicit reviewed Engine/image. Keep the development HTTP API local.

中文：粘贴样本、描述目标，先审阅保存，再在授权预算内探索。界面默认英文，可在设置
选择简体中文。结果提供真实输出、费用、步骤和版本；标签/裁判都不能直接当作可信真值。
预算与未知费用不会因重启清零。Python/ML 需要经过本机实际验收的 Docker；运行报告、
数据和密钥均留在 .sts/，不进入 Git。ML 镜像当前是手动本机构建与验收路径，不随源码
分发。受支持方案经过一次性最终检查和独立服务验证后才可导出；含模型调用方案尚未
支持独立服务。不要将本机开发 HTTP 服务当作生产托管。
