# 移植说明与覆盖表

## 1. 上游基线

| 项 | 值 |
|---|---|
| 仓库 | `https://github.com/huawei-noah/HEBO.git` |
| 分支 | `master` |
| Commit SHA | `77636146d2a2208d971e589062d4ec3d050029f0` |
| Commit 时间 | 2026-10-12T14:31:33+01:00 |
| 上游目录 | `ROSLLM/` |
| 许可证 | 根目录 `HEBO/LICENSE`，MIT，Copyright (C) 2019 Huawei Technologies Co., Ltd. |

获取方式（实际执行）：

```bash
git clone --filter=blob:none --sparse https://github.com/huawei-noah/HEBO.git
cd HEBO
git sparse-checkout set ROSLLM
git log -1 --format='%H %cI %s'
```

所有实现以该 commit 的实际源码为准，未依据论文描述补写未实现功能。

## 2. 环境核对

- 任务描述中的 `/home/moxy/rosllm_ros2_ws` 和
  `/home/moxy/rosllm_ros2_ws/REVIEW_2026-10-28.md` **不存在**。
- 只发现 `/home/moxy/ROSLLM_ROS2_Humble_Core_Port.zip`。解压后阅读了其
  README、PORTING_NOTES 和源码，作为“已知问题参考”，**没有复制其实现**。
- 旧版主要问题：没有 ROS 运行时验收；无整计划预检；超时与失败混淆；
  行为树在执行时才校验，后面的非法节点不会阻止前面的动作先执行；重试没有
  总预算；观测没有缺失/过期判断；没有跨进程计划互斥；测试是纯 Python
  unittest，`colcon test` 为 0 tests。
- 本工作区为全新目录，未修改旧目录（不存在）和 `/home/moxy/EAIR_ws`。

## 3. ROS 1 → ROS 2 迁移方式

| ROS 1 上游 | ROS 2 本移植 |
|---|---|
| catkin `message_generation` | `rosidl_default_generators` + `ament_cmake` |
| `rospy.init_node` / `rospy.spin` | `rclpy` + `SingleThreadedExecutor` / `MultiThreadedExecutor` |
| `rospy.Service` / `ServiceProxy` / `wait_for_service` | `create_service` / `create_client` + 异步 Future + 就绪轮询 |
| `rospy.Subscriber` / `Publisher` | `create_subscription` / `create_publisher` |
| `rospy.get_param("~x")` | ROS 2 参数（节点初始化时声明并校验） |
| `catkin_install_python` | `console_scripts` + `setup.cfg` 的 `install_scripts=$base/lib/<pkg>` |
| `catkin_python_setup` | `ament_python` + `find_packages` |
| XML `.launch` | `launch_ros` Python launch |
| `rospy.Time.now()` | `Node.get_clock().now().to_msg()`；`time_ns` 字段保留 |
| ROS 1 `BehaviorTree.CPP/BehaviorTree.ROS` C++ | 受限 Python XML 解释器（非等价替代） |
| ROS 1 SMACH 占位 | 上游只有 TODO，不适用 |
| `code_executor` 任意 Python | 有意排除 |

### 并发设计

执行器服务回调放在 `ReentrantCallbackGroup`，由
`MultiThreadedExecutor(num_threads=4)` 执行。服务回调内部不 spin 自身节点；
所有动作/观测客户端由一个独立的 `ClientHost` 节点在单独线程中 spin。这样：

- 第二个计划请求可以在另一个线程立即检查租约并返回 `busy`；
- 已完成的服务响应不会被阻塞中的动作等待误判为超时；
- Agent、观测管理器和执行器都不会在回调中调用 `spin_until_future_complete`。

## 4. 上游包、节点、接口与调用关系

```text
ros_agent_srv ──call_llm(LLM)──> llm_node / fake_llm_node
      │                              │
      │                              └── llm_prompt / llm_response (topics)
      │
      ├──get_observation(Observation)──> observation_manager_node
      │                                      ├── String topics
      │                                      └── Observation sub-services
      │
      └──execute_behavior(ExecuteBehavior)──> sequence_executor
                                                 └── AtomicAction services
                                                        (/forward, /turn,
                                                         /strafe, /command)
                                                        └── turtlesim
behavior_tree_executor ── ExecuteBehavior ──> BehaviorTree.CPP/ROS plugins
code_executor ── ExecuteBehavior ──> imports arbitrary Python
VLM / image_summarizer / user_interface_browser ── 独立视觉/GUI 链路
```

## 5. 接口保留与改变

### 保留

