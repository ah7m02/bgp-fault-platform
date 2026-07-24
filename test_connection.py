import json
import logging
from datetime import datetime, timezone
from netmiko import ConnectHandler

logging.basicConfig(level=logging.WARNING)

ROUTERS = {
    "R1": "192.168.0.241",
    "R2": "192.168.0.242",
    "R3": "192.168.0.243",
    "R4": "192.168.0.244",
    "R5": "192.168.0.245",
}

CREDS = {
    "username": "admin",
    "password": "cisco123",
    "secret": "cisco123",
}

OUTPUT_FILE = "baseline_bgp_state.json"


def snapshot_router(name, ip):
    device = {
        "device_type": "cisco_ios",
        "host": ip,
        "conn_timeout": 15,
        "auth_timeout": 15,
        "banner_timeout": 15,
        **CREDS,
    }

    result = {
        "name": name,
        "ip": ip,
        "reachable": False,
        "bgp_neighbors": [],
        "running_config": "",
        "error": None,
    }

    try:
        conn = ConnectHandler(**device)
        conn.enable()

        # Structured BGP neighbor state (for fast health checks / validation)
        parsed = conn.send_command("show ip bgp summary", use_textfsm=True)
        if isinstance(parsed, list):
            result["bgp_neighbors"] = parsed
        else:
            # textfsm failed to match -> fall back to raw text so nothing is lost
            result["bgp_neighbors"] = []
            result["bgp_summary_raw"] = parsed

        # Full running-config (for reset/replay baseline)
        running_config = conn.send_command("show running-config")
        result["running_config"] = running_config

        result["reachable"] = True
        conn.disconnect()

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {str(e)}"

    return result


def main():
    snapshot = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "routers": {},
    }

    for name, ip in ROUTERS.items():
        print(f"Snapshotting {name} ({ip}) ...")
        snapshot["routers"][name] = snapshot_router(name, ip)
        status = "OK" if snapshot["routers"][name]["reachable"] else "FAILED"
        print(f"  -> {status}")

    with open(OUTPUT_FILE, "w") as f:
        json.dump(snapshot, f, indent=2)

    print(f"\nBaseline snapshot saved to {OUTPUT_FILE}")

    # quick health summary
    print("\n=== Summary ===")
    for name, data in snapshot["routers"].items():
        if not data["reachable"]:
            print(f"{name}: UNREACHABLE ({data['error']})")
            continue
        neighbors = data["bgp_neighbors"]
        bad = [n for n in neighbors if not str(n.get("state_or_prefixes_received", "")).isdigit()]
        if bad:
            print(f"{name}: {len(neighbors)} neighbors, {len(bad)} NOT established")
        else:
            print(f"{name}: {len(neighbors)} neighbors, all Established")


if __name__ == "__main__":
    main()