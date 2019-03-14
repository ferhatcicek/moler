# -*- coding: utf-8 -*-
# Copyright (C) 2018 Nokia
"""
Runner abstraction goal is to hide concurrency machinery used
to make it exchangeable (threads, asyncio, twisted, curio)
"""

__author__ = 'Grzegorz Latuszek, Marcin Usielski, Michal Ernst'
__copyright__ = 'Copyright (C) 2018-2019, Nokia'
__email__ = 'grzegorz.latuszek@nokia.com, marcin.usielski@nokia.com, michal.ernst@nokia.com'

import atexit
import concurrent.futures
import logging
import time
import threading
from abc import abstractmethod, ABCMeta
from concurrent.futures import ThreadPoolExecutor, wait
from six import add_metaclass

from moler.exceptions import CommandTimeout
from moler.exceptions import ConnectionObserverTimeout
from moler.exceptions import MolerException
from moler.util.loghelper import log_into_logger

# fix for concurrent.futures  v.3.0.3  to have API of v.3.1.1 or above
try:
    from multiprocessing import cpu_count
except ImportError:
    # some platforms don't have multiprocessing
    def cpu_count():
        """Workarround fix"""
        return None


@add_metaclass(ABCMeta)
class ConnectionObserverRunner(object):
    @abstractmethod
    def shutdown(self):
        """Cleanup used resources."""
        pass

    @abstractmethod
    def submit(self, connection_observer):
        """
        Submit connection observer to background execution.
        Returns Future that could be used to await for connection_observer done.
        """
        pass

    @abstractmethod
    def wait_for(self, connection_observer, connection_observer_future, timeout=10.0):
        """
        Await for connection_observer running in background or timeout.

        :param connection_observer: The one we are awaiting for.
        :param connection_observer_future: Future of connection-observer returned from submit().
        :param timeout: Max time (in float seconds) you want to await before you give up.
        :return:
        """
        pass

    @abstractmethod
    def wait_for_iterator(self, connection_observer, connection_observer_future):
        """
        Version of wait_for() intended to be used by Python3 to implement iterable/awaitable object.

        Note: we don't have timeout parameter here. If you want to await with timeout please do use timeout machinery
        of selected parallelism.

        :param connection_observer: The one we are awaiting for.
        :param connection_observer_future: Future of connection-observer returned from submit().
        :return: iterator
        """
        pass

    @abstractmethod
    def feed(self, connection_observer):
        """
        Feeds connection_observer with data to let it become done.
        This is a place where runner is a glue between words of connection and connection-observer.
        Should be called from background-processing of connection observer.
        """
        pass

    @abstractmethod
    def timeout_change(self, timedelta):
        """
        Call this method to notify runner that timeout has been changed in observer
        :param timedelta: delta timeout in float seconds
        :return: Nothing
        """
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()
        return False  # exceptions (if any) should be reraised


def time_out_observer(connection_observer, timeout, passed_time, runner_logger, kind="background_run"):
    """Set connection_observer status to timed-out"""
    if not connection_observer.done():
        if hasattr(connection_observer, "command_string"):
            exception = CommandTimeout(connection_observer=connection_observer,
                                       timeout=timeout, kind=kind, passed_time=passed_time)
        else:
            exception = ConnectionObserverTimeout(connection_observer=connection_observer,
                                                  timeout=timeout, kind=kind, passed_time=passed_time)
        # TODO: secure_data_received() may change status of connection_observer
        # TODO: and if secure_data_received() runs inside threaded connection - we have race
        connection_observer.set_exception(exception)

        connection_observer.on_timeout()

        observer_info = "{}.{}".format(connection_observer.__class__.__module__, connection_observer)
        timeout_msg = "has timed out after {:.2f} seconds.".format(passed_time)
        msg = "{} {}".format(observer_info, timeout_msg)

        # levels_to_go_up: extract caller info to log where .time_out_observer has been called from
        connection_observer._log(logging.INFO, msg, levels_to_go_up=2)
        log_into_logger(runner_logger, level=logging.INFO,
                        msg="{} {}".format(connection_observer, timeout_msg),
                        levels_to_go_up=1)


def result_for_runners(connection_observer):
    """
    When runner takes result from connection-observer it should not
    modify ConnectionObserver._not_raised_exceptions

    :param connection_observer: observer to get result from
    :return: result or raised exception
    """
    if connection_observer._exception:
        raise connection_observer._exception
    return connection_observer.result()


