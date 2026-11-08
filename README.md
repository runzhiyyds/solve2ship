# Solve2Ship (STS)

**Define the outcome. Let solutions compete. Ship the best.**

[English](README.md) · [简体中文](README.zh-CN.md)

Supply input samples and a natural-language outcome. Solve2Ship proposes competing
executable approaches, builds and evaluates them, iterates under a budget and
packages supported winners as callable local services. Labels and an initial
workflow are optional. Rules, code, ML and model-based solutions share evidence
and budget controls; a simpler program can win over a more elaborate workflow.

**v0.11.0 engineering preview.** This repository contains the product source,
runtime recipes, tests and user documentation. Credentials, actual user datasets,
local databases, agent context and development records are excluded.

## Capabilities

- Immutable task revisions from JSON/JSONL samples, goals and optional constraints.
- Real Rules, Prompt, Hybrid, isolated Python, numeric scikit-learn and bounded
  model-backed workflow candidates; no invented optimization charts or outputs.
- Actual outputs/latency, token-based cost ceilings, per-step call attribution,
  grouped splits, bounded edits, paired retests, conservative pruning and rollback.
- English default; Settings → 简体中文 localizes controls, states, costs and errors.
- Local source ZIPs and independent HTTP checks for Rules/Python/ML and model-backed
  Prompt/Hybrid/workflow winners, with separate service credentials and usage budgets.

Generated code/model loading stays inside accepted resource-limited Docker
containers. Model calls use a metered gateway outside those containers. Keys,
Docker sockets and host mounts never enter candidate containers. Unknown charges
remain reserved and requests are not automatically replayed.

## Quick start

Python 3.13+ with SQLite; Node 24+ for tests. No control-plane package installation.

```sh
git clone https://github.com/runzhiyyds/solve2ship.git
cd solve2ship
make check
make run
```

Open http://127.0.0.1:8000. This is loopback-only development infrastructure,
without production authentication/TLS/tenancy.

```sh
PYTHONPATH=src python3 -m solve2ship configure-model
PYTHONPATH=src python3 -m solve2ship authorize-budget --cny YOUR_APPROVED_LIMIT
PYTHONPATH=src python3 -m solve2ship model-status
```

Replace YOUR_APPROVED_LIMIT with your actual authorization. Enter the key only in
the hidden terminal prompt. The owner-only local profile is plaintext, not a
production vault. Budgets are cumulative and never reset usage. The paid gateway
currently requires the reviewed DeepSeek Flash endpoint/model and a date-valid
price policy; other endpoints/models require review before paid calls.

Rules/Prompt/Hybrid do not need Docker. Python/ML/workflows require machine-specific
runtime acceptance. See [usage](docs/USAGE.md), including optional image setup.
Example input and outcome are in [examples](examples/).

## Evidence and limits

The product test suite has 127 Python tests and five Node tests. Protocol fixtures
are separate from real model/runtime execution. Local synthetic acceptance exercised
model/code workflows and independent exports; those small cases do not establish
business accuracy, general superiority or verified optimization gains. Provided
labels are unverified; model-judge scores are uncalibrated proxies.

General agents/harness, mature
optimizer adapters, calibrated criteria, broader model/data coverage and production
hosting remain incomplete. Final checks consume holdout inputs once; unsupported
serving fails before exposure. CPU/hosting cost is not included in token estimates.

## Documentation and releases

- [Usage / 使用说明](docs/USAGE.md)
- [Product architecture / 产品架构](docs/PRODUCT_ARCHITECTURE.md)
- [Dependencies and licenses / 依赖与许可](docs/DEPENDENCIES.md)
- [Security and scope / 安全与边界](docs/SECURITY.md)
- [Changelog / 版本变动](CHANGELOG.md), [contributing](CONTRIBUTING.md)

Push/PR and version-tag checks are configured. Hosted checks were unavailable at
initial publication; this preview passed the local suite. Once hosted execution is
available, version tags run tests before publishing bilingual preview notes. Only accepted product changes are synchronized;
ordinary file saves and private development records are not automatically uploaded.

## License

[Apache-2.0](LICENSE). Copyright 2026 郭辰远 (Chenyuan Guo).
Preserve [NOTICE](NOTICE). Third-party components retain their own licenses and
notices. Runtime binaries and container images are not bundled; their distribution
has separate obligations. No complete legal/CVE/SPDX certification is claimed.
