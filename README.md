# ROSLLM → ROS 2 Humble（非官方移植）

本项目把华为诺亚方舟实验室 HEBO 仓库中的 **ROSLLM** 从 ROS 1 Noetic 移植到
**ROS 2 Humble / Ubuntu 22.04**。这是一个**非官方、独立实现**的移植，不是华为
官方发布，也不是论文实验的完整复现。许可证与上游归属见 `NOTICE.md` 和
`licenses/HEBO_LICENSE_MIT.txt`。

- 上游：<https://github.com/huawei-noah/HEBO/tree/master/ROSLLM>
- 固定上游版本：`master` 分支 commit `77636146d2a2208d971e589062d4ec3d050029f0`
  （2026-10-12）
- 当前状态：离线 fake LLM + Turtlesim 全链路、DeepSeek 真实 API + Turtlesim
  全链路均已实际验收；VLM、GUI、其他 LLM 供应商和真机适配未验证。

> 任务说明中提到的旧目录 `/home/moxy/rosllm_ros2_ws` 和
> `REVIEW_2026-10-28.md` 在本机不存在；只发现了
> `/home/moxy/ROSLLM_ROS2_Humble_Core_Port.zip`。该 zip 仅作为问题参考，
> 本工作区没有复制其代码，全部实现基于上游源码重新完成。

---

## 1. 包与职责

| 包 | 构建类型 | 职责 |
|---|---|---|
| `rosllm_msgs` | `ament_cmake` | `LLMResponse.msg`、`Observation.msg`，字段与上游一致 |
| `rosllm_srvs` | `ament_cmake` | 6 个上游服务定义，字段一致 |
| `rosllm_core` | `ament_python` | 严格 JSON、动作库/计划校验、异步服务客户端、跨进程计划租约、观测新鲜度与核验、LLM 客户端、受限行为树解释器、执行报告 |
| `rosllm_executor` | `ament_python` | `sequence_executor`、`behavior_tree_executor`，计划级互斥、预检、超时/未知状态 |
| `rosllm_agent` | `ament_python` | `ros_agent` CLI、`fake_llm_node`、OpenAI 兼容 `llm_node` |
| `rosllm_observation` | `ament_python` | `observation_manager`、`template_summarizer` |
| `rosllm_turtlesim` | `ament_python` | Turtlesim 原子动作、位姿观测、统一配置与 launch |

数据流：

```text
用户任务
  -> ros_agent: get_observation（缺失/过期检查）
  -> 构造提示词 -> call_llm（fake 或 OpenAI 兼容）
  -> 提取 JSON Lines -> 本地严格校验
  -> execute_behavior（跨进程租约）
       -> 预检全部动作服务
       -> 逐步调用 AtomicAction（独立请求号、截止时间）
       -> 每步成功后抓取观测快照
  -> 按动作库 verify 规则核验观测变化
  -> success_verified / postcondition_failed / ...
```

---

## 2. 环境与依赖

已验证环境：

- Ubuntu 22.04，ROS 2 Humble（`/opt/ros/humble`）
- Python 3.10.12，`colcon`，`pytest` 6.2.5
- `ros-humble-turtlesim`、`geometry_msgs`、`sensor_msgs`、`std_msgs`、
  `launch`、`launch_ros`、`ament_index_python`、`PyYAML`

安装示例（若尚未安装）：

```bash
sudo apt update
sudo apt install -y \
  python3-colcon-common-extensions \
  python3-yaml python3-pytest \
  ros-humble-turtlesim
```

不需要 `openai` Python 包：LLM 客户端只使用标准库 `urllib`。

---

## 3. 构建

在干净终端中只 source ROS 2 和本工作区，避免混入其他工作区的同名包：

```bash
source /opt/ros/humble/setup.bash
cd /home/moxy/rosllm_ros2_ws_v2
colcon build
source install/setup.bash
```

验证：

```bash
ros2 pkg list | grep rosllm
ros2 interface show rosllm_srvs/srv/ExecuteBehavior
ros2 pkg executables rosllm_executor
```

