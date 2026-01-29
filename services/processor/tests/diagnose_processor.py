#!/usr/bin/env python3
"""Diagnostic script for Kitsu processor service.

Tests processor connectivity, endpoints, and status using AYON API.
"""

import os
import sys
from pathlib import Path
from datetime import datetime, timedelta

import httpx


def load_env_file(env_path: Path) -> dict:
    """Load environment variables from .env file."""
    env_vars = {}
    if env_path.exists():
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    env_vars[key] = value
    return env_vars


def get_addon_version() -> str:
    """Get addon version from package.py."""
    script_dir = Path(__file__).parent
    package_py = script_dir.parent.parent / "package.py"
    
    if not package_py.exists():
        return "1.2.6-dev.6"  # fallback
    
    with open(package_py, "r") as f:
        content = f.read()
        # Simple extraction
        for line in content.split("\n"):
            if line.strip().startswith("version"):
                try:
                    version = line.split("=")[1].strip().strip('"').strip("'")
                    return version
                except:
                    pass
    return "1.2.6-dev.6"


def test_endpoint(
    client: httpx.Client,
    method: str,
    url: str,
    description: str,
    **kwargs
) -> dict:
    """Test an endpoint and return results."""
    print(f"\n{'='*60}")
    print(f"Testing: {description}")
    print(f"URL: {url}")
    print(f"Method: {method}")
    print(f"{'='*60}")
    
    try:
        response = client.request(method, url, **kwargs)
        result = {
            "success": response.status_code < 400,
            "status_code": response.status_code,
            "url": url,
            "description": description,
        }
        
        try:
            result["data"] = response.json()
        except:
            result["text"] = response.text[:500]  # First 500 chars
        
        if result["success"]:
            print(f"[OK] SUCCESS (Status: {response.status_code})")
            if "data" in result:
                print(f"Response: {result['data']}")
            else:
                print(f"Response: {result.get('text', 'No content')[:200]}")
        else:
            print(f"[FAIL] FAILED (Status: {response.status_code})")
            if "data" in result:
                print(f"Error: {result['data']}")
            else:
                print(f"Error: {result.get('text', 'No error message')[:200]}")
        
        return result
        
    except Exception as e:
        print(f"[ERROR] EXCEPTION: {e}")
        return {
            "success": False,
            "error": str(e),
            "url": url,
            "description": description,
        }


