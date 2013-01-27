from functools import partial

from pulsar import TooManyConnections
from pulsar.utils.pep import get_event_loop
from pulsar.utils.sockets import get_transport_type, create_socket
from pulsar.async.defer import is_async

from .protocols import ProtocolConsumer, EventHandler, Producer, NOTHING
from .transport import create_transport

__all__ = ['ConnectionPool', 'Client', 'Request']

    
class Request(object):
    '''A :class:`Client` request class.'''
    def __init__(self, address, timeout=0):
        self.address = address
        self.timeout = timeout
        
    def __hash__(self):
        return hash((self.address, self.timeout))
    
    
class ConnectionPool(Producer):
    '''A :class:`Producer` of of active connections for client
protocols. It maintains a live set of connections.

.. attribute:: address

    Address to connect to
    '''    
    def __init__(self, request, **params):
        params['timeout'] = request.timeout
        super(ConnectionPool, self).__init__(**params)
        self._address = request.address
        self._available_connections = set()
    
    @property
    def address(self):
        return self._address
    
    def release(self, connection):
        "Releases the connection back to the pool"
        self._concurrent_connections.remove(connection)
        self._available_connections.add(connection)
        
    def _remove_connection(self, connection):
        super(ConnectionPool, self)._remove_connection(connection)
        self._available_connections.discard(connection)
        try:
            connection.close()
        except Exception:
            pass
        
    def get_or_create_connection(self, client):
        "Get or create a new connection for *client*"
        try:
            connection = self._available_connections.pop()
        except KeyError:
            connection = None
        else:
            # we have a connection, lets added it to the concurrent set
            self._concurrent_connections.add(connection)
        if connection is None:
            # build the new connection
            connection = self.new_connection(self.address,
                                             client.consumer_factory)
            #IMPORTANT: create client transport an connect to endpoint
            transport = create_transport(connection, address=connection.address)
            return transport.connect()
        else:
            return connection
    

class Client(EventHandler):
    '''A client for a remote server which handles one or more
:class:`ConnectionPool` of asynchronous connections.
'''
    connection_pool = ConnectionPool
    '''Factory of :class:`ConnectionPool`.'''
    consumer_factory = None
    '''A factory of :class:`ProtocolConsumer` for sending and consuming data.'''
    connection_factory = None
    '''A factory of :class:`Connection`.'''
    client_version = ''
    '''An optional version for this client'''
    timeout = 0
    '''Optional timeout in seconds for idle connections.'''
    max_connections = 0
    '''Maximum number of concurrent connections.'''
    
    MANY_TIMES_EVENTS = ('pre_request', 'post_request')
    
    def __init__(self, max_connections=None, timeout=None, client_version=None,
                 trust_env=True, consumer_factory=None,**params):
        super(Client, self).__init__()
        self.trust_env = trust_env
        self.client_version = client_version or self.client_version
        self.timeout = timeout if timeout is not None else self.timeout
        if consumer_factory:
            self.consumer_factory = consumer_factory
        self.max_connections = max_connections or self.max_connections or 2**31
        self.connection_pools = {}
        self.setup(**params)
    
    def setup(self, **params):
        '''Setup the client. By default it does nothing.'''
    
    def hash(self, address, timeout, request):
        return hash((address, timeout))
    
    def request(self, *args, **params):
        '''Create a request and invoke the :meth:`response` method.
Must be implemented by subclasses.'''
        raise NotImplementedError
    
    def response(self, request, consumer=None):
        '''Once a *request* object has been constructed, the :meth:`request`
method should invoke this method to start the response dance.

:parameter request: A custom :class:`Request` for the :class:`Client`.
:parameter consumer: An optional consumer of streaming data, it is the
    :attr:`ClientProtocolConsumer.consumer` attribute of the
    consumer returned by this method.
:rtype: An :class:`ClientProtocolConsumer` obtained form
    :attr:`consumer_factory`.
'''
        # Get a suitable connection pool
        pool = self.connection_pools.get(request)
        if pool is None:
            pool = self.connection_pool(
                                    request,
                                    max_connections=self.max_connections,
                                    connection_factory=self.connection_factory)
            self.connection_pools[request] = pool
        conn = pool.get_or_create_connection(self)
        # conn could be a Connection or a Connector (Deferred)
        response = self.consumer_factory(conn, request, consumer)
        conn.set_consumer(response)
        return response
    
    def update_parameters(self, params):
        '''Update *param* with attributes of this :class:`Client` defined
in :attr:`request_parameters` tuple.'''
        for name in self.request_parameters:
            if name not in params:
                params[name] = getattr(self, name)
            elif name == 'hooks':
                hooks = params[name]
                chooks = dict(((e, copy(h)) for e, h in iteritems(self.hooks)))
                for e, h in iteritems(hooks):
                    chooks[e].append(h)
                params[name] = chooks
        return params
        
    def close_connections(self):
        for p in self.connection_pools.values():
            p.close_connections()
            
    def close(self):
        self.close_connections()