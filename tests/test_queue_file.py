import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from rofication._notification import Notification, Urgency
from rofication._queue import NotificationQueue, read_queue_entries


def notification(application, summary='s', hints=None):
    ntf = Notification()
    ntf.id = 0
    ntf.application = application
    ntf.summary = summary
    ntf.body = ''
    ntf.hints = {} if hints is None else hints
    return ntf


def entry(nid, summary, **fields):
    return dict({'id': nid, 'application': 'app', 'summary': summary, 'body': '', 'hints': {}}, **fields)


class QueueFileTest(unittest.TestCase):
    """The saved queue survives failed and interrupted saves, and a damaged file gives back every
    notification that can still be read."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = os.path.join(self._dir.name, 'notifications.json')
        self.next_id_path = os.path.join(self._dir.name, 'notifications.next-id')
        self.damaged_path = self.path + '.damaged'

    def write(self, path, data):
        with open(path, 'wb') as fp:
            fp.write(data)

    def read(self, path):
        with open(path, 'rb') as fp:
            return fp.read()

    def load(self, raw):
        self.write(self.path, raw)
        return [(n.id, n.summary) for n in NotificationQueue.load(self.path)]

    def saved_queue(self):
        queue = NotificationQueue({}, single_notification_apps=())
        queue.put(notification('app', 'one'))
        queue.put(notification('app', 'two'))
        queue.save(self.path)
        return queue

    def test_failed_write_keeps_previous_files(self):
        queue = self.saved_queue()
        before = {name: self.read(os.path.join(self._dir.name, name)) for name in os.listdir(self._dir.name)}
        queue.put(notification('app', 'three'))
        with mock.patch('os.fsync', side_effect=OSError(28, 'No space left on device')):
            queue.save(self.path)
        after = {name: self.read(os.path.join(self._dir.name, name)) for name in os.listdir(self._dir.name)}
        self.assertEqual(after, before)

    def test_unsaveable_hints_are_left_out(self):
        for value in ({1, 2}, float('nan'), float('inf'), b'raw'):
            with self.subTest(value=value):
                queue = self.saved_queue()
                queue.put(notification('app', 'bad hints', hints={'value': value, 'category': 'x'}))
                queue.put(notification('app', 'four'))
                with self.assertWarns(UserWarning):
                    queue.save(self.path)
                saved = json.loads(self.read(self.path))
                self.assertEqual([(item['id'], item['summary'], item['hints']) for item in saved],
                                 [(1, 'one', {}), (2, 'two', {}), (3, 'bad hints', {}), (4, 'four', {})])
                self.assertEqual(self.read(self.next_id_path), b'5\n')
                # no temporary file left behind
                self.assertEqual([name for name in os.listdir(self._dir.name) if name.startswith('.')], [])
                self.assertEqual([n.summary for n in NotificationQueue.load(self.path)],
                                 ['one', 'two', 'bad hints', 'four'])
                self.assertFalse(os.path.exists(self.damaged_path))

    def test_unsaveable_notification_does_not_stop_the_save(self):
        queue = self.saved_queue()
        bad = notification('app', 'bad deadline')
        queue.put(bad)
        bad.deadline = float('nan')
        queue.put(notification('app', 'four'))
        with self.assertWarns(UserWarning):
            queue.save(self.path)
        self.assertEqual([item['summary'] for item in json.loads(self.read(self.path))], ['one', 'two', 'four'])
        self.assertEqual(self.read(self.next_id_path), b'5\n')

    def test_save_through_symbolic_link_keeps_link_and_mode(self):
        target = os.path.join(self._dir.name, 'target.json')
        self.write(target, b'[]')
        os.chmod(target, 0o640)
        os.symlink('target.json', self.path)
        self.saved_queue()
        self.assertTrue(os.path.islink(self.path))
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o640)
        self.assertEqual([item['summary'] for item in json.loads(self.read(target))], ['one', 'two'])

    def test_new_files_get_the_usual_mode(self):
        umask = os.umask(0o027)
        try:
            self.saved_queue()
        finally:
            os.umask(umask)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(os.stat(self.next_id_path).st_mode), 0o640)

    def test_cut_file_restores_complete_entries(self):
        full = json.dumps([entry(1, 'one'), entry(2, 'café')], ensure_ascii=False).encode('utf-8')
        cuts = {full[:-1]: [(1, 'one'), (2, 'café')],
                full[:full.index(b'\xc3') + 1]: [(1, 'one')],
                full[:full.index(b'{"id": 2') + 3]: [(1, 'one')],
                full[:full.index(b'{"id": 2')]: [(1, 'one')]}
        for raw, restored in cuts.items():
            self.assertEqual(self.load(raw), restored)
            self.assertEqual(self.read(self.damaged_path), raw)

    def test_reading_resumes_after_damage(self):
        one, two, three = (json.dumps(entry(n, s)).encode() for n, s in ((1, 'one'), (2, 'two'), (3, 'three')))
        for damage in (b', {"id": 9 "summary": "broken"}, ', b' ', b', @@@, ', b',, ',
                       b', {"id": 9, "summary": "cut\n, ', b', {"id": 9, "summary": "cut, ',
                       b', {"id": 9, "hints": {"a": [1,\n, ', b', \xff, '):
            raw = b'[' + one + damage + two + b', ' + three + b']'
            self.assertEqual(read_queue_entries(raw), ([entry(1, 'one'), entry(2, 'two'), entry(3, 'three')], True))
        self.assertEqual(read_queue_entries(b'[NaN]'), ([], True))
        self.assertEqual(read_queue_entries(b' [ ] '), ([], False))
        self.assertEqual(read_queue_entries(b' \n'), ([], False))
        self.assertEqual(read_queue_entries(b'{"id": 1}'), ([], True))

    def test_pieces_of_a_damaged_entry_are_not_entries(self):
        one, two = (json.dumps(entry(n, s)).encode() for n, s in ((1, 'one'), (2, 'two')))
        piece = json.dumps(entry(5, 'piece')).encode()
        for damaged in (b'{"id": 9 "hints": ' + piece + b'}',
                        b'{"id": 9 "hints": {"list": [1, ' + piece + b', 2]}}',
                        b'{"id": 9 "hints": {"list": [' + piece + b' 2]}}',
                        b'{"id": 9, "hints": {"list": [NaN, ' + piece + b']}}'):
            raw = b'[' + one + b', ' + damaged + b', ' + two + b']'
            self.assertEqual(read_queue_entries(raw), ([entry(1, 'one'), entry(2, 'two')], True))

    def test_bad_entries_are_put_aside_one_by_one(self):
        bad = [42, None, entry(True, 'bool id'), entry(0, 'zero id'), entry(3, 5),
               entry(4, 'actions', actions=['ok', 2]), entry(5, 'hints', hints=[]),
               entry(6, 'deadline', deadline='soon'), {'summary': 'no id'}]
        raw = json.dumps([entry(1, 'one')] + bad + [entry(2, 'two')]).encode()
        self.assertEqual(self.load(raw), [(1, 'one'), (2, 'two')])
        self.assertEqual(self.read(self.damaged_path), raw)

    def test_clean_load_leaves_damaged_copy_alone(self):
        self.write(self.damaged_path, b'earlier')
        raw = json.dumps([entry(1, 'one', hints={'transient': True}), entry(2, 'two'),
                          entry(2, 'replacement', urgency=7)]).encode()
        self.assertEqual(self.load(raw), [(2, 'replacement')])
        self.assertEqual(self.read(self.damaged_path), b'earlier')

    def test_saved_urgency(self):
        values = [0, 1, 2, 0.5, 1.5, 1.999, 2.5, -0.1, '2', None, True]
        raw = json.dumps([entry(i + 1, str(value), urgency=value) for i, value in enumerate(values)]).encode()
        self.write(self.path, raw)
        self.assertEqual([n.urgency for n in NotificationQueue.load(self.path)],
                         [Urgency.LOW, Urgency.NORMAL, Urgency.CRITICAL, Urgency.LOW, Urgency.NORMAL,
                          Urgency.NORMAL, Urgency.NORMAL, Urgency.NORMAL, Urgency.NORMAL, Urgency.NORMAL,
                          Urgency.NORMAL])
        self.assertFalse(os.path.exists(self.damaged_path))

    def test_ids_continue_after_salvaged_entries(self):
        self.write(self.next_id_path, b'4\n')
        self.write(self.path, b'[' + json.dumps(entry(9, 'kept')).encode() + b', {bad}, '
                   + json.dumps(entry(12, 'after the damage')).encode() + b',')
        queue = NotificationQueue.load(self.path)
        ntf = notification('app', 'new')
        queue.put(ntf)
        self.assertEqual(ntf.id, 13)

    def test_faulty_next_id_file_is_put_right(self):
        for stored in (b'', b' \n', b'garbage', b'4.5', b'+50', b'1 2', b'0\n', b'-7\n', b'\xff', b'3\n'):
            for ids, expected in (((7, 2), 8), ((), 3 if stored == b'3\n' else 1)):
                with self.subTest(stored=stored, ids=ids):
                    self.write(self.next_id_path, stored)
                    self.write(self.path, json.dumps([entry(nid, str(nid)) for nid in ids]).encode())
                    NotificationQueue.load(self.path).save(self.path)
                    self.assertEqual(self.read(self.next_id_path), f'{expected}\n'.encode())
                    ntf = notification('app', 'new')
                    NotificationQueue.load(self.path).put(ntf)
                    self.assertEqual(ntf.id, expected)
                    self.assertFalse(os.path.exists(self.damaged_path))

    def test_usable_next_id_file_is_kept(self):
        self.write(self.next_id_path, b' 0042\n')
        self.write(self.path, json.dumps([entry(7, 'seven')]).encode())
        ntf = notification('app', 'new')
        NotificationQueue.load(self.path).put(ntf)
        self.assertEqual(ntf.id, 42)


if __name__ == '__main__':
    unittest.main()