def main():
    """Main diagnostic function."""
    script_dir = Path(__file__).parent
    services_dir = script_dir.parent
    env_file = services_dir / ".env"
    
    print("="*60)
    print("Kitsu Processor Diagnostic Script")
    print("="*60)
    
    # Load environment variables
    env_vars = load_env_file(env_file)
    
    if not env_file.exists():
        print(f"\nWARNING: .env file not found at {env_file}")
        print("Using system environment variables...")
    
    # Get required variables
    server_url = env_vars.get("AYON_SERVER_URL") or os.environ.get("AYON_SERVER_URL")
    api_key = env_vars.get("AYON_API_KEY") or os.environ.get("AYON_API_KEY")
    addon_version = get_addon_version()
    
    if not server_url:
        print("\nERROR: AYON_SERVER_URL not found in .env or environment")
        sys.exit(1)
    
    if not api_key:
        print("\nERROR: AYON_API_KEY not found in .env or environment")
        sys.exit(1)
    
    print(f"\nConfiguration:")
    print(f"  Server URL: {server_url}")
    print(f"  API Key: {api_key[:20]}...")
    print(f"  Addon Version: {addon_version}")
    
    # Setup HTTP client
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    
    base_url = server_url.rstrip("/")
    addon_base = f"{base_url}/api/addons/kitsu/{addon_version}"
    
    results = []
    
    with httpx.Client(
        headers=headers,
        timeout=30.0,
        follow_redirects=True
    ) as client:
        
        # Test 1: Server health
        results.append(
            test_endpoint(
                client, "GET", f"{base_url}/api/health",
                "AYON Server Health Check"
            )
        )
        
        # Test 2: Pairing endpoint (critical for processor)
        results.append(
            test_endpoint(
                client, "GET", f"{addon_base}/pairing",
                "Pairing List Endpoint (processor needs this)"
            )
        )
        
        # Test 3: Processor status endpoint
        results.append(
            test_endpoint(
                client, "GET", f"{addon_base}/processor/status",
                "Processor Status Endpoint"
            )
        )
        
        # Test 4: Event handler status endpoint
        results.append(
            test_endpoint(
                client, "GET", f"{addon_base}/event-handler/status",
                "Event Handler Status Endpoint"
            )
        )
        
        # Test 5: Check for processor events via GraphQL
        print(f"\n{'='*60}")
        print("Checking for processor events...")
        print(f"{'='*60}")
        
        # Query for recent processor events
        query = """
        query GetProcessorEvents {
            events(
                topics: ["addon.kitsu.processor.%"]
                limit: 10
                orderBy: CREATED_AT_DESC
            ) {
                edges {
                    node {
                        id
                        topic
                        description
                        createdAt
                        summary
                        status
                    }
                }
            }
        }
        """
        
        try:
            graphql_response = client.post(
                f"{base_url}/api/graphql",
                json={"query": query}
            )
            
            if graphql_response.status_code == 200:
                graphql_data = graphql_response.json()
                if "data" in graphql_data and "events" in graphql_data["data"]:
                    events = graphql_data["data"]["events"]["edges"]
                    print(f"[OK] Found {len(events)} recent processor events")
                    for edge in events[:5]:  # Show first 5
                        event = edge["node"]
                        print(f"\n  Event: {event['topic']}")
                        print(f"    Created: {event['createdAt']}")
                        print(f"    Status: {event['status']}")
                        print(f"    Description: {event.get('description', 'N/A')[:100]}")
                else:
                    print("[FAIL] No processor events found")
                    print(f"Response: {graphql_data}")
            else:
                print(f"[FAIL] GraphQL query failed: {graphql_response.status_code}")
                print(f"Response: {graphql_response.text[:200]}")
        except Exception as e:
            print(f"[ERROR] Exception querying events: {e}")
        
        # Test 6: Check for pending sync jobs
        sync_query = """
        query GetPendingSyncJobs {
            events(
                topics: ["kitsu.sync_request"]
                statuses: [PENDING, IN_PROGRESS]
                limit: 10
            ) {
                edges {
                    node {
                        id
                        topic
                        status
                        createdAt
                        summary
                    }
                }
            }
        }
        """
        
        try:
            sync_response = client.post(
                f"{base_url}/api/graphql",
                json={"query": sync_query}
            )
            
            if sync_response.status_code == 200:
                sync_data = sync_response.json()
                if "data" in sync_data and "events" in sync_data["data"]:
                    jobs = sync_data["data"]["events"]["edges"]
                    print(f"\n{'='*60}")
                    print(f"Pending Sync Jobs: {len(jobs)}")
                    print(f"{'='*60}")
                    for edge in jobs:
                        job = edge["node"]
                        print(f"  Job ID: {job['id']}")
                        print(f"    Status: {job['status']}")
                        print(f"    Created: {job['createdAt']}")
                else:
                    print(f"\nNo pending sync jobs found")
        except Exception as e:
            print(f"\nException checking sync jobs: {e}")
    
    # Summary
    print(f"\n{'='*60}")
    print("DIAGNOSTIC SUMMARY")
    print(f"{'='*60}")
    
    successful = sum(1 for r in results if r.get("success"))
    total = len(results)
    
    print(f"\nEndpoints tested: {successful}/{total}")
    
    for result in results:
        status = "[OK]" if result.get("success") else "[FAIL]"
        print(f"{status} {result['description']}")
        if not result.get("success"):
            if "error" in result:
                print(f"    Error: {result['error']}")
            elif "data" in result and isinstance(result["data"], dict):
                if "detail" in result["data"]:
                    print(f"    Detail: {result['data']['detail']}")
    
    print(f"\n{'='*60}")
    print("RECOMMENDATIONS")
    print(f"{'='*60}")
    
    pairing_result = next((r for r in results if "pairing" in r["description"].lower()), None)
    if pairing_result and not pairing_result.get("success"):
        print("\n[WARNING] CRITICAL: Pairing endpoint is not accessible!")
        print("   The processor requires this endpoint to initialize.")
        print("   Check if the addon is properly deployed and the server is running.")
    
    processor_status_result = next((r for r in results if "processor status" in r["description"].lower()), None)
    if processor_status_result and not processor_status_result.get("success"):
        print("\n[WARNING] Processor status endpoint not found.")
        print("   This is expected if the addon package hasn't been updated.")
        print("   Check processor logs directly for initialization issues.")
    
    print("\nNext steps:")
    print("1. Check processor service logs for initialization errors")
    print("2. Verify Kitsu settings/secrets are configured in AYON")
    print("3. Verify processor service is actually running (not just container)")
    print("4. Check network connectivity from processor container to AYON server")


if __name__ == "__main__":
    main()
