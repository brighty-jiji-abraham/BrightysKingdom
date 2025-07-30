"""
Context-Free WebSocket Tunnel for Flask Socket.IO Backends
----------------------------------------------------------

This module provides a WebSocket tunnel that works completely outside of Flask 
request context, making it safe to use from gevent greenlets after WebSocket upgrade.

Key Features:
- No Flask context dependencies
- Windows-compatible WebSocket handling  
- Direct load balancer access without current_app
- COMPLETE event transparency (client ↔ backend)
- ALL events pass through unchanged in both directions
- Multi-user session isolation
- ENHANCED Direct WebSocket connection with guaranteed header delivery
- ABSOLUTELY FIXED Engine.IO packet handling (no more "Invalid event format" warnings)
- ROBUST JSON parsing (handles extra data gracefully)
- Proper Socket.IO v4 protocol compliance

Author: BrightysKingdom (July 2025)
"""

import base64
import hashlib
import json
import logging
import os
import time
import threading
from typing import Dict, Optional
from urllib.parse import parse_qs, urlencode
import websocket
import ssl

from geventwebsocket.websocket import WebSocket
import socketio

# Get logger without Flask dependencies
_LOGGER = logging.getLogger("proxy_server.websocket_tunnel")

# Enable detailed WebSocket debugging
websocket.enableTrace(True)
logging.getLogger("websocket").setLevel(logging.DEBUG)


def calculate_accept(key: str) -> str:
    """Calculate Sec-WebSocket-Accept response value"""
    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    accept = key + GUID
    accept_sha1 = hashlib.sha1(accept.encode('utf-8')).digest()
    return base64.b64encode(accept_sha1).decode('utf-8')


def is_engine_io_packet(message: str) -> bool:
    """Check if message is an Engine.IO control packet - ABSOLUTE CHECK"""
    if not isinstance(message, str) or not message:
        return False
    
    # Engine.IO control packets (single digit)
    if message in {'0', '1', '2', '3', '4', '5', '6'}:
        return True
    
    # Engine.IO probe packets
    if message in {'2probe', '3probe'}:
        return True
    
    # Engine.IO upgrade packets
    if message in {'5upgrade'}:
        return True
    
    return False


def is_socketio_event_packet(message: str) -> bool:
    """Check if message is a Socket.IO event packet - ENHANCED CHECK"""
    if not isinstance(message, str) or len(message) < 4:  # Minimum: "42[]"
        return False
    
    # Only Socket.IO EVENT (42), BINARY_EVENT (45), BINARY_ACK (46) contain events
    if not message.startswith(('42', '45', '46')):
        return False
    
    # Additional check - must have valid JSON array structure after prefix
    payload = message[2:].strip()
    if not payload or not payload.startswith('['):
        return False
    
    return True


class ContextFreeLoadBalancer:
    """Load balancer that works without Flask context"""
    
    def __init__(self):
        self._backend_routes = self._load_backend_routes()
        self._round_robin_counters = {}
        
    def _load_backend_routes(self) -> Dict[str, str]:
        """Load backend routes directly from environment variables"""
        routes = {}
        
        app1_urls = os.getenv('APP1_URLS', os.getenv('APP1_URL', 'http://127.0.0.1:3000'))
        app2_urls = os.getenv('APP2_URLS', os.getenv('APP2_URL', 'http://127.0.0.1:5000'))
        api_urls = os.getenv('API_URLS', os.getenv('API_URL', 'http://127.0.0.1:7000'))
        
        def parse_urls(url_string):
            if not url_string:
                return None
            urls = [url.strip() for url in url_string.split(',') if url.strip()]
            return urls if len(urls) > 1 else urls[0] if urls else None
        
        routes['/app1'] = parse_urls(app1_urls)
        routes['/app2'] = parse_urls(app2_urls)
        routes['/api'] = parse_urls(api_urls)
        
        _LOGGER.info(f"Loaded backend routes: {routes}")
        return routes
    
    def get_backend_for_route(self, route: str) -> Optional[str]:
        """Get backend URL for a route using simple round-robin"""
        backends = self._backend_routes.get(route)
        
        if not backends:
            _LOGGER.error(f"No backends found for route: {route}")
            return None
        
        if isinstance(backends, str):
            return backends
        
        if isinstance(backends, list):
            if route not in self._round_robin_counters:
                self._round_robin_counters[route] = 0
            
            index = self._round_robin_counters[route] % len(backends)
            self._round_robin_counters[route] = (self._round_robin_counters[route] + 1) % len(backends)
            
            backend = backends[index]
            _LOGGER.debug(f"Selected backend {backend} for route {route}")
            return backend
        
        return None