本机 `~/.local` 中较新的 setuptools 会丢弃 `tests_require`，导致 `colcon test`
回退到 unittest 并显示 0 tests。测试时使用：

```bash
PYTHONNOUSERSITE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  colcon test --executor sequential
colcon test-result --verbose
```

（`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` 还避开了本机用户级 anyio pytest 插件与
pytest 6.2.5 不兼容的问题。干净 Humble 环境通常不需要这两个变量。）

---

## 4. 离线 Turtlesim 演示（默认，不连真机）

Terminal 1：

```bash
source /opt/ros/humble/setup.bash
cd /home/moxy/rosllm_ros2_ws_v2
source install/setup.bash

# 独立域，避免与真机/其他实验混在一起。
# launch 在域未设置或为 0 时会自动使用 77；这里显式设置以便 Agent 也在同一域。
export ROS_DOMAIN_ID=77
export ROS_LOCALHOST_ONLY=1

ros2 launch rosllm_turtlesim turtlesim_demo.launch.py
# 无显示器时：
# ros2 launch rosllm_turtlesim turtlesim_demo.launch.py headless:=true
```

Terminal 2：

```bash
source /opt/ros/humble/setup.bash
cd /home/moxy/rosllm_ros2_ws_v2
source install/setup.bash
export ROS_DOMAIN_ID=77
export ROS_LOCALHOST_ONLY=1

SHARE=$(ros2 pkg prefix rosllm_turtlesim)/share/rosllm_turtlesim
ros2 run rosllm_agent ros_agent \
  --task "Move forward, turn left, then move forward again" --json \
  --library "$SHARE/config/action_library.yaml" \
  --expert-advice "$SHARE/config/expert_advice.txt"
```

默认 fake LLM 返回 3 个动作（forward / turn / forward）。成功时输出类似：

```json
{"status":"success_verified","exit_code":0,...}
```

并可在 `verification` 中看到每一步的观测变化（二维位移或 `theta` 的 delta）。
Agent 退出码：`0` 成功；`2` 规划/执行/核验失败；`3` 超时或未知；`4` 忙。

直接调用原子动作（调试）：

```bash
ros2 service call /turtle1/forward rosllm_srvs/srv/AtomicAction \
  '{input: "{\"vel\": 1.0}", prev_output: ""}'
```

> 动作服务位于 `/turtle1/*` 命名空间（上游是根命名空间下的 `/forward` 等）。
> 这是本移植明确改变的接口，原因见 `PORTING.md`。

---

## 5. 真实 LLM（OpenAI 兼容接口）

密钥**只**从环境变量读取，不写入源码、ROS 参数、日志或提交记录。
`llm_node` 支持 `model`、`base_url`、`temperature`、`timeout_sec`、
`api_key_env` 等参数。

```bash
export DEEPSEEK_API_KEY='...'
ros2 launch rosllm_turtlesim turtlesim_demo.launch.py \
  use_fake_llm:=false \
  model:=deepseek-chat \
  base_url:=https://api.deepseek.com/v1 \
  api_key_env:=DEEPSEEK_API_KEY
```

第三方 OpenAI 兼容服务请使用其真实 `base_url` 和模型名，不要假设与
DeepSeek 官方一致。**2026-10-28 已用 DeepSeek 官方 API 完成真实联调**
（`deepseek-flash` 和 `deepseek-chat` 均返回 HTTP 200，完整 Turtlesim 链路
`success_verified`）；其他供应商和模型仍未验证。协议客户端本身还有本地 mock
测试覆盖并发、超时、HTTP 错误、空输出、畸形响应和密钥脱敏。对于非本机地址，
若环境变量缺失，请求会在发出前以 `auth_missing` 结构化失败。

密钥不要放在工作区。推荐：

```bash
mkdir -p ~/.config/rosllm && chmod 700 ~/.config/rosllm
cp .env.example ~/.config/rosllm/llm.env
chmod 600 ~/.config/rosllm/llm.env
# 编辑 ~/.config/rosllm/llm.env，填入 DEEPSEEK_API_KEY
set -a; source ~/.config/rosllm/llm.env; set +a
```

