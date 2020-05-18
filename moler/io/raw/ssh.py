# -*- coding: utf-8 -*-
"""
External-IO connections based on Paramiko.

The only 3 requirements for these connections are:
(1) store Moler's connection inside self.moler_connection attribute
(2) plugin into Moler's connection the way IO outputs data to external world:

    self.moler_connection.how2send = self.send

(3) forward IO received data into self.moler_connection.data_received(data)
"""

__author__ = 'Grzegorz Latuszek'
__copyright__ = 'Copyright (C) 2020, Nokia'
__email__ = 'grzegorz.latuszek@nokia.com'

import socket
import sys
import threading
import contextlib
import paramiko
import time

from moler.io.io_exceptions import ConnectionTimeout
from moler.io.io_exceptions import RemoteEndpointDisconnected
from moler.io.io_exceptions import RemoteEndpointNotConnected
from moler.io.raw import TillDoneThread
import datetime


# TODO: logging - want to know what happens on GIVEN connection
# TODO: logging - rethink details


class Ssh(object):
    """Implementation of Ssh connection using python Paramiko module"""
    def __init__(self, host, port=22, username=None, password=None, receive_buffer_size=64 * 4096,
                 logger=None):
        """Initialization of Ssh connection."""
        super(Ssh, self).__init__()
        # TODO: do we want connection.name?
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.receive_buffer_size = receive_buffer_size
        self.logger = logger  # TODO: build default logger if given is None?

        self.ssh_client = paramiko.SSHClient()
        self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.shell_channel = None  # MOST IMPORTANT
        self.timeout = None

    def settimeout(self, timeout):
        if (self.timeout is None) or (timeout != self.timeout):
            if self.shell_channel:
                self.shell_channel.settimeout(timeout)
                self.timeout = timeout

    def open(self):
        """
        Open Ssh connection.

        Should allow for using as context manager: with connection.open():
        """
        self._debug('connecting to {}'.format(self))

        self.ssh_client.connect(self.host, username=self.username, password=self.password)
        transport = self.ssh_client.get_transport()
        transport_info = ['local version = {}'.format(transport.local_version),
                          'remote version = {}'.format(transport.remote_version),
                          'using socket = {}'.format(transport.sock)]
        self._debug('  established Ssh transport: {}\n    {}'.format(transport,
                                                                     "\n    ".join(transport_info)))
        self._debug('  opening shell ssh channel to {}'.format(self.host))
        self.shell_channel = self.ssh_client.invoke_shell()  # newly created channel will be connected to Pty
        self._debug('    established shell ssh channel {}'.format(self.shell_channel))
        self._debug('connection {} is open'.format(self))
        return contextlib.closing(self)

    def close(self):
        """
        Close Ssh connection. Close channel of that connection.

        Connection should allow for calling close on closed/not-open connection.
        """
        self._debug('closing {}'.format(self))
        if self.shell_channel is not None:
            self._debug('  closing shell ssh channel {}'.format(self.shell_channel))
            self.shell_channel.close()
            time.sleep(0.05)  # give Paramiko threads time to catch correct value of status variables
            self._debug('  closed  shell ssh channel {}'.format(self.shell_channel))
            self.shell_channel = None
        # TODO: don't close connection if there are still channels on it
        self._debug('  closing ssh transport {}'.format(self.ssh_client._transport))
        self.ssh_client.close()
        self._debug('connection {} is closed'.format(self))

    def __enter__(self):
        """While working as context manager connection should auto-open if it's not open yet."""
        if self.shell_channel is None:
            self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False  # reraise exceptions if any

    def __str__(self):
        address = 'ssh://{}@{}:{}'.format(self.username,self.host, self.port)
        return address

    def _debug(self, msg):  # TODO: refactor to class decorator or so
        if self.logger:
            self.logger.debug(msg)
        else:
            print(msg)

conn1 = Ssh(host='192.168.44.50', port=22, username='vagrant', password='vagrant')
conn1.open()
conn1.close()
