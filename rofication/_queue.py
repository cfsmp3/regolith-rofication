import bisect
import json
import os
import re
import threading
import time
from subprocess import CalledProcessError
from typing import Any, Iterable, Iterator, List, MutableMapping, Mapping, Optional, Tuple
from warnings import warn

from ._notification import Notification, CloseReason, Urgency
from ._util import Event, Resource, write_file_atomically

ALLOWED_TO_EXPIRE = ('notify-send',)
# Default list of applications that keep at most one notification in the queue.
SINGLE_NOTIFICATION_APPS = ('VLC media player',)

single_notification_apps_resource = Resource(env_name='rofication_notify_single_notification_apps',
                                             xres_name='rofication.notify.single_notification_apps',
                                             default=', '.join(SINGLE_NOTIFICATION_APPS))


def parse_app_list(value: str) -> Tuple[str, ...]:
    """Split a comma-separated list of application names."""
    return tuple(app.strip() for app in value.split(',') if app.strip())


def fetch_single_notification_apps() -> Tuple[str, ...]:
    try:
        return parse_app_list(single_notification_apps_resource.fetch())
    except (OSError, CalledProcessError):
        # no resource getter available: keep the built-in default
        return SINGLE_NOTIFICATION_APPS


def next_id_filename(filename: str) -> str:
    """The file next to the queue file that keeps the next notification ID."""
    return os.path.splitext(filename)[0] + '.next-id'


def damaged_filename(filename: str) -> str:
    """The file next to the queue file that keeps a copy of a queue file that couldn't be read in full."""
    return filename + '.damaged'


def read_next_id(filename: str) -> Optional[int]:
    """The number kept in a next-number file, or None when the file gives no usable number: it is
    missing or can't be read, or it holds anything but a single whole number of at least 1."""
    try:
        with open(filename, 'rb') as f:
            text = f.read().strip()
    except OSError:
        return None
    # only ASCII digits: no sign, fraction, exponent, digit separators or digits of other scripts
    if not text.isdigit():
        return None
    try:
        next_id = int(text)
    except ValueError:
        # more digits than Python converts
        return None
    return next_id if next_id >= 1 else None


_JSON_WHITESPACE = ' \t\n\r'
# Bytes that aren't UTF-8 are decoded to lone surrogates ('surrogateescape'), which no valid text
# decodes to: they mark the places where the file isn't text.
_NOT_UTF8 = re.compile('[\udc80-\udcff]')
_OPENING_BRACKET = re.compile(r'[\[{]')
_STRING_OR_BRACKET = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"|[\[\]{}]')
# how the json module reports a value or a member name that isn't followed by what must follow it
_MISSING_DELIMITER = ("Expecting ',' delimiter", "Expecting ':' delimiter")


