"""
Asynchronous framework adapter for asyncio.
"""
import logging
import socket
import asyncio
import functools
import ssl
from serial_asyncio import create_serial_connection
from pymodbus.exceptions import ConnectionException
from pymodbus.client.asynchronous.mixins import AsyncModbusClientMixin
from pymodbus.client.tls_helper import sslctx_provider
from pymodbus.utilities import hexlify_packets
from pymodbus.transaction import FifoTransactionManager

_logger = logging.getLogger(__name__)

DGRAM_TYPE = socket.SOCK_DGRAM


class BaseModbusAsyncClientProtocol(AsyncModbusClientMixin):
    """
    Asyncio specific implementation of asynchronous modbus client protocol.
    """

    #: Factory that created this instance.
    factory = None
    transport = None

    async def execute(self, request=None): # pylint: disable=invalid-overridden-method
        """
        Executes requests asynchronously
        :param request:
        :return:
        """
        req = self._execute(request)
        broadcast = (self.broadcast_enable
                     and request.unit_id == 0)
        if broadcast:
            resp = b'Broadcast write sent - no response expected'
        else:
            resp = await asyncio.wait_for(req, timeout=self._timeout)
        return resp

    def connection_made(self, transport):
        """
        Called when a connection is made.

        The transport argument is the transport representing the connection.
        :param transport:
        :return:
        """
        self.transport = transport
        self._connection_made()

        if self.factory:
            self.factory.protocol_made_connection(self)

    def connection_lost(self, reason):
        """
        Called when the connection is lost or closed.

        The argument is either an exception object or None
        :param reason:
        :return:
        """
        self.transport = None
        self._connection_lost(reason)

        if self.factory:
            self.factory.protocol_lost_connection(self)

    def data_received(self, data):
        """
        Called when some data is received.
        data is a non-empty bytes object containing the incoming data.
        :param data:
        :return:
        """
        self._data_received(data)

    def create_future(self): # pylint: disable=no-self-use
        """
        Helper function to create asyncio Future object
        :return:
        """
        return asyncio.Future()

    def resolve_future(self, f, result): # pylint: disable=no-self-use
        """
        Resolves the completed future and sets the result
        :param f:
        :param result:
        :return:
        """
        if not f.done():
            f.set_result(result)

    def raise_future(self, f, exc): # pylint: disable=no-self-use
        """
        Sets exception of a future if not done
        :param f:
        :param exc:
        :return:
        """
        if not f.done():
            f.set_exception(exc)

    def _connection_made(self):
        """
        Called upon a successful client connection.
        """
        _logger.debug("Client connected to modbus server")
        self._connected = True

    def _connection_lost(self, reason):
        """
        Called upon a client disconnect

        :param reason: The reason for the disconnect
        """
        txt = f"Client disconnected from modbus server: {reason}"
        _logger.debug(txt)
        self._connected = False
        for tid in list(self.transaction):
            self.raise_future(self.transaction.getTransaction(tid),
                              ConnectionException(
                                  'Connection lost during request'))

    @property
    def connected(self):
        """
        Return connection status.
        """
        return self._connected

    def write_transport(self, packet):
        """Write transport."""
        return self.transport.write(packet)

    def _execute(self, request, **kwargs): #NOSONAR pylint: disable=unused-argument
        """
        Starts the producer to send the next request to
        consumer.write(Frame(request))
        """
        request.transaction_id = self.transaction.getNextTID()
        packet = self.framer.buildPacket(request)
        txt = f"send: {hexlify_packets(packet)}"
        _logger.debug(txt)
        self.write_transport(packet)
        return self._build_response(request.transaction_id)

    def _data_received(self, data):
        ''' Get response, check for valid message, decode result

        :param data: The data returned from the server
        '''
        txt = f"recv: {hexlify_packets(data)}"
        _logger.debug(txt)
        unit = self.framer.decode_data(data).get("unit", 0)
        self.framer.processIncomingPacket(data, self._handle_response, unit=unit)

    def _handle_response(self, reply, **kwargs): # pylint: disable=unused-argument
        """
        Handle the processed response and link to correct deferred

        :param reply: The reply to process
        """
        if reply is not None:
            tid = reply.transaction_id
            handler = self.transaction.getTransaction(tid)
            if handler:
                self.resolve_future(handler, reply)
            else:
                txt = f"Unrequested message: {str(reply)}"
                _logger.debug(txt)

    def _build_response(self, tid):
        """
        Helper method to return a deferred response
        for the current request.

        :param tid: The transaction identifier for this response
        :returns: A defer linked to the latest request
        """
        f = self.create_future()
        if not self._connected:
            self.raise_future(f, ConnectionException(
                'Client is not connected'))
        else:
            self.transaction.addTransaction(f, tid)
        return f

    def close(self):
        self.transport.close()
        self._connected = False


