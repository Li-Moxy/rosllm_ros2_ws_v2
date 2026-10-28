# ROSLLM ROS 2 v2 独立复核

> 本文记录修复前的复核结果。五项问题已在后续修改，修复与验证见 [FIX_REPORT_2026-10-28.md](FIX_REPORT_2026-10-28.md)。

日期：2026-10-28（Asia/Shanghai）。目录：`/home/moxy/rosllm_ros2_ws_v2`。

## 1. 本次问题与结论

复核其他 AI 完成的 ROSLLM 移植，包括干净构建、现有测试和测试以外的执行边界。

**结论：新版已有可运行的离线原型，明显改善了旧版的启动和测试问题，但仍有两个应优先处理的执行/状态问题，以及三项已复现的中等优先级缺陷。不能把现有测试全部通过当作工程验收已完成。**

没有修改 `src`、现有配置、README 或原测试报告，也没有读取私有密钥、调用真实模型 API 或连接 EAIR/真机。新增内容只有本报告和 `review/independent_2026-10-28/` 的诊断脚本、证据日志。编译及测试产物位于 `/tmp/rosllm_v2_review`。

## 2. 验证思路与模块关系

当前链路是 Agent → LLM → NDJSON 计划 → 执行器 → AtomicAction → 仿真；观测链路是 Pose → TurtleObservation → String → ObservationManager → Agent/执行器。

先清理其他工作区相关环境路径，单独 source Humble，重新构建到临时目录；随后只加载该构建产物，运行项目现有包级测试。最后补充现有测试未覆盖的场景：超时后紧接新计划、原始位姿停止更新、客户端 shutdown 后节点状态、不同朝向的前进核验，以及带非法横移参数的混合计划。

现有集成测试自身选择随机 ROS 域并限制 localhost。补充诊断使用域 183、184 和 localhost；运动服务仅为本地测试桩或 Turtlesim 适配器，没有发布到 EAIR 控制话题。

## 3. 发现的问题与文件职责

### P1-01：超时后释放计划锁，后续计划可与尚未结束的动作重叠

位置：`src/rosllm_executor/rosllm_executor/executor_base.py:234-235`；相关超时分类在 `sequence_executor.py:129-132`。

`_handle()` 不论执行返回什么状态，均在 finally 里释放 lease。服务客户端超时只停止等待，远端动作仍可能运行；此时另一个请求却能马上取得计划锁，甚至可通过另一个执行器发送动作。

**实测：**本地桩动作持续 1.5 秒，执行器步超时为 0.2 秒。首个计划返回 -9 后立即发送第二个计划，第二个没有被拒绝，两个动作实际开始时间仅相差 **0.209 秒**，均返回 -9，动作调用数为 2。此时首个动作尚未结束，出现实质重叠。

新版已经解决“同一个 Agent 自动重发超时计划”，但没有解决“超时状态未确认时，接受后续新计划”。现有 timeout 测试只检查一次请求不会自行重试，漏掉了这个场景。

建议：机器人执行状态增加 `unknown/reconciliation_required` 等状态；超时或状态未知后拒绝新的运动计划，直到查询确认原动作结束/停止，或经明确复位流程恢复。不要仅靠客户端 Future.cancel 或 finally 解锁。复核后还应补测试：新请求在原动作未确认结束前被拒绝，确认结束后才能重新接受。

证据：`review/independent_2026-10-28/evidence/probes.log`，`timeout_overlap`。

### P1-02：定时重发旧位姿会掩盖传感器断流，过期数据仍被标为 fresh

位置：`src/rosllm_turtlesim/rosllm_turtlesim/turtle_observation.py:40-61`；相关接收时间更新在 `rosllm_observation/observation_manager_node.py:173-176,200-210`。

TurtleObservation 缓存最后一帧 Pose，却不保存原始接收时间；timer 会一直发布这帧数据。ObservationManager 按转换后的 String 接收时间算 age，因此只要转换节点还活着，原始 Pose 即使停更，管理器也会继续报告 fresh。

**实测：**只发布一帧 Pose，此后停止发布；管理器有效期设置为 0.3 秒。等待约 **1.204 秒**后，返回状态仍是 **fresh**，报告 age 只有 **0.054 秒**。

这会使 Agent 的观测新鲜度保护被绕过。当前测试覆盖“直接输入 stale 元数据”，没有覆盖“原始传感器已断流，中间节点仍重复发布”的链路。

