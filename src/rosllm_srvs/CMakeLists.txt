cmake_minimum_required(VERSION 3.8)
project(rosllm_srvs)

find_package(ament_cmake REQUIRED)
find_package(std_msgs REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(rosllm_msgs REQUIRED)
find_package(rosidl_default_generators REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "srv/ExecuteBehavior.srv"
  "srv/AtomicAction.srv"
  "srv/LLM.srv"
  "srv/VLM.srv"
  "srv/Observation.srv"
  "srv/GetUserInput.srv"
  DEPENDENCIES std_msgs sensor_msgs rosllm_msgs
)

ament_export_dependencies(rosidl_default_runtime)
ament_package()
