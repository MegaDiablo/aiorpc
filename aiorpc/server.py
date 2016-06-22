import asyncio
import msgpack
import logging
import sys
from . import message
from .client import ClientPool, Client
import inspect
from collections import namedtuple
import socket

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())

FuncDef = namedtuple('FuncDef', ['func', 'params', 'with_context'])

def socket_bind(host, port):
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port)) 
    sock.listen(1024) 
    sock.setblocking(False)
    return sock

class AgentMixin(object):

    def _set_aio_loop(self, loop):
        self._aio_loop = loop

    def _get_aio_loop(self):
        return self._aio_loop

    def _set_client_pool(self, pool):
        self._client_pool = pool

    def _get_client(self, endpoint):
        return self._client_pool.get(endpoint)

    def _activated(self, listener):
        """this method is called after server started
        """
        pass

class Server(object):

    def __init__(self, listener, app, *, loop=None, client_pool_request_timeout=None, client_class=Client):
        self._listener = listener
        self._app = app
        self._app_funcs = self._get_app_funcs(self._app)
        log.debug("app expose funcs: %s", self._app_funcs)
        self._loop = loop or asyncio.get_event_loop()
        self._server = None
        client_pool_setter = getattr(self._app, '_set_client_pool', None)
        if client_pool_setter:
            self._client_pool = ClientPool(self._loop, client_class, client_pool_request_timeout)
            client_pool_setter(self._client_pool)
        aio_loop_setter = getattr(self._app, '_set_aio_loop', None)
        if aio_loop_setter:
            aio_loop_setter(self._loop)

    def start(self):
        if isinstance(self._listener, socket.socket):
            coro = asyncio.start_server(self.handler, loop=self._loop, sock=self._listener)
        else:
            host, port = self._listener
            coro = asyncio.start_server(self.handler, host, port, loop=self._loop)
        self._server = self._loop.run_until_complete(coro)
        self._listener = self._server.sockets[0].getsockname()
        log.info('start server on %s', self._listener)
        activated_func = getattr(self._app, '_activated', None)
        if activated_func:
            activated_func(self._listener)

    def stop(self):
        self._server.close()
        self._loop.run_until_complete(self._server.wait_closed())

    def get_app(self):
        return self._app

    def get_listener(self):
        return self._listener

    def run_forever(self):
        if not self._server:
            self.start()
        try:
            self._loop.run_forever()
        except KeyboardInterrupt:
            pass

        # Close the server
        self._server.close()
        self._loop.run_until_complete(self._server.wait_closed())
        self._loop.close()

    @asyncio.coroutine
    def handler(self, reader, writer):
        unpacker = msgpack.Unpacker()
        log.debug("connection from %s", writer.get_extra_info('peername'))
        try:
            sock = writer.get_extra_info('socket')
            #set TCP_NODELAY to disable Nagle's algorithm which will cause an initial 40ms delay for small packet rpc at linux
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            while True:
                buf = yield from reader.read(4096)
                log.debug("recv dat: %s", buf)
                if not buf:
                    break
                unpacker.feed(buf)
                for msg in unpacker:
                    assert isinstance(msg, (list, tuple)), 'message is not list or tuple'
                    if msg[0] == message.TYPE_REQUEST:
                        assert len(msg) == 4, 'malformed request message'
                        msgobj = message.Request(*msg)
                    elif msg[0] == message.TYPE_NOTIFICATION:
                        assert len(msg) == 3, 'malformed notification message'
                        msgobj = message.Notification(*msg)
                    else:
                        raise RuntimeError('invalid message type {}'.format(msg[0]))
                    log.debug("recv msg: %s", msgobj)
                    asyncio.async(self.process(msgobj, writer), loop=self._loop)
        except:
            log.exception('handle %s except:', self._app)
        if not self._loop.is_closed():
            log.debug('close client socket')
            reader.feed_eof()
            writer.close()

    def _get_app_funcs(self, app):
        funcs = {}
        for method in dir(app):
            if method.startswith('_'):
                continue
            func = getattr(app, method)
            if callable(func):
                funcsig = inspect.signature(func)
                with_context = len(funcsig.parameters) and (next(iter(funcsig.parameters)) in ('ctx', 'context'))
                funcdef = FuncDef(func, funcsig.parameters, with_context)
                funcs[method] = funcdef
        return funcs

    def _get_exec_func(self, msg):
        if msg.method.startswith(b'\0'):
            funcdef = self._get_control_command(msg.method);
        else:
            method = msg.method.decode()
            funcdef = self._app_funcs[method]
            #func = getattr(self._app, method)
        return funcdef

    def _execute_quest(self, msg, writer):
        fdef = self._get_exec_func(msg)
        func = fdef.func
        if fdef.with_context:
            if isinstance(msg.params, (list, tuple)):
                result = func(writer.get_extra_info, *msg.params) 
            elif isinstance(msg.params, dict):
                #every element key startswith __ and endswith __ is ctx
                ctx = {k.decode():v for k,v in msg.params.items() if k.startswith(b'__') and k.endswith(b'__')}
                params = {k.decode():v for k,v in msg.params.items() if not k.startswith(b'__') or not k.endswith(b'__')}
                ctx_getter = lambda k:ctx.get(k, writer.get_extra_info(k))
                result = func(ctx_getter, **params)
            elif msg.params == None:
                result = func(writer.get_extra_info)
            else:
                result = func(writer.get_extra_info, msg.params)
        else:
            if isinstance(msg.params, (list, tuple)):
                result = func(*msg.params) 
            elif isinstance(msg.params, dict):
                result = func(**{key.decode():val for key, val in msg.params.items()})
            elif msg.params == None:
                result = func()
            else:
                result = func(msg.params)
        return result

    @asyncio.coroutine 
    def process(self, msg, writer):
        result = None
        error = None
        try:
            result = self._execute_quest(msg, writer)
            if asyncio.iscoroutine(result):
                result = yield from result 
        except:
            log.exception('process except:')
            error = list(map(str, sys.exc_info()[:2]))

        if isinstance(msg, message.Request):
            reply = [message.TYPE_RESPONSE, msg.mid, error, result]
            log.debug('reply msg: %s', reply)
            reply = msgpack.packb(reply)
            writer.write(reply)
            yield from writer.drain()

    def _get_control_command(self, method):
        method = method.decode()[1:]
        funcdef = getattr(self, '_ctrl_cmd_{}'.format(method))()
        return funcdef
        
    def _ctrl_cmd_reflection(self):
        def wrap():
            """ -> {'methodname': ['arg0', 'arg1', {'name': 'arg2', 'default': 'abc'}]}
            """
            methods = {name:[pdef.name if pdef.default == inspect.Parameter.empty else {'name': pdef.name, 'default': pdef.default} for pname, pdef in funcdef.params.items()] for name, funcdef in self._app_funcs.items()}
            return {'methods': methods}
        return FuncDef(wrap, None, False)


def run(app, endp=('0.0.0.0', 8888), server_class=Server):
    logging.basicConfig(level=logging.DEBUG)
    srv = server_class(endp, app)
    srv.start()

    srv.run_forever()

if __name__ == '__main__':
    import random
    class App(AgentMixin, object):

        def recv_notify(self, txt):
            print('Noitfy:', txt)

        def sleep(self, sec):
            yield from asyncio.sleep(sec, loop=self._get_aio_loop())
            return True

        def echo(self, *args):
            yield from asyncio.sleep(random.random(), loop=self._get_aio_loop())
            return args
        
        def test(self, a, b, c=2, d=None):
            return 1

    app = App()

    run(app)