本工作区已把原先的 `src/.env` 移到 `~/.config/rosllm/llm.env`，并在
`.gitignore` 中忽略 `.env`；仓库只保留无密钥的 `.env.example`。

---

## 6. 接口

### 6.1 消息与服务（字段与上游一致）

- `/call_llm` `rosllm_srvs/srv/LLM`：`prompt -> LLMResponse`
- `/llm_prompt` `std_msgs/msg/String`，`/llm_response` `rosllm_msgs/msg/LLMResponse`
- `/execute_behavior` `rosllm_srvs/srv/ExecuteBehavior`
- `/get_observation` `rosllm_srvs/srv/Observation`
- `/turtle1/forward`、`/turtle1/turn`、`/turtle1/command`
  `rosllm_srvs/srv/AtomicAction`
- `AtomicAction`：`input`、`prev_output -> success`、`output`
- `Observation`：`name`、`text[]`、`image[]`

`ExecuteBehavior.message` 现在携带机器可读 JSON：

```json
{
  "schema": "rosllm.execute.v1",
  "plan_id": "sequence_executor-1234-1",
  "robot_id": "turtlesim_demo",
  "status": "ok|invalid|busy|timeout|failed|unavailable|unknown|limit",
  "info": 0,
  "message": "all 3 step(s) executed successfully",
  "total_steps": 3,
  "completed_steps": 3,
  "elapsed_sec": 3.9,
  "steps": [
    {
      "index": 0, "name": "forward", "status": "ok", "info": 0,
      "elapsed_sec": 1.1, "request_id": "sequence_executor-1234-1:s0",
      "output": "{\"x\":6.55,...}",
      "observation": {"name":"turtle_pose","status":"fresh","age_sec":0.04,"payload":{"x":6.55}}
    }
  ]
}
```

`info` 沿用上游值，并新增：

| 值 | 含义 |
|---|---|
| `0` | OK |
| `1` | 空行为（上游 `NULL_BEHAVIOR`） |
| `-2` | 动作服务不可用 |
| `-6` | 动作明确失败 |
| `-7` | 计划非法 |
| `-8` | 忙，已有计划在执行 |
| `-9` | 超时 |
| `-10` | 状态未知（可能已执行） |
| `-11` | 未核验 |
| `-12` | 超出限制 |
| `-13` | 已停止/取消 |

### 6.2 动作库（单一事实来源）

同一个 YAML 同时驱动：Agent 提示词、执行器白名单、输入校验、观测核验。
示例见 `src/rosllm_turtlesim/config/action_library.yaml`：

```yaml
version: 1
actions:
  forward:
    service: /turtle1/forward
    description: |
      Move forward for 1 second, then stop.
    parameters:
      vel: {type: number, required: true, min: -2.0, max: 2.0}
    verify:
      observation: turtle_pose
      field: position
      metric: distance_2d
      min_abs_delta: 0.05
      direction: any
```

核心 Agent 通过 `action_library_path` 使用任意自定义动作库，不依赖
Turtlesim 包。

### 6.3 主要参数

执行器（`sequence_executor` / `behavior_tree_executor`）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `action_library_path` | 必填 | 动作库 YAML |
| `robot_id` | `default` | 计划租约作用域 |
| `lease_dir` | `/tmp` | 跨进程锁目录 |
| `service_name` | `execute_behavior` | 服务名 |
| `step_timeout_sec` | `30.0` | 单动作预算 |
| `plan_timeout_sec` | `90.0` | 整计划预算（含预检/快照） |
| `max_steps` | `8` | 步骤上限 |
| `max_plan_bytes` | `16384` | 计划总字节上限 |
| `max_step_input_bytes` | `4096` | 单步输入上限 |
| `preflight_timeout_sec` | `2.0` | 预检等待 |
| `observation_service` | `get_observation` | 快照来源 |
| `observation_timeout_sec` | `1.0`（demo launch 中为 `3.0`） | 快照超时 |
| `observation_settle_sec` | `0.15` | 快照前稳定等待 |
| `enable_observation_snapshots` | `true` | 是否记录每步观测 |
| `bt_max_nodes` / `bt_max_depth` | `64` / `12` | 行为树结构上限 |
| `bt_max_action_calls` | `32` | 最坏情况动作调用上限 |
| `bt_max_runtime_sec` | `60.0` | 行为树运行上限 |
| `bt_max_input_bytes` | `32768` | XML 上限 |

