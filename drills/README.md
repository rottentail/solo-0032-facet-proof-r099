# 曝光租约竞争演练

`lease_contention.py` 验证 Facet Proof 的核心并发承诺:**一个曝光在实际竞争中只产生一张最终图版**——即使两个工作器都完成了完整计算,也只有当前租约持有者的提交被接受。

## 运行

```sh
python3 drills/lease_contention.py                 # 单轮,报告写入临时文件
python3 drills/lease_contention.py --runs 5        # 多轮重复(每轮全新存储与端口)
python3 drills/lease_contention.py --report out.json --keep ./drill-data
```

退出码为 0 表示全部 invariant 与 expectation 通过;任何失败退出码为 1。

## 结构

- 服务以真实子进程运行(`python3 -m facetproof`),全部公开状态检查走真实 HTTP。
- 工作器 A 在演练进程内用独立的 `Application` 实例(独立 SQLite 连接,WAL 与服务进程共享同一存储),镜像 `Worker.run_one` 的计算路径但租约时长与提交时机可控;渲染期间不发心跳,模拟卡顿失联的工作器。
- 工作器 B 走 HTTP `POST /facet/v1/worker/step`(服务进程内的产品 Worker)。
- `http-race` 场景用 4 条并发 HTTP 连接同时领取同一曝光。

## 场景

| 场景 | 内容 |
|---|---|
| `takeover` | A 短租约领取并完整计算,卡在提交前;租约过期被 recover,B 接管完成;A 迟到的心跳/提交被 `lease_lost` 拒绝,迟到的 fail 为 no-op |
| `cancel-running` | 运行中取消:公开状态仍 `running` 但带取消标记,心跳/提交被 `cancelled` 拒绝,fail 落 `cancelled` |
| `cancel-recovered` | 取消后租约过期:recover 直接落 `cancelled`,过期后 fail 为 no-op |
| `http-race` | 4 连接并发领取:恰好一个 `succeeded`,其余 `idle` |
| `recover-live` | 未过期租约不受 recover 影响,持有者正常完成 |

## 检查分级(区分时序抖动与真实破坏)

- **invariant** — 任何调度下都必须成立:唯一图版行、唯一 `succeeded` 事件、迟到操作拒绝码、终态一致、迟到操作后公开状态逐字节稳定。失败即真实不变量破坏。
- **expectation** — 受控编排下确定成立:attempt 计数、事件因果骨架(子序列匹配,容忍新增事件类型)、租约令牌两两不同。失败意味实现行为改变。
- **observation** — 调度/时序相关,只记录不断言:谁赢得竞争、精确事件序列、各阶段耗时。

报告(JSON)含每个场景的编排时间线、服务器事件表全量、检查结果,以及 `relations` 摘要(每个曝光的 事件序列 ↔ attempt ↔ plate_id ↔ plates 表行)。

## 鉴别力验证(突变测试)

演练的鉴别力已用突变测试验证:临时移除 `plates.finish` 的租约检查与 `plates.exposure_id` 的 UNIQUE 约束后,演练报告 5 项 invariant 失败(两张图版行、plate_id 被迟到者覆盖、两个 `succeeded` 事件、迟到提交被接受);恢复原代码后全部通过。注意唯一性实际由两层保证:租约检查拒绝迟到提交,UNIQUE 约束兜底阻止第二行图版。
