#!/usr/bin/env python3
"""
Emergency script to clear stuck queue items in Sentinel.
Run this when the queue is blocked and users can't generate.

Usage:
    python clear_stuck_queue.py [--admin-token TOKEN] [--url URL]
    
Or set environment variables:
    SENTINEL_ADMIN_TOKEN=your_token
    SENTINEL_URL=http://localhost:8188
"""

import sys
import os
import json
import argparse
try:
    import requests
except ImportError:
    print("ERROR: requests library not installed. Install it with: pip install requests")
    sys.exit(1)

def clear_stuck_queue(admin_token=None, url="http://localhost:8188"):
    """Clear all stuck items from the queue."""
    
    if not admin_token:
        admin_token = os.environ.get('SENTINEL_ADMIN_TOKEN')
        if not admin_token:
            print("ERROR: No admin token provided.")
            print("Usage: python clear_stuck_queue.py --admin-token YOUR_TOKEN")
            print("Or set SENTINEL_ADMIN_TOKEN environment variable")
            return False
    
    # First, check what's stuck
    print("Checking for stuck items...")
    try:
        response = requests.get(
            f"{url}/sentinel/queue/stuck",
            cookies={"jwt_token": admin_token},
            timeout=10
        )
        if response.status_code == 200:
            data = response.json()
            stuck_count = data.get("count", 0)
            stuck_items = data.get("stuck_items", [])
            print(f"Found {stuck_count} stuck item(s):")
            for item in stuck_items:
                print(f"  - Item ID: {item.get('item_id')}, Prompt ID: {item.get('prompt_id')}, User: {item.get('username') or item.get('user_id')}")
        else:
            print(f"Warning: Could not check stuck items (status {response.status_code})")
    except Exception as e:
        print(f"Warning: Could not check stuck items: {e}")
    
    # Clear all stuck items
    print("\nClearing all stuck items...")
    try:
        response = requests.post(
            f"{url}/sentinel/queue/emergency_clear",
            cookies={"jwt_token": admin_token},
            timeout=10
        )
        
        if response.status_code == 200:
            data = response.json()
            cleared_count = data.get("cleared_count", 0)
            print(f"SUCCESS: Cleared {cleared_count} stuck item(s) from queue")
            return True
        elif response.status_code == 401:
            print("ERROR: Authentication failed. Check your admin token.")
            return False
        elif response.status_code == 403:
            print("ERROR: Access denied. You must be an admin user.")
            return False
        else:
            print(f"ERROR: Failed to clear stuck items (status {response.status_code})")
            print(f"Response: {response.text}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"ERROR: Could not connect to server: {e}")
        print(f"Make sure ComfyUI is running at {url}")
        return False
    except Exception as e:
        print(f"ERROR: Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Clear stuck queue items in Sentinel")
    parser.add_argument("--admin-token", help="Admin JWT token")
    parser.add_argument("--url", default="http://localhost:8188", help="ComfyUI server URL")
    args = parser.parse_args()
    
    success = clear_stuck_queue(args.admin_token, args.url)
    sys.exit(0 if success else 1)

