# fixed10 逐题执行路径索引

这十份英文提示根据过滤后的公开题面、需求和接口信息重新编写，用于为各题提供定位、依赖梳理、独立协作和验证路径。它们不是旧执行轨迹或旧计划的逐字恢复，不包含参考实现、精确补丁或评分信息。本次未重跑求解或评测，没有新的性能结论。

每份提示只适用于文档头部匹配的完整 instance ID 与 base commit。同仓库的不同题目也必须使用各自的文件。路径和符号来自公开题面，需在解题时确认；这些文档不表示已经检查过目标源码或执行过列出的验证。

多题初始化时使用 `--context-dir bootstrap/contexts/fixed10`；入口按完整 instance ID 读取对应的 `<instance_id>.md`。单题也可用 `--context-file` 指向对应文件。不要把索引或十份提示合并后作为所有题共用的 context。参数需在创建新运行目录时设置；已存在运行的输入不会因修改这些源文件而改变。

| 序号 | 仓库 | 题目摘要 | 完整 instance ID 与对应策略 |
|---|---|---|---|
| 1 | `future-architect/vuls` | Trivy 扫描元数据与漏洞检测资格 | [instance_future-architect__vuls-fd18df1dd4e4360f8932bc4b894bd8b40d654e7c](contexts/fixed10/instance_future-architect__vuls-fd18df1dd4e4360f8932bc4b894bd8b40d654e7c.md) |
| 2 | `tutao/tutanota` | Linux 钥匙串解密错误跨层传播 | [instance_tutao__tutanota-de49d486feef842101506adf040a0f00ded59519-v10a26bfb45a064b93f4fc044a0254925037b88f1](contexts/fixed10/instance_tutao__tutanota-de49d486feef842101506adf040a0f00ded59519-v10a26bfb45a064b93f4fc044a0254925037b88f1.md) |
| 3 | `flipt-io/flipt` | 本地 OCI bundle 复制、引用与元数据验证 | [instance_flipt-io__flipt-b22f5f02e40b225b6b93fff472914973422e97c6](contexts/fixed10/instance_flipt-io__flipt-b22f5f02e40b225b6b93fff472914973422e97c6.md) |
| 4 | `ansible/ansible` | Ansible vaulted 变量文件缓存回归 | [instance_ansible__ansible-3b823d908e8a5d17674f8c26d337d3114b7493b1-v0f01c69f1e2528b935359cfe578530722bca2c59](contexts/fixed10/instance_ansible__ansible-3b823d908e8a5d17674f8c26d337d3114b7493b1-v0f01c69f1e2528b935359cfe578530722bca2c59.md) |
| 5 | `protonmail/webclients` | Dropdown 统一尺寸接口与 CSS 行为 | [instance_protonmail__webclients-8be4f6cb9380fcd2e67bcb18cef931ae0d4b869c](contexts/fixed10/instance_protonmail__webclients-8be4f6cb9380fcd2e67bcb18cef931ae0d4b869c.md) |
| 6 | `gravitational/teleport` | Teleport 多审计后端初始化与资源关闭 | [instance_gravitational__teleport-ac2fb2f9b4fd1896b554d3011df23d3d71295779](contexts/fixed10/instance_gravitational__teleport-ac2fb2f9b4fd1896b554d3011df23d3d71295779.md) |
| 7 | `flipt-io/flipt` | Flipt 默认配置与环境变量覆盖优先级 | [instance_flipt-io__flipt-7161f7b876773a911afdd804b281e52681cb7321](contexts/fixed10/instance_flipt-io__flipt-7161f7b876773a911afdd804b281e52681cb7321.md) |
| 8 | `element-hq/element-web` | Element 房间加载事件、按钮与 permalink 焦点 | [instance_element-hq__element-web-494d9de6f0a94ffb491e74744d2735bce02dc0ab-vnan](contexts/fixed10/instance_element-hq__element-web-494d9de6f0a94ffb491e74744d2735bce02dc0ab-vnan.md) |
| 9 | `gravitational/teleport` | Touch ID 注册确认、回滚与原生平台边界 | [instance_gravitational__teleport-65438e6e44b6ce51458d09b7bb028a2797cfb0ea-vce94f93ad1030e3136852817f2423c1b3ac37bc4](contexts/fixed10/instance_gravitational__teleport-65438e6e44b6ce51458d09b7bb028a2797cfb0ea-vce94f93ad1030e3136852817f2423c1b3ac37bc4.md) |
| 10 | `protonmail/webclients` | ProtonMail 存储单位常量与跨模块使用一致性 | [instance_protonmail__webclients-7e54526774e577c0ebb58ced7ba8bef349a69fec](contexts/fixed10/instance_protonmail__webclients-7e54526774e577c0ebb58ced7ba8bef349a69fec.md) |
