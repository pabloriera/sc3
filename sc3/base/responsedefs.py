"""ResponseDefs.sc"""

from abc import ABC, abstractmethod
import logging

from ..synth import server as srv
from . import functions as fn
from . import systemactions as sac
from . import model as mdl
from . import main as _libsc3
from . import utils as utl
from ._oscmatch import osc_rematch_pattern as _match_osc_address_pattern


__all__ = ['OscFunc']


_logger = logging.getLogger(__name__)


# NOTE: There are tree types objects in this sublibrary: matchers,
# dispatchers and responders (e.g. OscFunc). Matchers filter incoming data
# for specific cases and sets of parameters. Dispatchers dispatch the
# filtered data to the corresponding responder's instances depending on the
# message type. Responders are the actual public interface (e.g. OscFunc).
# Documentation for matchers and dispatchers are in the original library
# written for sclang, here I only transcribed the functionality of the
# public interface by now.


class AbstractMessageMatcher(ABC):
    @abstractmethod
    def __call__(self):
        pass


class AbstractDispatcher(ABC):
    all = set()  # TODO: Not really used, needs a metaclass.

    def __init__(self):
        self.registered = False

    @abstractmethod
    def add(self, func_proxy):
        '''
        Proxies call this to add themselves to this dispatcher.
        Should register this if needed.
        '''
        pass

    @abstractmethod
    def remove(self, func_proxy):
        '''
        Proxies call this to remove themselves from this dispatcher.
        Should unregister if needed.
        '''
        pass

    @abstractmethod
    def __call__(self):
        pass

    @abstractmethod
    def register(self):
        '''Register this dispatcher to listen for its message type.'''
        pass

    @abstractmethod
    def unregister(self):
        '''Unregister this dispatcher so it no longer listens.'''
        pass

    def free(self):
        self.unregister()
        type(self).all.remove(self)

    @abstractmethod
    def type_key(self):
        '''Name of the dispatcher as str.'''
        pass


class AbstractWrappingDispatcher(AbstractDispatcher):
    # // basis for the default dispatchers
    # // uses function wrappers for matching

    def __init__(self):
        super().__init__()
        self.active = dict()
        self.wrapped_funcs = dict()

    def add(self, func_proxy):
        mdl.NotificationCenter.register(
            func_proxy, 'function', self, self.update_func_for_func_proxy)
        func = self.wrap_func(func_proxy)
        self.wrapped_funcs[func_proxy] = func
        keys = self.get_keys_for_func_proxy(func_proxy)
        for key in keys:
            try:
                self.active[key].append(func)
            except KeyError:
                self.active[key] = [func]
        if not self.registered:
            self.register()

    def remove(self, func_proxy):
        mdl.NotificationCenter.unregister(func_proxy, 'function', self)
        keys = self.get_keys_for_func_proxy(func_proxy)
        func = self.wrapped_funcs[func_proxy]
        for key in keys:
            self.active[key].remove(func)
        del self.wrapped_funcs[func_proxy]
        if len(self.active) == 0:
            self.unregister()

    def update_func_for_func_proxy(self, func_proxy):
        func = self.wrap_func(func_proxy)
        old_func = self.wrapped_funcs[func_proxy]
        self.wrapped_funcs[func_proxy] = func
        keys = self.get_keys_for_func_proxy(func_proxy)
        for key in keys:
            i = self.active[key].index(old_func)
            self.active[key][i] = func

    @abstractmethod
    def wrap_func(self, func_proxy):
        pass

    @abstractmethod
    def get_keys_for_func_proxy(self, func_proxy):
        pass

    def free(self):
        for func_proxy in self.wrapped_funcs:
            mdl.NotificationCenter.unregister(func_proxy, 'function', self)
        super().free()


