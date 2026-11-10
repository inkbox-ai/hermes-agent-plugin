"""Associate asynchronous turns with their durable delivery receipt."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from weakref import WeakKeyDictionary

_current = ContextVar('managed_deliveries', default=())
_owners = WeakKeyDictionary()


class AdmissionRejected(RuntimeError):
    """A typed native refusal before any execution was admitted."""

    code = 'native_sender_not_authorized'


class DeliveryWork:
    def __init__(self):
        self.parts = set()
        self.failed = False
        self.uncertain = False
        self.rejection = None
        self.sealed = False
        self.done = asyncio.get_running_loop().create_future()

    def add(self, part):
        if self.done.done():
            raise RuntimeError('Delivery work was already completed')
        self.parts.add(part)

    def finish(self, part, *, success, rejection=None):
        self.failed |= not success
        self.uncertain |= not success and rejection is None
        self.rejection = rejection or self.rejection
        self.parts.discard(part)
        self.check()

    def seal(self):
        self.sealed = True
        self.check()

    def check(self):
        if self.sealed and not self.parts and not self.done.done():
            self.done.set_result(not self.failed)


@contextmanager
def scope(work):
    token = _current.set(tuple(work))
    try:
        yield
    finally:
        _current.reset(token)


def spawn(coroutine, **kwargs):
    task = asyncio.create_task(coroutine, **kwargs)
    owners = list(_current.get())
    _owners[task] = owners
    for work in owners:
        work.add(task)
    def finished(done):
        _owners.pop(done, None)
        error = None if done.cancelled() else done.exception()
        success = not done.cancelled() and error is None
        rejection = error.code if isinstance(error, AdmissionRejected) else None
        for work in owners:
            work.finish(done, success=success, rejection=rejection)
    if owners:
        task.add_done_callback(finished)
    return task


def background(coroutine, **kwargs):
    with scope(()):
        return asyncio.create_task(coroutine, **kwargs)


def _take(task):
    if task is None:
        return ()
    owners = _owners.pop(task, [])
    transferred = tuple(owners)
    owners.clear()
    for work in transferred:
        work.parts.discard(task)
    return transferred


def replace(coroutine, previous):
    owners = tuple(dict.fromkeys((*_current.get(), *_take(previous))))
    with scope(owners):
        task = spawn(coroutine)
    if previous is not None and not previous.done():
        previous.cancel()
    return task


@contextmanager
def adopt(previous):
    if previous is asyncio.current_task():
        yield
        return
    owners = tuple(dict.fromkeys((*_current.get(), *_take(previous))))
    part = object()
    for work in owners:
        work.add(part)
    success = False
    rejection = None
    try:
        with scope(owners):
            yield
        success = True
    except AdmissionRejected as error:
        rejection = error.code
        raise
    finally:
        for work in owners:
            work.finish(part, success=success, rejection=rejection)
