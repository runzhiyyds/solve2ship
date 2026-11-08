# Security and scope / 安全与边界

This is loopback-only local development software, without public authentication,
TLS, multi-tenant authorization or a production vault. The model profile and SQLite
state are owner-only local files; the profile is plaintext. Other owner-controlled
local processes and Docker Engine are trusted. No hostile-tenancy certification or
complete CVE audit is claimed.

Generated code and model loading never run in the control plane. Accepted Docker
policy uses non-root execution, network none, readonly root, no host/socket/key
mounts, dropped capabilities, no-new-privileges, CPU/memory/PID/output/deadline limits
and cleanup. Crashes can leave a bounded orphan until runtime exit/startup reaping.
A trusted host HTTP gateway needs local Docker access; containers never get that
socket. This boundary is not a hardened public cloud isolation guarantee.

Model calls reserve money first and never follow redirects/retry automatically.
Unknown charges stay reserved. Workflow cancellation is checked between steps;
in-flight requests cannot be undone. Raw user/model evidence remains untrusted data.
Export checks scan allowlisted source for configured/suspected keys, but do not
certify arbitrary user data as secret-free or all generated code as legally original.

Never commit .sts/, keys, actual datasets, databases, image archives or private
agent context/development logs. Runtime acceptance/reports are generated locally
under .sts/, not part of the public source. Do not send credentials in public issues;
contact the repository maintainer privately for sensitive reports.

中文：本项目为本机开发软件，尚无生产鉴权、多租户或加密保险库。密钥配置为仅所有者
可读的明文。生成代码和模型加载只能在验收过的受限容器中运行，无宿主进程兜底。
模型调用先预留预算，未知费用保留且不自动重放。取消不能撤销已提交请求。发布源码
不包含用户数据、密钥、数据库、镜像归档和私有开发过程文件；安全/合法性检查均有明确
范围，不代表完整认证。敏感问题请私下联系维护者，不要在公开 Issue 中发送密钥。
