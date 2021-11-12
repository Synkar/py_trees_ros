#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# License: BSD
#   https://raw.github.com/splintered-reality/py_trees_ros/license/LICENSE
#
##############################################################################
# Documentation
##############################################################################

"""
The :class:`py_trees_ros.trees.BehaviourTree` class
extends the core :class:`py_trees.trees.BehaviourTree` class
with both blackboard and (tree) snapshot streaming services.
Interact with these services via the :ref:`py-trees-blackboard-watcher` and
:ref:`py-trees-tree-watcher` command line utilities.
"""

##############################################################################
# Imports
##############################################################################

import collections
import enum
import functools
import os
import math
import statistics
import subprocess
import tempfile
import time
import typing
import uuid

import diagnostic_msgs.msg as diagnostic_msgs  # noqa
import py_trees
import py_trees.console as console
import py_trees_ros_interfaces.msg as py_trees_msgs  # noqa
import py_trees_ros_interfaces.srv as py_trees_srvs  # noqa
import rcl_interfaces.msg as rcl_interfaces_msgs
import rospy
import unique_identifier_msgs.msg as unique_identifier_msgs

from . import blackboard
from . import conversions
from . import exceptions
from . import utilities
from . import visitors

##############################################################################
# Tree Management
##############################################################################


class SnapshotStream(object):
    """
    SnapshotStream instances are responsible for creating / destroying
    a snapshot stream as well as the configurable curation of snapshots
    that are published on it.
    """
    _counter = 0
    """Incremental counter guaranteeing unique watcher names"""

    class Parameters(object):
        """
        Reconfigurable parameters for the snapshot stream.

        Args:
            blackboard_data: publish blackboard variables on the visited path
            blackboard_activity: enable and publish blackboard activity in the last tick
            snapshot_period: period between snapshots (use /inf to only publish on tree status changes)
        """
        def __init__(
            self,
            blackboard_data: bool=False,
            blackboard_activity: bool=False,
            snapshot_period: float=py_trees.common.Duration.INFINITE
        ):
            self.blackboard_data = blackboard_data
            self.blackboard_activity = blackboard_activity
            self.snapshot_period = py_trees.common.Duration.INFINITE.value if snapshot_period == py_trees.common.Duration.INFINITE else snapshot_period

    def __init__(
        self,
        topic_name: str=None,
        parameters: 'SnapshotStream.Parameters'=None,
    ):
        """
        Create the publisher, ready for streaming.

        Args:
            topic_name: snapshot stream name, uniquely generated if None
            parameters: configuration of the snapshot stream
        """
        self.topic_name = SnapshotStream.expand_topic_name(topic_name)
        self.publisher = rospy.Publisher(self.topic_name, py_trees_msgs.BehaviourTree, latch=True, queue_size=1)
        self.parameters = parameters if parameters is not None else SnapshotStream.Parameters()
        self.last_snapshot_timestamp = None
        self.statistics = None

    @staticmethod
    def expand_topic_name(topic_name: str) -> str:
        """
        Custom name expansion depending on the topic name provided. This is part of the
        stream configuration on request which either provides no hint (automatic name
        generation), a simple hint (relative name expanded under the snapshot streams
        namespace) or a complete hint (absolute name that does not need expansion).

        Args:
            topic_name: hint for the topic name

        Returns:
            the expanded topic name
        """
        if topic_name is None or not topic_name:
            expanded_topic_name = rospy.resolve_name("~snapshot_streams/_snapshots_" + str(SnapshotStream._counter))
            SnapshotStream._counter += 1
            return expanded_topic_name
        elif topic_name.startswith("~"):
            return rospy.resolve_name(topic_name)
        elif not topic_name.startswith("/"):
            return rospy.resolve_name("~snapshot_streams/" + topic_name)
        else:
            return topic_name

    def publish(
            self,
            root: py_trees.behaviour.Behaviour,
            changed: bool,
            statistics: py_trees_msgs.Statistics,
            visited_behaviour_ids: typing.Set[uuid.UUID],
            visited_blackboard_client_ids: typing.Set[uuid.UUID]
    ):
        """"
        Publish a snapshot, including only what has been parameterised.

        Args:
            root: the tree
            changed: whether the tree status / graph changed or not
            statistics: add tree statistics to the snapshot data
            visited_behaviour_ids: behaviours on the visited path
            visited_blackboard_client_ids: blackboard clients belonging to behaviours on the visited path
        """
        if self.last_snapshot_timestamp is None:
            changed = True
            self.last_snapshot_timestamp = time.monotonic()
        current_timestamp = time.monotonic()
        elapsed_time = current_timestamp - self.last_snapshot_timestamp
        if_its_close_enough = 0.98  # https://github.com/splintered-reality/py_trees_ros/issues/144
        if (not changed and elapsed_time < if_its_close_enough * self.parameters.snapshot_period):
            return

        tree_message = py_trees_msgs.BehaviourTree()
        tree_message.changed = changed

        # tree
        for behaviour in root.iterate():
            msg = conversions.behaviour_to_msg(behaviour)
            msg.is_active = True if behaviour.id in visited_behaviour_ids else False
            tree_message.behaviours.append(msg)

        # blackboard
        if self.parameters.blackboard_data:
            visited_keys = py_trees.blackboard.Blackboard.keys_filtered_by_clients(
                client_ids=visited_blackboard_client_ids
            )
            for key in visited_keys:
                try:
                    value = str(py_trees.blackboard.Blackboard.get(key))
                except KeyError:
                    value = "-"
                tree_message.blackboard_on_visited_path.append(
                    diagnostic_msgs.KeyValue(
                        key=key,
                        value=value
                    )
                )

        # activity stream
        #   TODO: checking the stream is not None is redundant, perhaps use it as an exception check?
        if self.parameters.blackboard_activity and py_trees.blackboard.Blackboard.activity_stream is not None:
            tree_message.blackboard_activity = conversions.activity_stream_to_msgs()
        # other
        if statistics is not None:
            tree_message.statistics = statistics
        self.publisher.publish(tree_message)
        self.last_snapshot_timestamp = current_timestamp

    def shutdown(self):
        """
        Shutdown the (temporarily) created publisher.
        """
        self.publisher.unregister()


