r"""Minimal TELNET client, vendored from CPython's ``Lib/telnetlib.py``.

``telnetlib`` was deprecated in Python 3.11 and removed in 3.13.  pairshell
needs Telnet on 3.11 through 3.13+, so this is a trimmed copy of the stdlib
module (CPython 3.12) with the interactive helpers (``interact``,
``mt_interact``, ``listener``, ``test``) removed and a small write lock added
so option-negotiation replies never interleave with user data.

The original module is distributed under the Python Software Foundation
License, reproduced below.  pairshell's own code is MIT-licensed; this file
keeps the PSF notice.

PSF LICENSE AGREEMENT FOR PYTHON 3.12
-------------------------------------

Copyright (c) 2001-2023 Python Software Foundation; All Rights Reserved

1. This LICENSE AGREEMENT is between the Python Software Foundation ("PSF"),
   and the Individual or Organization ("Licensee") accessing and otherwise
   using Python 3.12 software in source or binary form and its associated
   documentation.

2. Subject to the terms and conditions of this License Agreement, PSF hereby
   grants Licensee a nonexclusive, royalty-free, world-wide license to
   reproduce, analyze, test, perform and/or display publicly, prepare
   derivative works, distribute, and otherwise use Python 3.12 alone or in any
   derivative version, provided, however, that PSF's License Agreement and
   PSF's notice of copyright, i.e., "Copyright (c) 2001-2023 Python Software
   Foundation; All Rights Reserved" are retained in Python 3.12 alone or in
   any derivative version prepared by Licensee.

3. In the event Licensee prepares a derivative work that is based on or
   incorporates Python 3.12 or any part thereof, and wants to make the
   derivative work available to others as provided herein, then Licensee
   hereby agrees to include in any such work a brief summary of the changes
   made to Python 3.12.

4. PSF is making Python 3.12 available to Licensee on an "AS IS" basis.
   PSF MAKES NO REPRESENTATIONS OR WARRANTIES, EXPRESS OR IMPLIED.  BY WAY OF
   EXAMPLE, BUT NOT LIMITATION, PSF MAKES NO AND DISCLAIMS ANY REPRESENTATION
   OR WARRANTY OF MERCHANTABILITY OR FITNESS FOR ANY PARTICULAR PURPOSE OR
   THAT THE USE OF PYTHON 3.12 WILL NOT INFRINGE ANY THIRD PARTY RIGHTS.

5. PSF SHALL NOT BE LIABLE TO LICENSEE OR ANY OTHER USERS OF PYTHON 3.12 FOR
   ANY INCIDENTAL, SPECIAL, OR CONSEQUENTIAL DAMAGES OR LOSS AS A RESULT OF
   MODIFYING, DISTRIBUTING, OR OTHERWISE USING PYTHON 3.12, OR ANY DERIVATIVE
   THEREOF, EVEN IF ADVISED OF THE POSSIBILITY THEREOF.

6. This License Agreement will automatically terminate upon a material breach
   of its terms and conditions.

7. Nothing in this License Agreement shall be deemed to create any
   relationship of agency, partnership, or joint venture between PSF and
   Licensee.  This License Agreement does not grant permission to use PSF
   trademarks or trade name in a trademark sense to endorse or promote
   products or services of Licensee, or any third party.

8. By copying, installing or otherwise using Python 3.12, Licensee agrees to
   be bound by the terms and conditions of this License Agreement.

Summary of changes relative to CPython 3.12 ``Lib/telnetlib.py``:

* removed ``interact``, ``mt_interact``, ``listener`` and ``test``;
* removed the ``DeprecationWarning`` emitted on import;
* added ``Telnet.send_raw`` and a lock so negotiation replies and user data
  cannot interleave when read and write happen on different threads;
* ``sys.audit`` hooks dropped.
"""

import selectors
import socket
import sys
import threading
from time import monotonic as _time

__all__ = ["Telnet"]

# Tunable parameters
DEBUGLEVEL = 0

# Telnet protocol defaults
TELNET_PORT = 23

# Telnet protocol characters (don't change)
IAC = bytes([255])  # "Interpret As Command"
DONT = bytes([254])
DO = bytes([253])
WONT = bytes([252])
WILL = bytes([251])
theNULL = bytes([0])