Agent（`ros_agent`）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `action_library_path` | 必填 | 动作库 |
| `llm_service` / `execute_service` / `observation_service` | `/call_llm` / `/execute_behavior` / `/get_observation` | 依赖服务 |
| `max_steps` | `8` | 本地计划校验上限 |
| `max_plan_bytes` | `16384` | 本地计划上限 |
| `llm_timeout_sec` | `60.0` | LLM 调用 |
| `execute_timeout_sec` | `95.0` | 执行调用，应大于执行器 `plan_timeout_sec` |
| `observation_timeout_sec` | `2.0` | 观测调用 |
| `max_observation_age_sec` | `3.0` | 观测过期阈值 |
| `require_observation_age` | `false` | 是否要求带 age 元数据 |
| `require_fresh_observation` | `false` | 无核验规则时是否仍要求观测 |
| `use_observations` | `true` | 是否采集观测 |
| `expert_advice_path` | `""` | 专家提示文件 |
| `max_task_bytes` | `4096` | 任务长度上限 |

`llm_node`：`model`、`base_url`、`temperature`、`timeout_sec`、`api_key_env`、
`max_response_bytes`、`num_workers`、`log_prompts`。
`fake_llm_node`：`path`、`delay_sec`。
`observation_manager`：`config_filename`、`staleness_sec`、`total_timeout_sec`、
`max_text_bytes`、`service_name`。
`turtle_actions`：`service_prefix`、`cmd_vel_topic`、`pose_topic`、
`max_linear_vel`、`max_angular_vel`、`move_duration_sec`、`publish_hz`、
`stop_timeout_sec`。

---

## 7. 安全与执行语义

1. **计划级互斥**：执行器用 `/tmp/rosllm_plan_<robot_id hash>.lock` 的
   `flock` 做跨进程互斥。正常执行中第二个计划立即返回
   `success=false, info=-8`，不执行任何动作。测试覆盖同进程并发和两个执行器
   进程。
2. **先校验后执行**：整份 NDJSON 先逐行解析、按动作库校验名称/参数类型/范围/
   有限数值/长度/步数，再预检**所有**动作服务是否存在；任何一项失败都不会
   执行前面的动作。
3. **超时语义**：服务客户端超时只取消本地等待，**不代表服务端动作停止**。
   超时返回 `-9`，并在共享租约文件中持久化待确认标记；后续计划返回 `-10`，
   直到确认停止后调用恢复服务。节点重启不会清除该标记。不会自动重发动作或整份计划。ROS 2
   service 不支持取消远端执行；若换成 ROS Action，需要另行定义取消/反馈
   语义（本版未使用 ROS Action）。
4. **失败/超时/未知区分**：动作明确 `success=false` 是 `failed`；本地超时是
   `timeout`；传输错误可能已执行，是 `unknown`；服务不存在是 `unavailable`。
5. **观测新鲜度**：观测管理器在 `Observation.text` 中附加保留元数据：
   `__rosllm_meta__:{"status":"fresh|stale|missing|error","age_sec":...}`。
   Turtlesim 转换节点仅发布新收到的源位姿，断流后不会重复刷新旧数据。
   Agent 在规划前拒绝缺失/过期观测。没有元数据的自定义观测为 `unknown_age`，
   可用 `require_observation_age:=true` 强制要求。
6. **结果核验**：执行器在每步成功后抓取观测快照；Agent 按动作库的 `verify`
   比较步前/步后字段变化。服务调用成功不会被直接当作任务成功：
   `success_verified` 需要所有声明了 `verify` 的动作都通过；没有 `verify` 规则
   时最多报告 `success_unverified`；快照缺失时报告
   `verification_unavailable`。
