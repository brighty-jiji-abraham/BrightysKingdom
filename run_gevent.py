"""
Gevent server with COMPLETE Socket.IO WebSocket handling
"""
import os
os.environ["EVENTLET_NO_GREENDNS"] = "yes"

from gevent import monkey
monkey.patch_all()

from gevent.pywsgi import WSGIServer
from geventwebsocket.handler import WebSocketHandler
import base64
import hashlib
from main import app

class CompleteSocketIOHandler(WebSocketHandler):
    """Complete Socket.IO WebSocket handler that bypasses Flask routing"""
    
    def upgrade_websocket(self):
        """WebSocket upgrade with immediate Socket.IO handling.

        NOTE: this overrides an *internal* geventwebsocket method —
        WebSocketHandler.run_application() calls self.upgrade_websocket() for
        every WebSocket upgrade it sees, not just the ones handle_one_response
        intercepts below. So without the guard, the hand-rolled handshake here
        would also fire for non-Socket.IO sockets (e.g. /ollama-agent), writing
        a second raw 101 response on top of geventwebsocket's own. The client
        then reads that HTTP text as a frame and dies with "rsv is not
        implemented", and pywsgi trips `assert self.result is None`.

        Anything that isn't Socket.IO goes back to the stock implementation.
        """
        if '/socket.io/' not in self.environ.get('PATH_INFO', ''):
            return super().upgrade_websocket()

        print(f"🔧 GEVENT: Starting complete Socket.IO WebSocket upgrade")

        try:
            # Validate WebSocket headers
            ws_key = self.headers.get('Sec-WebSocket-Key')
            ws_version = self.headers.get('Sec-WebSocket-Version')
            
            if not ws_key or ws_version != '13':
                print(f"🔧 GEVENT: Invalid WebSocket headers")
                return None
            
            # Calculate accept key and send upgrade response
            WEBSOCKET_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
            accept_key = base64.b64encode(
                hashlib.sha1((ws_key + WEBSOCKET_MAGIC).encode()).digest()
            ).decode()
            
            response_headers = [
                "HTTP/1.1 101 Switching Protocols",
                "Upgrade: websocket", 
                "Connection: Upgrade",
                f"Sec-WebSocket-Accept: {accept_key}",
                "Access-Control-Allow-Origin: *",
                "Access-Control-Allow-Credentials: true",
                "", ""
            ]
            
            response = "\r\n".join(response_headers).encode()
            self.socket.sendall(response)
            
            print(f"🔧 GEVENT: WebSocket upgrade response sent")
            
            # Create WebSocket wrapper
            websocket_wrapper = WindowsWebSocketWrapper(self.socket, self.environ)
            self.environ['wsgi.websocket'] = websocket_wrapper
            
            print(f"🔧 GEVENT: ✅ Complete WebSocket upgrade successful!")
            return websocket_wrapper
            
        except Exception as e:
            print(f"🔧 GEVENT: WebSocket upgrade failed: {e}")
            return None
    
    def handle_one_response(self):
        """Handle WebSocket with immediate Socket.IO processing"""
        # Check if this is a Socket.IO WebSocket upgrade
        if (self.environ.get('HTTP_UPGRADE', '').lower() == 'websocket' and
            'upgrade' in self.environ.get('HTTP_CONNECTION', '').lower() and
            '/socket.io/' in self.environ.get('PATH_INFO', '')):
            
            print(f"🔧 GEVENT: Processing Socket.IO WebSocket upgrade")
            
            # Perform WebSocket upgrade
            websocket = self.upgrade_websocket()
            
            if websocket:
                print(f"🔧 GEVENT: Starting direct Socket.IO handling")
                
                try:
                    # Extract app from path
                    path = self.environ.get('PATH_INFO', '')
                    app_name = path.split('/')[1] if len(path.split('/')) > 1 else 'app1'
                    
                    print(f"🔧 GEVENT: Detected app: {app_name}")
                    
                    # Handle Socket.IO connection directly
                    self._handle_socketio_connection(websocket, app_name, path)
                    
                except Exception as e:
                    print(f"🔧 GEVENT: Socket.IO handling error: {e}")
                    try:
                        websocket.close()
                    except:
                        pass
                
                return  # Exit without further processing
            else:
                print(f"🔧 GEVENT: WebSocket upgrade failed, returning 400")
                self.start_response('400 Bad Request', [('Content-Type', 'text/plain')])
                self.result = [b'WebSocket upgrade failed']
                return
        else:
            # Regular HTTP request - use normal WSGI flow
            return super().handle_one_response()
    
    def _handle_socketio_connection(self, websocket, app_name, path):
        """Handle Socket.IO connection using existing websocket_tunnel"""
        print(f"🚀 GEVENT: Starting Socket.IO connection for {app_name}")
        
        try:
            # Import the existing websocket tunnel
            from proxy_server.core.websocket_tunnel import websocket_tunnel
            
            # Build full path for tunnel
            full_path = f"/{app_name}/socket.io/"
            if self.environ.get('QUERY_STRING'):
                full_path += "?" + self.environ.get('QUERY_STRING')
            
            print(f"🚀 GEVENT: Using tunnel path: {full_path}")
            
            # Use existing websocket tunnel infrastructure
            websocket_tunnel.handle_client_connection(websocket, full_path)
            
            print(f"✅ GEVENT: Socket.IO connection completed successfully")
            
        except Exception as e:
            print(f"❌ GEVENT: Socket.IO connection error: {e}")
            try:
                websocket.close()
            except:
                pass


