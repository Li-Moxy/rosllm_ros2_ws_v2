# 实际验收报告

> 最新修复验收：177 tests，0 errors、0 failures、0 skipped。详见文末第 9 节；
> 原第 1–8 节保留首版验收历史，不能代表修复后的当前测试数量。

日期：2026-10-28
工作区：`/home/moxy/rosllm_ros2_ws_v2`
上游：HEBO `master` @ `77636146d2a2208d971e589062d4ec3d050029f0`

## 1. 环境

| 项 | 值 |
|---|---|
| OS | Ubuntu 22.04 |
| ROS | ROS 2 Humble（`/opt/ros/humble`） |
| Python | 3.10.12 |
| colcon | `/usr/bin/colcon` |
| pytest | 6.2.5 |
| Turtlesim | `ros-humble-turtlesim` 已安装 |
| 测试域 | 每个测试进程随机 `ROS_DOMAIN_ID`，`ROS_LOCALHOST_ONLY=1` |
| 手动演示域 | `ROS_DOMAIN_ID=77`，`ROS_LOCALHOST_ONLY=1` |

说明：任务描述中的旧目录 `/home/moxy/rosllm_ros2_ws` 和
`REVIEW_2026-10-28.md` 在本机不存在。只找到
`/home/moxy/ROSLLM_ROS2_Humble_Core_Port.zip`，已解压作为问题参考，未复制其
代码。本报告只记录 v2 的实测结果。

## 2. 干净构建

命令（`env -i`，只 source ROS 2）：

```bash
cd /home/moxy/rosllm_ros2_ws_v2
rm -rf build install log
source /opt/ros/humble/setup.bash
PYTHONNOUSERSITE=1 colcon build
```

结果：

```text
Summary: 7 packages finished [8.33s]
```

7 个包：`rosllm_msgs`、`rosllm_srvs`、`rosllm_core`、`rosllm_executor`、
`rosllm_agent`、`rosllm_observation`、`rosllm_turtlesim`。

后续增量构建与最终测试的构建：

```text
Summary: 7 packages finished [2.48s]
```

## 3. 接口与入口检查

```bash
source /opt/ros/humble/setup.bash
source install/setup.bash
ros2 pkg list | grep rosllm
ros2 interface show rosllm_msgs/msg/LLMResponse
# ... 其余 7 个接口
ros2 pkg executables rosllm_agent
ros2 pkg executables rosllm_executor
ros2 pkg executables rosllm_observation
ros2 pkg executables rosllm_turtlesim
ros2 launch rosllm_turtlesim turtlesim_demo.launch.py --show-args
```

结果：

- 7 个包全部可发现。
- 2 个消息、6 个服务全部 `ros2 interface show` 成功：
  `LLMResponse`、`Observation`、`AtomicAction`、`ExecuteBehavior`、`LLM`、
  `VLM`、`Observation`、`GetUserInput`。
- 9 个可执行入口可发现：

```text
rosllm_agent      fake_llm_node
rosllm_agent      llm_node
rosllm_agent      ros_agent
rosllm_executor   behavior_tree_executor
rosllm_executor   sequence_executor
rosllm_observation observation_manager
rosllm_observation template_summarizer
rosllm_turtlesim  turtle_actions
rosllm_turtlesim  turtle_observation
```

- `ros2 launch ... --show-args` 正常列出 14 个 launch 参数。
- `ros2 run rosllm_agent ros_agent --help` 正常。

## 4. 测试命令与总数

本机 `~/.local` 的 setuptools 79 会丢弃 `tests_require`，使 colcon 回退到
unittest 并显示 0 tests；用户级 anyio pytest 插件与 pytest 6.2.5 也不兼容。
因此实际验收使用：

```bash
source /opt/ros/humble/setup.bash
source install/setup.bash
PYTHONNOUSERSITE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  colcon test --executor sequential
colcon test-result --verbose
```

最终结果：

```text
Summary: 157 tests, 0 errors, 0 failures, 0 skipped
```

| 包 | 单元测试 | ROS 2 集成测试 | 合计 |
|---|---:|---:|---:|
| `rosllm_core` | 113 | 0 | 113 |
| `rosllm_executor` | 4 | 15 | 19 |
| `rosllm_agent` | 8 | 11（Agent 7 + LLM 4） | 19 |
| `rosllm_observation` | 2 | 3 | 5 |
| `rosllm_turtlesim` | 0 | 1 | 1 |
| **合计** | **127** | **30** | **157** |