- `rosllm_msgs/LLMResponse`、`Observation` 的全部字段名/类型/顺序不变。
- `rosllm_srvs/AtomicAction`、`ExecuteBehavior`、`GetUserInput`、`LLM`、
  `Observation`、`VLM` 的全部字段不变。
- 服务名 `call_llm`、`execute_behavior`、`get_observation`、`call_vlm` 保留
  （按 ROS 2 相对/绝对解析规则可用 `/call_llm` 等绝对名访问）。
- 话题 `llm_prompt`、`llm_response` 保留。
- NDJSON 计划格式保留；`prev_output` 串联语义保留。

### 必须改变

| 改变 | 原因 |
|---|---|
| `LLMResponse.msg` 的 `Header` → `std_msgs/Header` | ROS 2 `rosidl` 要求类型全限定 |
| Turtlesim 动作服务 `/forward` → `/turtle1/forward` 等 | ROS 2 命名空间化，避免全局服务名冲突；动作库/launch 统一使用 |
| `ExecuteBehavior.message` 改为结构化 JSON | 需要区分 busy/timeout/unknown/limit 并携带每步观测；JSON 内仍有 `message` 人类可读字段 |
| 新增 `info` 值 `-7..-13` | 上游只有 `0,1,-1..-6`，不足以表达本移植的安全状态 |
| 动作输入统一要求 JSON 对象 | 拒绝 Python 字面量、NaN/inf、重复键、未知参数 |
| 行为树 XML 子集 | 上游 C++ 依赖 BehaviorTree.CPP/ROS 插件，ROS 2 Humble 无对应发行包；本版不冒充完整移植 |
| 观测新鲜度写入保留文本 `__rosllm_meta__:` | 保持 `Observation.msg` 字段完全不变，同时让 Agent 判断 stale/missing |

`GetUserInput.srv` 保持上游 `string prompt --- string user_input`，但本版没有
实现 Gradio/Whisper GUI 节点。

## 6. 逐模块覆盖表

状态定义：
- **完整移植**：功能与上游等价，且已实际验收。
- **有限替代**：接口/角色保留，但实现范围或语义小于上游，已在 README 标注。
- **未实现**：本版没有功能实现。
- **不适用**：上游本身没有可运行实现，或属于本任务明确排除的范围。

| 上游文件/模块 | 本版对应 | 状态 | 说明 |
|---|---|---|---|
| `rosllm_msgs/msg/LLMResponse.msg` | `rosllm_msgs` | 完整移植 | 仅 Header 全限定 |
| `rosllm_msgs/msg/Observation.msg` | `rosllm_msgs` | 完整移植 | 字段不变 |
| `rosllm_srvs/srv/*.srv`（6 个） | `rosllm_srvs` | 完整移植 | 字段不变 |
| `agent_comm/scripts/fake_llm_node` | `rosllm_agent/fake_llm_node` | 完整移植 | 增加文件缺失/超大/并发请求号处理 |
| `agent_comm/scripts/llm_node` | `rosllm_agent/llm_node` | 完整移植 | 使用标准库 OpenAI 兼容客户端；DeepSeek 真实 API 已验证 |
| `agent_comm/src/agent_comm/llm.py` | `rosllm_core/llm.py` | 有限替代 | 仅 chat completions；无 openai SDK/重试 |
| `agent_comm/scripts/vlm_node` | — | 未实现 | 需要 cv_bridge/OpenCV/视觉 API；`VLM.srv` 保留 |
| `agent_comm/src/agent_comm/vlm.py` | — | 未实现 | 同上 |
| `agent_comm/scripts/user_interface_browser` | — | 未实现 | Gradio + Whisper + 阻塞服务；本版用 CLI |
| `agent_comm/scripts/ros_agent_srv` | `rosllm_agent/ros_agent` | 完整移植 | 保留“任务→观测→提示→计划→执行→反馈”；修正只提取第一个动作的问题；增加严格校验/核验/状态码 |
| `agent_comm/launch/ros_agent.launch` | `rosllm_executor`/CLI 参数 | 有限替代 | 上游示例 launch 只启动 llm_node；本版通过 demo launch + CLI 参数 |
| `behavior_executor/scripts/sequence_executor` | `rosllm_executor/sequence_executor` | 完整移植 | 严格 NDJSON、整计划预检、跨进程互斥、超时/未知、逐步观测 |
| `behavior_executor/scripts/code_executor` | — | 不适用（有意排除） | 执行模型生成的任意 Python，不安全 |
| `behavior_executor/scripts/state_machine_executor` | — | 不适用 | 上游 `execute()` 只有 `pass`/TODO |
| `behavior_executor/src/behavior_tree_executor.cpp` | `rosllm_executor/behavior_tree_executor` | 有限替代 | 受限 Python 解释器；支持 Sequence/Fallback/Retry/Timeout/Action；非 BehaviorTree.CPP |
| `behavior_executor/src/behavior_executor/node.py` | `rosllm_executor/executor_base.py` | 完整移植 | 服务基类、信息码、报告 |
| `behavior_executor/src/behavior_executor/info.py` | `rosllm_core/info.py` | 完整移植 | 保留上游值并扩展 |
| `observation_manager/scripts/observation_manager_node` | `rosllm_observation/observation_manager` | 完整移植 | 增加 staleness/missing 元数据、子服务超时 |
| `observation_manager/scripts/template_summarizer` | `rosllm_observation/template_summarizer` | 完整移植 | 修复格式/输入校验 |
| `observation_manager/scripts/general_sensor_summarizer` | — | 未实现 | 上游文件有语法错误（`rospy.Timer(...)` 缺逗号）、未定义 `deepcopy`、`req.prompt.data` 类型错误 |
| `observation_manager/scripts/image_summarizer` | — | 未实现 | 依赖 VLM，未移植 |
| `observation_manager/config/example.yaml` | 自有 `observation_config.yaml` | 有限替代 | 支持 topic/service 的 name 映射与超时 |
| `rosllm_example/scripts/actions` | `rosllm_turtlesim/turtle_actions` | 完整移植 | 限幅、有限时长、每次结束发零速、停止确认；strafe 明确拒绝 |
| `rosllm_example/scripts/pose_to_array` | `rosllm_turtlesim/turtle_observation` | 有限替代 | 不再发 Float64MultiArray，改发 JSON String，便于核验 |
| `rosllm_example/config/*` | `rosllm_turtlesim/config/*` | 有限替代 | 动作库改为带 schema/verify 的统一 YAML |
| `rosllm_example/launch/example.launch` | `turtlesim_demo.launch.py` | 完整移植 | launch_ros；fake/real LLM、headless、executor 选择 |
| `extern/BehaviorTree.ROS` | — | 未实现 | 不属于 ROSLLM 目录，且无 ROS 2 对应包 |
| `extern/executive_smach` | — | 不适用 | 上游未接入 |
| 上游测试脚本 | 本版 `test/` | 完整替代 | 统一为 pytest + JUnit，接入 colcon |

