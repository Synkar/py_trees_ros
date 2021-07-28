#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# License: BSD
#   https://raw.githubusercontent.com/splintered-reality/py_trees/devel/LICENSE
#
##############################################################################
# Documentation
##############################################################################

"""
.. argparse::
   :module: py_trees_ros.programs.blackboard_watcher
   :func: command_line_argument_parser
   :prog: py-trees-blackboard-watcher

Example interaction with the services of an :class:`py_trees_ros.blackboard.Exchange`:

.. image:: images/blackboard-watcher.gif
"""

##############################################################################
# Imports
##############################################################################

import argparse
import py_trees.console as console
import py_trees_ros
import rospy
import std_msgs.msg as std_msgs
import sys

##############################################################################
# Classes
##############################################################################


def description(formatted_for_sphinx):
    short = "Open up a window onto the blackboard!\n"
    long = ("\nIntrospect on the entire blackboard or a part thereof and receive a stream of\n"
            "updates whenever values change.\n"
            )
    examples = {
        "--list": "list all keys on the blackboard",
        "": "stream all variables",
        "--visited --activity": "stream only visited variables and access details",
        "odometry": "stream a single variable",
        "odometry.pose.pose.position": "stream only a single field within a variable"
    }
    script_name = "py-trees-blackboard-watcher"

    if formatted_for_sphinx:
        # for sphinx documentation (doesn't like raw text)
        s = short
        s += long
        s += "\n"
        s += "**Examples:**\n\n"
        s += ".. code-block:: bash\n"
        s += "    \n"
        for command, comment in examples.items():
            s += "    # {}\n".format(comment)
            s += "    $ " + script_name + " {}\n".format(command)
        s += "\n"
    else:
        banner_line = console.green + "*" * 79 + "\n" + console.reset
        s = "\n"
        s += banner_line
        s += console.bold_white + "Blackboard Watcher".center(79) + "\n" + console.reset
        s += banner_line
        s += "\n"
        s += short
        s += long
        s += "\n"
        s += console.bold + "Examples" + console.reset + "\n\n"
        for command, comment in examples.items():
            s += "    # {}\n".format(comment)
            s += "    $ " + console.cyan + script_name + console.yellow + " {}\n".format(command) + console.reset
        s += "\n\n"
        s += banner_line
    return s


def epilog(formatted_for_sphinx):
    if formatted_for_sphinx:
        return None
    else:
        return console.cyan + "And his noodly appendage reached forth to tickle the blessed...\n" + console.reset


def command_line_argument_parser(formatted_for_sphinx=True):
    # formatted_for_sphinx is an ugly hack to make sure sphinx does not pick up the colour codes.
    # works only by assuming that the only callee who calls it without setting the arg is sphinx's argparse
    parser = argparse.ArgumentParser(description=description(formatted_for_sphinx),
                                     epilog=epilog(formatted_for_sphinx),
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     )
    parser.add_argument('-l', '--list', action='store_true', default=None, help='list the blackboard variable names')
    parser.add_argument('-a', '--activity', action='store_true', help='include the logged activity stream for recent changes')
    parser.add_argument('-v', '--visited', action='store_true', help="filter selected keys from those associated with behaviours on the most recent tick's visited path")
    parser.add_argument('-n', '--namespace', nargs='?', default=None, help='namespace of blackboard services (if there should be more than one blackboard)')
    parser.add_argument('variables', nargs=argparse.REMAINDER, default=list(), help='space separated list of blackboard variable names (may be nested) to watch')
    return parser


def pretty_print_variables(variables):
    s = "\n"
    s += console.bold + console.cyan + "Blackboard Variables:" + console.reset + console.yellow + "\n"
    for variable in variables:
        variable = variable.split('.')
        if len(variable) > 1:
            sep = "."
        else:
            sep = ""
        s += "    " * len(variable) + sep + variable[-1] + "\n"
    s += console.reset
    print("{}".format(s))


##############################################################################
# Main
##############################################################################


def main(command_line_args=sys.argv[1:]):
    """
    Entry point for the blackboard watcher script.
    """
    # command_line_args = rclpy.utilities.remove_ros_args(command_line_args)[1:]
    parser = command_line_argument_parser(formatted_for_sphinx=False)
    args = parser.parse_args(command_line_args)

    rospy.init_node('blackboard_watcher', anonymous=True)
    blackboard_watcher = py_trees_ros.blackboard.BlackboardWatcher(
        namespace_hint=args.namespace
    )
    watcher_topic_name = None
    subscription = None
    ####################
    # Setup
    ####################
    try:
        blackboard_watcher.setup(timeout_sec=2.0)
    # setup discovery fails
    except py_trees_ros.exceptions.NotFoundError as e:
        print(console.red + "\nERROR: {}\n".format(str(e)) + console.reset)
        sys.exit(1)
    # setup discovery finds duplicates
    except py_trees_ros.exceptions.MultipleFoundError as e:
        print(console.red + "\nERROR: {}\n".format(str(e)) + console.reset)
        if args.namespace is None:
            print(console.red + "\nERROR: select one with the --namespace argument\n" + console.reset)
        else:
            print(console.red + "\nERROR: but none matching the requested '{}'\n".format(args.namespace) + console.reset)
        sys.exit(1)

    def close_connection():
        if subscription is not None:
            subscription.unregister()

        if watcher_topic_name is not None:
            request, client = blackboard_watcher.create_service_client('close')
            request.topic_name = watcher_topic_name
            try:
                response = client(request)
            except rospy.ServiceException as e:
                raise py_trees_ros.exceptions.ServiceError(
                    "service call to close connection failed [{}]".format(e)
                ) from e

    ####################
    # Execute
    ####################
    result = 0
    try:
        if args.list:
            request, client = blackboard_watcher.create_service_client('list')
            result = client(request)
            pretty_print_variables(result.variables)
        else:
            # request connection
            request, client = blackboard_watcher.create_service_client('open')
            request.variables = [variable.strip(',[]') for variable in args.variables]
            request.filter_on_visited_path = args.visited
            request.with_activity_stream = args.activity
            response = client(request)
            client.close()
            rospy.on_shutdown(close_connection)
            # connect
            watcher_topic_name = response.topic
            rospy.loginfo(
                "creating subscription [{}]".format(watcher_topic_name)
            )
            subscription = rospy.Subscriber(
                watcher_topic_name,
                std_msgs.String,
                callback=blackboard_watcher.echo_blackboard_contents,
                queue_size=1
            )
            # stream
            try:
                rospy.spin()
            except KeyboardInterrupt:
                pass
    # connection problems
    except (py_trees_ros.exceptions.NotReadyError,
            py_trees_ros.exceptions.ServiceError,
            py_trees_ros.exceptions.TimedOutError) as e:
        print(console.red + "\nERROR: {}".format(str(e)) + console.reset)
        result = 1
    if subscription is not None:
        subscription.unregister()
    blackboard_watcher.shutdown()
    rospy.signal_shutdown('Application finished')
    sys.exit(result)