这 157 项全部由 pytest 在 `colcon test` 中实际执行，并生成
`build/<pkg>/pytest.xml`；`colcon test-result` 读取的正是这些结果。
构建成功、单元测试通过、集成测试通过分开记录，未把 0 tests 当作通过。

### 4.1 核心单元测试（113）

- `test_bt.py` 28：整树预校验、非法后继零调用、嵌套重试预算、超时/未知不重试、
  深度/节点/XML 大小限制、DTD 拒绝、多树选择、运行预算。
- `test_jsonutil.py` 10：NaN/Infinity、重复键、畸形 JSON、深层嵌套。
- `test_lease.py` 5：同进程冲突、两个实例冲突、不同机器人独立、子进程死亡
  自动释放。
- `test_library.py` 22：YAML/schema/参数范围/枚举/verify/描述文件错误。
- `test_llm_client.py` 18：真实 HTTP mock 的成功、鉴权头、超时、HTTP 错误、
  空输出、畸形响应、超大响应、密钥脱敏、配置校验。
- `test_observation.py` 12：fresh/stale/missing/error/unknown_age、元数据、
  数值变化/方向/阈值核验。
- `test_plan.py` 18：未知动作、错误参数、NaN、重复键、超限步数/字节、
  空计划、NDJSON 往返。

### 4.2 执行器集成测试（15）

`rosllm_executor/test/test_executor_integration.py`：

- 两步成功、`prev_output` 串联、每步观测快照。
- 空计划/畸形 JSON/未知动作/错误参数/NaN/多余键/超限步数 → 全部在任何动作
  执行前拒绝，动作调用计数为 0。
- 服务不可用预检 → `-2`，零动作。
- 明确失败停止后续步骤。
- 动作延迟超过单步预算 → `-9`，只调用一次；等待 1.2 s 后仍只有一次（无重试，
  迟到响应被忽略）。
- 同进程并发计划：一个成功，另一个立即 `-8`；随后可再次执行。
- 两个执行器进程共享 `robot_id`：第二个被文件租约拒绝；前一个结束后可执行。
- 观测 stale/missing/error 快照状态正确。
- 行为树：非法后继零调用；重试预算预检拒绝；明确失败重试 3 次；超时不重试；
  成功后每步快照。
- SIGINT 后进程退出码 0，随后同 `robot_id` 的新执行器可正常获取租约。

### 4.3 Agent 集成测试（7）

`rosllm_agent/test/test_agent_integration.py`：

- fake LLM 多动作计划 → `success_verified`，x/theta 变化通过核验。
- 动作“成功”但观测无变化 → `postcondition_failed`，不自动重试。
- 观测 stale/missing → `observation_unavailable`，LLM 和执行器均未被调用。
- LLM 空输出 → `llm_error`，零动作。
- 非法计划 → `plan_rejected`，零动作。
- LLM 服务不存在 → `llm_error`，零动作。

### 4.4 LLM 节点集成测试（4）

`rosllm_agent/test/test_llm_node_integration.py`，对本地 mock OpenAI 兼容
HTTP 服务：

- 5 个并发请求：响应与提示词一一对应，`req` 编号互不相同且 ≥1。
- HTTP 500 / 空输出 / 畸形 JSON / 密钥回显：结构化失败，节点继续服务；
  错误信息不含密钥。
- 超时：结构化 `timeout`，空响应。
- 话题接口 `llm_prompt` / `llm_response` 仍可用。

### 4.5 观测管理器测试（3）

- 新鲜话题：`fresh`、payload、age < 1 s。
- 过期与从未收到：`stale` / `missing`，payload 保留但 `is_fresh` 为假。
- 子服务观测 + 不存在子服务：成功返回、失败不导致节点退出。

### 4.6 真实 DeepSeek API（手工验收，不计入 157 项自动测试）

2026-10-28 使用 `~/.config/rosllm/llm.env` 中的私有 DeepSeek 密钥（权限 600，
不在工作区）实际调用官方 API：

