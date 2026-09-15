import os
import time
import asyncio
import json
from telethon import TelegramClient, network
from telethon.errors import RPCError

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

# Configuration matrix for our test scenarios
TEST_CASES = {
    "valid": {
        "server": os.getenv("PROXY_SERVER", "127.0.0.1"),
        "port": int(os.getenv("PROXY_PORT", 443)),
        "secret": os.getenv("PROXY_SECRET", "ee00000000000000000000000000000000676f6f676c652e636f6d")
    },
    "dead_endpoint": {"server": "198.51.100.1", "port": 443, "secret": "ee00000000000000000000000000000000"},
    "wrong_secret": {
        "server": os.getenv("PROXY_SERVER", "127.0.0.1"),
        "port": int(os.getenv("PROXY_PORT", 443)),
        "secret": "eeffffffffffffffffffffffffffffffff676f6f676c652e636f6d"
    },
    "non_mtproto": {"server": "1.1.1.1", "port": 80, "secret": "ee00000000000000000000000000000000"}
}

async def check_tcp(server: str, port: int, timeout: float = 3.0):
    start = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(server, port), timeout)
        writer.close()
        await writer.wait_closed()
        return True, time.monotonic() - start, None
    except asyncio.TimeoutError:
        return False, time.monotonic() - start, "TCP_TIMEOUT"
    except ConnectionRefusedError:
        return False, time.monotonic() - start, "TCP_REFUSED"
    except Exception as e:
        return False, time.monotonic() - start, "UNKNOWN"

async def run_test(case_name, config):
    print(f"--- Running Telethon Test: {case_name} ---")
    result = {
        "library": "telethon",
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
    # Telethon allows strict proxy definition via the connection class
    connection_kwargs = {
        "connection": network.connection.ConnectionTcpMTProxyRandomizedIntermediate,
        "proxy": (config["server"], config["port"], config["secret"])
    }
    
    # Ephemeral session (in-memory) to avoid locking DBs
    client = TelegramClient(f"memory_{case_name}", int(API_ID), API_HASH, **connection_kwargs)
    
    t1 = time.monotonic()
    try:
        # client.connect() establishes transport and DH exchange without user auth
        connected = await asyncio.wait_for(client.connect(), timeout=5.0)
        dur_mtp = time.monotonic() - t1
        result["mtproto_connect_ms"] = round(dur_mtp * 1000, 2)
        
        if connected:
            result["state"] = "MT_PROTO_CONNECTED"
            result["error_category"] = "SUCCESS"
        else:
            result["state"] = "FAILED"
            result["error_category"] = "LIBRARY_ERROR"

    except asyncio.TimeoutError:
        result["error_category"] = "MTPROXY_HANDSHAKE_FAILED" # Proxy blackholed us
        result["exception_type"] = "TimeoutError"
    except ConnectionError as e:
        result["error_category"] = "MTPROXY_HANDSHAKE_FAILED"
        result["exception_type"] = e.__class__.__name__
    except RPCError as e:
        result["error_category"] = "TELEGRAM_CONNECTION_FAILED"
        result["exception_type"] = e.__class__.__name__
    except Exception as e:
        result["error_category"] = "UNKNOWN"
        result["exception_type"] = e.__class__.__name__
    finally:
        await client.disconnect()
        result["total_duration_ms"] = round((time.monotonic() - t0) * 1000, 2)
        
    return result

async def main():
    if not API_ID or not API_HASH:
        raise ValueError("API_ID and API_HASH must be set")
        
    results = []
    for case_name, config in TEST_CASES.items():
        res = await run_test(case_name, config)
        results.append(res)
        
    with open("telethon_results.json", "w") as f:
        json.dump(results, f, indent=2)

if __name__ == "__main__":
    asyncio.run(main())