class CancellableFuture(object):
    def __init__(self, future, observer_lock, stop_running, is_done, stop_timeout=0.5):
        """
        Wrapper to allow cancelling already running concurrent.futures.Future

        Assumes that executor submitted function with following parameters
        fun(stop_running, is_done)
        and that such function correctly handles that events (threading.Event)

        :param future: wrapped instance of concurrent.futures.Future
        :param stop_running: set externally to finish thread execution of function
        :param is_done: set when function finished running in thread
        :param stop_timeout: timeout to await is_done after setting stop_running
        """
        self._future = future
        self.observer_lock = observer_lock  # against threads race write-access to observer
        self._stop_running = stop_running
        self._stop_timeout = stop_timeout
        self._is_done = is_done

    def __getattr__(self, attr):
        """Make it proxy to embedded future"""
        attribute = getattr(self._future, attr)
        return attribute

    def cancel(self, no_wait=False):
        """
        Cancel embedded future
        :param no_wait: if True - just set self._stop_running event to let thread exit loop
        :return:
        """
        if self.running():
            self._stop(no_wait)
            if no_wait:
                return True
            # after exiting threaded-function future.state == FINISHED
            # we need to change it to PENDING to allow for correct cancel via concurrent.futures.Future
            with self._condition:
                self._future._state = concurrent.futures._base.PENDING

        return self._future.cancel()

    def _stop(self, no_wait=False):
        self._stop_running.set()  # force threaded-function to exit
        if no_wait:
            return
        if not self._is_done.wait(timeout=self._stop_timeout):
            err_msg = "Failed to stop thread-running function within {} sec".format(self._stop_timeout)
            # TODO: should we break current thread or just set this exception inside connection-observer
            #       (is it symetric to failed-start ?)
            # may cause leaking resources - no call to moler_conn.unsubscribe()
            raise MolerException(err_msg)