class WindowsWebSocketWrapper:
    """WebSocket wrapper compatible with existing websocket_tunnel"""
    
    def __init__(self, socket, environ):
        self.socket = socket
        self.environ = environ
        self.closed = False
        print(f"🔧 WRAPPER: Created compatible WebSocket wrapper")
    
    def send(self, data):
        """Send data through WebSocket"""
        if self.closed:
            raise Exception("WebSocket is closed")
        
        try:
            if isinstance(data, str):
                data = data.encode('utf-8')
            
            # WebSocket frame format
            frame = bytearray()
            frame.append(0x81)  # FIN + TEXT frame
            
            data_length = len(data)
            if data_length < 126:
                frame.append(data_length)
            elif data_length < 65536:
                frame.append(126)
                frame.extend(data_length.to_bytes(2, 'big'))
            else:
                frame.append(127)
                frame.extend(data_length.to_bytes(8, 'big'))
            
            frame.extend(data)
            self.socket.sendall(bytes(frame))
            print(f"🔧 WRAPPER: Sent {len(data)} bytes")
            
        except Exception as e:
            print(f"🔧 WRAPPER: Send error: {e}")
            self.closed = True
            raise
    
    def receive(self):
        """Receive data from WebSocket"""
        if self.closed:
            return None
        
        try:
            # Read WebSocket frame
            header = self.socket.recv(2)
            if len(header) < 2:
                self.closed = True
                return None
            
            # Parse frame
            opcode = header[0] & 0x0F
            masked = header[1] & 0x80
            payload_length = header[1] & 0x7F
            
            # Handle extended payload length
            if payload_length == 126:
                length_data = self.socket.recv(2)
                payload_length = int.from_bytes(length_data, 'big')
            elif payload_length == 127:
                length_data = self.socket.recv(8)
                payload_length = int.from_bytes(length_data, 'big')
            
            # Read mask if present
            mask = None
            if masked:
                mask = self.socket.recv(4)
            
            # Read payload
            payload = self.socket.recv(payload_length)
            
            # Unmask if necessary
            if masked and mask:
                payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
            
            # Handle different frame types
            if opcode == 0x8:  # Close frame
                self.closed = True
                return None
            elif opcode == 0x9:  # Ping frame
                # Send pong
                pong_frame = bytearray([0x8A, len(payload)])
                pong_frame.extend(payload)
                self.socket.sendall(bytes(pong_frame))
                return self.receive()  # Continue receiving
            elif opcode == 0x1:  # Text frame
                return payload.decode('utf-8')
            else:
                return payload
            
        except Exception as e:
            print(f"🔧 WRAPPER: Receive error: {e}")
            self.closed = True
            return None
    
    def close(self):
        """Close WebSocket connection"""
        if not self.closed:
            try:
                close_frame = bytes([0x88, 0x00])
                self.socket.sendall(close_frame)
                print(f"🔧 WRAPPER: WebSocket closed")
            except:
                pass
            finally:
                self.closed = True
                try:
                    self.socket.close()
                except:
                    pass


def main():
    print("🚀 Starting Flask Reverse Proxy Server with WebSocket Support")
    print("🔧 DNS Fix Applied: EVENTLET_NO_GREENDNS=yes")

    # Create server with complete Socket.IO handler
    http_server = WSGIServer(
        ('0.0.0.0', 2000),
        application=app,
        handler_class=CompleteSocketIOHandler,
        environ={'wsgi.multithread': True}
    )

    # Dial out to the public tunnel head, if one is configured. This is what
    # makes every local service behind this proxy reachable from the internet
    # without exposing this machine. No-op when TUNNEL_SERVER_URL is unset.
    from proxy_server.core.tunnel_client import start_in_background
    if start_in_background():
        print("🌍 Tunnel client started — local services are being published")

    try:
        print("✅ Server ready with COMPLETE Socket.IO WebSocket support")
        print("📍 Server: http://0.0.0.0:2000")
        print("🔧 Socket.IO endpoints:")
        print("   - WebSocket: ws://192.168.12.49:2000/app1/socket.io/?EIO=4&transport=websocket")
        print("   - HTTP polling: http://192.168.12.49:2000/app1/socket.io/?EIO=4&transport=polling")
        print("   - Proxy stats: http://192.168.12.49:2000/proxy-stats")
        http_server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 Server stopped by user")

if __name__ == '__main__':
    main()