## 7. 上游缺陷记录（已在本版规避）

1. `general_sensor_summarizer` 存在 Python 语法错误，按原样无法导入。
2. `sequence_executor.execute_atomic_action` 的异常分支引用未绑定的 `resp`。
3. `ros_agent_srv.get_action` 的正则只捕获第一个 JSON 对象，上游提示词却要求
   输出多行动作，导致计划被截断。
4. `ros_agent_srv` 在 LLM 服务异常时返回空字符串并继续执行空行为。
5. `examples/actions.py` 只发布一次速度且从不发零速，Turtlesim 会一直运动。
6. `LLMResponse.msg` 的 `Header` 未全限定，ROS 2 接口生成失败。
7. 上游 `package.xml` 许可证字段不一致（`TODO`/`BSD`/`MIT`）；本版统一按
   HEBO 根许可证 MIT，并保留上游归属。
8. README 说执行器服务叫 `behavior_executor`，代码实际是
   `execute_behavior`；本版以代码和 `ExecuteBehavior.srv` 为准。

## 8. 分阶段结论

- **第一阶段（已完成并验收）**：接口、顺序执行器、观测管理器、fake LLM、
  Turtlesim 动作/观测/launch、CLI Agent，离线全链路。
- **第二阶段（已完成）**：OpenAI 兼容 LLM 节点、参数化配置、环境变量密钥、
  超时/空输出/格式错误/HTTP 错误/并发请求；本地 mock 与 DeepSeek 官方 API
  均已验收。
- **第三阶段（有限实现 + 评估）**：受限行为树；VLM/GUI/图像摘要器/
  `general_sensor_summarizer` 未实现并记录；`code_executor` 排除；
  `state_machine_executor` 不适用；首版不扩展 EAIR 硬件。


## 2026-10-28 独立复核后修复

保留原有消息/服务字段。新增 `std_srvs/Trigger` 类型的
`<execute_service>/confirm_stopped` 恢复入口；超时/未知状态标记存入共享租约文件，
同主机同 robot_id/lease_dir 的执行器必须显式确认后才能恢复计划接收。
Turtlesim 观测仅转发新源位姿，增加 position 对象用于二维位移核验；
原有 x/y/theta 字段保留。动作库新增可选 verify.metric（默认 delta，
另支持 distance_2d），旧动作库和 VerifySpec 位置参数保持兼容。