class ModbusClientProtocol(BaseModbusAsyncClientProtocol, asyncio.Protocol):
    """
    Asyncio specific implementation of asynchronous modbus client protocol.
    """

    #: Factory that created this instance.
    factory = None
    transport = None

    def data_received(self, data):
        """
        Called when some data is received.
        data is a non-empty bytes object containing the incoming data.
        :param data:
        :return:
        """
        self._data_received(data)


class ModbusUdpClientProtocol(BaseModbusAsyncClientProtocol,
                              asyncio.DatagramProtocol):
    """
    Asyncio specific implementation of asynchronous modbus udp client protocol.
    """

    #: Factory that created this instance.
    factory = None

    def __init__(self, host=None, port=0, **kwargs):
        self.host = host
        self.port = port
        super().__init__(**kwargs)

    def datagram_received(self, data, addr):
        self._data_received(data)

    def write_transport(self, packet):
        return self.transport.sendto(packet)


class ReconnectingAsyncioModbusTcpClient: # pylint: disable=too-many-instance-attributes
    """
    Client to connect to modbus device repeatedly over TCP/IP."
    """
    #: Minimum delay in milli seconds before reconnect is attempted.
    DELAY_MIN_MS = 100
    #: Maximum delay in milli seconds before reconnect is attempted.
    DELAY_MAX_MS = 1000 * 60 * 5

    def __init__(self, protocol_class=None, loop=None, **kwargs):
        """
        Initialize ReconnectingAsyncioModbusTcpClient
        :param protocol_class: Protocol used to talk to modbus device.
        :param loop: Event loop to use
        """
        #: Protocol used to talk to modbus device.
        self.protocol_class = protocol_class or ModbusClientProtocol
        #: Current protocol instance.
        self.protocol = None
        #: Event loop to use.
        self.loop = loop or asyncio.get_event_loop()
        self.host = None
        self.port = 0
        self.connected = False
        #: Reconnect delay in milli seconds.
        self.delay_ms = self.DELAY_MIN_MS
        self._proto_args = kwargs

    def reset_delay(self):
        """
        Resets wait before next reconnect to minimal period.
        """
        self.delay_ms = self.DELAY_MIN_MS

    async def start(self, host, port=502):
        """
        Initiates connection to start client
        :param host:
        :param port:
        :return:
        """
        # force reconnect if required:
        self.stop()

        txt = f"Connecting to {host}:{port}."
        _logger.debug(txt)
        self.host = host
        self.port = port
        return await self._connect()

    def stop(self):
        """
        Stops client
        :return:
        """
        # prevent reconnect:
        self.host = None

        if self.connected:
            if self.protocol:
                if self.protocol.transport:
                    self.protocol.transport.close()

    def _create_protocol(self):
        """
        Factory function to create initialized protocol instance.
        """
        protocol = self.protocol_class(**self._proto_args)
        protocol.factory = self
        return protocol

    async def _connect(self):
        _logger.debug('Connecting.')
        try:
            transport, protocol = await self.loop.create_connection(
                self._create_protocol, self.host, self.port)
            return transport, protocol
        except Exception as exc: # pylint: disable=broad-except
            txt = f"Failed to connect: {exc}"
            _logger.warning(txt)
            asyncio.ensure_future(self._reconnect(), loop=self.loop)
        else:
            txt = f"Connected to {self.host}:{self.port}."
            _logger.info(txt)
            self.reset_delay()

    def protocol_made_connection(self, protocol):
        """
        Protocol notification of successful connection.
        """
        _logger.info('Protocol made connection.')
        if not self.connected:
            self.connected = True
            self.protocol = protocol
        else:
            _logger.error('Factory protocol connect '
                          'callback called while connected.')

    def protocol_lost_connection(self, protocol):
        """
        Protocol notification of lost connection.
        """
        if self.connected:
            _logger.info('Protocol lost connection.')
            if protocol is not self.protocol:
                _logger.error('Factory protocol callback called '
                              'from unexpected protocol instance.')

            self.connected = False
            self.protocol = None
            if self.host:
                asyncio.ensure_future(self._reconnect(), loop=self.loop)
        else:
            _logger.error('Factory protocol disconnect callback called while not connected.')


    async def _reconnect(self):
        txt = f"Waiting {self.delay_ms} ms before next connection attempt."
        _logger.debug(txt)
        await asyncio.sleep(self.delay_ms / 1000)
        self.delay_ms = min(2 * self.delay_ms, self.DELAY_MAX_MS)

        return await self._connect()

