import json
import os
import random
import signal
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rosllm_msgs.msg import LLMResponse
from rosllm_srvs.srv import LLM
from std_msgs.msg import String

from rosllm_core.action_client import ClientHost

os.environ["ROS_DOMAIN_ID"] = str(random.randint(10, 220))
os.environ["ROS_LOCALHOST_ONLY"] = "1"
SECRET = "supersecret-test-key"


@pytest.fixture(scope="module", autouse=True)
def ros_context():
    rclpy.init()
    yield
    rclpy.shutdown()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        prompt = body["messages"][0]["content"]
        self.server.requests.append(
            {"prompt": prompt, "auth": self.headers.get("Authorization", "")}
        )
        status = 200
        content = "ok:" + prompt
        if prompt.startswith("slow:"):
            _, delay, content = prompt.split(":", 2)
            time.sleep(float(delay))
        elif prompt == "fail":
            status, content = 500, "server failure"
        elif prompt == "empty":
            content = ""
        elif prompt == "leak":
            status, content = 500, f"server echoed {SECRET}"
        raw = "not json" if prompt == "malformed" else json.dumps(
            {"choices": [{"message": {"content": content}}]}
        )
        payload = raw.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def http_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    httpd.requests = []
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=2.0)


@pytest.fixture(scope="module")
def llm_node(http_server, tmp_path_factory):
    port = http_server.server_address[1]
    env_file = tmp_path_factory.mktemp("llm_env") / "llm.env"
    env_file.write_text(f"# private test config\nTEST_FILE_KEY={SECRET}\n", encoding="utf-8")
    os.chmod(env_file, 0o600)
    env = os.environ.copy()
    env.pop("TEST_KEY", None)
    cmd = [
        "ros2",
        "run",
        "rosllm_agent",
        "llm_node",
        "--ros-args",
        "-p",
        "model:=mock-model",
        "-p",
        f"base_url:=http://127.0.0.1:{port}/v1",
        "-p",
        "timeout_sec:=1.0",
        "-p",
        "api_key_env:=TEST_FILE_KEY",
        "-p",
        f"env_file:={env_file}",
    ]
    log_path = f"/tmp/rosllm_llm_node_{uuid.uuid4().hex[:8]}.log"
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=env,
    )
    probe = ClientHost(f"llm_probe_{uuid.uuid4().hex[:8]}")
    try:
        probe.create_client("/call_llm", LLM)
        ready = probe.wait_ready("/call_llm", 25.0)
    finally:
        probe.shutdown()
    if not ready or proc.poll() is not None:
        log.flush()
        raise AssertionError(f"llm_node failed to start:\n{open(log_path).read()}")
    yield proc
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=8.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=5.0)
    log.close()


@pytest.fixture()
def caller():
    hosts = []

    def factory():
        host = ClientHost(f"llm_caller_{uuid.uuid4().hex[:8]}")
        host.create_client("/call_llm", LLM)
        hosts.append(host)
        return host

    yield factory
    for host in hosts:
        host.shutdown()


def call(host, prompt, timeout=5.0):
    return host.call(
        "/call_llm", LLM.Request(prompt=prompt), timeout, request_id=f"p-{prompt[:8]}"
    )


def test_private_env_file_is_used(llm_node, caller, http_server):
    result = call(caller(), "envfile")
    assert result.ok
    assert result.response.response.success
    assert result.response.response.response == "ok:envfile"
    auths = [
        request["auth"]
        for request in http_server.requests
        if request["prompt"] == "envfile"
    ]
    assert auths and auths[-1] == f"Bearer {SECRET}"


def test_concurrent_requests_get_unique_ids_and_matching_responses(llm_node, caller):
    prompts = [
        "slow:0.20:alpha",
        "slow:0.05:beta",
        "slow:0.12:gamma",
        "slow:0.0:delta",
        "slow:0.16:epsilon",
    ]
    results = {}
    errors = []

    def run(prompt):
        try:
            host = caller()
            result = call(host, prompt, timeout=6.0)
            results[prompt] = result
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(prompt,)) for prompt in prompts]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)
    assert not errors
    assert len(results) == 5
    expected = {prompt.split(":")[2] for prompt in prompts}
    assert {result.response.response.response for result in results.values()} == expected
    ids = [result.response.response.req for result in results.values()]
    assert len(set(ids)) == 5
    assert all(req >= 1 for req in ids)
    assert all(result.response.response.success for result in results.values())


def test_error_paths_do_not_kill_the_node(llm_node, caller):
    host = caller()
    cases = [
        ("fail", "http_error"),
        ("empty", "empty_response"),
        ("malformed", "invalid_response"),
        ("leak", "http_error"),
    ]
    for prompt, code in cases:
        result = call(host, prompt)
        assert result.ok
        message = result.response.response
        assert not message.success
        assert code in message.info
        assert SECRET not in message.info

    ok = call(host, "healthy")
    assert ok.response.response.success
    assert ok.response.response.response == "ok:healthy"


def test_timeout_path(llm_node, caller):
    host = caller()
    result = call(host, "slow:1.6:too_late", timeout=4.0)
    assert result.ok
    message = result.response.response
    assert not message.success
    assert "timeout" in message.info
    assert message.response == ""


class TopicProbe(Node):
    def __init__(self):
        super().__init__(f"topic_probe_{uuid.uuid4().hex[:8]}")
        self.received = []
        self.create_subscription(LLMResponse, "llm_response", self._on_response, 10)
        self.publisher = self.create_publisher(String, "llm_prompt", 10)

    def _on_response(self, message):
        self.received.append(message)


def test_topic_interface_still_works(llm_node):
    probe = TopicProbe()
    executor = MultiThreadedExecutor(num_threads=2)
    executor.add_node(probe)
    thread = threading.Thread(target=executor.spin, daemon=True)
    thread.start()
    try:
        message = String()
        message.data = "topic-test"
        # Wait for the publisher to discover the llm_node subscription.
        deadline = time.monotonic() + 8.0
        while probe.publisher.get_subscription_count() == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        probe.publisher.publish(message)
        deadline = time.monotonic() + 8.0
        while not probe.received and time.monotonic() < deadline:
            time.sleep(0.05)
        assert probe.received
        assert probe.received[0].success
        assert probe.received[0].response == "ok:topic-test"
    finally:
        executor.shutdown(timeout_sec=2.0)
        thread.join(timeout=2.0)
        probe.destroy_node()
