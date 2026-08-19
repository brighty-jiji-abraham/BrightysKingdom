#!/usr/bin/env python3
"""
Test script for API key authentication
"""
import os
import sys

import requests
import json

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_URL = os.getenv("PROXY_BASE_URL", "http://localhost:2000")

# Read from .env rather than hardcoding. The old literal was the placeholder
# from .env.example, so this script silently passed against an unconfigured
# proxy and failed against a properly configured one.
MASTER_API_KEY = os.getenv("MASTER_API_KEY", "")
if not MASTER_API_KEY:
    sys.exit("MASTER_API_KEY is not set - put it in .env or export it first.")

def test_create_api_key():
    """Test creating a new API key"""
    print("🔑 Testing API key creation...")
    
    headers = {'X-API-Key': MASTER_API_KEY}
    data = {
        'user_id': 'test_user',
        'name': 'Test API Key'
    }
    
    try:
        response = requests.post(
            f"{BASE_URL}/admin/api-keys",
            headers=headers,
            json=data
        )
        
        if response.status_code == 201:
            result = response.json()
            print(f"✅ API key created: {result['api_key'][:10]}...")
            return result['api_key']
        else:
            print(f"❌ Failed to create API key: {response.status_code} - {response.text}")
            return None
            
    except Exception as e:
        print(f"❌ Error creating API key: {e}")
        return None

def test_validate_api_key(api_key):
    """Test validating an API key"""
    print(f"\n🔍 Testing API key validation...")
    
    data = {'api_key': api_key}
    
    try:
        response = requests.post(
            f"{BASE_URL}/admin/api-keys/validate",
            json=data
        )
        
        result = response.json()
        if result.get('valid'):
            print(f"✅ API key is valid for user: {result['user_id']}")
        else:
            print("❌ API key is invalid")
            
    except Exception as e:
        print(f"❌ Error validating API key: {e}")

def test_authenticated_request(api_key):
    """Test making authenticated requests"""
    print(f"\n🔐 Testing authenticated requests...")
    
    headers = {'X-API-Key': api_key}
    
    try:
        # Test admin config endpoint (requires admin key)
        response = requests.get(f"{BASE_URL}/admin/config", headers=headers)
        if response.status_code == 403:
            print("✅ Non-admin key correctly blocked from admin endpoint")
        
        # Test metrics endpoint (optional auth)
        response = requests.get(f"{BASE_URL}/admin/metrics", headers=headers)
        print(f"📊 Metrics endpoint: {response.status_code}")
        
    except Exception as e:
        print(f"❌ Error testing authenticated requests: {e}")

def test_proxy_with_api_key(api_key):
    """Test proxy requests with API key"""
    print(f"\n🔄 Testing proxy with API key...")
    
    headers = {'X-API-Key': api_key}
    
    routes = ['/app1/test', '/app2/test', '/api/test']
    
    for route in routes:
        try:
            response = requests.get(f"{BASE_URL}{route}", headers=headers, timeout=5)
            print(f"📤 {route}: {response.status_code}")
        except requests.exceptions.ConnectionError:
            print(f"🔌 {route}: Backend not available (expected)")
        except Exception as e:
            print(f"❌ {route} failed: {e}")

def test_list_api_keys():
    """Test listing API keys"""
    print(f"\n📋 Testing API key listing...")
    
    headers = {'X-API-Key': MASTER_API_KEY}
    
    try:
        response = requests.get(f"{BASE_URL}/admin/api-keys", headers=headers)
        
        if response.status_code == 200:
            result = response.json()
            print(f"✅ Found {len(result['api_keys'])} API keys")
            for key_info in result['api_keys']:
                print(f"   👤 {key_info['user_id']}: {key_info['name']}")
        else:
            print(f"❌ Failed to list API keys: {response.status_code}")
            
    except Exception as e:
        print(f"❌ Error listing API keys: {e}")

if __name__ == '__main__':
    print("🧪 Starting API Key Authentication Tests")
    print("=" * 50)
    
    # Test API key creation
    api_key = test_create_api_key()
    
    if api_key:
        # Test validation
        test_validate_api_key(api_key)
        
        # Test authenticated requests
        test_authenticated_request(api_key)
        
        # Test proxy with API key
        test_proxy_with_api_key(api_key)
    
    # Test listing keys
    test_list_api_keys()
    
    print("\n" + "=" * 50)
    print("🏁 API Key tests completed!")
    print(f"\nYour master API key: {MASTER_API_KEY}")
    print("\nAPI Key Usage:")
    print("  Header: X-API-Key: your-api-key-here")
    print("  Query: ?api_key=your-api-key-here")
    print("  Bearer: Authorization: Bearer your-api-key-here")
