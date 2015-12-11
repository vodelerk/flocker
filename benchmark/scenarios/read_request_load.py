# Copyright 2015 ClusterHQ Inc.  See LICENSE file for details.
"""
Read request load scenario for the control service benchmarks.
"""

from zope.interface import implementer

from twisted.internet.defer import CancelledError, Deferred
from twisted.internet.task import LoopingCall

from flocker.common import gather_deferreds, loop_until, timeout

from .._interfaces import IScenario

DEFAULT_SAMPLE_SIZE = 5


class RateMeasurer(object):
    """
    Measures the rate of requests in requests per second
    """

    def __init__(self, reactor, sample_size=DEFAULT_SAMPLE_SIZE):
        self.counts = []
        self.count = 0
        self.reactor = reactor
        self.sample_size = sample_size
        self.last_second = int(self.reactor.seconds())

    def new_sample(self):
        now = int(self.reactor.seconds())
        if now > self.last_second:
            self.counts.append(self.count)
            self.counts = self.counts[-self.sample_size:]
            self.last_second = now
            self.count = 0
        self.count += 1

    def rate(self):
        num_counts = len(self.counts)
        if num_counts == self.sample_size:
            return float(sum(self.counts) / float(num_counts))
        else:
            return float('nan')


class LoadGenerator(object):
    def __init__(self, request_generator, req_per_sec, interval, reactor):
        self._request_generator = request_generator
        self.req_per_sec = req_per_sec
        self.interval = interval
        self.reactor = reactor
        self._loops = []
        self._starts = []

    def start(self):
        for i in range(self.req_per_sec * self.interval):
            loop = LoopingCall(
                self._request_generator,
            )
            loop.clock = self.reactor
            self._loops.append(loop)
            started = loop.start(interval=self.interval)
            self._starts.append(started)

    def stop(self):
        for loop in self._loops:
            loop.stop()
        return gather_deferreds(self._starts)


class RequestRateTooLow(Exception):
    """
    The RequestRate dropped below a threshold.
    """


class RequestRateNotReached(Exception):
    """
    The RequestRate did not reach the target level.
    """


@implementer(IScenario)
class ReadRequestLoadScenario(object):
    """
    A scenario that places load on the cluster by performing read
    requests at a specified rate.
    """

    def __init__(
        self, reactor, cluster, request_rate=10, interval=10, timeout=45
    ):
        self._maintained = Deferred()
        self.reactor = reactor
        self.control_service = cluster.get_control_service(reactor)
        self.request_rate = request_rate
        self.interval = interval
        self.timeout = timeout
        self.rate_measurer = RateMeasurer(self.reactor)

    def _sample_and_return(self, result):
        self.rate_measurer.new_sample()
        return result

    def _request_and_measure(self):
        d = self.control_service.list_nodes()
        d.addCallback(self._sample_and_return)
        return d

    def start(self):
        """
        :return: A Deferred that fires when the desired scenario is
            established (e.g. that a certain load is being applied).
        """
        self.load_generator = LoadGenerator(
            request_generator=self._request_and_measure,
            req_per_sec=self.request_rate,
            interval=self.interval,
            reactor=self.reactor
        )
        self.load_generator.start()

        def reached_target_rate():
            current_rate = self.rate_measurer.rate()
            return current_rate >= self.request_rate

        def handle_timeout(failure):
            failure.trap(CancelledError)
            raise RequestRateNotReached

        waiting_for_target_rate = loop_until(self.reactor, reached_target_rate)
        timeout(self.reactor, waiting_for_target_rate, self.timeout)
        waiting_for_target_rate.addErrback(handle_timeout)

        def scenario_collapsed():
            return self.rate_measurer.rate() < self.request_rate

        # Start monitoring the scenario as soon as the target rate is reached.
        def monitor_scenario_status(result):
            scenario_monitor = loop_until(self.reactor, scenario_collapsed)
            scenario_monitor.addCallback(
                lambda ignored: self._maintained.errback(
                    RequestRateTooLow(self.rate_measurer.rate())
                )
            )
            return result

        waiting_for_target_rate.addCallback(monitor_scenario_status)

        return waiting_for_target_rate

    def maintained(self):
        """
        :return: A Deferred that fires with an errback if the desired
            scenario fails to hold between being established and being
            stopped.  This Deferred never fires with a callback.
        """
        return self._maintained

    def stop(self):
        """
        Stop the scenario from being maintained.

        :return: A Deferred that fires when the desired scenario is
            stopped.
        """
        return self.load_generator.stop()
