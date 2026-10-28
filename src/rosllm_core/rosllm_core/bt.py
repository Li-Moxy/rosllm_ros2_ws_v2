"""A deliberately small, safe XML behaviour-tree interpreter.

This is **not** a BehaviorTree.CPP port.  It implements only the subset needed
by the upstream example (Sequence, Fallback, RetryUntilSuccessful, Timeout and
service Action nodes) and validates the whole tree, including worst-case retry
multiplication, before the first action is allowed to run.

Safety rules:

* unknown tags/attributes are rejected instead of ignored;
* malformed XML and DTD/entity declarations are rejected;
* node count, depth, total action calls and runtime are bounded;
* a retry/fallback never runs after a timeout or an unknown state, because that
  would silently resend an action whose effect is not known.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from .info import Info
from .jsonutil import dumps
from .library import ActionLibrary, ActionSpec
from .report import StepRecord

_CONTROL_TAGS = {"Sequence", "Fallback", "Selector", "RetryUntilSuccessful", "Timeout"}
_ACTION_TAGS = {"Action"}
_ALLOWED_ROOT_ATTRS = {"main_tree_to_execute"}
_ALLOWED_TREE_ATTRS = {"ID"}
_MAX_XML_BYTES = 256 * 1024


class TreeError(ValueError):
    def __init__(self, message: str, info: Info = Info.INVALID_BEHAVIOR):
        super().__init__(message)
        self.info = info


@dataclass(frozen=True)
class BTBudget:
    max_nodes: int = 64
    max_depth: int = 12
    max_action_calls: int = 32
    max_runtime_sec: float = 60.0
    max_input_bytes: int = 32768


@dataclass
class BTNode:
    kind: str
    name: str = ""
    children: list["BTNode"] = field(default_factory=list)
    spec: ActionSpec | None = None
    input_text: str = ""
    attempts: int = 1
    timeout_sec: float | None = None


@dataclass(frozen=True)
class CompiledTree:
    root: BTNode
    node_count: int
    max_depth: int
    worst_case_actions: int
    actions: tuple[ActionSpec, ...]


class NodeStatus(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    ABORT = "abort"


@dataclass(frozen=True)
class ActionOutcome:
    """Result of one atomic action, supplied by the executor."""

    result: str  # success | failed | timeout | unavailable | unknown | cancelled
    output: str = ""
    error: str = ""
    request_id: str = ""
    elapsed_sec: float = 0.0
    info: int = int(Info.OK)
    observation: dict | None = None


@dataclass
class BTExecutionResult:
    status: NodeStatus
    info: int
    message: str
    steps: list = field(default_factory=list)
    action_calls: int = 0
    elapsed_sec: float = 0.0


def compile_tree(
    xml_text: str,
    library: ActionLibrary,
    budget: BTBudget | None = None,
) -> CompiledTree:
    if budget is None:
        budget = BTBudget()
    if not isinstance(xml_text, str):
        raise TreeError("behaviour tree must be a string")
    encoded = xml_text.encode("utf-8")
    if len(encoded) > budget.max_input_bytes:
        raise TreeError(
            f"behaviour tree is {len(encoded)} bytes, limit is {budget.max_input_bytes}",
            Info.LIMIT_EXCEEDED,
        )
    lowered = xml_text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise TreeError("DOCTYPE/ENTITY declarations are not allowed")
    try:
        root_element = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise TreeError(f"invalid XML: {exc}") from None

    selected = _select_tree_root(root_element)
    counter = {"nodes": 0, "worst": 0, "max_depth": 0}
    action_nodes: list[BTNode] = []

    def compile_element(element: ET.Element, depth: int) -> BTNode:
        counter["nodes"] += 1
        counter["max_depth"] = max(counter["max_depth"], depth)
        if counter["nodes"] > budget.max_nodes:
            raise TreeError(
                f"tree has more than {budget.max_nodes} nodes", Info.LIMIT_EXCEEDED
            )
        if depth > budget.max_depth:
            raise TreeError(
                f"tree depth exceeds {budget.max_depth}", Info.LIMIT_EXCEEDED
            )
        tag = element.tag
        if tag in _CONTROL_TAGS or tag in _ACTION_TAGS:
            return compile_behavior(element, depth)
        raise TreeError(f"unsupported behaviour-tree node {tag!r}")

    def compile_behavior(element: ET.Element, depth: int) -> BTNode:
        tag = element.tag
        children = list(element)
        if tag == "Action":
            if children:
                raise TreeError("Action node must not have child nodes")
            if set(element.attrib) - {"name", "input"}:
                raise TreeError(
                    f"Action node has unsupported attributes {sorted(set(element.attrib) - {'name', 'input'})!r}"
                )
            name = element.attrib.get("name", "")
            if not name:
                raise TreeError("Action node is missing 'name'")
            spec = library.get(name)
            if spec is None:
                raise TreeError(
                    f"Action {name!r} is not in the allowed action library",
                    Info.INVALID_BEHAVIOR,
                )
            raw_input = element.attrib.get("input", "")
            ok, normalized, error = spec.validate_input(raw_input)
            if not ok:
                raise TreeError(f"Action {name!r}: {error}")

            node = BTNode(
                kind="action",
                name=name,
                spec=spec,
                input_text=dumps(normalized),
            )
            action_nodes.append(node)
            return node

        if tag in ("Sequence", "Fallback", "Selector"):
            if not children:
                raise TreeError(f"{tag} node must have at least one child")
            if set(element.attrib):
                raise TreeError(f"{tag} node must not have attributes")
            kind = "fallback" if tag in ("Fallback", "Selector") else "sequence"
            compiled = [compile_element(child, depth + 1) for child in children]
            return BTNode(kind=kind, children=compiled)

        if tag == "RetryUntilSuccessful":
            if len(children) != 1:
                raise TreeError("RetryUntilSuccessful must have exactly one child")
            unknown = set(element.attrib) - {"num_attempts"}
            if unknown:
                raise TreeError(
                    f"RetryUntilSuccessful has unsupported attributes {sorted(unknown)!r}"
                )
            raw = element.attrib.get("num_attempts")
            try:
                attempts = int(raw)
            except (TypeError, ValueError):
                raise TreeError("RetryUntilSuccessful needs integer 'num_attempts'") from None
            if not 1 <= attempts <= 10:
                raise TreeError("RetryUntilSuccessful num_attempts must be within 1..10")
            child = compile_element(children[0], depth + 1)
            return BTNode(kind="retry", children=[child], attempts=attempts)

        if tag == "Timeout":
            if len(children) != 1:
                raise TreeError("Timeout must have exactly one child")
            unknown = set(element.attrib) - {"msec"}
            if unknown:
                raise TreeError(f"Timeout has unsupported attributes {sorted(unknown)!r}")
            raw = element.attrib.get("msec")
            try:
                msec = int(raw)
            except (TypeError, ValueError):
                raise TreeError("Timeout needs integer 'msec'") from None
            if not 1 <= msec <= 600000:
                raise TreeError("Timeout msec must be within 1..600000")
            child = compile_element(children[0], depth + 1)
            return BTNode(kind="timeout", children=[child], timeout_sec=msec / 1000.0)

        raise TreeError(f"unsupported behaviour-tree node {tag!r}")

    def _worst(node: BTNode) -> int:
        if node.kind == "action":
            return 1
        if node.kind == "retry":
            return sum(_worst(child) for child in node.children) * node.attempts
        return sum(_worst(child) for child in node.children)

    root_node = compile_element(selected, 0)
    worst = _worst(root_node)
    if worst > budget.max_action_calls:
        raise TreeError(
            f"worst-case action calls ({worst}) exceed {budget.max_action_calls}",
            Info.LIMIT_EXCEEDED,
        )
    return CompiledTree(
        root=root_node,
        node_count=counter["nodes"],
        max_depth=counter["max_depth"],
        worst_case_actions=worst,
        actions=tuple(node.spec for node in action_nodes),
    )


def _select_tree_root(root: ET.Element) -> ET.Element:
    if root.tag == "root":
        unknown = set(root.attrib) - _ALLOWED_ROOT_ATTRS
        if unknown:
            raise TreeError(f"root has unsupported attributes {sorted(unknown)!r}")
        trees = [child for child in list(root) if child.tag == "BehaviorTree"]
        if not trees:
            raise TreeError("root must contain a BehaviorTree")
        main = root.attrib.get("main_tree_to_execute")
        if main:
            matches = [tree for tree in trees if tree.attrib.get("ID") == main]
            if len(matches) != 1:
                raise TreeError(
                    f"main_tree_to_execute {main!r} does not select exactly one tree"
                )
            selected = matches[0]
        else:
            if len(trees) != 1:
                raise TreeError(
                    "root must contain exactly one BehaviorTree or set main_tree_to_execute"
                )
            selected = trees[0]
        _validate_tree_wrapper(selected)
        children = [child for child in list(selected)]
        if len(children) != 1:
            raise TreeError("BehaviorTree must contain exactly one root behaviour node")
        return children[0]
    if root.tag == "BehaviorTree":
        _validate_tree_wrapper(root)
        children = list(root)
        if len(children) != 1:
            raise TreeError("BehaviorTree must contain exactly one root behaviour node")
        return children[0]
    # Allow a bare subtree such as <Sequence>...</Sequence> for tests and demos.
    return root


def _validate_tree_wrapper(tree: ET.Element) -> None:
    unknown = set(tree.attrib) - _ALLOWED_TREE_ATTRS
    if unknown:
        raise TreeError(f"BehaviorTree has unsupported attributes {sorted(unknown)!r}")


class BTInterpreter:
    def __init__(
        self,
        tree: CompiledTree,
        invoke: Callable[[ActionSpec, str, str, float], ActionOutcome],
        *,
        budget: BTBudget | None = None,
        default_action_timeout_sec: float = 30.0,
        on_step: Callable[[object], None] | None = None,
    ):
        self.tree = tree
        self.invoke = invoke
        self.budget = budget or BTBudget()
        self.default_action_timeout_sec = max(0.0, float(default_action_timeout_sec))
        self.on_step = on_step
        self._deadline = 0.0
        self._action_calls = 0
        self._steps: list = []
        self._last_output = ""

    def run(self, deadline: float) -> BTExecutionResult:
        started = time.monotonic()
        self._deadline = min(deadline, started + self.budget.max_runtime_sec)
        self._action_calls = 0
        self._steps = []
        self._last_output = ""
        self._abort_info = None
        self._message = ""
        try:
            status = self._tick(self.tree.root, 0)
        except Exception as exc:  # noqa: BLE001 - never let the node exit
            return BTExecutionResult(
                status=NodeStatus.ABORT,
                info=int(Info.UNKNOWN_STATE),
                message=f"behaviour tree execution error: {type(exc).__name__}: {exc}",
                steps=self._steps,
                action_calls=self._action_calls,
                elapsed_sec=time.monotonic() - started,
            )
        info = int(Info.OK)
        if status is NodeStatus.FAILURE:
            info = int(Info.ERROR_ATOMIC_ACTION)
        elif status is NodeStatus.ABORT:
            info = self._abort_info or int(Info.UNKNOWN_STATE)
        return BTExecutionResult(
            status=status,
            info=info,
            message=self._message,
            steps=self._steps,
            action_calls=self._action_calls,
            elapsed_sec=time.monotonic() - started,
        )

    # ------------------------------------------------------------------ ticks
    _abort_info: int | None = None
    _message: str = ""

    def _expired(self) -> bool:
        return time.monotonic() >= self._deadline

    def _abort(self, info: Info, message: str) -> NodeStatus:
        self._abort_info = int(info)
        self._message = message
        return NodeStatus.ABORT

    def _tick(self, node: BTNode, depth: int) -> NodeStatus:
        if depth > self.budget.max_depth:
            self._message = "runtime tree depth limit exceeded"
            return NodeStatus.ABORT
        if self._expired():
            return self._abort(Info.TIMEOUT, "behaviour tree runtime budget exhausted")

        if node.kind == "action":
            return self._tick_action(node)
        if node.kind == "sequence":
            for child in node.children:
                status = self._tick(child, depth + 1)
                if status is not NodeStatus.SUCCESS:
                    return status
            return NodeStatus.SUCCESS
        if node.kind == "fallback":
            for child in node.children:
                status = self._tick(child, depth + 1)
                if status is NodeStatus.SUCCESS:
                    return NodeStatus.SUCCESS
                if status is NodeStatus.ABORT:
                    return status
            self._message = "all fallback children failed"
            return NodeStatus.FAILURE
        if node.kind == "retry":
            last = NodeStatus.FAILURE
            for _ in range(node.attempts):
                if self._expired():
                    return self._abort(
                        Info.TIMEOUT, "behaviour tree runtime budget exhausted"
                    )
                last = self._tick(node.children[0], depth + 1)
                if last is NodeStatus.SUCCESS:
                    return NodeStatus.SUCCESS
                if last is NodeStatus.ABORT:
                    return last
            self._message = f"retry exhausted after {node.attempts} attempt(s)"
            return last
        if node.kind == "timeout":
            subtree_deadline = time.monotonic() + float(node.timeout_sec or 0.0)
            previous = self._deadline
            self._deadline = min(previous, subtree_deadline)
            try:
                status = self._tick(node.children[0], depth + 1)
            finally:
                self._deadline = previous
            if time.monotonic() > subtree_deadline:
                return self._abort(Info.TIMEOUT, "Timeout node expired")
            return status
        return self._abort(Info.UNKNOWN_STATE, f"unknown node kind {node.kind!r}")

    def _tick_action(self, node: BTNode) -> NodeStatus:
        if self._action_calls >= self.budget.max_action_calls:
            return self._abort(
                Info.LIMIT_EXCEEDED, "total action-call budget exhausted at runtime"
            )
        self._action_calls += 1
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            return self._abort(Info.TIMEOUT, "behaviour tree runtime budget exhausted")
        timeout = (
            min(self.default_action_timeout_sec, remaining)
            if self.default_action_timeout_sec > 0
            else remaining
        )
        assert node.spec is not None
        try:
            outcome = self.invoke(node.spec, node.input_text, self._last_output, timeout)
        except Exception as exc:  # noqa: BLE001 - convert to unknown state
            return self._abort(
                Info.UNKNOWN_STATE,
                f"atomic action {node.name!r} raised {type(exc).__name__}: {exc}",
            )

        step = StepRecord(
            index=len(self._steps),
            name=node.name,
            status=outcome.result,
            info=outcome.info,
            output=outcome.output,
            error=outcome.error,
            elapsed_sec=outcome.elapsed_sec,
            request_id=outcome.request_id,
            observation=outcome.observation,
        )
        self._steps.append(step)
        if self.on_step is not None:
            self.on_step(step)
        self._last_output = outcome.output
        if time.monotonic() > self._deadline:
            return self._abort(
                Info.TIMEOUT,
                f"runtime budget exhausted during action {node.name!r}",
            )

        if outcome.result == "success":
            return NodeStatus.SUCCESS
        if outcome.result == "failed":
            self._message = f"action {node.name!r} failed: {outcome.error}"
            return NodeStatus.FAILURE
        if outcome.result == "timeout":
            return self._abort(
                Info.TIMEOUT, f"action {node.name!r} timed out: {outcome.error}"
            )
        if outcome.result == "unavailable":
            return self._abort(
                Info.ATOMIC_ACTION_UNAVAILABLE,
                f"action {node.name!r} unavailable: {outcome.error}",
            )
        if outcome.result == "cancelled":
            return self._abort(
                Info.STOPPED, f"action {node.name!r} was cancelled: {outcome.error}"
            )
        return self._abort(
            Info.UNKNOWN_STATE, f"action {node.name!r} state unknown: {outcome.error}"
        )
