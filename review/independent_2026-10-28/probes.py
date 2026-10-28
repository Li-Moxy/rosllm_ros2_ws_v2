"""Independent review probes. No source changes, external model calls or hardware."""
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT=Path('/home/moxy/rosllm_ros2_ws_v2')
MODE=sys.argv[1]

def load_helpers():
    spec=importlib.util.spec_from_file_location('review_executor_helpers',ROOT/'src/rosllm_executor/test/test_executor_integration.py')
    mod=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=mod
    spec.loader.exec_module(mod)
    return mod

if MODE in ('timeout_overlap','strafe_partial'):
    h=load_helpers()
    os.environ['ROS_DOMAIN_ID']='183'
    os.environ['ROS_LOCALHOST_ONLY']='1'
    import rclpy
    from rosllm_core.action_client import ClientHost
    rclpy.init(args=[])
    directory=Path(tempfile.mkdtemp(prefix='rosllm_v2_timeout_'))
    world=h.DummyWorld()
    world.delay=1.5 if MODE=='timeout_overlap' else 0.0
    host=h.NodeHost(world)
    caller=ClientHost('independent_review_caller')
    handle=None
    turtle_host=None
    try:
        if MODE=='timeout_overlap':
            library=h.make_library(directory/'actions.yaml')
            handle=h.start_executor(directory,library,robot_id='independent-review-timeout',params={'step_timeout_sec':0.2,'enable_observation_snapshots':False})
            behavior='{"name":"forward","input":{"vel":1.0}}'
            first,_=h.call_plan(caller,handle,behavior)
            second,_=h.call_plan(caller,handle,behavior)
            with world.lock:
                calls=list(world.calls)
            starts=[c['at'] for c in calls]
            print(json.dumps({'probe':MODE,'response_infos':[first.info,second.info],'calls':len(calls),'start_gap':starts[1]-starts[0] if len(starts)>1 else None,'action_duration':world.delay,'overlap':len(starts)>1 and starts[1]-starts[0]<world.delay}),flush=True)
            time.sleep(1.6)
        else:
            import yaml
            from rosllm_turtlesim.turtle_actions import TurtleActions
            data=yaml.safe_load((ROOT/'src/rosllm_turtlesim/config/action_library.yaml').read_text())
            data['actions']['forward']['service']='/test/forward'
            library=directory/'actions.yaml'
            library.write_text(yaml.safe_dump(data))
            turtle_host=h.NodeHost(TurtleActions())
            handle=h.start_executor(directory,library,robot_id='independent-review-strafe',params={'enable_observation_snapshots':False})
            behavior='{"name":"forward","input":{"vel":1.0}}\n{"name":"command","input":{"fwd_vel":1,"strafe_vel":1,"ang_vel":0}}'
            response,report=h.call_plan(caller,handle,behavior)
            print(json.dumps({'probe':MODE,'response_info':response.info,'response_success':response.success,'completed_steps':report.completed_steps,'dummy_forward_calls':world.call_count(),'last_error':report.steps[-1].error}),flush=True)
    finally:
        if handle: h.stop_proc(handle)
        if turtle_host: turtle_host.stop()
        caller.shutdown()
        caller.node.destroy_node()
        host.stop()
        rclpy.shutdown()
