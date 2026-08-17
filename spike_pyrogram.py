import os
import time
import asyncio
import json
from pyrogram import Client
from pyrogram.errors import RPCError

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

# Same TEST_CASES configuration as Telethon (omitted here for brevity, assume exact same dict)
TEST_CASES = {
    # ... Same as above
}

async def check_tcp(server: str, port: int, timeout: float = 3.0):
    # Same TCP ping logic as above
    pass 

async def run_test(case_name, config):
    print(f"--- Running Pyrogram Test: {case_name} ---")
    result = {
        "library": "pyrogram",
        "case": case_name,
        "tcp_connect_ms": None,
        "mtproto_connect_ms": None,
        "total_duration_ms": None,
        "state": "FAILED",
        "error_category": None,
        "exception_type": None
    }
    
    t0 = time.monotonic()
    
    # 1. TCP Measurement
    tcp_ok, tcp_dur, tcp_err = await check_tcp(config["server"], config["port"])
    result["tcp_connect_ms"] = round(tcp_dur * 1000, 2)
    if not tcp_ok:
        result["error_category"] = tcp_err
        result["total_duration_ms"] = round((time.monotonic() - t0) * 1000, 2)
        return result
        
    result["state"] = "TCP_CONNECTED"

    # 2. MTProto Setup
    proxy_dict = {
        "scheme": "mtproxy",
        "hostname": config["server"],
        "port": config["port"],
        "secret": config["secret"]
    }
    
    client = Client(f"memory_{case_name}", api_id=API_ID, api_hash=API_HASH, proxy=proxy_dict, in_memory=True)
    
    t1 = time.monotonic()
    try:
        # Connect to MTProto
        connected = await asyncio.wait_for(client.connect(), timeout=5.0)
        dur_mtp = time.monotonic() - t1
        result["mtproto_connect_ms"] = round(dur_mtp * 1000, 2)
        
        if connected:
            result["state"] = "MT_PROTO_CONNECTED"
            result["error_category"] = "SUCCESS"
            
    except asyncio.TimeoutError:
        result["error_category"] = "MTPROXY_HANDSHAKE_FAILED"
        result["exception_type"] = "TimeoutError"
    except OSError as e: # Pyrogram often surfaces raw OSErrors for socket drops
        result["error_category"] = "MTPROXY_HANDSHAKE_FAILED"
        result["exception_type"] = e.__class__.__name__
    except RPCError as e:
        result["error_category"] = "TELEGRAM_CONNECTION_FAILED"
        result["exception_type"] = e.__class__.__name__
    except Exception as e:
        result["error_category"] = "UNKNOWN"
        result["exception_type"] = e.__class__.__name__
    finally:
        if client.is_connected:
            await client.disconnect()
        result["total_duration_ms"] = round((time.monotonic() - t0) * 1000, 2)
        
    return result

# ... (main function identical to Telethon, outputs to pyrogram_results.json)