class BehaviourTree(py_trees.trees.BehaviourTree):
    """
    Extend the :class:`py_trees.trees.BehaviourTree` class with
    a few bells and whistles for ROS.

    ROS Parameters:
        * **default_snapshot_stream**: enable/disable the default snapshots stream in ~snapshots` (default: False)
        * **default_snapshot_period**: periodically publish (default: :data:`math.inf`)
        * **default_snapshot_blackboard_data**: include tracking and visited variables (default: True)
        * **default_snapshot_blackboard_activity**: include the blackboard activity (default: False)
        * **setup_timeout**: time (s) to wait (default: :data:`math.inf`)

          * if :data:`math.inf`, it will block indefinitely

    ROS Publishers:
        * **~snapshots** (:class:`py_trees_interfaces.msg.BehaviourTree`): the default snapshot stream, if enabled

    ROS Services:
        * **~blackboard_streams/close** (:class:`py_trees_ros_interfaces.srv.CloselackboardWatcher`)
        * **~blackboard_streams/get_variables** (:class:`py_trees_ros_interfaces.srv.GetBlackboardVariables`)
        * **~blackboard_streams/open** (:class:`py_trees_ros_interfaces.srv.OpenBlackboardWatcher`)
        * **~snapshot_streams/close** (:class:`py_trees_ros_interfaces.srv.CloseSnapshotsStream`)
        * **~snapshot_streams/open** (:class:`py_trees_ros_interfaces.srv.OpenSnapshotsStream`)
        * **~snapshot_streams/reconfigure** (:class:`py_trees_ros_interfaces.srv.ReconfigureSnapshotsStream`)

    Topics and services are not intended for direct use, but facilitate the operation of the
    utilities :ref:`py-trees-tree-watcher` and :ref:`py-trees-blackboard-watcher`.

    Args:
        root: root node of the tree
        unicode_tree_debug: print to console the visited ascii tree after every tick

    Raises:
        AssertionError: if incoming root variable is not the correct type
    """
    def __init__(self,
                 root: py_trees.behaviour.Behaviour,
                 unicode_tree_debug: bool=False):
        super(BehaviourTree, self).__init__(root)
        if unicode_tree_debug:
            self.snapshot_visitor = py_trees.visitors.DisplaySnapshotVisitor()
        else:
            self.snapshot_visitor = py_trees.visitors.SnapshotVisitor()
        self.visitors.append(self.snapshot_visitor)

        self.statistics = None
        self.tick_start_time = None
        self.time_series = collections.deque([])
        self.tick_interval_series = collections.deque([])
        self.tick_duration_series = collections.deque([])

        self.pre_tick_handlers.append(self._statistics_pre_tick_handler)
        self.post_tick_handlers.append(self._statistics_post_tick_handler)

        self.timer = None

        # delay ROS artifacts so we can construct the tree without a ROS connection
        self.snapshot_streams = {}

    def setup(
            self,
            timeout: float=py_trees.common.Duration.INFINITE,
            visitor: py_trees.visitors.VisitorBase=None
    ):
        """
        Setup the publishers, exechange and add ros-relevant pre/post tick handlers to the tree.
        Ultimately relays this call down to all the behaviours in the tree.

        Args:
            timeout: time (s) to wait (use common.Duration.INFINITE to block indefinitely)
            visitor: runnable entities on each node after it's setup

        .. note:

           This method declares parameters for the snapshot_period and setup_timeout.
           These parameters take precedence over the period and timeout args provided here.
           If parameters are not configured at runtime, then the period and timeout args
           provided here will inialise the declared parameters.
        """
        if visitor is None:
            visitor = visitors.SetupLogger()
        self.default_snapshot_stream_topic_name = SnapshotStream.expand_topic_name("~snapshots")

        ########################################
        # ROS Comms
        ########################################
        self.snapshot_stream_services = utilities.Services(
            service_details=[
                ("close", "~snapshot_streams/close", py_trees_srvs.CloseSnapshotStream, self._close_snapshot_stream),
                ("open", "~snapshot_streams/open", py_trees_srvs.OpenSnapshotStream, self._open_snapshot_stream),
                ("reconfigure", "~snapshot_streams/reconfigure", py_trees_srvs.ReconfigureSnapshotStream, self._reconfigure_snapshot_stream),
            ],
            introspection_topic_name="snapshot_streams/services"
        )
        self.blackboard_exchange = blackboard.Exchange()
        self.blackboard_exchange.setup()

        ########################################
        # default_snapshot_stream
        ########################################
        default_snapshot_stream = rospy.get_param('~default_snapshot_stream', False)
        if default_snapshot_stream:
            parameters = SnapshotStream.Parameters(
                snapshot_period=rospy.get_param("~default_snapshot_period", py_trees.common.Duration.INFINITE.value),
                blackboard_data=rospy.get_param("~default_snapshot_blackboard_data", True),
                blackboard_activity=rospy.get_param("~default_snapshot_blackboard_activity", False)
            )
            self.snapshot_streams[self.default_snapshot_stream_topic_name] = SnapshotStream(
                topic_name=self.default_snapshot_stream_topic_name,
                parameters=parameters
            )
            if self.snapshot_streams[self.default_snapshot_stream_topic_name].parameters.blackboard_activity:
                self.blackboard_exchange.register_activity_stream_client()

        ########################################
        # setup_timeout
        ########################################
        default_setup_timeout = timeout if timeout != py_trees.common.Duration.INFINITE else py_trees.common.Duration.INFINITE.value
        setup_timeout = rospy.get_param("~setup_timeout", default_setup_timeout)
        # Ugly workaround to accomodate use of the enum (TODO: rewind this)
        #   Need to pass the enum for now (instead of just a float) in case
        #   there are behaviours out in the wild that apply logic around the
        #   use of the enum
        if setup_timeout == py_trees.common.Duration.INFINITE.value:
            setup_timeout = py_trees.common.Duration.INFINITE

        ########################################
        # Behaviours
        ########################################
        try:
            super().setup(
                timeout=setup_timeout,
                visitor=visitor
            )
        except RuntimeError as e:
            if str(e) == "tree setup interrupted or timed out":
                raise exceptions.TimedOutError(str(e))
            else:
                raise

        ########################################
        # Setup Handlers
        ########################################
        # set a handler to publish future modifications whenever the tree is modified
        # (e.g. pruned). The tree_update_handler method is in the base class, set this
        # to the callback function here.
        self.tree_update_handler = self._on_tree_update_handler
        self.post_tick_handlers.append(self._snapshots_post_tick_handler)

    def tick_tock(
            self,
            period_ms,
            number_of_iterations=py_trees.trees.CONTINUOUS_TICK_TOCK,
            pre_tick_handler=None,
            post_tick_handler=None):
        """
        Tick continuously at the period specified.

        This is a re-implementation of the
        :meth:`~py_trees.trees.BehaviourTree.tick_tock`
        tick_tock that takes advantage of the rclpy timers so callbacks are interleaved inbetween
        rclpy callbacks (keeps everything synchronous so no need for locks).

        Args:
            period_ms (:obj:`float`): sleep this much between ticks (milliseconds)
            number_of_iterations (:obj:`int`): number of iterations to tick-tock
            pre_tick_handler (:obj:`func`): function to execute before ticking
            post_tick_handler (:obj:`func`): function to execute after ticking
        """
        self.timer = rospy.Timer(
            rospy.Duration(period_ms / 1000.0),  # unit 'seconds'
            functools.partial(
                self._tick_tock_timer_callback,
                number_of_iterations=number_of_iterations,
                pre_tick_handler=pre_tick_handler,
                post_tick_handler=post_tick_handler
            )
        )
        self.tick_tock_count = 0

    def shutdown(self):
        """
        Cleanly shut down rclpy timers and nodes.
        """
        # stop ticking if we're ticking
        if self.timer is not None:
            self.timer.shutdown()
        # call shutdown on each behaviour first, in case it has
        # some esoteric shutdown steps
        super().shutdown()

    def _tick_tock_timer_callback(
            self,
            event: rospy.timer.TimerEvent,
            number_of_iterations,
            pre_tick_handler,
            post_tick_handler):
        """
        Tick tock callback passed to the timer to be periodically triggered.

        Args:
            number_of_iterations (:obj:`int`): number of iterations to tick-tock
            pre_tick_handler (:obj:`func`): function to execute before ticking
            post_tick_handler (:obj:`func`): function to execute after ticking
            event (:obj:`rospy.timer.TimerEvent`): event of the ros timer
        """
        if (number_of_iterations == py_trees.trees.CONTINUOUS_TICK_TOCK or
                self.tick_tock_count < number_of_iterations):
            self.tick(pre_tick_handler, post_tick_handler)
            self.tick_tock_count += 1
        else:
            self.timer.shutdown()

    def _on_tree_update_handler(self):
        """
        Whenever there has been a modification to the tree (insertion/pruning), publish
        the snapshot.
        """
        # only worth notifying once we've actually commenced
        if self.statistics is not None:
            rclpy_start_time = rospy.Time.now()
            self.statistics.stamp = rclpy_start_time
            for unused_topic_name, snapshot_stream in self.snapshot_streams.items():
                snapshot_stream.publish(
                    root=self.root,
                    changed=True,
                    statistics=self.statistics,
                    visited_behaviour_ids=self.snapshot_visitor.visited.keys(),
                    visited_blackboard_client_ids=self.snapshot_visitor.visited_blackboard_client_ids
                )

    def _statistics_pre_tick_handler(self, tree: py_trees.trees.BehaviourTree):
        """
        Pre-tick handler that resets the statistics and starts the clock.

        Args:
            tree (:class:`~py_trees.trees.BehaviourTree`): the behaviour tree that has just been ticked
        """
        if len(self.time_series) == 10:
            self.time_series.popleft()
            self.tick_interval_series.popleft()

        rclpy_start_time = rospy.Time.now()
        self.time_series.append(conversions.rospy_time_to_float(rclpy_start_time))
        if len(self.time_series) == 1:
            self.tick_interval_series.append(0.0)
        else:
            self.tick_interval_series.append(self.time_series[-1] - self.time_series[-2])

        self.statistics = py_trees_msgs.Statistics()
        self.statistics.count = self.count
        self.statistics.stamp = rclpy_start_time
        self.statistics.tick_interval = self.tick_interval_series[-1]
        self.statistics.tick_interval_average = sum(self.tick_interval_series) / len(self.tick_interval_series)
        if len(self.tick_interval_series) > 1:
            self.statistics.tick_interval_variance = statistics.variance(
                self.tick_interval_series,
                self.statistics.tick_interval_average
            )
        else:
            self.statistics.tick_interval_variance = 0.0

    def _statistics_post_tick_handler(self, tree: py_trees.trees.BehaviourTree):
        """
        Post-tick handler that completes the statistics generation.

        Args:
            tree (:class:`~py_trees.trees.BehaviourTree`): the behaviour tree that has just been ticked
        """
        duration = conversions.rospy_time_to_float(rospy.Time.now()) - self.time_series[-1]

        if len(self.tick_duration_series) == 10:
            self.tick_duration_series.popleft()
        self.tick_duration_series.append(duration)

        self.statistics.tick_duration = duration
        self.statistics.tick_duration_average = sum(self.tick_duration_series) / len(self.tick_duration_series)
        if len(self.tick_duration_series) > 1:
            self.statistics.tick_duration_variance = statistics.variance(
                self.tick_duration_series,
                self.statistics.tick_duration_average
            )
        else:
            self.statistics.tick_duration_variance = 0.0

    def _snapshots_post_tick_handler(self, tree: py_trees.trees.BehaviourTree):
        """
        Post-tick handler that checks for changes in the tree/blackboard as a result
        of it's last tick and publish updates on ROS topics.

        Args:
            tree (:class:`~py_trees.trees.BehaviourTree`): the behaviour tree that has just been ticked
        """
        # checks
        if self.root.tip() is None:
            rospy.logerr("the root behaviour failed to return a tip [hint: tree is in an INVALID state, usually as a result of incorrectly coded behaviours]")
            return

        # publish the default snapshot stream (useful for logging)
        for unused_topic_name, snapshot_stream in self.snapshot_streams.items():
            snapshot_stream.publish(
                root=self.root,
                changed=self.snapshot_visitor.changed,
                statistics=self.statistics,
                visited_behaviour_ids=self.snapshot_visitor.visited.keys(),
                visited_blackboard_client_ids=self.snapshot_visitor.visited_blackboard_client_ids
            )

        # every tick publish on watchers, clear activity stream (note: not expensive as watchers by default aren't connected)
        self.blackboard_exchange.post_tick_handler(
            visited_client_ids=self.snapshot_visitor.visited_blackboard_client_ids  # .keys()
        )

    def _close_snapshot_stream(
        self,
        request: py_trees_srvs.CloseSnapshotStream._request_class  # noqa
    ) -> py_trees_srvs.CloseSnapshotStream._response_class:
        response = py_trees_srvs.CloseSnapshotStreamResponse()
        response.result = True
        try:
            self.snapshot_streams[request.topic_name].shutdown()
            if self.snapshot_streams[request.topic_name].parameters.blackboard_activity:
                self.blackboard_exchange.unregister_activity_stream_client()
            del self.snapshot_streams[request.topic_name]
        except KeyError:
            response.result = False
        return response

    def _open_snapshot_stream(
        self,
        request: py_trees_srvs.OpenSnapshotStream._request_class  # noqa
    ) -> py_trees_srvs.OpenSnapshotStream._response_class:
        response = py_trees_srvs.OpenSnapshotStreamResponse()
        snapshot_stream = SnapshotStream(
            topic_name=request.topic_name,
            parameters=SnapshotStream.Parameters(
                blackboard_data=request.parameters.blackboard_data,
                blackboard_activity=request.parameters.blackboard_activity,
                snapshot_period=request.parameters.snapshot_period
            )
        )
        if snapshot_stream.parameters.blackboard_activity:
            self.blackboard_exchange.register_activity_stream_client()
        self.snapshot_streams[snapshot_stream.topic_name] = snapshot_stream
        response.topic_name = snapshot_stream.topic_name
        return response

    def _reconfigure_snapshot_stream(
        self,
        request: py_trees_srvs.ReconfigureSnapshotStream._request_class  # noqa
    ) -> py_trees_srvs.ReconfigureSnapshotStream._response_class:
        response = py_trees_srvs.ReconfigureSnapshotStreamResponse()
        response.result = True
        try:
            snapshot_stream = self.snapshot_streams[request.topic_name]
            snapshot_stream.parameters.blackboard_data = request.parameters.blackboard_data
            if (snapshot_stream.parameters.blackboard_activity != request.parameters.blackboard_activity):
                if request.parameters.blackboard_activity:
                    self.blackboard_exchange.register_activity_stream_client()
                else:
                    self.blackboard_exchange.unregister_activity_stream_client()
            snapshot_stream.parameters.blackboard_activity = request.parameters.blackboard_activity
            snapshot_stream.parameters.snapshot_period = request.parameters.snapshot_period
        except KeyError:
            response.result = False
        return response

    def _cleanup(self):
        with self.lock:
            # self.bag.close()
            self.interrupt_tick_tocking = True
            self._bag_closed = True