class AsyncioModbusTcpClient:
    """Client to connect to modbus device over TCP/IP."""

    def __init__(self, host=None, port=502, protocol_class=None, loop=None, **kwargs):
        """
        Initializes Asyncio Modbus Tcp Client
        :param host: Host IP address
        :param port: Port to connect
        :param protocol_class: Protocol used to talk to modbus device.
        :param loop: Asyncio Event loop
        """
        #: Protocol used to talk to modbus device.
        self.protocol_class = protocol_class or ModbusClientProtocol
        #: Current protocol instance.
        self.protocol = None
        #: Event loop to use.
        self.loop = loop or asyncio.get_event_loop()

        self.host = host
        self.port = port

        self.connected = False
        self._proto_args = kwargs

    def stop(self):
        """
        Stops the client
        :return:
        """
        if self.connected:
            if self.protocol:
                if self.protocol.transport:
                    self.protocol.transport.close()

    def _create_protocol(self):
        """
        Factory function to create initialized protocol instance.
        """
        protocol = self.protocol_class(**self._proto_args)
        protocol.factory = self
        return protocol

    async def connect(self):
        """
        Connect and start Async client
        :return:
        """
        _logger.debug('Connecting.')
        try:
            transport, protocol = await self.loop.create_connection(
                self._create_protocol, self.host, self.port)
            txt = f"Connected to {self.host}:{self.port}."
            _logger.info(txt)
            return transport, protocol
        except Exception as exc: # pylint: disable=broad-except
            txt = f"Failed to connect: {exc}"
            _logger.warning(txt)
            # asyncio.asynchronous(self._reconnect(), loop=self.loop)

    def protocol_made_connection(self, protocol):
        """
        Protocol notification of successful connection.
        """
        _logger.info('Protocol made connection.')
        if not self.connected:
            self.connected = True
            self.protocol = protocol
        else:
            _logger.error('Factory protocol connect '
                          'callback called while connected.')

    def protocol_lost_connection(self, protocol):
        """
        Protocol notification of lost connection.
        """
        if self.connected:
            _logger.info('Protocol lost connection.')
            if protocol is not self.protocol:
                _logger.error('Factory protocol callback called'
                              ' from unexpected protocol instance.')

            self.connected = False
            self.protocol = None
            # if self.host:
            #     asyncio.asynchronous(self._reconnect(), loop=self.loop)
        else:
            _logger.error('Factory protocol disconnect'
                          ' callback called while not connected.')