7. **行为树**：先编译整棵树（节点数、深度、最坏情况动作调用数、输入模式），
   再执行。`Retry` 只在明确失败时重试；超时/未知/不可用会中止整棵树，不触发
   `Fallback` 或再次重试，避免静默放大动作次数。它是受限 Python 解释器，
   **不是** BehaviorTree.CPP 移植。
8. **不执行模型生成的任意代码**：上游 `code_executor` 被有意排除。
9. **离线优先**：默认 launch 只启动 fake LLM + Turtlesim；示例命令使用
   `ROS_DOMAIN_ID=77` 和 `ROS_LOCALHOST_ONLY=1`，不涉及真机。

---

## 8. 测试

最终验收使用干净环境构建和顺序测试：

```bash
PYTHONNOUSERSITE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  colcon test --executor sequential
colcon test-result --verbose
```

修复后重新实测：**177 tests, 0 errors, 0 failures, 0 skipped**。

按包统计：core 129、executor 20、agent 20、observation 5、turtlesim 3。
包含断流恢复、超时后跨进程/重启保护、显式恢复、客户端销毁、四个方向位移
核验和非法后继参数零执行等回归用例。

详细命令、逐项验收矩阵、旧版问题与环境说明见 `TEST_REPORT.md`；逐模块覆盖
状态见 `PORTING.md`。

---

## 超时后的恢复流程（2026-10-28 修复）

执行器超时/状态未知后，在相同主机、相同 `lease_dir` 和 `robot_id` 下拒绝新计划。
需先通过控制器状态、现场检查等方式确认旧动作已结束且机器人已停止，再调用：

```bash
ros2 service call /execute_behavior/confirm_stopped std_srvs/srv/Trigger '{}'
```

服务只表示调用者确认停止，不发送停止命令，也不自动检查硬件状态；活动计划
尚未返回时拒绝恢复。自定义 `service_name` 时，恢复服务名为其后加
`/confirm_stopped`。持久化标记与租约存于同一文件；不要删除文件来绕过保护。
默认 `/tmp` 在系统重启/清理后可能消失，此机制不代替控制器、硬件安全或跨主机协调。

新增 ROS 依赖：`std_srvs`；未增加 pip 依赖。观测保留原有 x/y 字段，并新增
`position: {x, y}`，供 `metric: distance_2d` 计算与朝向无关的位移。默认横移
参数用 `enum: [0.0]` 在整个计划执行前拒绝非法值。

## 9. 已知限制

- DeepSeek 真实 API 已验证；其他 LLM 供应商/模型、VLM、Gradio/Whisper GUI、
  图像摘要器未实现/未验证。
- 行为树只支持 `Sequence`、`Fallback`/`Selector`、
  `RetryUntilSuccessful`、`Timeout`、`Action`；不支持上游
  BehaviorTree.CPP 的插件、端口映射、异步节点等。
- `code_executor` 故意不移植；`state_machine_executor` 上游本身只有 TODO。
- Turtlesim 没有横向运动，`/turtle1/command` 的 `strafe_vel` 必须为 0。
- 动作停止确认依赖 `/turtle1/pose`；如果位姿话题长时间不更新，动作会明确
  报告 `stop_confirmed=false` 并失败，而不是假装成功。
- 服务超时不能停止远端动作；真机部署前必须增加独立看门狗、速度/位置限制、
  急停和硬件适配层。
- 观测元数据使用保留文本前缀，是为保持 `rosllm_msgs/Observation.msg` 字段
  兼容而做的约定；不使用该约定的第三方观测只能标记为 `unknown_age`。
- 不扩展到 `/home/moxy/EAIR_ws` 的硬件适配；本工作区未修改它。

## 10. 引用

如果使用上游 ROSLLM 的概念或接口，请引用：

> C. E. Mower et al., *ROS-LLM: A ROS framework for embodied AI with task
> feedback and structured reasoning*, arXiv:2406.19741, 2024.
> <https://github.com/huawei-noah/HEBO/tree/master/ROSLLM>