##############################################################################
# Tree Watcher
##############################################################################


class WatcherMode(enum.Enum):
    """An enumerator specifying the view mode for the watcher"""

    SNAPSHOTS = "SNAPSHOTS"
    """Render ascii/unicode snapshots from a snapshot stream"""
    DOT_GRAPH = "DOT_GRAPH"
    """Render with the dot graph representation of the static tree (using an application or text to console)."""


class Watcher(object):
    """
    The tree watcher sits on the other side of a running
    :class:`~py_trees_ros.trees.BehaviourTree` and is a useful mechanism for
    quick introspection of it's current state.

    Args:
        topic_name: location of the snapshot stream (optional)
        namespace_hint: refine search for snapshot stream services if there is more than one tree (optional)
        parameters: snapshot stream configuration controlling both on-the-fly stream creation and display
        statistics: display statistics

    .. seealso:: :mod:`py_trees_ros.programs.tree_watcher`
    """
    def __init__(
        self,
        topic_name: str=None,
        namespace_hint: str=None,
        parameters: SnapshotStream.Parameters=None,
        statistics: bool=False,
        mode: WatcherMode=WatcherMode.SNAPSHOTS
    ):
        self.topic_name = topic_name
        self.namespace_hint = namespace_hint

        self.mode = mode
        self.parameters = parameters if parameters is not None else SnapshotStream.Parameters()
        self.statistics = statistics

        self.subscriber = None
        self.snapshot_visitor = py_trees.visitors.SnapshotVisitor()
        self.done = False
        self.xdot_process = None
        self.rendered = None

        self.services = {
            'open': None,
            'close': None
        }

        self.service_names = {
            'open': None,
            'close': None
        }
        self.service_type_strings = {
            'open': 'py_trees_ros_interfaces/OpenSnapshotStream',
            'close': 'py_trees_ros_interfaces/CloseSnapshotStream'
        }
        self.service_types = {
            'open': py_trees_srvs.OpenSnapshotStream,
            'close': py_trees_srvs.CloseSnapshotStream
        }

    def setup(self, timeout_sec: float):
        """
        Creates the node and checks that all of the server-side services are available
        for calling on to open a connection.

        Args:
            timeout_sec: time (s) to wait (use common.Duration.INFINITE to block indefinitely)

        Raises:
            :class:`~py_trees_ros.exceptions.NotFoundError`: if services/topics were not found
            :class:`~py_trees_ros.exceptions.MultipleFoundError`: if multiple services were found
            :class:`~py_trees_ros.exceptions.TimedOutError`: if service clients time out waiting for the server
        """
        # open a snapshot stream
        if self.topic_name is None:
            # discover actual service names
            for service_name in self.service_names.keys():
                # can raise NotFoundError and MultipleFoundError
                self.service_names[service_name] = utilities.find_service(
                    service_type=self.service_type_strings[service_name],
                    namespace=self.namespace_hint,
                    timeout=timeout_sec
                )
            # create service clients
            self.services["open"] = self.create_service_client(key="open")
            self.services["close"] = self.create_service_client(key="close")
            # request a stream
            request = self.service_types["open"]._request_class()
            request.parameters.blackboard_data = self.parameters.blackboard_data
            request.parameters.blackboard_activity = self.parameters.blackboard_activity
            request.parameters.snapshot_period = self.parameters.snapshot_period
            response = self.services["open"](request)
            self.topic_name = response.topic_name
        # connect to a snapshot stream
        self.subscriber = rospy.Subscriber(
            self.topic_name,
            py_trees_msgs.BehaviourTree,
            callback=self.callback_snapshot,
            queue_size=1
        )
        start_time = time.monotonic()
        while True:
            elapsed_time = time.monotonic() - start_time
            if elapsed_time > timeout_sec:
                raise exceptions.TimedOutError("timed out waiting for a snapshot stream publisher [{}]".format(self.topic_name))
            if self.subscriber.get_num_connections() > 0:
                break
            time.sleep(0.1)

    def shutdown(self):
        if self.subscriber is not None:
            self.subscriber.unregister()
        if self.services["close"] is not None:
            request = self.service_types["close"]._request_class()
            request.topic_name = self.topic_name
            unused_response = self.services["close"](request)

    def create_service_client(self, key: str):
        """
        Convenience api for opening a service client and waiting for the service to appear.

        Args:
            key: one of 'open', 'close'.

        Raises:
            :class:`~py_trees_ros.exceptions.NotReadyError`: if setup() wasn't called to identify the relevant services to connect to.
            :class:`~py_trees_ros.exceptions.TimedOutError`: if it times out waiting for the server
        """
        if self.service_names[key] is None:
            raise exceptions.NotReadyError(
                "no known '{}' service known [did you call setup()?]".format(self.service_types[key])
            )
        # hardcoding timeouts will get us into trouble
        try:
            rospy.wait_for_service(self.service_names[key], timeout=3.0)
        except:
            raise exceptions.TimedOutError(
                "timed out waiting for {}".format(self.service_names['close'])
            )
        return rospy.ServiceProxy(self.service_names[key], self.service_types[key])

    def callback_snapshot(self, msg):
        """
        Formats the string message coming in.

        Args
            msg (:class:`py_trees_ros_interfaces.msg.BehaviourTree`):serialised snapshot
        """
        ####################
        # Processing
        ####################
        self.snapshot_visitor.previously_visited = self.snapshot_visitor.visited
        self.snapshot_visitor.visited = {}
        serialised_behaviours = {}
        root_id = None
        for serialised_behaviour in msg.behaviours:
            if serialised_behaviour.parent_id == unique_identifier_msgs.UUID():
                root_id = conversions.msg_to_uuid4(serialised_behaviour.own_id)
            serialised_behaviours[
                conversions.msg_to_uuid4(serialised_behaviour.own_id)
            ] = serialised_behaviour

        def deserialise_tree_recursively(msg):
            behaviour = conversions.msg_to_behaviour(msg)
            for serialised_child_id in msg.child_ids:
                child_id = conversions.msg_to_uuid4(serialised_child_id)
                child = deserialise_tree_recursively(
                    serialised_behaviours[child_id]
                )
                # invasive hack to revert the dummy child we added in msg_to_behaviour
                if isinstance(behaviour, py_trees.decorators.Decorator):
                    behaviour.children = [child]
                    behaviour.decorated = behaviour.children[0]
                else:
                    behaviour.children.append(child)
                child.parent = behaviour
            # set the current child so tip() works properly everywhere
            if behaviour.children:
                if msg.current_child_id != unique_identifier_msgs.UUID():
                    current_child_id = conversions.msg_to_uuid4(msg.current_child_id)
                    for index, child in enumerate(behaviour.children):
                        if child.id == current_child_id:
                            # somewhat ugly not having a consistent api here
                            if isinstance(behaviour, py_trees.composites.Selector):
                                behaviour.current_child = child
                            elif isinstance(behaviour, py_trees.composites.Sequence):
                                behaviour.current_index = index
                            # else Parallel, nothing to do since it infers
                            # the current child from children's status on the fly
                            break
            if msg.is_active:
                self.snapshot_visitor.visited[behaviour.id] = behaviour.status
            return behaviour

        # we didn't set the tip in any behaviour, but nothing depends
        # on that right now
        root = deserialise_tree_recursively(serialised_behaviours[root_id])

        ####################
        # Streaming
        ####################
        if self.mode == WatcherMode.SNAPSHOTS:
            if msg.changed:
                colour = console.green
            else:
                colour = console.bold
            ####################
            # Banner
            ####################
            title = "Tick {}".format(msg.statistics.count)
            print(colour + "\n" + 80 * "*" + console.reset)
            print(colour + "* " + console.bold + title.center(80) + console.reset)
            print(colour + 80 * "*" + "\n" + console.reset)
            ####################
            # Tree Snapshot
            ####################
            print(
                py_trees.display.unicode_tree(
                    root=root,
                    visited=self.snapshot_visitor.visited,
                    previously_visited=self.snapshot_visitor.previously_visited
                )
            )
            print(colour + "-" * 80 + console.reset)
            ####################
            # Stream Variables
            ####################
            if self.parameters.blackboard_data:
                print("")
                print(colour + "Blackboard Data" + console.reset)
                indent = " " * 4
                if not msg.blackboard_on_visited_path:
                    print(
                        console.cyan + indent + "-" + console.reset + " : " +
                        console.yellow + "-" + console.reset
                    )
                else:
                    # could probably re-use the unicode_blackboard by passing a dict to it
                    # like we've done for the activity stream
                    max_length = 0
                    for variable in msg.blackboard_on_visited_path:
                        max_length = len(variable.key) if len(variable.key) > max_length else max_length
                    for variable in msg.blackboard_on_visited_path:
                        print(
                            console.cyan + indent +
                            '{0: <{1}}'.format(variable.key, max_length + 1) + console.reset + ": " +
                            console.yellow + '{0}'.format(variable.value) + console.reset
                        )
            ####################
            # Stream Activity
            ####################
            if self.parameters.blackboard_activity:
                print("")
                print(colour + "Blackboard Activity Stream" + console.reset)
                if msg.blackboard_activity:
                    print(py_trees.display.unicode_blackboard_activity_stream(
                        msg.blackboard_activity,
                        indent=0,
                        show_title=False
                    ))
                else:
                    indent = " " * 4
                    print(
                        console.cyan + indent + "-" + console.reset + " : " +
                        console.yellow + "-" + console.reset
                    )
            ####################
            # Stream Statistics
            ####################
            if self.statistics:
                print("")
                print(colour + "Statistics" + console.reset)
                print(
                    console.cyan + "    Timestamp: " + console.yellow +
                    "{}".format(
                        conversions.rospy_time_to_float(
                            msg.statistics.stamp
                        )
                    )
                )
                print(
                    console.cyan + "    Duration : " + console.yellow +
                    "{:.3f}/{:.3f}/{:.3f} (ms) [time/avg/stddev]".format(
                        msg.statistics.tick_duration * 1000,
                        msg.statistics.tick_duration_average * 1000,
                        math.sqrt(msg.statistics.tick_duration_variance) * 1000
                    )
                )
                print(
                    console.cyan + "    Interval : " + console.yellow +
                    "{:.3f}/{:.3f}/{:.3f} (s) [time/avg/stddev]".format(
                        msg.statistics.tick_interval,
                        msg.statistics.tick_interval_average,
                        math.sqrt(msg.statistics.tick_interval_variance)
                    )
                )
                print(console.reset)

        ####################
        # Dot Graph
        ####################
        elif self.mode == WatcherMode.DOT_GRAPH and not self.rendered:
            self.rendered = True
            directory_name = tempfile.mkdtemp()
            py_trees.display.render_dot_tree(
                root=root,
                target_directory=directory_name,
                with_blackboard_variables=self.parameters.blackboard_data
            )
            xdot_program = py_trees.utilities.which('xdot')

            if not xdot_program:
                print("")
                console.logerror("No xdot viewer found [hint: sudo apt install xdot]")
                print("")
                print(py_trees.display.dot_tree(root=root).to_string())
                self.done = True
                self.xdot_process = None
                return

            filename = py_trees.utilities.get_valid_filename(root.name) + '.dot'
            if xdot_program:
                try:
                    self.xdot_process = subprocess.Popen(
                        [
                            xdot_program,
                            os.path.join(directory_name, filename)
                        ]
                    )
                except KeyboardInterrupt:
                    pass
            self.done = True
