#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# License: BSD
#   https://raw.githubusercontent.com/splintered-reality/py_trees_ros/devel/LICENSE
#
##############################################################################
# Documentation
##############################################################################

"""
Assorted utility functions.
"""

##############################################################################
# Imports
##############################################################################

import os
import pathlib

import py_trees_ros_interfaces.msg as py_trees_msgs  # noqa
import py_trees_ros_interfaces.srv as py_trees_srvs  # noqa
import rospkg
import rospy
import rosservice
import rostopic
import time
import typing

from . import exceptions

##############################################################################
# Methods
##############################################################################


def find_service(service_type: str,
                 namespace: str=None,
                 timeout: float=0.5):
    """
    Discover a service of the specified type and if necessary, under the specified
    namespace.

    Args:
        service_type (:obj:`str`): primary lookup hint
        namespace (:obj:`str`): secondary lookup hint
        timeout: immediately post node creation, can take time to discover the graph (sec)

    Returns:
        :obj:`str`: fully expanded service name

    Raises:
        :class:`~py_trees_ros.exceptions.NotFoundError`: if no services were found
        :class:`~py_trees_ros.exceptions.MultipleFoundError`: if multiple services were found
    """
    loop_period = 0.1  # seconds
    start_time = rospy.Time.now()
    service_names = []
    while rospy.Time.now() - start_time < rospy.Duration(secs=timeout):
        service_names = rosservice.rosservice_find(service_type)
        if namespace is not None:
            service_names = [name for name in service_names if namespace in name]
        if service_names:
            break
        rospy.sleep(loop_period)

    if not service_names:
        raise exceptions.NotFoundError("service not found [type: {}]".format(service_type))
    elif len(service_names) == 1:
        return service_names[0]
    else:
        raise exceptions.MultipleFoundError("multiple services found [type: {}]".format(service_type))


def find_topics(
        topic_type: str,
        namespace: str=None,
        timeout: float=0.5) -> typing.List[str]:
    """
    Discover a topic of the specified type and if necessary, under the specified
    namespace.

    Args:
        topic_type: primary lookup hint
        namespace: secondary lookup hint
        timeout: check every 0.1s until this timeout is reached (can be None -> checks once)

    .. note: Immediately post node creation, it can take some time to discover the graph.

    Returns:
        list of fully expanded topic names (can be empty)
    """
    loop_period = 0.1  # seconds
    start_time = rospy.Time.now()
    topic_names = []
    while True:
        topic_names = rostopic.find_by_type(topic_type)
        if namespace is not None:
            topic_names = [name for name in topic_names if namespace in name]
        if topic_names:
            break
        if timeout is None or (rospy.Time.now() - start_time) > rospy.Duration(secs=timeout):
            break
        else:
            rospy.sleep(loop_period)
    return topic_names


def basename(name):
    """
    Generate the basename from a ros name.

    Args:
        name (:obj:`str`): ros name

    Returns:
        :obj:`str`: name stripped up until the last slash or tilde character.
    Examples:

        .. code-block:: python

           basename("~dude")
           # 'dude'
           basename("/gang/dude")
           # 'dude'
    """
    return name.rsplit('/', 1)[-1].rsplit('~', 1)[-1]


def get_py_trees_home():
    """
    Find the default home directory used for logging, bagging and other
    esoterica.
    """
    return os.path.join(rospkg.get_ros_home(), "py_trees")


def resolve_name(name):
    """
    Convenience function for getting the resolved name (similar to 'publisher.resolved_name' in ROS1).

    Args:
        name (obj:`str`): topic or service name

    .. note::

       This entirely depends on the user providing the relevant node, name pair.
    """
    return rospy.resolve_name(name)


def create_anonymous_node_name(node_name="node") -> str:
    """
    Creates an anonoymous node name by adding a suffix created from
    a monotonic timestamp, sans the decimal.

    Returns:
        :obj:`str`: the unique, anonymous node name
    """
    return node_name + "_" + str(time.monotonic()).replace('.', '')


##############################################################################
# Convenience Classes
##############################################################################