class ReconnectingAsyncioModbusTlsClient(ReconnectingAsyncioModbusTcpClient):
    """
    Client to connect to modbus device repeatedly over TLS."
    """
    def __init__(self, protocol_class=None, loop=None, framer=None, **kwargs):
        """
        Initialize ReconnectingAsyncioModbusTcpClient
        :param protocol_class: Protocol used to talk to modbus device.
        :param loop: Event loop to use
        """
        self.framer = framer
        self.server_hostname = None
        self.sslctx = None
        ReconnectingAsyncioModbusTcpClient.__init__(self, protocol_class, loop, **kwargs)

    async def start(self, host, port=802, sslctx=None, server_hostname=None):
        """
        Initiates connection to start client
        :param host:
        :param port:
        :param sslctx:
        :param server_hostname:
        :return:
        """

        self.sslctx = sslctx
        if self.sslctx is None:
            self.sslctx = ssl.create_default_context()
            # According to MODBUS/TCP Security Protocol Specification, it is
            # TLSv2 at least
            self.sslctx.options |= ssl.OP_NO_TLSv1_1
            self.sslctx.options |= ssl.OP_NO_TLSv1
            self.sslctx.options |= ssl.OP_NO_SSLv3
            self.sslctx.options |= ssl.OP_NO_SSLv2
        self.server_hostname = server_hostname
        return await ReconnectingAsyncioModbusTcpClient.start(self, host, port)

    async def _connect(self):
        _logger.debug('Connecting.')
        try:
            return await self.loop.create_connection(
                self._create_protocol, self.host,
                                      self.port,
                                      ssl=self.sslctx,
                                      server_hostname=self.host
            )
        except Exception as exc: # pylint: disable=broad-except
            txt = f"Failed to connect: {exc}"
            _logger.warning(txt)
            asyncio.ensure_future(self._reconnect(), loop=self.loop)
        else:
            txt = f"Connected to {self.host}:{self.port}."
            _logger.info(txt)
            self.reset_delay()

    def _create_protocol(self):
        """
        Factory function to create initialized protocol instance.
        """
        protocol = self.protocol_class(framer=self.framer, **self._proto_args)
        protocol.transaction = FifoTransactionManager(self)
        protocol.factory = self
        return protocol

class ReconnectingAsyncioModbusUdpClient: # pylint: disable=too-many-instance-attributes
    """
    Client to connect to modbus device repeatedly over UDP.
    """

    #: Reconnect delay in milli seconds.
    delay_ms = 0

    #: Maximum delay in milli seconds before reconnect is attempted.
    DELAY_MAX_MS = 1000 * 60 * 5

    def __init__(self, protocol_class=None, loop=None, **kwargs):
        """
        Initializes ReconnectingAsyncioModbusUdpClient
        :param protocol_class: Protocol used to talk to modbus device.
        :param loop: Asyncio Event loop
        """
        #: Protocol used to talk to modbus device.
        self.protocol_class = protocol_class or ModbusUdpClientProtocol
        #: Current protocol instance.
        self.protocol = None
        #: Event loop to use.
        self.loop = loop or asyncio.get_event_loop()

        self.host = None
        self.port = 0

        self.connected = False
        self._proto_args = kwargs
        self.reset_delay()

    def reset_delay(self):
        """
        Resets wait before next reconnect to minimal period.
        """
        self.delay_ms = 100

    async def start(self, host, port=502):
        """
        Start reconnecting asynchronous udp client
        :param host: Host IP to connect
        :param port: Host port to connect
        :return:
        """
        # force reconnect if required:
        self.stop()

        txt = f"Connecting to {host}:{port}."
        _logger.debug(txt)

        # getaddrinfo returns a list of tuples
        # - [(family, type, proto, canonname, sockaddr),]
        # We want sockaddr which is a (ip, port) tuple
        # udp needs ip addresses, not hostnames
        addrinfo = await self.loop.getaddrinfo(host,
                                                    port,
                                                    type=DGRAM_TYPE)
        self.host, self.port = addrinfo[0][-1]
        return await self._connect()


    def stop(self):
        """
        Stops connection and prevents reconnect
        :return:
        """
        # prevent reconnect:
        self.host = None

        if self.connected:
            if self.protocol:
                if self.protocol.transport:
                    self.protocol.transport.close()

    def _create_protocol(self, host=None, port=0):
        """
        Factory function to create initialized protocol instance.
        """
        protocol = self.protocol_class(**self._proto_args)
        protocol.host = host
        protocol.port = port
        protocol.factory = self
        return protocol


    async def _connect(self):
        _logger.debug('Connecting.')
        try:
            endpoint = await self.loop.create_datagram_endpoint(
                functools.partial(self._create_protocol,
                                  host=self.host,
                                  port=self.port),
                remote_addr=(self.host, self.port)
            )
            txt = f"Connected to {self.host}:{self.port}."
            _logger.info(txt)
            return endpoint
        except Exception as exc: # pylint: disable=broad-except
            txt = f"Failed to connect: {exc}"
            _logger.warning(txt)
            asyncio.ensure_future(self._reconnect(), loop=self.loop)

    def protocol_made_connection(self, protocol):
        """
        Protocol notification of successful connection.
        """
        _logger.info('Protocol made connection.')
        if not self.connected:
            self.connected = True
            self.protocol = protocol
        else:
            _logger.error('Factory protocol connect callback '
                          'called while connected.')

    def protocol_lost_connection(self, protocol):
        """
        Protocol notification of lost connection.
        """
        if self.connected:
            _logger.info('Protocol lost connection.')
            if protocol is not self.protocol:
                _logger.error('Factory protocol callback called '
                              'from unexpected protocol instance.')

            self.connected = False
            self.protocol = None
            if self.host:
                asyncio.create_task(self._reconnect())
        else:
            _logger.error('Factory protocol disconnect '
                          'callback called while not connected.')

    async def _reconnect(self):
        txt = f"Waiting {self.delay_ms} ms before next connection attempt."
        _logger.debug(txt)
        await asyncio.sleep(self.delay_ms / 1000)
        self.delay_ms = min(2 * self.delay_ms, self.DELAY_MAX_MS)
        return await self._connect()