def _skip_whitespace(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in _JSON_WHITESPACE:
        pos += 1
    return pos


def _skip_whitespace_back(text: str, pos: int) -> int:
    """The position of the last character before pos that isn't whitespace, or -1."""
    pos -= 1
    while pos >= 0 and text[pos] in _JSON_WHITESPACE:
        pos -= 1
    return pos


def _closing_bracket_end(text: str, pos: int) -> int:
    """Where the array or object that begins at pos ends, counting brackets outside strings only,
    or the end of the text when it isn't closed."""
    depth = 0
    for match in _STRING_OR_BRACKET.finditer(text, pos):
        token = match.group()
        if token in ('[', '{'):
            depth += 1
        elif token in (']', '}'):
            depth -= 1
            if depth == 0:
                return match.end()
    return len(text)


class _Unreadable(Exception):
    """No complete JSON value begins at a position: msg says why, pos where reading failed."""

    def __init__(self, msg: str, pos: int) -> None:
        super().__init__(msg, pos)
        self.msg = msg
        self.pos = pos


class _ElementDecoder:
    """Decodes one JSON value at a time, telling whether it holds something JSON doesn't allow or
    that can't be restored: NaN or Infinity, or an integer with more digits than Python converts."""

    # A value is first read from a window of text after its start rather than from the whole
    # file, so that reporting a failure costs time in proportion to the window: the json module
    # works out the line of every error by scanning all text before it.
    WINDOW = 4096
    # A failure is only trusted when the window goes on this far after it: the json module never
    # looks further ahead than that before failing (an unterminated string is reported where it
    # starts, and so is never trusted before the end of the file).
    LOOKAHEAD = 16

    def __init__(self) -> None:
        self._decoder = json.JSONDecoder(parse_constant=self._constant, parse_int=self._integer)
        self._unusable = False

    def _constant(self, name: str) -> Any:
        self._unusable = True
        return None

    def _integer(self, digits: str) -> Any:
        try:
            return int(digits)
        except ValueError:
            self._unusable = True
            return None

    def decode(self, text: str, pos: int) -> Tuple[Any, int, bool]:
        """The value that begins at pos, where it ends, and whether it is unusable. Raises
        _Unreadable when no complete value begins at pos."""
        window = self.WINDOW
        while True:
            whole = pos + window >= len(text)
            doc = text[pos:] if whole else text[pos:pos + window]
            self._unusable = False
            try:
                value, end = self._decoder.raw_decode(doc)
            except json.JSONDecodeError as e:
                if whole or (e.pos + self.LOOKAHEAD < len(doc) and not e.msg.startswith('Unterminated string')):
                    raise _Unreadable(e.msg, pos + e.pos) from None
            except RecursionError:
                # nested too deeply to read: the whole value is taken as damaged, up to the bracket
                # that closes it
                raise _Unreadable('Nested too deeply', _closing_bracket_end(text, pos)) from None
            else:
                # a value that ends with the window, such as a number, may go on after it
                if whole or end < len(doc):
                    return value, pos + end, self._unusable
            window *= 4


def _break_point(text: str, start: int, error: _Unreadable) -> int:
    """Where the damage in the element at start begins, given where parsing it failed.

    Up to the error the element parsed as valid JSON, so nothing before it can be the start of
    another element, with one exception: a string that lost its closing quote runs on to the next
    quote, which is usually inside the element that follows. When the failure comes right after
    a string, the damage is taken to begin inside that string."""
    last = _skip_whitespace_back(text, error.pos)
    if error.msg not in _MISSING_DELIMITER or last <= start or text[last] != '"':
        return max(error.pos, start + 1)
    # find the quote that opened the string: inside a string every quote is escaped by an odd
    # number of backslashes, and the opening quote follows structure, never a backslash
    quote = last - 1
    while quote > start:
        if text[quote] == '"':
            backslashes = 0
            while quote - 1 - backslashes > start and text[quote - 1 - backslashes] == '\\':
                backslashes += 1
            if backslashes % 2 == 0:
                break
        quote -= 1
    return quote + 1


def _resynchronise(decoder: _ElementDecoder, text: str, start: int) -> Optional[int]:
    """The position of the next element after damage that begins at start: the first object that
    is complete on its own and stands where an element of the list can stand.

    An object is not taken when it is the value of a member (it follows a colon), the first item of
    a nested array or object (it follows an opening bracket), or part of a complete array or
    object that begins after the damage, such as the hints of a damaged entry: those are pieces
    of an entry, not entries."""
    covered = start  # everything before this is inside a complete value seen since the damage
    for match in _OPENING_BRACKET.finditer(text, start):
        pos = match.start()
        if pos < covered:
            continue
        try:
            _, end, _ = decoder.decode(text, pos)
        except _Unreadable as e:
            # up to where it fails, this is one value, as for an element of the list
            covered = _break_point(text, pos, e)
            continue
        if text[pos] == '{' and text[_skip_whitespace_back(text, pos)] not in ':[{':
            return pos
        covered = end
    return None


def read_queue_entries(raw: bytes) -> Tuple[List[Any], bool]:
    """The entries of a saved queue, in the order they were written, and whether anything had to be
    left unread.

    Each element of the list is read on its own. Damaged material, such as a broken entry, stray
    text or a cut-off tail, costs only itself: reading picks up again at the next complete entry
    after it. An element whose text isn't valid UTF-8 is left out. A file that holds something
    other than a list gives no entries."""
    text = raw.decode('utf-8', 'surrogateescape')
    not_utf8 = [match.start() for match in _NOT_UTF8.finditer(text)]
    damaged = bool(not_utf8)

    pos = _skip_whitespace(text, 0)
    if pos == len(text):
        # an empty file is an empty queue
        return [], damaged
    if text[pos] != '[':
        return [], True

    decoder = _ElementDecoder()
    entries = []
    pos = _skip_whitespace(text, pos + 1)
    if text.startswith(']', pos):
        return entries, damaged or _skip_whitespace(text, pos + 1) != len(text)
    while pos < len(text):
        # pos is where the next element should begin
        try:
            entry, end, unusable = decoder.decode(text, pos)
        except _Unreadable as e:
            damaged = True
            pos = _resynchronise(decoder, text, _break_point(text, pos, e))
            if pos is None:
                break
            continue
        if unusable or bisect.bisect_left(not_utf8, pos) != bisect.bisect_left(not_utf8, end):
            damaged = True
        else:
            entries.append(entry)
        pos = _skip_whitespace(text, end)
        if text.startswith(',', pos):
            pos = _skip_whitespace(text, pos + 1)
        elif text.startswith(']', pos):
            return entries, damaged or _skip_whitespace(text, pos + 1) != len(text)
        else:
            # cut off after the element, or stray text after it
            damaged = True
    return entries, True


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_saved_notification(entry: Any) -> bool:
    """Whether an entry of the saved queue describes a notification that can be restored."""
    if not isinstance(entry, dict):
        return False
    nid = entry.get('id')
    if not isinstance(nid, int) or isinstance(nid, bool) or nid < 1:
        return False
    for field in ('summary', 'body', 'application', 'icon'):
        value = entry.get(field)
        if value is not None and not isinstance(value, str):
            return False
    actions = entry.get('actions')
    if actions is not None and not (isinstance(actions, list) and all(isinstance(a, str) for a in actions)):
        return False
    hints = entry.get('hints')
    if hints is not None and not isinstance(hints, dict):
        return False
    for field in ('deadline', 'timestamp'):
        value = entry.get(field)
        if value is not None and value != '' and not _is_number(value):
            return False
    return True


def saved_urgency(value: Any) -> Urgency:
    """The urgency of a saved notification. As for the urgency hint of Notify(), a value that isn't a
    number, or lies outside the range the specification defines, is taken as normal; a fraction is
    cut off."""
    if _is_number(value) and Urgency.LOW <= value <= Urgency.CRITICAL:
        return Urgency(int(value))
    return Urgency.NORMAL


def _encode_saved_notification(notification: Notification) -> Optional[str]:
    """The JSON text of a notification in the saved queue, or None when it can't be written.

    Only the hints can hold values JSON has no way to write, such as NaN or bytes, because the
    D-Bus signature of Notify() fixes the type of every other field. A notification whose hints
    can't be written is saved without them rather than lost, and it never stops the rest of the
    queue from being saved."""
    fields = notification.asdict()
    try:
        return json.dumps(fields, allow_nan=False)
    except (TypeError, ValueError) as e:
        warn(f'Saving notification {notification.id} without its hints: {e}')
    try:
        return json.dumps(dict(fields, hints={}), allow_nan=False)
    except (TypeError, ValueError) as e:
        warn(f'Failed to save notification {notification.id}: {e}')
        return None


class NotificationQueue:
    def __init__(self, mapping: Mapping[int, Notification] = None,
                 single_notification_apps: Iterable[str] = None, next_id: int = 1) -> None:
        self._lock = threading.Lock()
        self._single_notification_apps: Tuple[str, ...] = fetch_single_notification_apps() \
            if single_notification_apps is None else tuple(single_notification_apps)
        # IDs are never handed out twice, also across restarts (the spec allows reuse only after
        # MAXINT notifications): continue after the highest ID kept in the queue or used before.
        self._last_id: int = max([next_id, *(nid + 1 for nid in mapping or {})])
        self._mapping: MutableMapping[int, Notification] = {} if mapping is None else dict(mapping)
        self.notification_seen = Event()
        self.notification_closed = Event()

    def __len__(self) -> int:
        return len(self._mapping)

    def __iter__(self) -> Iterator[Notification]:
        return iter(self._mapping.values())

    @property
    def lock(self) -> threading.Lock:
        return self._lock

    def save(self, filename: str) -> None:
        # Both files are replaced in one step, so a save that fails or is cut short leaves the
        # previous file in place instead of a half-written one.
        print('Saving notification queue to file')
        # transient notifications by-pass persistence: they don't come back after a restart
        entries = (_encode_saved_notification(ntf) for ntf in self._mapping.values() if not ntf.transient)
        data = '[' + ', '.join(entry for entry in entries if entry is not None) + ']'
        try:
            write_file_atomically(filename, data.encode('utf-8'))
        except OSError as e:
            warn(f'Failed to save notification queue: {e}')
        try:
            write_file_atomically(next_id_filename(filename), f'{self._last_id}\n'.encode('utf-8'))
        except OSError as e:
            warn(f'Failed to save the next notification ID: {e}')

    def see(self, nid: int) -> None:
        if nid in self._mapping:
            print(f'Seeing: {nid}')
            notification = self._mapping[nid]
            notification.urgency = Urgency.NORMAL
            self.notification_seen.notify(notification)
            return
        warn(f'Unable to find notification {nid}')

    def remove(self, nid: int, reason: CloseReason = CloseReason.DISMISSED) -> None:
        if nid in self._mapping:
            print(f'Removing: {nid}')
            notification = self._mapping.pop(nid)
            self.notification_closed.notify(notification, reason)
            return
        warn(f'Unable to find notification {nid}')

    def remove_all(self, nids: Iterable[int], reason: CloseReason = CloseReason.DISMISSED) -> None:
        for nid in nids:
            self.remove(nid, reason)

    def put(self, notification: Notification) -> None:
        to_replace: Optional[int]
        if notification.application in self._single_notification_apps:
            # replace notification for applications that only allow one
            to_replace = next((ntf.id for ntf in self._mapping.values()
                               if ntf.application == notification.application), None)
        else:
            # cannot have two notifications with the same ID
            to_replace = notification.id if notification.id in self._mapping else None

        if to_replace:
            notification.id = to_replace
            print(f'Replacing: {notification.id}')
            self._mapping[notification.id] = notification
            return

        notification.id = self._last_id
        self._last_id += 1
        print(f'Adding: {notification.id}')
        self._mapping[notification.id] = notification

    def cleanup(self) -> None:
        now = time.time()
        to_remove = [ntf.id for ntf in self._mapping.values()
                     if ntf.deadline and ntf.deadline < now
                     and (ntf.application in ALLOWED_TO_EXPIRE or ntf.transient)]
        if to_remove:
            print(f'Expired: {to_remove}')
            for nid in to_remove:
                self.notification_closed.notify(self._mapping[nid], CloseReason.EXPIRED)
                del self._mapping[nid]

    @classmethod
    def load(cls, filename: str) -> 'NotificationQueue':
        # without a usable number, numbering continues after the restored notifications, or at 1;
        # the next save writes the number in force, which puts a faulty file right
        next_id = read_next_id(next_id_filename(filename)) or 1

        try:
            with open(filename, 'rb') as f:
                print('Loading notification queue from file')
                raw = f.read()
        except FileNotFoundError:
            print('Creating empty notification queue')
            return cls({}, next_id=next_id)
        except OSError as e:
            warn(f'Failed to load notification queue: {e}')
            return cls({}, next_id=next_id)

        # Restore every notification that can still be read: a damaged entry or a damaged tail
        # costs only what is damaged, not the whole queue.
        entries, damaged = read_queue_entries(raw)
        notifications = []
        for entry in entries:
            if not _is_saved_notification(entry):
                damaged = True
                continue
            # decode only the top-level objects, so each notification keeps its hints as a dict
            fields = dict(entry, urgency=saved_urgency(entry.get('urgency')), actions=entry.get('actions') or ())
            notifications.append(Notification.make(fields))
        mapping = {n.id: n for n in notifications if not n.transient}

        if damaged:
            # keep what couldn't be read, before the queue file is written again
            warn(f'Notification queue file is damaged, restored {len(mapping)} notifications')
            try:
                write_file_atomically(damaged_filename(filename), raw)
            except OSError as e:
                warn(f'Failed to keep a copy of the damaged notification queue: {e}')

        return cls(mapping, next_id=next_id)