SE = bytes([240])  # Subnegotiation End
NOP = bytes([241])  # No Operation
DM = bytes([242])  # Data Mark
BRK = bytes([243])  # Break
IP = bytes([244])  # Interrupt process
AO = bytes([245])  # Abort output
AYT = bytes([246])  # Are You There
EC = bytes([247])  # Erase Character
EL = bytes([248])  # Erase Line
GA = bytes([249])  # Go Ahead
SB = bytes([250])  # Subnegotiation Begin

# Telnet protocol options code (don't change)
# These ones all come from arpa/telnet.h
BINARY = bytes([0])  # 8-bit data path
ECHO = bytes([1])  # echo
RCP = bytes([2])  # prepare to reconnect
SGA = bytes([3])  # suppress go ahead
NAMS = bytes([4])  # approximate message size
STATUS = bytes([5])  # give status
TM = bytes([6])  # timing mark
RCTE = bytes([7])  # remote controlled transmission and echo
NAOL = bytes([8])  # negotiate about output line width
NAOP = bytes([9])  # negotiate about output page size
NAOCRD = bytes([10])  # negotiate about CR disposition
NAOHTS = bytes([11])  # negotiate about horizontal tabstops
NAOHTD = bytes([12])  # negotiate about horizontal tab disposition
NAOFFD = bytes([13])  # negotiate about formfeed disposition
NAOVTS = bytes([14])  # negotiate about vertical tab stops
NAOVTD = bytes([15])  # negotiate about vertical tab disposition
NAOLFD = bytes([16])  # negotiate about output LF disposition
XASCII = bytes([17])  # extended ascii character set
LOGOUT = bytes([18])  # force logout
BM = bytes([19])  # byte macro
DET = bytes([20])  # data entry terminal
SUPDUP = bytes([21])  # supdup protocol
SUPDUPOUTPUT = bytes([22])  # supdup output
SNDLOC = bytes([23])  # send location
TTYPE = bytes([24])  # terminal type
EOR = bytes([25])  # end or record
TUID = bytes([26])  # TACACS user identification
OUTMRK = bytes([27])  # output marking
TTYLOC = bytes([28])  # terminal location number
VT3270REGIME = bytes([29])  # 3270 regime
X3PAD = bytes([30])  # X.3 PAD
NAWS = bytes([31])  # window size
TSPEED = bytes([32])  # terminal speed
LFLOW = bytes([33])  # remote flow control
LINEMODE = bytes([34])  # Linemode option
XDISPLOC = bytes([35])  # X Display Location
OLD_ENVIRON = bytes([36])  # Old - Environment variables
AUTHENTICATION = bytes([37])  # Authenticate
ENCRYPT = bytes([38])  # Encryption option
NEW_ENVIRON = bytes([39])  # New - Environment variables
# the following ones come from
# http://www.iana.org/assignments/telnet-options
# Unfortunately, that document does not assign identifiers
# to all of them, so we are making them up
TN3270E = bytes([40])  # TN3270E
XAUTH = bytes([41])  # XAUTH
CHARSET = bytes([42])  # CHARSET
RSP = bytes([43])  # Telnet Remote Serial Port
COM_PORT_OPTION = bytes([44])  # Com Port Control Option
SUPPRESS_LOCAL_ECHO = bytes([45])  # Telnet Suppress Local Echo
TLS = bytes([46])  # Telnet Start TLS
KERMIT = bytes([47])  # KERMIT
SEND_URL = bytes([48])  # SEND-URL
FORWARD_X = bytes([49])  # FORWARD_X
PRAGMA_LOGON = bytes([138])  # TELOPT PRAGMA LOGON
SSPI_LOGON = bytes([139])  # TELOPT SSPI LOGON
PRAGMA_HEARTBEAT = bytes([140])  # TELOPT PRAGMA HEARTBEAT
EXOPL = bytes([255])  # Extended-Options-List
NOOPT = bytes([0])


# poll/select have the advantage of not requiring any extra file descriptor,
# contrarily to epoll/kqueue (also, they require a single syscall).
if hasattr(selectors, "PollSelector"):
    _TelnetSelector = selectors.PollSelector
else:  # pragma: no cover - platform dependent
    _TelnetSelector = selectors.SelectSelector