class Publishers(object):
    """
    Utility class that groups the publishers together in one convenient structure.

    Args:
        publisher_details (obj:`tuple`): list of (str, str, msgType, bool, int) tuples representing
                                  (unique_name, topic_name, publisher_type, latched)
                                  specifications for creating publishers

    Examples:
        Convert the incoming list of specifications into proper variables of this class.

        .. code-block:: python

           publishers = py_trees.utilities.Publishers(
               [
                   ('foo', '~/foo', std_msgs.String, True, 5),
                   ('bar', '/foo/bar', std_msgs.String, False, 5),
                   ('foobar', '/foo/bar', std_msgs.String, False, 5),
               ]
           )
    """
    def __init__(self, publisher_details, introspection_topic_name="publishers"):
        # TODO: check for the correct setting of publisher_details
        self.publisher_details_msg = []
        for (name, topic_name, publisher_type, latched) in publisher_details:
            self.__dict__[name] = rospy.Publisher(topic_name, publisher_type, latch=latched, queue_size=1)

            resolved_name = resolve_name(topic_name)
            message_type = publisher_type.__module__.split('.')[0] + "/" + publisher_type.__name__
            self.publisher_details_msg.append(
                py_trees_msgs.PublisherDetails(
                    topic_name=resolved_name,
                    message_type=message_type,
                    latched=latched
                )
            )

        self.introspection_service = rospy.Service(
            name="~/introspection/" + introspection_topic_name,
            service_class=py_trees_srvs.IntrospectPublishers,
            handler=self.introspection_callback
        )

    def introspection_callback(self, unused_request):
        response = py_trees_srvs.IntrospectPublishersResponse()
        response.publisher_details = self.publisher_details_msg
        return response


class Subscribers(object):
    """
    Utility class that groups subscribers together in one convenient structure.

    Args:
        subscriber_details (obj:`tuple`): list of (str, str, msgType, bool, func) tuples representing
                                  (unique_name, topic_name, subscriber_type, latched, callback)
                                  specifications for creating subscribers

    Examples:
        Convert the incoming list of specifications into proper variables of this class.

        .. code-block:: python

           subscribers = py_trees.utilities.Subscribers(
               [
                   ('foo', '~/foo', std_msgs.String, True, foo),
                   ('bar', '/foo/bar', std_msgs.String, False, self.foo),
                   ('foobar', '/foo/bar', std_msgs.String, False, foo.bar),
               ]
           )
    """
    def __init__(self, subscriber_details, introspection_topic_name="subscribers"):
        # TODO: check for the correct setting of subscriber_details
        self.subscriber_details_msg = []
        for (name, topic_name, subscriber_type, latched, callback) in subscriber_details:
            self.__dict__[name] = rospy.Subscriber(topic_name, subscriber_type, callback=callback)

            resolved_name = resolve_name(topic_name)
            message_type = subscriber_type.__module__.split('.')[0] + "/" + subscriber_type.__name__
            self.subscriber_details_msg.append(
                py_trees_msgs.SubscriberDetails(
                    topic_name=resolved_name,
                    message_type=message_type,
                    latched=latched
                )
            )

        self.introspection_service = rospy.Service(
            name="~/introspection/" + introspection_topic_name,
            service_class=py_trees_srvs.IntrospectSubscribers,
            handler=self.introspection_callback
        )

    def introspection_callback(self, unused_request):
        response = py_trees_srvs.IntrospectSubscribersResponse()
        response.subscriber_details = self.subscriber_details_msg
        return response


class Services(object):
    """
    Utility class that groups services together in one convenient structure.

    Args:
        service_details (obj:`tuple`): list of (str, str, srvType, func) tuples representing
                                  (unique_name, topic_name, service_type, callback)
                                  specifications for creating services

    Examples:
        Convert the incoming list of specifications into proper variables of this class.

        .. code-block:: python

           services = py_trees.utilities.Services(
               [
                   ('open_foo', '~/get_foo', foo_interfaces.srv.OpenFoo, open_foo_callback),
                   ('open_foo', '/foo/open', foo_interfaces.srv.OpenFoo, self.open_foo_callback),
                   ('get_foo_bar', '/foo/bar', foo_interfaces.srv.GetBar, self.foo.get_bar_callback),
               ]
           )
    """
    def __init__(self, service_details, introspection_topic_name="services"):
        # TODO: check for the correct setting of subscriber_details
        self.service_details_msg = []
        for (name, service_name, service_type, callback) in service_details:
            self.__dict__[name] = rospy.Service(service_name, service_type, callback)

            resolved_name = resolve_name(service_name)
            service_type = service_type.__module__.split('.')[0] + "/" + service_type.__name__
            self.service_details_msg.append(
                py_trees_msgs.ServiceDetails(
                    service_name=resolved_name,
                    service_type=service_type,
                )
            )

        self.introspection_service = rospy.Service(
            name="~/introspection/" + introspection_topic_name,
            service_class=py_trees_srvs.IntrospectServices,
            handler=self.introspection_callback
        )

    def introspection_callback(self, unused_request):
        response = py_trees_srvs.IntrospectServicesResponse()
        response.subscriber_details = self.subscriber_details_msg
        return response
