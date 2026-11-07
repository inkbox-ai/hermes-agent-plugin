"""Associate asynchronous turns with their durable delivery receipt."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from weakref import WeakKeyDictionary

_current = ContextVar('managed_deliveries', default=())
_owners = WeakKeyDictionary()


class DeliveryWork:
    def __init__(self):
        self.parts = set()
        self.failed = False
        self.sealed = False
        self.done = asyncio.get_running_loop().create_future()

    def add(self, part):
        if self.done.done():
            raise RuntimeError('Delivery work was already completed')
        self.parts.add(part)

    def finish(self, part, *, success):
        self.failed |= not success
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
        success = not done.cancelled() and done.exception() is None
        for work in owners:
            work.finish(done, success=success)
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
    try:
        with scope(owners):
            yield
        success = True
    finally:
        for work in owners:
            work.finish(part, success=success)