- 直接客户端：`deepseek-flash`、`deepseek-chat` 均 HTTP 200，返回 `OK`。
- 实际 ROSLLM 提示词（动作库 + 观测 + 任务）返回：

```json
{"name":"forward","input":{"vel":1.0}}
{"name":"turn","input":{"vel":1.0}}
```

- 完整 launch（`use_fake_llm:=false`，`model:=deepseek-flash`，
  `base_url:=https://api.deepseek.com/v1`）：
  - Agent 退出码 0，状态 `success_verified`；
  - 2 步全部完成，观测变化 `x +1.008`、`theta +1.008`；
  - 总耗时约 7.4 s，其中 LLM 约 1.25 s。
- 密钥卫生：`src/.env` 已移除并迁移到 `~/.config/rosllm/llm.env`；
  `.gitignore` 忽略 `.env`，工作区只保留 `.env.example`；对 launch 日志、
  Agent stdout/stderr 做字面扫描，未发现密钥。

该验收依赖网络和私有密钥，未加入 `colcon test`，因此自动测试总数仍是 157。

### 4.7 Turtlesim 全链路（1）

`rosllm_turtlesim/test/test_turtlesim_integration.py` 启动默认 launch
（`headless:=true`），运行真实 Turtlesim、动作节点、观测发布、观测管理器、
fake LLM、sequence_executor 和 Agent CLI：

- fake LLM 计划包含 **3 个动作**：forward、turn、forward。
- Agent 返回 `returncode == 0`、`success_verified`。
- 每步 `verify` 通过；一次实际运行的 delta 为
  `x +1.008`、`theta +0.992`、`x +0.441`（不同运行有正常数值浮动）。
- 最终观测 `turtle_pose` 为 fresh，`x` 比初始值大 0.5 以上，
  `|theta| > 0.5`。
- 非法动作输入（vel=5、NaN、多余键、strafe≠0、非 JSON）全部 `success=false`，
  节点继续服务。
- launch 终止后 6 个子进程都输出 `process has finished cleanly`，日志无
  Traceback。

## 5. 逐项验收矩阵

| 要求 | 实际证据 | 结果 |
|---|---|---|
| 干净环境 colcon build | 7/7 包，0 失败 | 通过 |
| `ros2 pkg` / `interface` / `run` / `launch` | 7 包、8 接口、9 入口、launch 参数 | 通过 |
| 核心节点构造与默认 launch 冒烟 | Turtlesim 集成测试、全节点 clean exit | 通过 |
| fake LLM 至少两个动作 + Turtlesim 位姿变化 | 3 个动作，逐步 delta 断言 | 通过 |
| 未知动作、错误参数、超限步骤 | 执行器非法计划测试，零动作 | 通过 |
| 畸形 JSON / XML | 执行器/核心测试，结构化失败，节点不退出 | 通过 |
| 并发计划只接受一个 | 同进程 + 跨进程租约测试，另一个 `-8` | 通过 |
| 服务不可用/失败/延迟/超时/迟到响应 | 预检 `-2`、失败 `-6`、超时 `-9`、迟到不重发 | 通过 |
| 超时后不自动重复动作/计划 | 动作调用计数保持 1；Agent 无重试 | 通过 |
| 观测缺失/过期 | Agent `observation_unavailable`；管理器 stale/missing | 通过 |
| BT 非法后继不先执行前序动作 | 编译阶段拒绝，调用计数 0 | 通过 |
| 嵌套重试受总预算限制 | `10 × 4` 最坏调用 >32，预检拒绝 | 通过 |
| 并发 LLM 请求编号/响应关联 | 5 并发，`req` 唯一，内容匹配 | 通过 |
| 正常退出/中断资源清理 | SIGINT 退出码 0、租约释放、launch 全子进程 clean exit | 通过 |
| `colcon test` + `colcon test-result` | 157 tests, 0 failures | 通过 |

## 6. 未验证/未覆盖

- **其他 LLM 供应商/模型**：DeepSeek 官方 API 已验证；其他供应商、模型别名、
  限流和输出差异未验证。