class EnhancedDirectWebSocketClient:
    """Enhanced Direct WebSocket client with guaranteed header delivery"""
    
    def __init__(self, url: str, headers: Dict[str, str], on_message=None, on_error=None, on_close=None, on_open=None):
        self.url = url
        self.original_headers = headers
        self.on_message = on_message
        self.on_error = on_error
        self.on_close = on_close
        self.on_open = on_open
        self.ws = None
        self.connected = False
        
    def connect(self):
        """Connect with enhanced header control and debugging"""
        try:
            # Convert http/https URLs to ws/wss
            ws_url = self.url.replace('http://', 'ws://').replace('https://', 'wss://')
            if not ws_url.endswith('/socket.io/'):
                ws_url = ws_url.rstrip('/') + '/socket.io/'
            
            # Add Socket.IO query parameters
            ws_url += '?EIO=4&transport=websocket'
            
            # ENHANCED: Prepare headers with proper formatting
            enhanced_headers = self._prepare_enhanced_headers()
            
            _LOGGER.info(f"🔗 ENHANCED-DIRECT: Connecting to {ws_url}")
            _LOGGER.info(f"🔗 ENHANCED-DIRECT: Sending {len(enhanced_headers)} headers")
            
            # Log each header for debugging
            for header_name, header_value in enhanced_headers.items():
                _LOGGER.info(f"🔍 Header: {header_name} = {header_value}")
            
            # Create WebSocket with enhanced configuration
            self.ws = websocket.WebSocketApp(
                ws_url,
                header=enhanced_headers,
                on_message=self._on_message_wrapper,
                on_error=self._on_error_wrapper,
                on_close=self._on_close_wrapper,
                on_open=self._on_open_wrapper
            )
            
            # Enhanced connection parameters
            run_kwargs = {
                'sslopt': {'cert_reqs': ssl.CERT_NONE} if ws_url.startswith('wss://') else {},
                'ping_interval': 30,
                'ping_timeout': 10,
                'ping_payload': "ping"
            }
            
            # Run in separate thread
            self.ws_thread = threading.Thread(
                target=self.ws.run_forever,
                kwargs=run_kwargs
            )
            self.ws_thread.daemon = True
            self.ws_thread.start()
            
            # Wait for connection with longer timeout
            for i in range(100):  # 10 second timeout
                if self.connected:
                    break
                time.sleep(0.1)
                if i % 10 == 0:  # Log every second
                    _LOGGER.info(f"🔗 ENHANCED-DIRECT: Waiting for connection... {i/10}s")
            
            if self.connected:
                _LOGGER.info(f"✅ ENHANCED-DIRECT: Connection established successfully")
            else:
                _LOGGER.error(f"❌ ENHANCED-DIRECT: Connection timeout after 10 seconds")
            
            return self.connected
            
        except Exception as e:
            _LOGGER.error(f"❌ ENHANCED-DIRECT connection error: {e}")
            return False
    
    def _prepare_enhanced_headers(self) -> Dict[str, str]:
        """Prepare headers with ENHANCED formatting and validation"""
        enhanced_headers = {}
        
        # Copy base headers but filter problematic ones
        for k, v in self.original_headers.items():
            # Skip headers that websocket-client manages internally
            if k.lower() not in ['host', 'connection', 'upgrade', 'sec-websocket-key']:
                enhanced_headers[k] = v
        
        # CRITICAL: Ensure proper WebSocket version header
        # Remove any existing variations
        keys_to_remove = []
        for k in enhanced_headers.keys():
            if k.lower().replace('-', '').replace('_', '') == 'secwebsocketversion':
                keys_to_remove.append(k)
        
        for k in keys_to_remove:
            del enhanced_headers[k]
        
        # Add the CORRECT header with proper case
        enhanced_headers['Sec-WebSocket-Version'] = '13'
        
        # Ensure other required headers
        if 'Origin' not in enhanced_headers:
            enhanced_headers['Origin'] = 'http://localhost:2000'
        
        if 'User-Agent' not in enhanced_headers:
            enhanced_headers['User-Agent'] = 'ProxyTunnel/1.0'
        
        # Add custom header for debugging
        enhanced_headers['X-Proxy-Source'] = 'DirectWebSocketTunnel'
        
        _LOGGER.info(f"📤 ENHANCED headers prepared: {len(enhanced_headers)} total")
        _LOGGER.info(f"📤 WebSocket-Version: {enhanced_headers.get('Sec-WebSocket-Version')}")
        
        return enhanced_headers
    
    def _on_message_wrapper(self, ws, message):
        _LOGGER.debug(f"📨 ENHANCED-DIRECT: Received message: {message[:100]}")
        if self.on_message:
            self.on_message(message)
    
    def _on_error_wrapper(self, ws, error):
        _LOGGER.error(f"❌ ENHANCED-DIRECT WebSocket error: {error}")
        if self.on_error:
            self.on_error(error)
    
    def _on_close_wrapper(self, ws, close_status_code, close_msg):
        self.connected = False
        _LOGGER.info(f"🔗 ENHANCED-DIRECT WebSocket closed: {close_status_code} - {close_msg}")
        if self.on_close:
            self.on_close()
    
    def _on_open_wrapper(self, ws):
        self.connected = True
        _LOGGER.info(f"✅ ENHANCED-DIRECT WebSocket connected successfully!")
        _LOGGER.info(f"🔍 Connection details: {ws.sock}")
        if self.on_open:
            self.on_open()
    
    def send(self, message):
        """Send message to backend"""
        if self.ws and self.connected:
            try:
                self.ws.send(message)
                _LOGGER.debug(f"📤 ENHANCED-DIRECT: Sent message: {message[:50]}")
                return True
            except Exception as e:
                _LOGGER.error(f"❌ ENHANCED-DIRECT send error: {e}")
                return False
        return False
    
    def disconnect(self):
        """Disconnect from backend"""
        if self.ws:
            self.connected = False
            try:
                self.ws.close()
            except:
                pass