class ThreadPoolExecutorRunner(ConnectionObserverRunner):
    def __init__(self, executor=None):
        """Create instance of ThreadPoolExecutorRunner class"""
        self._in_shutdown = False
        self._i_own_executor = False
        self.executor = executor
        self.logger = logging.getLogger('moler.runner.thread-pool')
        self.logger.debug("created")
        atexit.register(self.shutdown)
        if executor is None:
            max_workers = (cpu_count() or 1) * 5  # fix for concurrent.futures  v.3.0.3  to have API of v.3.1.1 or above
            try:  # concurrent.futures  v.3.2.0 introduced prefix we like :-)
                self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='ThrdPoolRunner')
            except TypeError as exc:
                if ('unexpected' in str(exc)) and ('thread_name_prefix' in str(exc)):
                    self.executor = ThreadPoolExecutor(max_workers=max_workers)
                else:
                    raise
            self.logger.debug("created own executor {!r}".format(self.executor))
            self._i_own_executor = True
        else:
            self.logger.debug("reusing provided executor {!r}".format(self.executor))

    def shutdown(self):
        self.logger.debug("shutting down")
        self._in_shutdown = True  # will exit from feed() without stopping executor (since others may still use that executor)
        if self._i_own_executor:
            self.executor.shutdown()  # also stop executor since only I use it

    def submit(self, connection_observer):
        """
        Submit connection observer to background execution.
        Returns Future that could be used to await for connection_observer done.
        """
        assert connection_observer.start_time > 0.0  # connection-observer lifetime should already been started
        observer_timeout = connection_observer.timeout
        remain_time, msg = his_remaining_time("remaining", he=connection_observer, timeout=observer_timeout)
        self.logger.debug("go background: {!r} - {}".format(connection_observer, msg))
        # TODO: check dependency - connection_observer.connection

        # Our submit consists of two steps:
        # 1. _start_feeding() which establishes data path from connection to observer
        # 2. scheduling "background feed" via executor.submit()
        #
        # By using the code of _start_feeding() we ensure that after submit() connection data could reach
        # data_received() of observer - as it would be "virtually running in background"
        # Another words, no data will be lost-for-observer between runner.submit() and runner.feed() really running
        #
        # We do not await here (before returning from submit()) for "background feed" to be really started.
        # That is in sync with generic nature of threading.Thread - after thread.start() we do not have
        # running thread - it is user responsibility to await for threads switch.
        # User may check "if thread is running" via Thread.is_alive() API.
        # For concurrent.futures same is done via future.running() API.
        #
        # However, lifetime of connection_observer starts in connection_observer.start().
        # It gains it's own timer so that timeout is calculated from that connection_observer.start_time
        # That lifetime may start even before this submit() if observer is command and we have commands queue.
        #
        # As a corner case runner.wait_for() may timeout before feeding thread has started.

        stop_feeding = threading.Event()
        feed_done = threading.Event()
        observer_lock = threading.Lock()  # against threads race write-access to observer
        subscribed_data_receiver = self._start_feeding(connection_observer, observer_lock)
        connection_observer_future = self.executor.submit(self.feed, connection_observer,
                                                          subscribed_data_receiver,
                                                          stop_feeding, feed_done, observer_lock)
        if connection_observer_future.done():
            # most probably we have some exception during submit(); it should be stored inside future
            try:
                too_early_result = connection_observer_future.result()
                err_msg = "PROBLEM: future returned {} already in runner.submit()".format(too_early_result)
                self.logger.warning("go background: {} - {}".format(connection_observer, err_msg))
            except Exception as err:
                err_msg = "PROBLEM: future raised {!r} during runner.submit()".format(err)
                self.logger.warning("go background: {} - {}".format(connection_observer, err_msg))
                self.logger.exception(err_msg)
                raise

        c_future = CancellableFuture(connection_observer_future, observer_lock,
                                     stop_feeding, feed_done)
        return c_future

    def wait_for(self, connection_observer, connection_observer_future, timeout=None):
        """
        Await for connection_observer running in background or timeout.

        :param connection_observer: The one we are awaiting for.
        :param connection_observer_future: Future of connection-observer returned from submit().
        :param timeout: Max time (in float seconds) you want to await before you give up. If None then taken from connection_observer
        :return:
        """
        # TODO: calculate remaining timeout before logging + done(result/exception) info
        if connection_observer.done():
            # 1. done() might mean "timed out" before future start
            #    Observer lifetime started with its timeout clock so, it might timeout even before
            #    connection_observer_future started - since future's thread might not get control yet
            # 2. done() might mean "timed out" before wait_for()
            #    wait_for() might be called so late after submit() that observer already "timed out"
            # 3. done() might mean have result or got exception
            #    wait_for() might be called so late after submit() that observer already got result/exception
            #
            # In all above cases we want to stop future if it is still running

            self.logger.debug("go foreground: {} is already done".format(connection_observer))
            if not connection_observer_future.done():
                connection_observer_future.cancel(no_wait=True)
            return None

        start_time = connection_observer.start_time
        max_timeout = timeout
        observer_timeout = connection_observer.timeout
        if max_timeout:
            remain_time, msg = his_remaining_time("await max.", he=connection_observer, timeout=max_timeout)
        else:
            remain_time, msg = his_remaining_time("remaining", he=connection_observer, timeout=observer_timeout)

        self.logger.debug("go foreground: {} - {}".format(connection_observer, msg))

        # either we wait forced-max-timeout or we check done-status each 0.1sec tick
        if max_timeout:
            if remain_time > 0.0:
                done, not_done = wait([connection_observer_future], timeout=remain_time)
                if connection_observer_future in done:
                    return None
        else:
            wait_tick = 0.1
            while remain_time > 0.0:
                done, not_done = wait([connection_observer_future], timeout=wait_tick)
                if connection_observer_future in done:
                    return None
                timeout = connection_observer.timeout
                already_passed = time.time() - start_time
                remain_time = timeout - already_passed

        # code below is for timed out observer
        passed = time.time() - start_time
        connection_observer_future.cancel(no_wait=True)
        fired_timeout = timeout if timeout else connection_observer.timeout
        with connection_observer_future.observer_lock:
            time_out_observer(connection_observer=connection_observer,
                              timeout=fired_timeout, passed_time=passed,
                              runner_logger=self.logger, kind="await_done")

        return None

    def wait_for_iterator(self, connection_observer, connection_observer_future):
        """
        Version of wait_for() intended to be used by Python3 to implement iterable/awaitable object.

        Note: we don't have timeout parameter here. If you want to await with timeout please do use timeout machinery
        of selected parallelism.

        :param connection_observer: The one we are awaiting for.
        :param connection_observer_future: Future of connection-observer returned from submit().
        :return: iterator
        """
        while not connection_observer_future.done():
            yield None
        # return result_for_runners(connection_observer)  # May raise too.   # Python > 3.3
        res = result_for_runners(connection_observer)
        raise StopIteration(res)  # Python 2 compatibility

    def _start_feeding(self, connection_observer, observer_lock):
        """
        Start feeding connection_observer by establishing data-channel from connection to observer.
        """
        def secure_data_received(data):
            try:
                if connection_observer.done() or self._in_shutdown:
                    return  # even not unsubscribed secure_data_received() won't pass data to done observer
                with observer_lock:
                    connection_observer.data_received(data)

            except Exception as exc:  # TODO: handling stacktrace
                # observers should not raise exceptions during data parsing
                # but if they do so - we fix it
                with observer_lock:
                    connection_observer.set_exception(exc)
            finally:
                if connection_observer.done() and not connection_observer.cancelled():
                    if connection_observer._exception:
                        self.logger.debug("{} raised: {!r}".format(connection_observer, connection_observer._exception))
                    else:
                        self.logger.debug("{} returned: {}".format(connection_observer, connection_observer._result))

        moler_conn = connection_observer.connection
        self.logger.debug("subscribing for data {}".format(connection_observer))
        moler_conn.subscribe(secure_data_received)
        if connection_observer.is_command():
            connection_observer.connection.sendline(connection_observer.command_string)
        return secure_data_received  # to know what to unsubscribe

    def feed(self, connection_observer, subscribed_data_receiver, stop_feeding, feed_done,
             observer_lock):
        """
        Feeds connection_observer by transferring data from connection and passing it to connection_observer.
        Should be called from background-processing of connection observer.
        """
        remain_time, msg = his_remaining_time("remaining", he=connection_observer, timeout=connection_observer.timeout)
        self.logger.info("{} started, {}".format(connection_observer, msg))
        connection_observer._log(logging.INFO, "{} started, {}".format(connection_observer.get_long_desc(), msg))

        if not subscribed_data_receiver:
            subscribed_data_receiver = self._start_feeding(connection_observer, observer_lock)

        time.sleep(0.005)  # give control back before we start processing

        moler_conn = connection_observer.connection

        self._feed_loop(connection_observer, stop_feeding, observer_lock)

        self.logger.debug("unsubscribing {}".format(connection_observer))
        moler_conn.unsubscribe(subscribed_data_receiver)
        feed_done.set()

        remain_time, msg = his_remaining_time("remaining", he=connection_observer, timeout=connection_observer.timeout)
        connection_observer._log(logging.INFO, "{} finished, {}".format(connection_observer.get_short_desc(), msg))
        self.logger.info("{} finished, {}".format(connection_observer, msg))
        return None

    def _feed_loop(self, connection_observer, stop_feeding, observer_lock):
        start_time = connection_observer.start_time
        while True:
            if stop_feeding.is_set():
                # TODO: should it be renamed to 'cancelled' to be in sync with initial action?
                self.logger.debug("stopped {}".format(connection_observer))
                break
            if connection_observer.done():
                self.logger.debug("done {}".format(connection_observer))
                break
            run_duration = time.time() - start_time
            # we need to check connection_observer.timeout at each round since timeout may change
            # during lifetime of connection_observer
            if (connection_observer.timeout is not None) and (run_duration >= connection_observer.timeout):
                with observer_lock:
                    time_out_observer(connection_observer,
                                      timeout=connection_observer.timeout,
                                      passed_time=run_duration,
                                      runner_logger=self.logger)
                break
            if self._in_shutdown:
                self.logger.debug("shutdown so cancelling {}".format(connection_observer))
                connection_observer.cancel()
            time.sleep(0.005)  # give moler_conn a chance to feed observer

    def timeout_change(self, timedelta):
        pass


# utilities to be used by runners


def his_remaining_time(prefix, he, timeout):
    """
    Calculate remaining time of "he" object assuming that "he" has .start_time attribute

    :param prefix: string to be used inside 'remaining time description'
    :param he: object to calculate remaining time for
    :param timeout: max lifetime of object
    :return: remaining time as float and related description message
    """
    start_time = he.start_time
    already_passed = time.time() - start_time
    remain_time = timeout - already_passed
    if remain_time < 0.0:
        remain_time = 0.0
    msg = "{} {:.3f} [sec], already passed {:.3f} [sec]".format(prefix, remain_time, already_passed)
    return remain_time, msg
