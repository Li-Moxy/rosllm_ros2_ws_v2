from setuptools import find_packages, setup

package_name = "rosllm_executor"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    tests_require=["pytest"],
    maintainer="ROSLLM ROS 2 port maintainers",
    maintainer_email="rosllm-ros2-port@example.com",
    description="Behaviour executors for the unofficial ROSLLM ROS 2 port",
    license="MIT",
    entry_points={
        "console_scripts": [
            "sequence_executor = rosllm_executor.sequence_executor:main",
            "behavior_tree_executor = rosllm_executor.behavior_tree_executor:main",
        ]
    },
)