class AbstractResponderFunc(ABC):
    '''
    Abstract superclass of responder funcs, which are classes which register
    one or more functions to respond to a particular type of input.
    It provides some common functionality such as introspection. Its two main
    subclasses are OSCFunc, and MIDIFunc. By default responder funcs do not
    persist beyond `CmdPeriod.run()` (see `permanent` property below).

    Instances will register with a dispatcher (an instance of a subclass of
    `AbstractDispatcher`), which will actually dispatch incoming messages
    to an instance's function(s).
    '''

    # _all_func_proxies is set()

    def __init__(self):
        self._func = None
        self._permanent = False
        self.src_id = None
        self.enabled = False
        self.dispatcher = None

    @property
    def func(self):
        '''Responder function.'''
        return self._func

    @func.setter
    def func(self, value):  # prFunc_
        self._func = value
        mdl.NotificationCenter.notify(self, 'function')

    @property
    def permanent(self):
        '''A permanent responder is persistent after `CmdPeriod.run()`.

        By default this property is `False`.
        '''
        return self._permanent

    @permanent.setter
    def permanent(self, value):
        self._permanent = value
        if value and self.enabled:
            sac.CmdPeriod.remove(self.__on_cmd_period)
        else:
            sac.CmdPeriod.add(self.__on_cmd_period)

    def enable(self):
        '''Enable the responder to process incoming data.'''
        if not self.enabled:
            if not self.permanent:
                sac.CmdPeriod.add(self.__on_cmd_period)
            self.dispatcher.add(self)
            self.enabled = True
            type(self)._all_func_proxies.add(self)

    def disable(self):
        '''Disable the responder, no data is processed.'''
        if not self.permanent:
            sac.CmdPeriod.remove(self.__on_cmd_period)
        self.dispatcher.remove(self)
        self.enabled = False

    def __on_cmd_period(self):  # Avoid clash.
        self.free()

    def one_shot(self):
        '''Make the responder a one time action.'''
        wrapped_func = self._func

        def one_shot_func(*args):
            self.free()
            wrapped_func(*args)

        self.func = one_shot_func

    # def fix(self):  # Use oscfunc.permanent = True.
    #     self.permanent = True

    def free(self):
        '''
        Remove the responder form the set of available
        responders and disable it. This should be done
        when you are finished using this object.
        '''

        cls = type(self)
        if self in cls._all_func_proxies:
            cls._all_func_proxies.remove(self)
        if self.enabled:
            self.disable()

    def clear(self):
        '''Clear the responder's function.'''
        self.func = None

    @classmethod
    def _all_func_proxies(cls):
        '''
        Get all current instances of this classes
        concrete subclasses, sorted by type.
        '''

        result = dict()
        for func_proxy in cls._all_func_proxies:
            key = func_proxy.dispatcher.type_key()
            try:
                result[key].append(func_proxy)
            except KeyError:
                result[key] = [func_proxy]
        return result

    @classmethod
    def all_enabled(cls):
        '''
        Return a dict with all the enabled
        responders' dispatchers by `type_name`.
        '''

        result = dict()
        enabled_proxies = [x for x in cls._all_func_proxies if x.enabled]
        for func_proxy in enabled_proxies:
            key = func_proxy.dispatcher.type_key()
            try:
                result[key].append(func_proxy)
            except KeyError:
                result[key] = [func_proxy]
        return result

    @classmethod
    def all_disabled(cls):
        '''
        Return a dict with all the disabled
        responders' dispatchers by `type_name`.
        '''

        result = dict()
        disabled_proxies = [x for x in cls._all_func_proxies if not x.enabled]
        for func_proxy in disabled_proxies:
            key = func_proxy.dispatcher.type_key()
            try:
                result[key].append(func_proxy)
            except KeyError:
                result[key] = [func_proxy]
        return result


### OSC ###


class OscFuncAddrMessageMatcher(AbstractMessageMatcher):
    # // If you need to test for address func gets wrapped in this.
    def __init__(self, addr, func):
        self.addr = addr
        self.func = func

    def __call__(self, msg, time, addr, recv_port):
        if self.addr.addr == addr.addr\
        and (self.addr.port is None or self.addr.port == addr.port):  # was matchItem
            fn.value(self.func, msg, time, addr, recv_port)