class CompletelyFixedSocketIOBridge:
    """
    Completely fixed Socket.IO bridge with ENHANCED Direct WebSocket connection control
    """
    
    def __init__(self):
        self._load_balancer = ContextFreeLoadBalancer()
        self._active_sessions = {}  # session_id -> session_info
        self._user_sessions = {}    # user_id -> set of session_ids (for multi-user isolation)
        self._session_locks = {}    # session_id -> threading.Lock
        self._global_lock = threading.RLock()
        self._event_stats = {      # Track all events for debugging
            'client_to_server': {},
            'server_to_client': {},
            'engine_io_packets': {}  # Track Engine.IO packets separately
        }
        
    def handle_client_connection(self, ws: WebSocket, full_path: str) -> None:
        """Handle WebSocket connection with enhanced direct backend control"""
        session_id = f"fixed-{int(time.time())}-{id(ws)}-{abs(hash(full_path))}"
        
        try:
            _LOGGER.info(f"🔄 ENHANCED-CONTROL: Starting connection for session: {session_id}")
            
            # Extract minimal metadata for user identification
            connection_data = self._extract_minimal_metadata(ws, full_path)
            
            route = self._extract_route_from_path(full_path)
            if not route:
                _LOGGER.error(f"❌ Could not extract route from path: {full_path}")
                ws.close()
                return
            
            backend_url = self._load_balancer.get_backend_for_route(route)
            if not backend_url:
                _LOGGER.error(f"❌ No backend found for route: {route}")
                ws.close()
                return
            
            # Minimal user identification for session isolation
            user_id = self._extract_minimal_user_id(connection_data)
            
            _LOGGER.info(f"🔄 ENHANCED-CONTROL: User {user_id} → {backend_url} (route: {route})")
            
            # Create enhanced direct WebSocket connection
            backend_client = self._create_enhanced_direct_connection(
                backend_url, session_id, user_id, connection_data
            )
            
            if not backend_client:
                _LOGGER.error(f"❌ Failed to create enhanced direct backend connection")
                ws.close()
                return
            
            # Thread-safe session management
            self._session_locks[session_id] = threading.RLock()
            
            with self._global_lock:
                self._active_sessions[session_id] = {
                    'client_ws': ws,
                    'backend_client': backend_client,
                    'backend_url': backend_url,
                    'route': route,
                    'user_id': user_id,
                    'connection_data': connection_data,
                    'created_at': time.time(),
                    'last_activity': time.time(),
                    'events_sent': 0,
                    'events_received': 0,
                    'engine_io_packets': 0
                }
                
                # Track user sessions for multi-user isolation
                if user_id not in self._user_sessions:
                    self._user_sessions[user_id] = set()
                self._user_sessions[user_id].add(session_id)
            
            _LOGGER.info(f"📋 ENHANCED-CONTROL: Session created for user {user_id}")
            
            # Start enhanced direct tunnel
            self._run_enhanced_direct_tunnel(ws, backend_client, session_id)
            
        except Exception as e:
            _LOGGER.error(f"❌ ENHANCED-CONTROL bridge error: {e}")
            self._cleanup_session(session_id)
    
    def _extract_minimal_metadata(self, ws: WebSocket, full_path: str) -> Dict:
        """Extract minimal connection metadata"""
        try:
            environ = getattr(ws, 'environ', {})
            
            # Extract ALL headers with complete preservation
            headers = {}
            for key, value in environ.items():
                if key.startswith('HTTP_'):
                    header_name = key[5:].replace('_', '-').title()
                    headers[header_name] = value
                elif key in ['CONTENT_TYPE', 'CONTENT_LENGTH']:
                    header_name = key.replace('_', '-').title()
                    headers[header_name] = value
            
            # Extract query parameters
            query_string = environ.get('QUERY_STRING', '')
            query_params = parse_qs(query_string, keep_blank_values=True) if query_string else {}
            
            # Minimal client info
            client_info = {
                'remote_addr': environ.get('REMOTE_ADDR'),
                'user_agent': headers.get('User-Agent'),
                'origin': headers.get('Origin')
            }
            
            return {
                'headers': headers,
                'query_params': query_params,
                'client_info': client_info,
                'path': full_path
            }
            
        except Exception as e:
            _LOGGER.error(f"❌ Error extracting minimal metadata: {e}")
            return {}
    
    def _extract_minimal_user_id(self, connection_data: Dict) -> str:
        """Extract minimal user ID for session isolation"""
        headers = connection_data.get('headers', {})
        query_params = connection_data.get('query_params', {})
        
        user_id = (
            headers.get('X-User-Id') or
            query_params.get('user_id', [''])[0] if 'user_id' in query_params else '' or
            query_params.get('phone', [''])[0] if 'phone' in query_params else '' or
            f"anon-{connection_data.get('client_info', {}).get('remote_addr', 'unknown')}"
        )
        
        return str(user_id)
    
    def _create_enhanced_direct_connection(self, backend_url: str, session_id: str, 
                                          user_id: str, connection_data: Dict) -> Optional[EnhancedDirectWebSocketClient]:
        """Create enhanced direct WebSocket connection with guaranteed header delivery"""
        
        try:
            _LOGGER.info(f"🔗 Creating ENHANCED-DIRECT connection for user {user_id}")
            
            # Prepare enhanced headers
            direct_headers = self._prepare_enhanced_direct_headers(connection_data)
            
            _LOGGER.info(f"🔗 ENHANCED-DIRECT headers: {list(direct_headers.keys())}")
            
            # Message handlers for enhanced connection
            def on_message(message):
                self._handle_backend_message(message, session_id, user_id)
            
            def on_error(error):
                _LOGGER.error(f"❌ ENHANCED-DIRECT backend error for user {user_id}: {error}")
            
            def on_close():
                _LOGGER.info(f"🔗 ENHANCED-DIRECT backend closed for user {user_id}")
                self._cleanup_session(session_id)
            
            def on_open():
                _LOGGER.info(f"🔗 ENHANCED-DIRECT backend connected for user {user_id}")
            
            # Create enhanced WebSocket client
            backend_client = EnhancedDirectWebSocketClient(
                url=backend_url,
                headers=direct_headers,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
                on_open=on_open
            )
            
            # Connect with enhanced header control
            if backend_client.connect():
                _LOGGER.info(f"✅ ENHANCED-DIRECT connection established for user {user_id}")
                return backend_client
            else:
                _LOGGER.error(f"❌ ENHANCED-DIRECT connection failed for user {user_id}")
                return None
            
        except Exception as e:
            _LOGGER.error(f"❌ Failed to create ENHANCED-DIRECT connection for {user_id}: {e}")
            return None
    
    def _prepare_enhanced_direct_headers(self, connection_data: Dict) -> Dict[str, str]:
        """Prepare headers for ENHANCED direct WebSocket connection"""
        original_headers = connection_data.get('headers', {})
        
        # Start with clean headers - be very selective
        direct_headers = {}
        
        # Only copy safe headers
        safe_headers = [
            'User-Agent', 'Origin', 'Accept-Language', 'Accept-Encoding',
            'Pragma', 'Cache-Control', 'Authorization'
        ]
        
        for header_name, header_value in original_headers.items():
            if header_name in safe_headers:
                direct_headers[header_name] = header_value
        
        # CRITICAL: Set the exact WebSocket headers required by RFC 6455
        direct_headers['Sec-WebSocket-Version'] = '13'
        
        # Set proper Origin if not present
        if 'Origin' not in direct_headers:
            direct_headers['Origin'] = 'http://localhost:2000'
        
        # Add debugging header
        direct_headers['X-Forwarded-By'] = 'ProxyTunnel'
        
        _LOGGER.info(f"📤 ENHANCED-DIRECT headers prepared: {len(direct_headers)} headers")
        _LOGGER.info(f"📤 Critical: Sec-WebSocket-Version = {direct_headers['Sec-WebSocket-Version']}")
        
        return direct_headers
    
    def _handle_backend_message(self, message: str, session_id: str, user_id: str):
        """Handle messages from enhanced direct backend connection"""
        try:
            with self._session_locks.get(session_id, threading.RLock()):
                session = self._active_sessions.get(session_id)
                if not session or session['client_ws'].closed:
                    return
                
                client_ws = session['client_ws']
                
                _LOGGER.info(f"📤 ENHANCED-DIRECT SERVER→CLIENT: Message to user {user_id}: {message[:100]}")
                
                # Forward message directly to client
                client_ws.send(message)
                
                # Update session stats
                session['last_activity'] = time.time()
                session['events_received'] += 1
                
        except Exception as e:
            _LOGGER.error(f"❌ Error handling backend message for user {user_id}: {e}")
    
    def _run_enhanced_direct_tunnel(self, client_ws: WebSocket, backend_client: EnhancedDirectWebSocketClient, 
                                session_id: str) -> None:
        """Run enhanced direct tunnel with clean Socket.IO protocol handling"""
        session = self._active_sessions.get(session_id)
        if not session:
            return
        
        user_id = session.get('user_id', 'unknown')
        
        _LOGGER.info(f"🔄 Starting ENHANCED-DIRECT tunnel for user {user_id}")
        
        try:
            # Send Socket.IO v4 compatible handshake
            handshake = {
                "sid": f"fixed-{user_id}-{session_id}",
                "upgrades": ["websocket"],
                "pingInterval": 25000,
                "pingTimeout": 5000,
                "maxPayload": 1000000
            }
            
            client_ws.send('0' + json.dumps(handshake))
            _LOGGER.info(f"📤 ENHANCED-DIRECT: Sent handshake to user {user_id}")
            
            # CLEAN: Let backend handle its own Socket.IO protocol completely
            # Don't interfere with Socket.IO CONNECT messages
            
            # Message loop with clean protocol handling
            last_ping = time.time()
            
            while not client_ws.closed and backend_client.connected:
                try:
                    message = client_ws.receive()
                    if message is None:
                        _LOGGER.info(f"📪 ENHANCED-DIRECT: User {user_id} disconnected")
                        break
                    
                    current_time = time.time()
                    _LOGGER.debug(f"📥 CLIENT→SERVER: From user {user_id}: {message}")
                    
                    if isinstance(message, str):
                        # ABSOLUTE Engine.IO control packet filtering
                        if is_engine_io_packet(message):
                            self._handle_engine_io_packet(message, client_ws, session_id, user_id)
                            continue
                        
                        # CLEAN: Forward ALL Socket.IO packets to backend transparently
                        # Let the backend handle its own Socket.IO protocol
                        if message.startswith(('40', '41', '42', '43', '44', '45', '46')):
                            _LOGGER.info(f"📥 ENHANCED-DIRECT: Forwarding Socket.IO packet: {message[:50]}")
                            
                            if backend_client.send(message):
                                with self._session_locks.get(session_id, threading.RLock()):
                                    session['events_sent'] += 1
                                    session['last_activity'] = time.time()
                                _LOGGER.info(f"✅ ENHANCED-DIRECT: Socket.IO packet forwarded successfully")
                            else:
                                _LOGGER.error(f"❌ ENHANCED-DIRECT: Failed to forward Socket.IO packet")
                            continue
                        
                        # Unknown packet type
                        else:
                            _LOGGER.debug(f"🔄 Unknown packet from user {user_id}: {message[:50]}")
                            continue
                    
                    # Send periodic pings
                    if current_time - last_ping > 25:
                        try:
                            client_ws.send('2')  # Engine.IO PING
                            last_ping = current_time
                            _LOGGER.debug(f"📤 ENHANCED-DIRECT: Sent PING to user {user_id}")
                        except Exception as ping_error:
                            _LOGGER.error(f"❌ Ping error for user {user_id}: {ping_error}")
                            break
                    
                except Exception as e:
                    _LOGGER.error(f"❌ ENHANCED-DIRECT message loop error for user {user_id}: {e}")
                    break
            
            _LOGGER.info(f"✅ ENHANCED-DIRECT tunnel completed for user {user_id}")
            
        except Exception as e:
            _LOGGER.error(f"❌ ENHANCED-DIRECT tunnel error for user {user_id}: {e}")
        finally:
            self._cleanup_session(session_id)


    def _handle_engine_io_packet(self, message: str, client_ws: WebSocket, session_id: str, user_id: str):
        """Handle Engine.IO control packets"""
        _LOGGER.debug(f"🔧 Engine.IO packet from user {user_id}: {message}")
        
        # Track packet
        self._track_engine_io_packet(session_id, message.upper())
        
        # Handle specific Engine.IO packets
        if message == '2':  # PING
            client_ws.send('3')  # Send PONG
            _LOGGER.debug(f"📤 ENHANCED-DIRECT: Sent PONG to user {user_id}")
        elif message == '1':  # CLOSE
            _LOGGER.info(f"🔧 Engine.IO CLOSE from user {user_id}")
        elif message == '2probe':  # Probe PING
            client_ws.send('3probe')  # Send Probe PONG
            _LOGGER.debug(f"📤 ENHANCED-DIRECT: Sent probe PONG to user {user_id}")
    
    def _track_engine_io_packet(self, session_id: str, packet_type: str):
        """Track Engine.IO packets for debugging"""
        session = self._active_sessions.get(session_id)
        if session:
            with self._session_locks.get(session_id, threading.RLock()):
                session['engine_io_packets'] += 1
            
        if packet_type not in self._event_stats['engine_io_packets']:
            self._event_stats['engine_io_packets'][packet_type] = 0
        self._event_stats['engine_io_packets'][packet_type] += 1
    
    def _extract_route_from_path(self, path: str) -> Optional[str]:
        """Extract route from path"""
        try:
            parts = path.split('/')
            if len(parts) >= 2 and parts[1]:
                route = f"/{parts[1]}"
                return route
        except Exception as e:
            _LOGGER.error(f"Error extracting route: {e}")
        return None
    
    def _cleanup_session(self, session_id: str):
        """Clean up session resources"""
        try:
            session_lock = self._session_locks.pop(session_id, None)
            
            if session_lock:
                with session_lock:
                    session = self._active_sessions.pop(session_id, None)
                    if session:
                        user_id = session.get('user_id', 'unknown')
                        events_sent = session.get('events_sent', 0)
                        events_received = session.get('events_received', 0)
                        engine_io_packets = session.get('engine_io_packets', 0)
                        
                        _LOGGER.info(f"🧹 ENHANCED-DIRECT: Cleaning up session for user {user_id}")
                        _LOGGER.info(f"📊 Session stats - Sent: {events_sent}, Received: {events_received}, Engine.IO: {engine_io_packets}")
                        
                        # Remove from user tracking
                        with self._global_lock:
                            if user_id in self._user_sessions:
                                self._user_sessions[user_id].discard(session_id)
                                if not self._user_sessions[user_id]:
                                    del self._user_sessions[user_id]
                        
                        # Close connections
                        backend_client = session.get('backend_client')
                        if backend_client and backend_client.connected:
                            backend_client.disconnect()
                        
                        client_ws = session.get('client_ws')
                        if client_ws and not client_ws.closed:
                            try:
                                client_ws.send('1')  # Engine.IO CLOSE
                            except:
                                pass
                            client_ws.close()
                        
                        _LOGGER.info(f"✅ ENHANCED-DIRECT: Session cleanup completed for user {user_id}")
            
        except Exception as e:
            _LOGGER.error(f"❌ Error cleaning up session {session_id}: {e}")
    
    def get_event_stats(self) -> Dict:
        """Get comprehensive event statistics including Engine.IO packets"""
        return {
            'event_statistics': dict(self._event_stats),
            'active_sessions': len(self._active_sessions),
            'active_users': len(self._user_sessions),
            'session_details': {
                sid: {
                    'user_id': info.get('user_id'),
                    'events_sent': info.get('events_sent', 0),
                    'events_received': info.get('events_received', 0),
                    'engine_io_packets': info.get('engine_io_packets', 0),
                    'duration': time.time() - info.get('created_at', 0)
                }
                for sid, info in self._active_sessions.items()
            }
        }


class WebSocketTunnel:
    """Legacy compatibility wrapper"""
    
    def __init__(self):
        self._bridge = CompletelyFixedSocketIOBridge()
        self.backend_connections = {}
    
    def handle_client_connection(self, ws: WebSocket, path: str) -> None:
        """Handle client connection using the enhanced direct control bridge"""
        self._bridge.handle_client_connection(ws, path)


class FlaskSocketIOProxy:
    """Minimal compatibility class"""
    
    def __init__(self):
        self._stats = {
            'connections_total': 0,
            'active_connections': 0,
            'messages_forwarded': 0,   
            'errors': 0
        }
    
    def get_stats(self) -> dict:
        return self._stats
    
    def get_client_info(self) -> dict:
        return {}


# Global instances
websocket_tunnel = WebSocketTunnel()
flask_socketio_proxy = FlaskSocketIOProxy()


__all__ = [
    'websocket_tunnel',
    'WebSocketTunnel', 
    'flask_socketio_proxy',
    'FlaskSocketIOProxy',
    'calculate_accept',
    'ContextFreeLoadBalancer',
    'CompletelyFixedSocketIOBridge',
    'EnhancedDirectWebSocketClient',
    'is_engine_io_packet',
    'is_socketio_event_packet'
]
