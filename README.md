# Facet Proof

Facet Proof 是纯 Python 标准库实现的技术插图渲染后端：接收三角网格、材质及场景布置，冻结资源快照，渲染颜色、深度和物体编号图版，支持图版比对、校验、审阅和归档。使用 CPU 光栅化，不需要 GPU。

需要 Python 3.10 或更高版本。启动：

```sh
python3 -m facetproof --root ./facet-data --host 127.0.0.1 --port 8796
```

默认绑定本机；服务没有账户或认证机制，只适合受信任的本地环境。持久化根目录包含 SQLite 数据库和内容寻址的图版文件。`POST /facet/v1/worker/step` 同步领取并执行一个待处理任务；`POST /facet/v1/worker/recover` 回收过期租约。进程启动时也会回收已过期租约。

可运行完整生产演示，输出三张 PNG：

```sh
python3 -m facetproof.demo --flow render-plates --output ./preview
```

演示使用临时数据库，不影响服务数据。其他流程名见 `facetproof/demo.py` 的 `FLOWS`。输入按顺序为：创建 `/facet/v1/meshes` 和 `/pigments`，创建 `/assemblies`，以版本号更新布置，再调用 `/assemblies/{id}/seal`；对 `/seals/{id}/exposures` 提交带唯一 `key` 的请求，然后执行 worker。图版的 `/passes/color.png`、`depth.png`、`object.png` 可供查看；`/pick?x=...&y=...` 返回物体及摄像机前向距离。`/verify` 校验文件，`/bundle` 导出包含快照及全部通道的 ZIP。

坐标使用右手空间，镜头支持透视和正交投影，旋转以度为单位；颜色输入为 0–1 sRGB，内部以线性光计算。每个图版包含 RGB、32 位浮点深度、32 位无符号物体编号和对应 PNG；背景深度为正无穷。图版保留颜色与深度的输入快照和摘要。服务有图像尺寸、采样、面数和渲染时间上限。

当前基线不包含测试套件，自动化测试有意延期；`facetproof.demo` 提供有界的业务流程检查，不应当作完整的测试覆盖。