class OscFuncRecvPortMessageMatcher(AbstractMessageMatcher):
    # // If you need to test for recvPort func gets wrapped in this.
    def __init__(self, recv_port, func):
        self.recv_port = recv_port
        self.func = func

    def __call__(self, msg, time, addr, recv_port):
        if self.recv_port == recv_port:
            fn.value(self.func, msg, time, addr, recv_port)


class OscFuncBothMessageMatcher(AbstractMessageMatcher):
    def __init__(self, addr, recv_port, func):
        self.addr = addr
        self.recv_port = recv_port
        self.func = func

    def __call__(self, msg, time, addr, recv_port):
        if  self.addr.addr == addr.addr\
        and (self.addr.port is None or self.addr.port == addr.port)\
        and self.recv_port == recv_port:
            fn.value(self.func, msg, time, addr, recv_port)


class OSCArgsMatcher(AbstractMessageMatcher):
    def __init__(self, arg_template, func):
        self.arg_template = utl.as_list(arg_template)
        self.func = func

    def __call__(self, msg, time, addr, recv_port):
        args = msg[1:]
        for i, item in enumerate(self.arg_template):
            if item is not None and item != args[i]:  # was matchItem.not
                return
        fn.value(self.func, msg, time, addr, recv_port)


# // The default dispatchers below store by the 'most significant'
# // message argument for fast lookup. These are for use when more
# // than just the 'most significant' argument needs to be matched.

class OSCMessageDispatcher(AbstractWrappingDispatcher):
    def wrap_func(self, func_proxy):
        func = func_proxy.func
        src_id = func_proxy.src_id
        recv_port = getattr(func_proxy, 'recv_port', None)
        arg_template = getattr(func_proxy, 'arg_template', None)
        if arg_template is not None:
            func = OSCArgsMatcher(arg_template, func)
        if src_id is not None and recv_port is not None:
            return OscFuncBothMessageMatcher(src_id, recv_port, func)
        elif src_id is not None:
            return OscFuncAddrMessageMatcher(src_id, func)
        elif recv_port is not None:
            return OscFuncRecvPortMessageMatcher(recv_port, func)
        else:
            return func

    def get_keys_for_func_proxy(self, func_proxy):
        return [func_proxy.path]

    def __call__(self, msg, time, addr, recv_port):
        if msg[0] in self.active:
            for func in self.active[msg[0]]:
                fn.value(func, msg, time, addr, recv_port)

    def register(self):
        _libsc3.main.add_osc_recv_func(self) # thisProcess.addOSCRecvFunc(this)
        self.registered = True

    def unregister(self):
        _libsc3.main.remove_osc_recv_func(self) # thisProcess.removeOSCRecvFunc(this)
        self.registered = False

    def type_key(self):
        return 'OSC unmatched'


class OSCMessagePatternDispatcher(OSCMessageDispatcher):
    def __call__(self, msg, time, addr, recv_port):
        pattern = msg[0]
        for key, funcs in self.active.items():
            if _match_osc_address_pattern(pattern, key):
                for func in funcs:
                    fn.value(func, msg, time, addr, recv_port)

    def type_key(self):
        return 'OSC matched'