class AsyncioModbusUdpClient:
    """
    Client to connect to modbus device over UDP.
    """

    def __init__(self, host=None, port=502, protocol_class=None, loop=None, **kwargs):
        """
        Initializes Asyncio Modbus UDP Client
        :param host: Host IP address
        :param port: Port to connect
        :param protocol_class: Protocol used to talk to modbus device.
        :param loop: Asyncio Event loop
        """
        #: Protocol used to talk to modbus device.
        self.protocol_class = protocol_class or ModbusUdpClientProtocol
        #: Current protocol instance.
        self.protocol = None
        #: Event loop to use.
        self.loop = loop or asyncio.get_event_loop()

        self.host = host
        self.port = port

        self.connected = False
        self._proto_args = kwargs

    def stop(self):
        """
        Stops connection
        :return:
        """
        # prevent reconnect:
        # self.host = None

        if self.connected:
            if self.protocol:
                if self.protocol.transport:
                    self.protocol.transport.close()

    def _create_protocol(self, host=None, port=0):
        """
        Factory function to create initialized protocol instance.
        """
        protocol = self.protocol_class(**self._proto_args)
        protocol.host = host
        protocol.port = port
        protocol.factory = self
        return protocol

    async def connect(self):
        """Connect."""
        _logger.debug('Connecting.')
        try:
            addrinfo = await self.loop.getaddrinfo(
                self.host,
                self.port,
                type=DGRAM_TYPE)
            _host, _port = addrinfo[0][-1]

            endpoint = await self.loop.create_datagram_endpoint(
                functools.partial(self._create_protocol,
                                  host=_host, port=_port),
                remote_addr=(self.host, self.port)
            )
            txt = f"Connected to {self.host}:{self.port}."
            _logger.info(txt)
            return endpoint
        except Exception as exc: # pylint: disable=broad-except
            txt = f"Failed to connect: {exc}"
            _logger.warning(txt)
            # asyncio.asynchronous(self._reconnect(), loop=self.loop)

    def protocol_made_connection(self, protocol):
        """
        Protocol notification of successful connection.
        """
        _logger.info('Protocol made connection.')
        if not self.connected:
            self.connected = True
            self.protocol = protocol
        else:
            _logger.error('Factory protocol connect '
                          'callback called while connected.')

    def protocol_lost_connection(self, protocol):
        """
        Protocol notification of lost connection.
        """
        if self.connected:
            _logger.info('Protocol lost connection.')
            if protocol is not self.protocol:
                _logger.error('Factory protocol callback '
                              'called from unexpected protocol instance.')

            self.connected = False
            self.protocol = None
            # if self.host:
            #    asyncio.asynchronous(self._reconnect(), loop=self.loop)
        else:
            _logger.error('Factory protocol disconnect '
                          'callback called while not connected.')


