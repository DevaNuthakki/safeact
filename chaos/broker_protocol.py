"""Explicit wire protocols for owned development brokers."""

import re
from dataclasses import dataclass

from chaos.baselines.trace import queued_message as baseline_message
from deploy.stack.broker_trace import queued_message as safeact_message


@dataclass(frozen=True)
class Protocol:
    name: str
    queue_pattern: str

    def valid_queue(self,queue):
        return type(queue) is str and re.fullmatch(self.queue_pattern,queue) is not None

    def decode(self,envelope,queue):
        if not self.valid_queue(queue):raise ValueError('Unexpected owned queue')
        if self.name=='safeact':return safeact_message(envelope,queue)
        return baseline_message(envelope,queue)


def protocol(name):
    if name=='safeact':return Protocol(name,r'safeact-q[0-6]-[0-9a-f]{6}')
    if name=='baseline':return Protocol(name,r'baseline-[0-9a-f]{32}')
    raise ValueError('Unknown owned broker protocol')
