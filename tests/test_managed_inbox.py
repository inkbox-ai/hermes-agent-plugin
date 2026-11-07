import json
import os
import subprocess
import sys
from uuid import uuid4

import pytest

from inkbox_plugin.managed_inbox import InboxError, InboxFull, ManagedInbox


def test_import_does_not_require_posix_locking():
    code = '''import builtins
original = builtins.__import__
def portable_import(name, *args, **kwargs):
    if name == "fcntl":
        raise ModuleNotFoundError("POSIX locking unavailable")
    return original(name, *args, **kwargs)
builtins.__import__ = portable_import
import managed_inbox
'''
    subprocess.run([sys.executable, '-c', code], check=True)


def event(activation, number=1):
    event_id = 'evt_' + f'{number:064x}'
    return {'id': event_id, 'event_type': 'message.received', 'data': {'body': 'Hello'},
            'hosted_delivery': {'version': 1, 'activation_id': activation, 'event_id': event_id, 'starts_agent': True}}


def test_accept_survives_process_death_and_unstarted_work_can_run(tmp_path):
    path, activation = tmp_path / 'inbox' / 'events.sqlite3', str(uuid4())
    code = '''from pathlib import Path
import json,os,sys
from managed_inbox import ManagedInbox
inbox=ManagedInbox(Path(sys.argv[1]),sys.argv[2])
inbox.accept(json.loads(sys.argv[3]))
os._exit(0)
'''
    subprocess.run([sys.executable, '-c', code, str(path), activation, json.dumps(event(activation))], check=True)
    inbox = ManagedInbox(path, activation)
    try:
        assert inbox.summary() == {'pending': 1, 'active': 0, 'unknown': 0, 'review_required': 0}
        assert inbox.claim() == (event(activation)['id'], event(activation))
        assert inbox.claim() is None
        assert inbox.finish(event(activation)['id'], completed=True)
        receipt = inbox.accept(event(activation))
        assert receipt['state'] == 'completed' and inbox.claim() is None
    finally:
        inbox.close()


def test_started_work_is_quarantined_after_restart_and_not_replayed(tmp_path):
    activation, path = str(uuid4()), tmp_path / 'inbox' / 'events.sqlite3'
    inbox = ManagedInbox(path, activation)
    receipt = inbox.accept(event(activation))
    inbox.claim()
    inbox.close()
    inbox = ManagedInbox(path, activation)
    try:
        assert inbox.claim() is None
        assert inbox.accept(event(activation)) == receipt | {'state': 'unknown'}
        assert not inbox.finish(event(activation)['id'], completed=True)
        inbox.accept(event(activation, 2))
        assert inbox.claim()[0] == event(activation, 2)['id']
    finally:
        inbox.close()


def test_duplicate_identity_keeps_first_payload_despite_refreshed_projection(tmp_path):
    activation, path = str(uuid4()), tmp_path / 'inbox' / 'events.sqlite3'
    inbox = ManagedInbox(path, activation)
    try:
        original = inbox.accept(event(activation))
        assert inbox.accept(event(activation) | {'timestamp': 'changed'}) == original
        assert inbox.accept(event(activation) | {'data': {'body': 'updated', 'url': 'refreshed'}}) == original
        assert inbox.claim()[1] == event(activation)
        with pytest.raises(InboxError):
            inbox.accept(event(activation) | {'event_type': 'text.received'})
        with pytest.raises(InboxError):
            inbox.accept(event(str(uuid4())))
        if os.name != 'nt':
            assert os.stat(path).st_mode & 0o077 == 0
        with pytest.raises(BlockingIOError):
            ManagedInbox(path, activation)
    finally:
        inbox.close()


def test_full_inbox_refuses_without_evicting_pending_or_unknown(tmp_path, monkeypatch):
    import inkbox_plugin.managed_inbox as module
    activation = str(uuid4())
    monkeypatch.setattr(module, 'MAX_EVENTS', 1)
    inbox = ManagedInbox(tmp_path / 'inbox' / 'events.sqlite3', activation)
    try:
        first = inbox.accept(event(activation))
        with pytest.raises(InboxFull):
            inbox.accept(event(activation, 2))
        assert inbox.accept(event(activation)) == first
        assert inbox.summary()['pending'] == 1
    finally:
        inbox.close()


def test_old_completed_receipts_expire_without_dropping_unknown_or_pending(tmp_path):
    activation = str(uuid4())
    inbox = ManagedInbox(tmp_path / 'inbox' / 'events.sqlite3', activation)
    try:
        inbox.accept(event(activation))
        inbox.claim()
        inbox.finish(event(activation)['id'], completed=True)
        inbox.accept(event(activation, 2))
        inbox.claim()
        inbox.finish(event(activation, 2)['id'], completed=False)
        inbox.db.execute('UPDATE deliveries SET finished=0')
        inbox.accept(event(activation, 3))
        assert inbox.summary() == {'pending': 1, 'active': 0, 'unknown': 1, 'review_required': 0}
        assert inbox.db.execute('SELECT count(*) FROM deliveries').fetchone()[0] == 2
    finally:
        inbox.close()


def test_protocol_receipts_have_separate_admission_from_agent_turns(tmp_path):
    activation = str(uuid4())
    inbox = ManagedInbox(tmp_path / 'inbox' / 'events.sqlite3', activation)
    try:
        ordinary = event(activation)
        control = event(activation, 2)
        control['hosted_delivery']['starts_agent'] = False
        inbox.accept(ordinary)
        inbox.accept(control)
        assert inbox.claim(protocol_only=True)[0] == control['id']
        assert inbox.claim(protocol_only=True) is None
        assert inbox.claim()[0] == ordinary['id']
    finally:
        inbox.close()


