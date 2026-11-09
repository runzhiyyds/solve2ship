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


## Optional structural evaluation adapter

M8 uses jsonschema's Draft202012Validator behind a bounded adapter. Its model-proposed
schema cannot use references, regex, formats, combinators or external retrieval. It
checks structure, not semantic truth. Python 3.13 optional installation is pinned in
[requirements/schema.lock](../requirements/schema.lock); the lock accepts pure wheels
and CPython 3.13 macOS x86_64/arm64 and manylinux x86_64/arm64 rpds wheels. Other
Python/platform combinations need a reviewed lock update; no silent source build.

| Package | Version | License |
|---|---|---|
| jsonschema | 4.26.0 | MIT |
| jsonschema-specifications | 2025.9.1 | MIT |
| attrs | 26.1.0 | MIT |
| referencing | 0.37.0 | MIT |
| rpds-py | 2026.9.1 | MIT, native extension |

All required Python 3.13 transitives, wheel SHA256s, upstream URLs and license-file
hashes were recorded before installation in dependencies.json. Unmodified upstream
license texts are in [third_party/licenses](../third_party/licenses/). No wheel/native
binary or vendored package implementation is distributed here. rpds-py incorporates
compiled Rust code; its complete native dependency/CVE audit is not certified. When
redistributing software, preserve its applicable upstream licenses, copyright and
notices. Optional format/network extras are not installed or authorized.

中文：可选结构校验复用 jsonschema，而非自行发明验证器。五项依赖的版本、包哈希、
来源和许可已锁定，安装前核验；包实现与二进制不随源码分发。模型提出的规范禁止外部
引用、正则与执行代码，结构通过不等于语义正确。不支持的平台不能偷偷回退到源码构建。
