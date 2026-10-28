# ROSLLM v2 五项修复与验收（2026-10-28）

## 1. 本次问题

修复独立复核报告中的五项缺陷，保留现有节点拆分与消息、服务字段。没有修改 EAIR 工作区，没有连接真机，也没有调用真实 LLM API。

## 2. 实现思路

执行器继续使用共享 flock，只在超时/状态未知后留下持久化待确认标记；后续计划读取该标记并返回 -10。显式确认停止的 Trigger 服务持有同一把锁清除标记，活动计划期间不能清除。该设计避免仅取消本地等待就再次发送动作，同时保留现有 ExecuteBehavior 接口。

观测转换仍由定时器限速，但只有收到新 Pose 才发布，断流时不再重发旧数据，ObservationManager 因而能够检测 stale。前进核验使用 position.x/y 的二维距离，不依赖世界坐标 x 或当前朝向。其余两项分别修正节点销毁属性和 YAML 横移约束。

## 3. 文件职责

- `src/rosllm_core/rosllm_core/lease.py`：共享状态标记、停止确认、锁的持有与释放。
- `src/rosllm_executor/rosllm_executor/executor_base.py`：将 TIMEOUT/UNKNOWN 转为待确认状态，对后续计划返回 -10，提供确认服务。
- `src/rosllm_core/rosllm_core/action_client.py`：正确销毁 self.node；重复 shutdown 保持安全。
- `src/rosllm_turtlesim/rosllm_turtlesim/turtle_observation.py`：只发布新源数据，保留 x/y/theta，增加 position 对象。
- `src/rosllm_core/rosllm_core/library.py`、`observation.py`：向后兼容的 verify.metric，默认 delta，新增 distance_2d。
- `src/rosllm_turtlesim/config/action_library.yaml`：forward 使用二维位移；strafe_vel 的 enum 为 [0.0]。
- 各包 test 目录：新增或扩展对应回归测试；README/TEST_REPORT/PORTING 更新接口和验证说明。

## 4. 修改差异

可读的完整差异保存在 `review/fixes_2026-10-28/fixes.patch`，包括源代码、回归测试和主要文档修改。首次 GitHub 上传将包含完整工作区源码，而非仅补丁。

## 5. 核心流程与接口

`PlanLease.mark_uncertain(reason)` 只能由当前锁持有者调用，写入并 fsync 状态标记；释放操作系统锁后，其他执行器仍因该标记被拒绝。标记读取和确认使用相同 flock，避免新计划与确认操作竞争。

`ExecutorBase._handle` 保留原超时响应 -9；下一次计划请求返回 UNKNOWN/-10，且不调用原子动作。显式恢复后才接受新计划。持久化写入失败时保留当前锁，不静默放行。

新增服务：`<service_name>/confirm_stopped`，类型 `std_srvs/srv/Trigger`。默认为 `/execute_behavior/confirm_stopped`。调用者必须先确认旧动作已结束、机器人已经停止：

```bash
ros2 service call /execute_behavior/confirm_stopped std_srvs/srv/Trigger '{}'
```

这是调用者对停止状态的确认，不是停止命令、取消命令或自动硬件检查。
新增 ROS 依赖为 std_srvs，无新增 pip 依赖，无新增 ROS Action。

`verify_effect` 的 distance_2d 分支检查 position 对象含有限数值 x/y，再计算 hypot(dx,dy)，应用原有位移阈值。现有标量核验和 VerifySpec 位置参数保持兼容；二维距离只检查位移幅度，不能证明完整轨迹、方向或任意自然语言目标。

## 6. 实际测试与运行

在清理其他工作区路径后，单独加载 Humble 构建到 `/tmp/rosllm_v2_fix`：7 包完成。运行完整包级测试：

```text
Summary: 177 tests, 0 errors, 0 failures, 0 skipped
```

按包：core 129、executor 20、agent 20、observation 5、turtlesim 3。
相较复核时 160 项新增 17 项，覆盖持久化状态、活动计划禁止恢复、客户端生命周期、四个方向和错误坐标、源位姿断流/恢复、非法横移整计划零执行。正常 fake LLM + Turtlesim 三步演示继续通过。

本机原工作区 build/install 也已增量重建。以后可直接运行：

```bash
source /opt/ros/humble/setup.bash
cd /home/moxy/rosllm_ros2_ws_v2
PYTHONNOUSERSITE=1 colcon build
source install/setup.bash
PYTHONNOUSERSITE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 colcon test --executor sequential
colcon test-result --verbose
export ROS_DOMAIN_ID=77 ROS_LOCALHOST_ONLY=1
ros2 launch rosllm_turtlesim turtlesim_demo.launch.py headless:=true
```

预期测试总数 177、无失败；演示启动 fake LLM。Agent 命令见 README。若后续请求返回 -10，先确认动作状态再使用确认服务；不要删除租约文件。构建和测试日志保存于 `review/fixes_2026-10-28/evidence/`。

## 7. 限制与下一步

共享标记只协调同一主机、相同 robot_id 与 lease_dir 的执行器；默认 /tmp 的系统清理可能删除文件，不是硬件安全保证，也不覆盖任意崩溃瞬间或跨主机协调。远端 Service 仍没有取消语义。确认服务依赖调用者已检查停止，不自行推断。

真实模型、EAIR 仿真/硬件、VLM、完整 BehaviorTree.CPP 未在本次测试。后续硬件适配应通过有反馈、可查询状态的控制器/Action 实现停止确认；本次没有扩大到这些功能。