else:
    os.environ['ROS_DOMAIN_ID']='184'
    os.environ['ROS_LOCALHOST_ONLY']='1'
    import rclpy
    from rosllm_core.action_client import ClientHost
    if MODE=='stale_pose':
        from rosllm_turtlesim.turtle_observation import TurtleObservation
        from rosllm_observation.observation_manager_node import ObservationManagerNode
        from rosllm_core.observation import ObservationView
        from rosllm_srvs.srv import Observation
        from rclpy.node import Node
        from rclpy.executors import MultiThreadedExecutor
        from turtlesim.msg import Pose
        directory=Path(tempfile.mkdtemp(prefix='rosllm_v2_stale_'))
        config=directory/'observations.yaml'
        config.write_text('staleness_sec: 0.3\nobservations_topics:\n  - name: turtle_pose\n    topic: /turtle1/observation\nobservations_srvs: []\n')
        rclpy.init(args=['--ros-args','-p',f'config_filename:={config}'])
        summary=TurtleObservation()
        manager=ObservationManagerNode()
        sensor=Node('independent_review_one_shot_pose')
        publisher=sensor.create_publisher(Pose,'/turtle1/pose',10)
        executor=MultiThreadedExecutor(num_threads=4)
        for node in (summary,manager,sensor): executor.add_node(node)
        thread=threading.Thread(target=executor.spin,daemon=True)
        thread.start()
        caller=ClientHost('independent_staleness_probe')
        caller.create_client('/get_observation',Observation)
        try:
            assert caller.wait_ready('/get_observation',3)
            until=time.monotonic()+3
            while publisher.get_subscription_count()<1 and time.monotonic()<until: time.sleep(.02)
            publisher.publish(Pose(x=5.0,y=5.0,theta=0.0))
            published=time.monotonic()
            time.sleep(1.2)
            result=caller.call('/get_observation',Observation.Request(),2)
            entry=ObservationView.from_messages(result.response.observation).get('turtle_pose')
            print(json.dumps({'probe':MODE,'since_last_source_pose':time.monotonic()-published,'configured_staleness':0.3,'reported_status':entry.status,'reported_age':entry.age_sec,'payload':entry.payload}),flush=True)
        finally:
            executor.shutdown(timeout_sec=3)
            thread.join(timeout=3)
            manager.shutdown()
            manager.host.node.destroy_node()
            caller.shutdown()
            caller.node.destroy_node()
            for node in (summary,manager,sensor): node.destroy_node()
            rclpy.shutdown()
    elif MODE=='shutdown':
        rclpy.init(args=[])
        host=ClientHost('independent_shutdown_probe')
        node=host.node
        host.shutdown()
        try:
            name=node.get_name()
            print(json.dumps({'probe':MODE,'node_still_valid':True,'node_name':name,'declared_node_attr':hasattr(host,'_node')}),flush=True)
        except Exception as exc:
            print(json.dumps({'probe':MODE,'node_still_valid':False,'exception':str(exc)}),flush=True)
        finally:
            node.destroy_node()
            rclpy.shutdown()
    elif MODE=='validation':
        from rosllm_core.library import ActionLibrary
        from rosllm_core.plan import parse_plan
        from rosllm_core.observation import ObservationEntry,verify_effect
        library=ActionLibrary.load(ROOT/'src/rosllm_turtlesim/config/action_library.yaml')
        before=ObservationEntry('turtle_pose',payload={'x':5.0,'y':5.0,'theta':math.pi/2},status='fresh',age_sec=0)
        after=ObservationEntry('turtle_pose',payload={'x':5.0,'y':6.0,'theta':math.pi/2},status='fresh',age_sec=0)
        check=verify_effect('forward',library.get('forward').verify,before,after)
        plan=parse_plan('{"name":"forward","input":{"vel":1}}\n{"name":"command","input":{"fwd_vel":1,"strafe_vel":1,"ang_vel":0}}',library)
        print(json.dumps({'probe':'forward_heading','actual_distance':1.0,'verified':check.ok,'reason':check.reason}),flush=True)
        from rosllm_turtlesim.turtle_actions import TurtleActions
        from rosllm_srvs.srv import AtomicAction
        rclpy.init(args=[])
        node=TurtleActions()
        try:
            response=node._command(AtomicAction.Request(input=plan.steps[1].input_text),AtomicAction.Response())
            print(json.dumps({'probe':'strafe_precheck','parsed_steps':len(plan.steps),'plan_accepted':True,'command_service_success':response.success,'command_output':response.output}),flush=True)
        finally:
            node.destroy_node()
            rclpy.shutdown()