class OscFunc(AbstractResponderFunc):
    '''Fast Responder for incoming Open Sound Control Messages.

    Instances of this class register one or more functions to respond to an
    incoming OSC message which matches a specified OSC Address. Many of its
    methods are inherited from its superclass `AbstractResponderFunc`.
    It supports pattern matching of wildcards etc. in incoming messages.
    For efficiency reasons you must specify that an `OSCFunc` will employ
    pattern matching by creating it with the matching constructor, or by
    passing a matching dispatcher to `dispatcher` parameter of the default
    constructor. For details on the Open Sound Control protocol,
    see http://opensoundcontrol.org/spec-1_0.

    Parameters
    ----------
    func | callable
        A function which will respond to the incoming message. When evaluated
        it will be passed the following arguments: `msg`, the OSC message as
        a list in the form `['/oscaddress', args1, arg2, ...]`, `time`, the
        bundle's time plus the latency or the time of reception for messages,
        `addr`, a `NetAddr` instance corresponding to the IP address of the
        sender, and `recv_port`, an `int` corresponding to the port on which
        the message was received.
    path | str
        The path of the OSC address of this object. Note that `OSCFunc`
        demands OSC compliant addresses. If the path does not begin with a
        '/' one will be added automatically.
    src_id | NetAddr
        An optional instance of NetAddr indicating the IP address of the
        sender. If set this object will only respond to messages from that
        source.
    recv_port | int
        Optional parameter indicating the port on which messages will be
        received. If set, this object will only respond to message received
        on that port. Note that setting this parameter it will open an UDP
        port if not opened already.
    arg_template | list
        An optional list composed of valid OSC values or functions used to
        match the arguments of an incoming OSC message by position. If a
        function, it will be evaluated with the corresponding message's value
        at the same positon as an argument, and should return a boolean
        indicating whether the argument matches and this `OSCFunc` should
        respond (providing all other arguments match). Template values of
        `None` will match any incoming argument value at that position.
    dispatcher | AbstractDispatcher
        An optional instance of an appropriate subclass of `AbstractDispatcher`.
        This can be used to allow for customised dispatching. Normally this
        should not be needed.
    '''

    _all_func_proxies = set()

    default_dispatcher = OSCMessageDispatcher()
    '''
    Default dispatcher object for new instances (this is what you get if you
    pass `None` as the `dispatcher` argument). This object will decide if any
    of its registered `OSCFuncs` should respond to an incoming OSC message.

    By default this will be an `OSCMessageDispatcher`, but it can be set to
    any instance of an appropriate subclass of `AbstractDispatcher`.
    '''

    default_matching_dispatcher = OSCMessagePatternDispatcher()
    '''
    Default matching dispatcher object for new instances (this is what you
    get if when you create an OSCFunc using `matching`). This object will
    decide if any of its registered OSCFuncs should respond to an incoming
    OSC message using pattern matching.

    By default this will be an `OSCMessagePatternDispatcher`, but it can be
    set to any instance of an appropriate subclass of `AbstractDispatcher`.
    '''

    @classmethod
    def _trace_func_show_status(cls, msg, time, addr, recv_port):
        log = ('OSC Message Received:\n'
               f'    time: {time}\n'
               f'    address: {addr}\n'
               f'    recv_port: {recv_port}\n'
               f'    msg: {msg}')
        _logger.info(log)

    @classmethod
    def _trace_func_hide_status(cls, msg, time, addr, recv_port):
        if msg[0] == '/status.reply'\
        and any(server.addr == addr for server in srv.Server.all):
            return
        cls._trace_func_show_status(msg, time, addr, recv_port)

    _trace_func = _trace_func_show_status
    _trace_running = False

    def __init__(self, func, path, src_id=None, *, recv_port=None,
                 arg_template=None, dispatcher=None):
        super().__init__()
        if path[0] != '/':
            path = '/' + path
        self.path = path
        self.src_id = src_id
        self.recv_port = recv_port
        if recv_port is not None:
            _libsc3.main.open_udp_port(recv_port)
        self.arg_template = arg_template
        self._func = func
        self.dispatcher = dispatcher or type(self).default_dispatcher
        self.enable()
        # type(self)._all_func_proxies.add(self)  # Called by enable() already.

    @classmethod
    def matching(cls, func, path, src_id=None,
                 recv_port=None, arg_template=None):
        '''Create a responder with pattern matching capabilities.

        Pattern matching will be applied to any incoming messages to see
        if they match this address (`path`). Note that according to the OSC
        spec, regular expression wildcards are only permitted in the incoming
        message's address pattern. Thus path should not contain wildcards.
        For more details on OSC pattern matching,
        see http://opensoundcontrol.org/spec-1_0.

        See the default constructor other parameters description.
        '''

        return cls(
            func, path, src_id, recv_port, arg_template,
            cls.default_matching_dispatcher)

    @classmethod
    def __on_cmd_period(cls):  # Avoid clash.
        cls.trace(False)

    @classmethod
    def trace(cls, flag=True, hide_status=False):
        '''A convenience method which dumps all incoming OSC messages.

        Parameters
        ----------
        flag | bool
            Dumping on or off.
        hide_status | bool
            Whether server status messages are excluded from the dump or not.
        '''

        if flag and not cls._trace_running:
            if hide_status:
                cls._trace_func = cls._trace_func_hide_status
            else:
                cls._trace_func = cls._trace_func_show_status
            _libsc3.main.add_osc_recv_func(cls._trace_func)
            sac.CmdPeriod.add(cls.__on_cmd_period)
            cls._trace_running = True
        elif cls._trace_running:
            _libsc3.main.remove_osc_recv_func(cls._trace_func)
            sac.CmdPeriod.remove(cls.__on_cmd_period)
            cls._trace_running = False

    def __repr__(self):
        return (
            f'{type(self).__name__}({self.path}, {self.src_id}, '
            f'{self.recv_port}, {self.arg_template})')


