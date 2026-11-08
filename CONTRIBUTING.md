# Contributing / 贡献说明

Read README, docs/PRODUCT_ARCHITECTURE.md and docs/SECURITY.md. Keep changes focused,
preserve extensible candidate IDs, and run make check before a PR. Add meaningful
regression tests, update English/Chinese controls and relevant user documentation,
and keep CHANGELOG/release notes bilingual. Describe the problem, resulting behavior,
actual validation and remaining limitations. Generated code/model loading stays
isolated; fixtures and model-judge scores are not business accuracy.

Never commit credentials, .sts/, actual user datasets, runtime binaries or private
agent/development records. Preserve Apache-2.0/NOTICE and exact third-party provenance.
Public CI checks enforce this source-only boundary. Code changes need no live model
key or paid call for the automated suite. Real acceptance uses explicit resources.

修改前阅读项目介绍、产品架构及安全说明。每次聚焦明确行为，运行 make check，补充有
意义的回归测试，同步中英界面与相关使用文档；版本说明保持双语。说明问题、最终行为、
实际验证和限制，不把夹具或裁判分当作业务准确率。

不要提交密钥、.sts/、真实用户数据、运行环境二进制或私有 Agent/开发过程文件。
保留 Apache-2.0/NOTICE 与第三方精确来源。公开 CI 会检查产品源码分发边界。
自动化测试无需真实模型密钥或付费调用；真实验收必须有明确资源授权。