建议：转换节点记录并传递原始数据年龄，或者原始数据超时后停止正常发布并报告 stale；管理器不能通过包装/转发把旧数据年龄重置为零。新增一次性源消息后断流的全链路测试。

证据：`probes.log`，`stale_pose`。

### P2-01：ClientHost.shutdown 没有真正销毁其 ROS 节点

位置：`src/rosllm_core/rosllm_core/action_client.py:260-272`，对应创建位置 `49-50`。

创建时使用 `self.node`，销毁时却调用不存在的 `self._node.destroy_node()`；异常又被 `except Exception: pass` 吞掉。executor 虽然关闭，ROS 节点和它的实体仍未按该接口的职责销毁。长期进程内重复创建/关闭 ClientHost 会积累资源；进程最终退出或 context 关闭可能掩盖问题。

**实测：**调用 `host.shutdown()` 后，`host.node.get_name()` 仍正常返回节点名，且该对象没有 `_node` 属性。

建议：修正属性名，明确关闭与销毁顺序；增加 shutdown 后节点 handle 已无效、重复 shutdown 安全的生命周期测试。不要用 blanket except 掩盖拼写错误。

证据：`probes.log`，`shutdown`。

### P2-02：前进核验只检查世界坐标 x，会误判正常前进

位置：`src/rosllm_turtlesim/config/action_library.yaml:16-20`；差值计算在 `rosllm_core/observation.py:328-338`。

默认 forward 的 verify 固定检查 `x`。机器人朝向约 90° 时，前进主要改变 y；实际移动成功，但 x 不变，因此 Agent 报 `postcondition_failed`。这不是极端参数，而是转向后正常运动。

**纯函数实测：**从 `(x=5,y=5,theta=π/2)` 移动到 `(x=5,y=6,theta=π/2)`，真实二维位移 1 米，当前核验返回 false，原因是 x 变化为 0。

建议：核验二维位移，或者将位移投影到动作开始时的朝向，并结合输入速度符号和合理容差；转向核验也应使用归一化角差，避免 ±π 跨界影响。不要通过随意把字段从 x 改成 y 解决。

证据：`probes.log`，`forward_heading`。这一探针使用构造的合法观测，没有额外运行朝向 90° 的 Turtlesim 动作。

### P2-03：动作库允许非零横移，服务端才拒绝，破坏整计划预检

位置：`src/rosllm_turtlesim/config/action_library.yaml:55-60`；拒绝发生于 `rosllm_turtlesim/turtle_actions.py:167-169`。

动作库文字说明 `strafe_vel` 必须为 0，但机器可执行约束却是 -2..2。计划解析器据此接受非零值；执行到 command 服务时才被拒绝。如果它在计划后面，前面的动作已执行，违背“任何非法参数应在整个计划执行前被拒绝”的设计。

**实测：**提交两步计划：第一步 forward，第二步 command 的 `strafe_vel=1`。第一步使用本地测试桩；第二步使用真实 TurtleActions 输入校验。最终返回 -6，报告 `completed_steps=1`，桩 forward 实际被调用 1 次，然后 command 才失败。这里没有真实机器人运动。

建议：YAML 将该参数设为 `enum: [0.0]` 或 min=max=0，并补“后继参数非法，前序调用计数为 0”的集成测试。

证据：`probes.log` 的 `strafe_precheck`；`strafe-partial.log` 的 `strafe_partial`。

## 4. 代码及修改差异

本次为只读实现复核，没有修复上述实现。新增诊断文件：

- `review/independent_2026-10-28/probes.py`：复现上述缺陷；timeout 与 mixed-plan 诊断复用项目已有执行器测试桩和启动 helper，新增调用时序与断流场景。
- `review/independent_2026-10-28/evidence/*.log`：本次重新运行的构建、测试、接口检查及补充诊断结果。

保留原始 README 和 TEST_REPORT，便于原作者按本次结果修复并更新声明。

## 5. 已验证的改进，以及需要明确的边界

已实际确认：

