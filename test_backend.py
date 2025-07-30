# test_backend.py
import requests
import socketio

# Test HTTP connectivity
try:
    response = requests.get("http://localhost:3000/socket.io/", timeout=5)
    print(f"✅ Backend HTTP reachable: {response.status_code}")
except Exception as e:
    print(f"❌ Backend HTTP failed: {e}")

# Test direct Socket.IO connection
try:
    sio = socketio.Client()
    sio.connect('http://localhost:3000', wait_timeout=10)
    print(f"✅ Direct Socket.IO connection successful: {sio.sid}")
    sio.disconnect()
except Exception as e:
    print(f"❌ Direct Socket.IO failed: {e}")