class AsyncioModbusSerialClient: # pylint: disable=too-many-instance-attributes
    """
    Client to connect to modbus device over serial.
    """
    transport = None
    framer = None

    def __init__(self, port, protocol_class=None, framer=None,  loop=None, # pylint: disable=too-many-arguments
                 baudrate=9600, bytesize=8, parity='N', stopbits=1, **serial_kwargs):
        """
        Initializes Asyncio Modbus Serial Client
        :param port: Port to connect
        :param protocol_class: Protocol used to talk to modbus device.
        :param framer: Framer to use
        :param loop: Asyncio Event loop
        """
        #: Protocol used to talk to modbus device.
        self.protocol_class = protocol_class or ModbusClientProtocol
        #: Current protocol instance.
        self.protocol = None
        #: Event loop to use.
        self.loop = loop or asyncio.get_event_loop()
        self.port = port
        self.baudrate = baudrate
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.framer = framer
        self._extra_serial_kwargs = serial_kwargs
        self._connected_event = asyncio.Event()

    def stop(self):
        """
        Stops connection
        :return:
        """
        if self._connected:
            if self.protocol:
                if self.protocol.transport:
                    self.protocol.transport.close()

    def _create_protocol(self):
        protocol = self.protocol_class(framer=self.framer)
        protocol.factory = self
        return protocol

    @property
    def _connected(self):
        return self._connected_event.is_set()

    async def connect(self):
        """
        Connect Async client
        :return:
        """
        _logger.debug('Connecting.')
        try:
            await create_serial_connection(
                self.loop, self._create_protocol, self.port, baudrate=self.baudrate,
                bytesize=self.bytesize, stopbits=self.stopbits,
                parity=self.parity, **self._extra_serial_kwargs
            )
            await self._connected_event.wait()
            txt = f"Connected to {self.port}"
            _logger.info(txt)
        except Exception as exc: # pylint: disable=broad-except
            txt = f"Failed to connect: {exc}"
            _logger.warning(txt)

    def protocol_made_connection(self, protocol):
        """
        Protocol notification of successful connection.
        """
        _logger.info('Protocol made connection.')
        if not self._connected:
            self._connected_event.set()
            self.protocol = protocol
        else:
            _logger.error('Factory protocol connect '
                          'callback called while connected.')

    def protocol_lost_connection(self, protocol):
        """
        Protocol notification of lost connection.
        """
        if self._connected:
            _logger.info('Protocol lost connection.')
            if protocol is not self.protocol:
                _logger.error('Factory protocol callback called'
                              ' from unexpected protocol instance.')

            self._connected_event.clear()
            self.protocol = None
            # if self.host:
            #     asyncio.asynchronous(self._reconnect(), loop=self.loop)
        else:
            _logger.error('Factory protocol disconnect callback '
                          'called while not connected.')


async def init_tcp_client(proto_cls, loop, host, port, **kwargs):
    """
    Helper function to initialize tcp client
    :param proto_cls:
    :param loop:
    :param host:
    :param port:
    :param kwargs:
    :return:
    """

    client = ReconnectingAsyncioModbusTcpClient(protocol_class=proto_cls,
                                                loop=loop, **kwargs)
    await client.start(host, port)
    return client


async def init_tls_client(proto_cls, loop, host, port, sslctx=None,  # pylint: disable=too-many-arguments
                    server_hostname=None, framer=None, **kwargs):
    """
    Helper function to initialize tcp client
    :param proto_cls:
    :param loop:
    :param host:
    :param port:
    :param sslctx:
    :param server_hostname:
    :param framer:
    :param kwargs:
    :return:
    """

    client = ReconnectingAsyncioModbusTlsClient(protocol_class=proto_cls,
                                                loop=loop, framer=framer,
                                                **kwargs)
    await client.start(host, port, sslctx, server_hostname)
    return client


async def init_udp_client(proto_cls, loop, host, port, **kwargs):
    """
    Helper function to initialize UDP client
    :param proto_cls:
    :param loop:
    :param host:
    :param port:
    :param kwargs:
    :return:
    """

    client = ReconnectingAsyncioModbusUdpClient(protocol_class=proto_cls,
                                                loop=loop, **kwargs)
    await client.start(host, port)
    return client