- **VLM / 图像摘要 / Gradio+Whisper GUI**：未实现，未验证。
- **完整 BehaviorTree.CPP**：未移植，只验证了受限解释器的子集。
- **真机 / EAIR 硬件**：明确不在本版范围，未连接、未控制任何真机。
- **Turtlesim GUI 窗口模式**：集成测试使用 `headless:=true`；GUI 模式使用同一
  组节点，但未单独做窗口验收。
- **服务端取消**：ROS 2 service 没有取消语义；超时只停止本地等待，远端动作
  可能继续。已在 README 中作为部署边界说明。

## 7. 稳定性说明

Turtlesim 的停止确认依赖 `/turtle1/pose`，在负载较高时曾出现偶发
`stop_confirmed=false`；另一次运行中出现执行器 1.0 s 观测快照超时，导致
`postcondition_failed`。处理方式：

- 动作只发布一次速度命令，再显式发布零速，避免高频指令队列造成过冲；
- 停止确认接受“最近 0.35 s 内收到的零线速度/角速度位姿”；
- 默认停止等待从 0.5 s 调整为 1.0 s，失败时明确返回 `success=false`，不自动
  重发；
- demo launch 将执行器 `observation_timeout_sec` 设为 3.0 s，观测管理器
  `total_timeout_sec` 设为 2.0 s，给负载下的 DDS 响应留出余量。

调整后 Turtlesim 集成测试连续 5 次通过；最终完整套件再次通过（157/157）。
即使确认失败，动作也不会被自动重发，且会报告未确认，而不是假装停止成功；
观测快照超时同样不会被当成“已核验”。

## 8. 复现命令汇总

```bash
# 构建
source /opt/ros/humble/setup.bash
cd /home/moxy/rosllm_ros2_ws_v2
PYTHONNOUSERSITE=1 colcon build
source install/setup.bash

# 测试
PYTHONNOUSERSITE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  colcon test --executor sequential
colcon test-result --verbose

# 离线演示
export ROS_DOMAIN_ID=77 ROS_LOCALHOST_ONLY=1
ros2 launch rosllm_turtlesim turtlesim_demo.launch.py headless:=true
# 另一个终端
SHARE=$(ros2 pkg prefix rosllm_turtlesim)/share/rosllm_turtlesim
ros2 run rosllm_agent ros_agent \
  --task "Move forward, turn left, then move forward again" --json \
  --library "$SHARE/config/action_library.yaml"
```


## 9. 独立复核后修复验收（2026-10-28）

修复独立报告中的五项问题：未知动作状态后的计划隔离；源位姿断流；
ClientHost 节点销毁；前进二维位移核验；横移参数预检。

本次使用仅加载 Humble 的环境，重新构建到 `/tmp/rosllm_v2_fix`，再运行：

```bash
source /opt/ros/humble/setup.bash
source /tmp/rosllm_v2_fix/install/setup.bash
PYTHONNOUSERSITE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  colcon --log-base /tmp/rosllm_v2_fix/test-log test \
  --base-paths /home/moxy/rosllm_ros2_ws_v2/src \
  --build-base /tmp/rosllm_v2_fix/build \
  --install-base /tmp/rosllm_v2_fix/install --executor sequential
colcon test-result --test-result-base /tmp/rosllm_v2_fix/build --verbose
```

实际结果：7 包构建完成，177 tests, 0 errors, 0 failures, 0 skipped。
按包：core 129、executor 20、agent 20、observation 5、turtlesim 3。
相较独立复核时的 160 项，新增 17 项；另在正常并发测试中增加活动计划拒绝恢复断言。

- 超时后新计划被拒绝，执行器重启后仍被拒绝；桩动作确认完成、显式恢复后才接受。
- 确认服务不能解除正在执行计划的锁；同主机共享状态对其他执行器有效。
- 一帧源位姿后停止发送，会变为 stale；重新发送源位姿恢复 fresh。
- shutdown 后 ROS 节点 handle 无效，重复 shutdown 不报错。
- 向 ±x、±y 移动均可通过二维位移核验；不动和错误坐标被拒绝。
- 非零 strafe 的混合计划在预检阶段拒绝，completed_steps=0，steps=[]。
- 默认 fake LLM + 真实 Turtlesim 三步链路继续通过。

真实模型 API、本机以外的控制器、EAIR 仿真和真机未在本次修复中验证。
确认停止服务是显式声明，不是硬件停止检测，也不是远端取消服务。
