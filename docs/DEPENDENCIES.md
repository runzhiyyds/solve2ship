# Dependencies and licenses / 依赖与许可

STS-authored source is Apache-2.0, Copyright 2026 郭辰远 (Chenyuan Guo); preserve
LICENSE/NOTICE. dependencies.json records exact versions, sources, image digests,
wheel hashes, incorporated license-file hashes and distribution duties.

The control plane uses installed CPython 3.13+ / SQLite and native browser modules,
with no external application package install. Node 24+ is test tooling. Installed
Python has PSF/incorporated terms, SQLite is public domain and Node has MIT plus
incorporated notices. Their binaries are not bundled.

Official Python 3.13.18-slim-trixie image has a pinned linux/arm64 digest and exact
Docker-library source commit. Its recipe is MIT, Python has PSF/incorporated terms,
and Debian components have individual licenses. Neither the image nor its binaries
are blanket Apache-2.0. The local ML image adds these reviewed binary wheels:

| Package | Exact version | Primary / incorporated license scope |
|---|---|---|
| scikit-learn | 1.9.1 | BSD-3-Clause |
| numpy | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| scipy | 1.18.1 | BSD-3-Clause plus bundled notices (review wheel license files) |
| narwhals | 2.28.0 | MIT |
| joblib | 1.6.0 | BSD-3-Clause |
| threadpoolctl | 3.8.0 | BSD-3-Clause |
| cloudpickle | 3.1.2 | BSD-3-Clause plus bundled notices (review wheel license files) |

Keep all original wheel/image notices, including OpenBLAS/LAPACK/GCC runtime
exceptions recorded in the inventory. Reviewed wheels are installed only inside the
ML container; STS never imports sklearn or unpickles data on the host. The source
includes the recipe/lock, without wheels, pretrained weights or an image archive.
Image redistribution/hosting has separate corresponding-source/notice duties.

GitHub CI uses the inventory's commit-pinned MIT-licensed checkout/setup actions;
no action source is copied into STS. The Ubuntu runner is not hermetically pinned.
This inventory is not a formal SPDX SBOM or legal/vulnerability certification.

中文：项目自有源码采用 Apache-2.0，保留郭辰远（Chenyuan Guo）署名及 NOTICE。
第三方依赖保留自身条款，不能统一改标 Apache。精确版本、来源、摘要和分发义务在
机器可读清单中；镜像和二进制不随源码分发。ML 包只在容器中安装与运行。镜像分发、
完整 SBOM 和漏洞/法律审计仍须单独完成。