def test_completed_receipts_do_not_consume_pending_capacity(tmp_path, monkeypatch):
    import importlib
    module = importlib.import_module(ManagedInbox.__module__)
    monkeypatch.setattr(module, 'MAX_EVENTS', 1)
    activation = str(uuid4())
    inbox = ManagedInbox(tmp_path / 'inbox' / 'events.sqlite3', activation)
    try:
        for number in range(1, 4):
            value = event(activation, number)
            inbox.accept(value)
            assert inbox.claim()[0] == value['id']
            inbox.finish(value['id'], completed=True)
        assert inbox.accept(event(activation))['state'] == 'completed'
        assert inbox.db.execute('SELECT count(*) FROM deliveries').fetchone()[0] == 3
    finally:
        inbox.close()


def test_conversation_admission_is_serial_but_other_contacts_continue(tmp_path):
    activation = str(uuid4())
    inbox = ManagedInbox(tmp_path / 'inbox' / 'events.sqlite3', activation)
    try:
        for number, contact in ((1, 'contact-a'), (2, 'contact-a'), (3, 'contact-b')):
            inbox.accept(event(activation, number) | {'companion': {'scope_id': contact, 'sequence': number}})
        assert inbox.claim()[0] == event(activation, 1)['id']
        assert inbox.claim()[0] == event(activation, 3)['id']
        assert inbox.claim() is None
        inbox.finish(event(activation, 1)['id'], completed=True)
        assert inbox.claim()[0] == event(activation, 2)['id']
    finally:
        inbox.close()


def test_quarantine_requires_a_cold_restart_and_never_replays(tmp_path, monkeypatch):
    import importlib
    module = importlib.import_module(ManagedInbox.__module__)
    monkeypatch.setattr(module, 'boot_id', lambda: 'boot-one')
    activation = str(uuid4())
    path = tmp_path / 'inbox' / 'events.sqlite3'
    inbox = ManagedInbox(path, activation)
    value = event(activation)
    inbox.accept(value)
    inbox.claim()
    inbox.finish(value['id'], completed=False)
    with pytest.raises(InboxError, match='Cold restart'):
        inbox.quarantine(value['id'])
    inbox.close()
    monkeypatch.setattr(module, 'boot_id', lambda: 'boot-two')
    inbox = ManagedInbox(path, activation)
    try:
        assert inbox.summary()['unknown'] == 0
        assert inbox.summary()['review_required'] == 1
        assert inbox.quarantine(value['id'])['quarantined']
        assert inbox.summary()['unknown'] == 0
        assert inbox.accept(value)['state'] == 'unknown'
        assert inbox.claim() is None
        assert inbox.quarantine(value['id'])['quarantined']
    finally:
        inbox.close()


def test_control_event_can_cancel_an_active_conversation(tmp_path):
    activation = str(uuid4())
    inbox = ManagedInbox(tmp_path / 'inbox' / 'events.sqlite3', activation)
    try:
        turn = event(activation) | {'companion': {'scope_id': 'contact-a', 'sequence': 1}}
        control = event(activation, 2) | {'companion': {'scope_id': 'contact-a', 'sequence': 2}}
        control['hosted_delivery']['starts_agent'] = False
        inbox.accept(turn)
        assert inbox.claim()[0] == turn['id']
        inbox.accept(control)
        assert inbox.claim(protocol_only=True)[0] == control['id']
    finally:
        inbox.close()


def test_previous_boot_unknown_blocks_only_its_scope(tmp_path, monkeypatch):
    import importlib
    module = importlib.import_module(ManagedInbox.__module__)
    monkeypatch.setattr(module, 'boot_id', lambda: 'old-boot')
    activation = str(uuid4())
    inbox = ManagedInbox(tmp_path / 'inbox.sqlite3', activation)
    first = event(activation) | {'companion': {'scope_id': 'scope-a'}}
    inbox.accept(first)
    inbox.claim()
    inbox.finish(first['id'], completed=False)
    inbox.accept(event(activation, 2) | {'companion': {'scope_id': 'scope-a'}})
    monkeypatch.setattr(module, 'boot_id', lambda: 'new-boot')
    assert inbox.summary() == {'pending': 0, 'active': 0, 'unknown': 0, 'review_required': 1}
    assert inbox.claim() is None
    other = event(activation, 3) | {'companion': {'scope_id': 'scope-b'}}
    inbox.accept(other)
    assert inbox.claim()[0] == other['id']
    assert inbox.accept(first)['state'] == 'unknown'
    inbox.close()


def test_fenced_owner_replacement_releases_pin_without_replay(tmp_path, monkeypatch):
    monkeypatch.setenv('INKBOX_MANAGED_OWNER_ID', 'old-owner')
    activation = str(uuid4())
    inbox = ManagedInbox(tmp_path / 'inbox.sqlite3', activation)
    first = event(activation)
    inbox.accept(first)
    inbox.claim()
    inbox.finish(first['id'], completed=False)
    assert inbox.summary()['unknown'] == 1
    monkeypatch.setenv('INKBOX_MANAGED_OWNER_ID', 'new-owner')
    assert inbox.summary()['unknown'] == 0
    assert inbox.summary()['review_required'] == 1
    assert inbox.claim() is None
    assert inbox.accept(first)['state'] == 'unknown'
    inbox.close()
