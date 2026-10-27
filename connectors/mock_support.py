"""Shared mechanics for the in-process mock providers (test doubles)."""

from threading import Event, Thread


class HeldCall:
    """A request the provider accepted but has not committed yet.

    respond_now=True models a late commit: the caller gets no answer
    at once (a timeout), and the provider commits when released.
    """

    def __init__(self, *, respond_now=False):
        self.respond_now = respond_now
        self.started = Event()
        self._release = Event()
        self.committed = Event()

    def release(self):
        self._release.set()

    def wait_committed(self, timeout=5):
        return self.committed.wait(timeout)


def await_held(held, timeout, commit, no_answer):
    """Wait for the held request; commit late if the caller gives up.

    commit() applies the effect under the provider's lock and returns
    the receipt. no_answer is the exception raised to the caller when
    it stops waiting before the provider commits.
    """

    held.started.set()
    held._release.wait(0 if held.respond_now else timeout)
    if not held._release.is_set():
        def later():
            held._release.wait()
            commit()
            held.committed.set()

        Thread(target=later, daemon=True).start()
        raise no_answer

    receipt = commit()
    held.committed.set()
    return receipt
