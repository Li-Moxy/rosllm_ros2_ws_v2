import os
from glob import glob

from setuptools import find_packages, setup

package_name = "rosllm_turtlesim"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        (os.path.join("share", package_name, "config"), glob("config/*")),
        (os.path.join("share", package_name, "launch"), glob("launch/*.launch.py")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    tests_require=["pytest"],
    maintainer="ROSLLM ROS 2 port maintainers",
    maintainer_email="rosllm-ros2-port@example.com",
    description="Turtlesim adapters and offline demo for the unofficial ROSLLM ROS 2 port",
    license="MIT",
    entry_points={
        "console_scripts": [
            "turtle_actions = rosllm_turtlesim.turtle_actions:main",
            "turtle_observation = rosllm_turtlesim.turtle_observation:main",
        ]
    },
)
