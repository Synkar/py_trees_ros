#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# License: BSD
#   https://raw.githubusercontent.com/splintered-reality/py_trees_ros_tutorials/devel/LICENSE
#
##############################################################################
# Documentation
##############################################################################

"""
Action server/client templates.
"""

##############################################################################
# Imports
##############################################################################

import actionlib
import rospy

##############################################################################
# Action Server
##############################################################################
#
# References:
#
#  action client         : https://github.com/ros2/rclpy/blob/master/rclpy/rclpy/action/client.py
#  action server         : https://github.com/ros2/rclpy/blob/master/rclpy/rclpy/action/server.py
#  action client PR      : https://github.com/ros2/rclpy/pull/257
#  action server PR      : https://github.com/ros2/rclpy/pull/270
#  action examples       : https://github.com/ros2/examples/tree/master/rclpy/actions
#
# Note:
#  currently:  py_trees_ros_interfaces.action.Dock_Goal.Request()
#  to come(?): py_trees_ros_interfaces.action.Dock.GoalRequestService.Request()


class GenericServer(object):
    """
    Generic action server that can be subclassed to quickly create action
    servers of varying types in a mock simulation.

    Dynamic Reconfigure:
        * **~duration** (:obj:`float`)

          * reconfigure the duration to be used for the next goal execution

    Args:
        action_name (:obj:`str`): name of the action server (e.g. dock)
        action_type (:obj:`any`): type of the action server (e.g. py_trees_ros_interfaces.Dock
        custom_execute_callback (:obj:`func`): callback to be executed inside the execute loop, no args
        generate_cancelled_result (action_type.Result): custom result method
        generate_preempted_result (action_type.Result): custom result method
        generate_success_result (action_type.Result): custom result method
        goal_recieved_callback(:obj:`func`): callback to be executed immediately upon receiving a goal
        duration (:obj:`float`): forcibly override the dyn reconf time for a goal to complete (seconds)

    Use the ``dashboard`` to dynamically reconfigure the parameters.

    There are some shortcomings in this class that could be addressed to make it more robust:

     - check for matching goal id's when disrupting execution
     - execute multiple requests in parallel / pre-emption (not even tried)
    """
    def __init__(self,
                 action_name,
                 action_type,  # e.g. py_trees_ros_interfaces.action.Dock
                 generate_feedback_message=None,
                 generate_preempted_result=None,
                 generate_success_result=None,
                 goal_received_callback=lambda goal: None,
                 duration=5.0):
        # Needs a member variable to capture the dynamic parameter
        # value when accepting the goal and pass that to the execution
        # of the goal. Not necessary to initialise here, but done
        # for completeness
        self.duration = duration

        self.frequency = 3.0  # hz
        self.percent_completed = 0

        self.action_type = action_type
        try:
            a = self.action_type()

            self.action_goal_type = type(a.action_goal.goal)
            self.action_feedback_type = type(a.action_feedback.feedback)
            self.action_result_type = type(a.action_result.result)
        except AttributeError:
            raise actionlib.ActionException("Type is not an action spec: %s" % str(self.action_type))

        if generate_feedback_message is None:
            self.generate_feedback_message = lambda: self.action_feedback_type()
        else:
            self.generate_feedback_message = generate_feedback_message
        if generate_preempted_result is None:
            self.generate_preempted_result = lambda: self.action_result_type(message="goal preempted")
        else:
            self.generate_preempted_result = generate_preempted_result
        if generate_success_result is None:
            self.generate_success_result = lambda: self.action_result_type(message="goal executed with success")
        else:
            self.generate_success_result = generate_success_result
        self.goal_received_callback = goal_received_callback
        self.goal_handle = None

        self.action_server = actionlib.SimpleActionServer(
            action_name,
            self.action_type,
            execute_cb=self.execute_goal_callback,
            auto_start=False
        )

        self.action_server.register_preempt_callback(self.preempt_callback)
        self.action_server.start()

    def preempt_callback(self):
        """
        Preempt any currently executing goal
        """
        rospy.loginfo("preempt requested: [{goal_id}]".format(goal_id=self.action_server.current_goal.get_goal_id()))

    def execute_goal_callback(self, goal):
        """
        Check for pre-emption, but otherwise just spin around gradually incrementing
        a hypothetical 'percent' done.

        Args:
            goal_handle (:class:`~rclpy.action.server.ServerGoalHandle`): the goal handle of the executing action
        """
        # goal.details (e.g. pose) = don't care
        rospy.loginfo("executing a goal")
        self.goal_received_callback(self.action_server.current_goal.get_goal())
        self.percent_completed = 0
        increment = 100 / (self.frequency * self.duration)
        rate = rospy.Rate(self.frequency)
        while not rospy.is_shutdown():
            rate.sleep()
            self.percent_completed += increment
            if self.action_server.is_preempt_requested():
                result = self.generate_preempted_result()
                message = "goal preempted at {percentage:.2f}%%".format(percentage=self.percent_completed)
                rospy.loginfo(message)
                self.action_server.set_preempted(result, text=message)
                break
            elif self.percent_completed >= 100.0:
                self.percent_completed = 100.0
                result = self.generate_success_result()
                message = "goal executed with success"
                rospy.loginfo(message)
                self.action_server.set_succeeded(result, text=message)
                break
            else:
                rospy.loginfo("sending feedback {percentage:.2f}%%".format(percentage=self.percent_completed))
                self.action_server.publish_feedback(self.generate_feedback_message())

    def abort_goal(self):
        """
        This method is typically only used when the system is shutting down and
        there is an executing goal that needs to be abruptly terminated.
        """
        if self.action_server.is_active():
            if self.action_server.is_preempt_requested():
                self.action_server.set_preempted()
            else:
                self.action_server.set_aborted()

    def shutdown(self):
        """
        Cleanup
        """
        self.abort_goal()
        self.action_server.action_server.stop()