- 7 个包干净构建完成，无旧版接口包误识别为 catkin 的问题；两个接口包识别为 ament_cmake。
- source 本次构建产物后，ExecuteBehavior 接口可发现，执行器入口可发现。
- 完整现有测试套件通过，包含真实 ROS 节点/服务和默认离线 Turtlesim 集成。
- 现有测试对正常并发拒绝、跨进程 lease、同一任务超时不自动重试、行为树非法后继预检、重试总预算及 LLM 请求编号进行了验证；这些是有效的改善，但覆盖不到上述补充场景。

尚需注意的设计边界：

- Agent 规划前读取一次观测，LLM 返回后直接执行；耗时推理期间观测可能已过期，执行前没有重新读取和确认。该点本次基于 `agent.py:179-217,238-306` 源码确认，未另做延迟模型的复现。
- 当前结果核验检查字段变化是否超过阈值，不等于任意自然语言目标已达成。README 的 success_verified 应严格限定为“已配置的效果检查通过”。
- CLI Agent 的提示和解析仅支持 NDJSON；行为树执行器消费 XML。launch 虽提供 executor:=behavior_tree，但不能据此认为相同 CLI/fake LLM 链路可直接切成行为树规划。应补对应规划模式或明确手工 XML 调用边界。
- 原报告记载 157 项测试；本次代码实际为 160 项，应同步文档。
- DeepSeek 真实 API 已验收是原报告的声明，本次没有独立重测；本次没有使用密钥或产生外部 API 调用。
- 没有验证 EAIR 仿真、Nav2、机械臂、升降、物理机器人、VLM、完整 BehaviorTree.CPP 或全部上游实验复现。

## 6. 本次实际命令、结果与复现方法

构建与测试均清理了 AMENT_PREFIX_PATH、CMAKE_PREFIX_PATH、COLCON_PREFIX_PATH、PYTHONPATH、LD_LIBRARY_PATH、ROS_PACKAGE_PATH、ROSLISP_PACKAGE_DIRECTORIES、PKG_CONFIG_PATH，再只加载 Humble 和本次产物，避免其他工作区污染。

核心命令如下；建议在仅加载 Humble 的干净终端复现：

```bash
source /opt/ros/humble/setup.bash
export PYTHONNOUSERSITE=1
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
colcon --log-base /tmp/rosllm_v2_review/log build \
  --base-paths /home/moxy/rosllm_ros2_ws_v2/src \
  --build-base /tmp/rosllm_v2_review/build \
  --install-base /tmp/rosllm_v2_review/install
source /tmp/rosllm_v2_review/install/setup.bash
colcon --log-base /tmp/rosllm_v2_review/test-log test \
  --base-paths /home/moxy/rosllm_ros2_ws_v2/src \
  --build-base /tmp/rosllm_v2_review/build \
  --install-base /tmp/rosllm_v2_review/install --executor sequential
colcon test-result --test-result-base /tmp/rosllm_v2_review/build --verbose
```

实际结果：

```text
构建：Summary: 7 packages finished [7.38s]
测试执行：Summary: 7 packages finished [51.4s]
测试结果：Summary: 160 tests, 0 errors, 0 failures, 0 skipped
```

按 pytest.xml 的 testcase 数量：core 115，executor 19，agent 20，observation 5，turtlesim 1，总计 160。

补充诊断（代码中固定使用域 183/184；运行前确认这些域未被其他应用占用）：

```bash
for mode in timeout_overlap stale_pose shutdown validation strafe_partial; do
  timeout --kill-after=3s 25s python3 \
    /home/moxy/rosllm_ros2_ws_v2/review/independent_2026-10-28/probes.py "$mode"
done
```

当前预期结果分别是：overlap=true；源数据超期后仍 fresh；node_still_valid=true；合法二维前进核验失败且非零 strafe 计划被接受；混合计划完成一步后才失败。**这些输出是缺陷的复现，不是验收通过。**

排错：脚本依赖 pytest 和本次构建包；helper 在启动子进程时调用 ros2 run，因此父进程必须 source 同一份 install。服务找不到先检查域和环境路径，避免用加长超时掩盖逻辑问题。临时构建目录可能被系统清理，可用上面命令重新生成。

## 7. 建议下一步

先处理 P1-01 的未知状态隔离和 P1-02 的源数据新鲜度，再修 ClientHost 清理、前进核验和横移参数约束。每一项附一个能在当前代码上失败、修复后通过的回归用例，然后重跑现有套件。

暂不需要再次整体重写。新版主体链路和测试基础可以保留，适合围绕上述五项做小范围修复后再验收。