# class OscDef(OscFunc):


### MIDI ###

# MIDI implementation needs some thought, mostly because there
# are many libraries and so far sc3 is dependencies free.

class MidiFuncSrcMessageMatcher(AbstractMessageMatcher):
    # // If you need to test for srcID func gets wrapped in this.
    ...


class MidiFuncChanMessageMatcher(AbstractMessageMatcher):
    # // If you need to test for chan func gets wrapped in this.
    ...


class MidiFuncChanArrayMessageMatcher(AbstractMessageMatcher):
    # // If you need to test for chanArray func gets wrapped in this.
    ...


class MidiFuncSrcMessageMatcherNV(MidiFuncSrcMessageMatcher):
    # // Version for message types which don't pass a val.
    ...


class MidiFuncSrcSysMessageMatcher(MidiFuncSrcMessageMatcher):
    # // Version for message types which don't pass a val.
    ...


class MidiFuncSrcSysMessageMatcherND(MidiFuncSrcMessageMatcher):
    ...


class MidiFuncBothMessageMatcher(AbstractMessageMatcher):
    # // If you need to test for chan and srcID func gets wrapped in this.
    ...


class MidiFuncBothCAMessageMatcher(AbstractMessageMatcher):
    # // If you need to test for chanArray and srcID func gets wrapped in this.
    ...


class MIDIValueMatcher(AbstractMessageMatcher):
    # // If you want to test for the actual message value,
    # // the func gets wrapped in this.
    ...


class MIDIMessageDispatcher(AbstractWrappingDispatcher):
    # // For 'note_on', 'note_off', 'control', 'polytouch'.
    ...


class MIDIMessageDispatcherNV(MIDIMessageDispatcher):
    # // For 'touch', 'program', 'bend'.
    ...


class MIDISysexDispatcher(MIDIMessageDispatcher):
    # // For 'sysex'.
    ...


class MIDISysDataDispatcher(MIDIMessageDispatcher):
    # // sysrt with data.
    ...

class MIDISysDataDropIndDispatcher(MIDISysDataDispatcher):
    ...


class MIDISysNoDataDispatcher(MIDISysDataDispatcher):
    ...


class MIDIMTCtoSMPTEDispatcher(MIDISysexDispatcher):
    ...


class MIDISMPTEAssembler(AbstractMessageMatcher):
    # // Thanks to nescivi for code from MTC class!!
    ...


class MidiFunc(AbstractResponderFunc):
    ...

# class MidiDef(MidiFunc):