class Telnet:
    """Telnet interface class.

    An instance of this class represents a connection to a telnet
    server.  The instance is initially not connected; the open()
    method must be used to establish a connection.  Alternatively, the
    host name and optional port number can be passed to the
    constructor, too.

    Don't try to reopen an already connected instance.

    This class has many read_*() methods.  Note that some of them
    raise EOFError when the end of the connection is read, because
    they can return an empty string for other reasons.  See the
    individual doc strings.

    read_until(expected, [timeout])
        Read until the expected string has been seen, or a timeout is
        hit (default is no timeout); may block.

    read_all()
        Read all data until EOF; may block.

    read_some()
        Read at least one byte or EOF; may block.

    read_very_eager()
        Read all data available already queued or on the socket,
        without blocking.

    read_eager()
        Read either data already queued or some data available on the
        socket, without blocking.

    read_lazy()
        Read all data in the raw queue (processing it first), without
        doing any socket I/O.

    read_very_lazy()
        Reads all data in the cooked queue, without doing any socket
        I/O.

    read_sb_data()
        Reads available data between SB ... SE sequence. Don't block.

    set_option_negotiation_callback(callback)
        Each time a telnet option is read on the input flow, this callback
        (if set) is called with the following parameters :
        callback(telnet socket, command, option)
            option will be chr(0) when there is no option.
        No other action is done afterwards by telnetlib.

    """

    def __init__(self, host=None, port=0, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
        """Constructor.

        When called without arguments, create an unconnected instance.
        With a hostname argument, it connects the instance; port number
        and timeout are optional.
        """
        self.debuglevel = DEBUGLEVEL
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = None
        self.rawq = b""
        self.irawq = 0
        self.cookedq = b""
        self.eof = 0
        self.iacseq = b""  # Buffer for IAC sequence.
        self.sb = 0  # flag for SB and SE sequence.
        self.sbdataq = b""
        self.option_callback = None
        self._send_lock = threading.Lock()
        if host is not None:
            self.open(host, port, timeout)

    def open(self, host, port=0, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
        """Connect to a host.

        The optional second argument is the port number, which
        defaults to the standard telnet port (23).

        Don't try to reopen an already connected instance.
        """
        self.eof = 0
        if not port:
            port = TELNET_PORT
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = socket.create_connection((host, port), timeout)

    def __del__(self):
        """Destructor -- close the connection."""
        self.close()

    def msg(self, msg, *args):
        """Print a debug message, when the debug level is > 0.

        If extra arguments are present, they are substituted in the
        message using the standard string formatting operator.

        """
        if self.debuglevel > 0:
            print("Telnet(%s,%s):" % (self.host, self.port), end=" ")
            if args:
                print(msg % args)
            else:
                print(msg)

    def set_debuglevel(self, debuglevel):
        """Set the debug level.

        The higher it is, the more debug output you get (on sys.stdout).

        """
        self.debuglevel = debuglevel

    def close(self):
        """Close the connection."""
        sock = self.sock
        self.sock = None
        self.eof = True
        self.iacseq = b""
        self.sb = 0
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def get_socket(self):
        """Return the socket object used internally."""
        return self.sock

    def fileno(self):
        """Return the fileno() of the socket object used internally."""
        return self.sock.fileno()

    def send_raw(self, buffer):
        """Send bytes as-is (no IAC doubling), serialised with write()."""
        sock = self.sock
        if sock is None:
            raise OSError("telnet connection closed")
        with self._send_lock:
            sock.sendall(buffer)

    def write(self, buffer):
        """Write a string to the socket, doubling any IAC characters.

        Can block if the connection is blocked.  May raise
        OSError if the connection is closed.

        """
        if IAC in buffer:
            buffer = buffer.replace(IAC, IAC + IAC)
        self.msg("send %r", buffer)
        self.send_raw(buffer)

    def read_until(self, match, timeout=None):
        """Read until a given string is encountered or until timeout.

        When no match is found, return whatever is available instead,
        possibly the empty string.  Raise EOFError if the connection
        is closed and no cooked data is available.

        """
        n = len(match)
        self.process_rawq()
        i = self.cookedq.find(match)
        if i >= 0:
            i = i + n
            buf = self.cookedq[:i]
            self.cookedq = self.cookedq[i:]
            return buf
        if timeout is not None:
            deadline = _time() + timeout
        with _TelnetSelector() as selector:
            selector.register(self, selectors.EVENT_READ)
            while not self.eof:
                if selector.select(timeout):
                    i = max(0, len(self.cookedq) - n)
                    self.fill_rawq()
                    self.process_rawq()
                    i = self.cookedq.find(match, i)
                    if i >= 0:
                        i = i + n
                        buf = self.cookedq[:i]
                        self.cookedq = self.cookedq[i:]
                        return buf
                if timeout is not None:
                    timeout = deadline - _time()
                    if timeout < 0:
                        break
        return self.read_very_lazy()

    def read_all(self):
        """Read all data until EOF; block until connection closed."""
        self.process_rawq()
        while not self.eof:
            self.fill_rawq()
            self.process_rawq()
        buf = self.cookedq
        self.cookedq = b""
        return buf

    def read_some(self):
        """Read at least one byte of cooked data unless EOF is hit.

        Return b'' if EOF is hit.  Block if no data is immediately
        available.

        """
        self.process_rawq()
        while not self.cookedq and not self.eof:
            self.fill_rawq()
            self.process_rawq()
        buf = self.cookedq
        self.cookedq = b""
        return buf

    def read_very_eager(self):
        """Read everything that's possible without blocking in I/O (eager).

        Raise EOFError if connection closed and no cooked data
        available.  Return b'' if no cooked data available otherwise.
        Don't block unless in the midst of an IAC sequence.

        """
        self.process_rawq()
        while not self.eof and self.sock_avail():
            self.fill_rawq()
            self.process_rawq()
        return self.read_very_lazy()

    def read_eager(self):
        """Read readily available data.

        Raise EOFError if connection closed and no cooked data
        available.  Return b'' if no cooked data available otherwise.
        Don't block unless in the midst of an IAC sequence.

        """
        self.process_rawq()
        while not self.cookedq and not self.eof and self.sock_avail():
            self.fill_rawq()
            self.process_rawq()
        return self.read_very_lazy()

    def read_lazy(self):
        """Process and return data that's already in the queues (lazy).

        Raise EOFError if connection closed and no data available.
        Return b'' if no cooked data available otherwise.  Don't block
        unless in the midst of an IAC sequence.

        """
        self.process_rawq()
        return self.read_very_lazy()

    def read_very_lazy(self):
        """Return any data available in the cooked queue (very lazy).

        Raise EOFError if connection closed and no data available.
        Return b'' if no cooked data available otherwise.  Don't block.

        """
        buf = self.cookedq
        self.cookedq = b""
        if not buf and self.eof and not self.rawq:
            raise EOFError("telnet connection closed")
        return buf

    def read_sb_data(self):
        """Return any data available in the SB ... SE queue.

        Return b'' if no SB ... SE available. Should only be called
        after seeing a SB or SE command. When a new SB command is
        found, old unread SB data will be discarded. Don't block.

        """
        buf = self.sbdataq
        self.sbdataq = b""
        return buf

    def set_option_negotiation_callback(self, callback):
        """Provide a callback function called after each receipt of a telnet option."""
        self.option_callback = callback

    def process_rawq(self):
        """Transfer from raw queue to cooked queue.

        Set self.eof when connection is closed.  Don't block unless in
        the midst of an IAC sequence.

        """
        buf = [b"", b""]
        try:
            while self.rawq:
                c = self.rawq_getchar()
                if not self.iacseq:
                    if c == theNULL:
                        continue
                    if c == b"\021":
                        continue
                    if c != IAC:
                        buf[self.sb] = buf[self.sb] + c
                        continue
                    else:
                        self.iacseq += c
                elif len(self.iacseq) == 1:
                    # 'IAC: IAC CMD [OPTION only for WILL/WONT/DO/DONT]'
                    if c in (DO, DONT, WILL, WONT):
                        self.iacseq += c
                        continue

                    self.iacseq = b""
                    if c == IAC:
                        buf[self.sb] = buf[self.sb] + c
                    else:
                        if c == SB:  # SB ... SE start.
                            self.sb = 1
                            self.sbdataq = b""
                        elif c == SE:
                            self.sb = 0
                            self.sbdataq = self.sbdataq + buf[1]
                            buf[1] = b""
                        if self.option_callback:
                            # Callback is supposed to look into
                            # the sbdataq
                            self.option_callback(self.sock, c, NOOPT)
                        else:
                            # We can't offer automatic processing of
                            # suboptions. Alas, we should not get any
                            # unless we did a WILL/DO before.
                            self.msg("IAC %d not recognized" % ord(c))
                elif len(self.iacseq) == 2:
                    cmd = self.iacseq[1:2]
                    self.iacseq = b""
                    opt = c
                    if cmd in (DO, DONT):
                        self.msg("IAC %s %d", cmd == DO and "DO" or "DONT", ord(opt))
                        if self.option_callback:
                            self.option_callback(self.sock, cmd, opt)
                        else:
                            self.send_raw(IAC + WONT + opt)
                    elif cmd in (WILL, WONT):
                        self.msg("IAC %s %d", cmd == WILL and "WILL" or "WONT", ord(opt))
                        if self.option_callback:
                            self.option_callback(self.sock, cmd, opt)
                        else:
                            self.send_raw(IAC + DONT + opt)
        except EOFError:  # raised by self.rawq_getchar()
            self.iacseq = b""  # Reset on EOF
            self.sb = 0
        self.cookedq = self.cookedq + buf[0]
        self.sbdataq = self.sbdataq + buf[1]

    def rawq_getchar(self):
        """Get next char from raw queue.

        Block if no data is immediately available.  Raise EOFError
        when connection is closed.

        """
        if not self.rawq:
            self.fill_rawq()
            if self.eof:
                raise EOFError
        c = self.rawq[self.irawq : self.irawq + 1]
        self.irawq = self.irawq + 1
        if self.irawq >= len(self.rawq):
            self.rawq = b""
            self.irawq = 0
        return c

    def fill_rawq(self):
        """Fill raw queue from exactly one recv() system call.

        Block if no data is immediately available.  Set self.eof when
        connection is closed.

        """
        if self.irawq >= len(self.rawq):
            self.rawq = b""
            self.irawq = 0
        # The buffer size should be fairly small so as to avoid quadratic
        # behavior in process_rawq() above
        sock = self.sock
        if sock is None:
            self.eof = 1
            return
        try:
            buf = sock.recv(4096)
        except OSError:
            if self.sock is None:  # closed from another thread
                self.eof = 1
                return
            raise
        self.msg("recv %r", buf)
        self.eof = not buf
        self.rawq = self.rawq + buf

    def sock_avail(self):
        """Test whether data is available on the socket."""
        with _TelnetSelector() as selector:
            selector.register(self, selectors.EVENT_READ)
            return bool(selector.select(0))

    def expect(self, list, timeout=None):
        """Read until one from a list of a regular expressions matches.

        The first argument is a list of regular expressions, either
        compiled (re.Pattern instances) or uncompiled (strings).
        The optional second argument is a timeout, in seconds; default
        is no timeout.

        Return a tuple of three items: the index in the list of the
        first regular expression that matches; the re.Match object
        returned; and the text read up till and including the match.

        If EOF is read and no text was read, raise EOFError.
        Otherwise, when nothing matches, return (-1, None, text) where
        text is the text received so far (may be the empty string if a
        timeout happened).

        If a regular expression ends with a greedy match (e.g. '.*')
        or if more than one expression can match the same input, the
        results are undeterministic, and may depend on the I/O timing.

        """
        re = None
        list = list[:]
        indices = range(len(list))
        for i in indices:
            if not hasattr(list[i], "search"):
                if not re:
                    import re
                list[i] = re.compile(list[i])
        if timeout is not None:
            deadline = _time() + timeout
        with _TelnetSelector() as selector:
            selector.register(self, selectors.EVENT_READ)
            while not self.eof:
                self.process_rawq()
                for i in indices:
                    m = list[i].search(self.cookedq)
                    if m:
                        e = m.end()
                        text = self.cookedq[:e]
                        self.cookedq = self.cookedq[e:]
                        return (i, m, text)
                if timeout is not None:
                    ready = selector.select(timeout)
                    timeout = deadline - _time()
                    if not ready:
                        if timeout < 0:
                            break
                        else:
                            continue
                self.fill_rawq()
        text = self.read_very_lazy()
        if not text and self.eof:
            raise EOFError
        return (-1, None, text)

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.close()
