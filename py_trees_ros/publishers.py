#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# License: BSD
#   https://raw.githubusercontent.com/splintered-reality/py_trees_ros/devel/LICENSE
#
##############################################################################
# Description
##############################################################################

"""
Convenience behaviours for publishing ROS messages.
"""

##############################################################################
# Imports
##############################################################################

import typing

import py_trees
import rospy

##############################################################################
# Behaviours
##############################################################################


class FromBlackboard(py_trees.behaviour.Behaviour):
    """
    This behaviour looks up the blackboard for content to publish ...
    and publishes it.

    This is a non-blocking behaviour - if there is no data yet on
    the blackboard it will tick with :data:`~py_trees.common.Status.FAILURE`,
    otherwise :data:`~py_trees.common.Status.SUCCESS`.

    To convert it to a blocking behaviour, simply use with the
    :class:`py_trees.behaviours.WaitForBlackboardVariable`. e.g.

    .. code-block:: python

        sequence = py_trees.composites.Sequence(name="Sequence")
        wait_for_data = py_trees.behaviours.WaitForBlackboardVariable(
            name="WaitForData",
            variable_name="/my_message"
        )
        publisher = py_trees_ros.publishers.FromBlackboard(
            topic_name="/foo",
            topic_type=std_msgs.msg.Empty
            blackboard_variable="/my_message"
        )
        sequence.add_children([wait_for_data, publisher])

    The various set/unset blackboard variable behaviours can also be useful for
    setting and unsetting the message to be published (typically elsewhere
    in the tree).

    Args:
        topic_name: name of the topic to connect to
        topic_type: class of the message type (e.g. :obj:`std_msgs.msg.String`)
        blackboard_variable: name of the variable on the blackboard (can be nested)
        latch: whether the publisher should be latched
        queue_size: queue size of the publisher
        name: name of the behaviour
    """
    def __init__(self,
                 topic_name: str,
                 topic_type: typing.Any,
                 blackboard_variable: str,
                 latch: bool=False,
                 queue_size: int=1,
                 name: str=py_trees.common.Name.AUTO_GENERATED,
                 ):
        super().__init__(name=name)
        self.topic_name = topic_name
        self.topic_type = topic_type
        self.latch = latch
        self.queue_size = queue_size
        self.blackboard = self.attach_blackboard_client(name=self.name)
        self.blackboard_variable = blackboard_variable
        self.key = blackboard_variable.split('.')[0]  # in case it is nested
        self.blackboard.register_key(
            key=self.key,
            access=py_trees.common.Access.READ
        )
        self.publisher = None

    def setup(self, **kwargs):
        """
        Initialises the publisher.

        Args:
            **kwargs (:obj:`dict`): distribute arguments to this
               behaviour and in turn, all of it's children
        """
        self.publisher = rospy.Publisher(
            name=self.topic_name,
            data_class=self.topic_type,
            latch=self.latch,
            queue_size=self.queue_size
        )

    def update(self):
        """
        Publish the specified variable from the blackboard.

        Raises:
            TypeError if the blackboard variable is not of the required type

        Returns:
            :data:`~py_trees.common.Status.FAILURE` (variable does not exist on the blackboard) or :data:`~py_trees.common.Status.SUCCESS` (published)
        """
        self.logger.debug("%s.update()" % self.__class__.__name__)
        try:
            if isinstance(self.blackboard.get(self.blackboard_variable), self.topic_type):
                self.publisher.publish(self.blackboard.get(self.blackboard_variable))
            else:
                raise TypeError("{} is not the required type [{}][{}]".format(
                    self.blackboard_variable,
                    self.topic_type,
                    type(self.blackboard.get(self.blackboard_variable)))
                )
            self.feedback_message = "published"
            return py_trees.common.Status.SUCCESS
        except KeyError:
            self.feedback_message = "nothing to publish"
            return py_trees.common.Status.FAILURE
