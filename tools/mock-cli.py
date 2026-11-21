#!/usr/bin/python3
"""Mock adguardvpn-cli for UI development. Not part of the shipped app."""
import json
import os
import signal
import sys
import time

STATE = os.environ.get("AGVPN_MOCK_STATE", "/tmp/agvpn-mock-state.json")
if os.environ.get("AGVPN_MOCK_CONNECTED") == "1" and not os.path.exists(STATE):
    state = {
        "connected": True,
        "city": "Helsinki",
        "location": "Helsinki, Finland",
        "mode": "tun",
        "protocol": "auto",
        "dns": "default",
        "exclusions_general": ["example.com", "*.example.org"],
        "exclusions_selective": [],
        "exclusion_mode": "general",
        "socks_host": "127.0.0.1",
        "socks_port": "1080",
        "post_quantum": True,
        "system_dns": True,
        "routing": "auto",
        "telemetry": False,
        "crash": False,
        "debug": False,
        "channel": "release",
    }
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(state, fh)


def load():
    try:
        with open(STATE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"connected": False, "exclusion_mode": "general", "mode": "tun"}


def save(state):
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(state, fh)


LOC_ROWS = [
    ("LT", "Lithuania", "Vilnius", 19),
    ("LV", "Latvia", "Riga", 20),
    ("UA", "Ukraine", "Kyiv", 21),
    ("EE", "Estonia", "Tallinn", 28),
    ("FI", "Finland", "Helsinki", 22),
    ("PL", "Poland", "Warsaw", 33),
    ("DE", "Germany", "Berlin", 43),
    ("DE", "Germany", "Frankfurt", 56),
    ("NL", "Netherlands", "Amsterdam", 60),
    ("FR", "France", "Paris", 69),
    ("GB", "United Kingdom", "London", 70),
    ("SE", "Sweden", "Stockholm", 39),
    ("CH", "Switzerland", "Zurich", 59),
    ("US", "United States", "New York", 120),
    ("JP", "Japan", "Tokyo", 210),
    ("AU", "Australia", "Sydney", 422),
    ("RU", "Russia", "Moscow (Virtual)", 49),
    ("TR", "Turkey", "Istanbul", 48),
    ("KZ", "Kazakhstan", "Astana", 65),
    ("GE", "Georgia", "Tbilisi", 70),
]

LOCATIONS = "ISO   COUNTRY              CITY                           PING ESTIMATE\n" + "".join(
    f"{iso:<6}{country:<21}{city:<31}{ping:<10}\n" for iso, country, city, ping in LOC_ROWS
)

LICENSE = "Logged in as user@example.com\nYou are using the PREMIUM version\nUp to 10 devices simultaneously\nYour subscription is valid until 2028-11-21\n"


def config(state):
    mode = state.get("mode", "tun")
    protocol = {"auto": "Default (auto)", "http2": "HTTP/2", "quic": "QUIC"}.get(state.get("protocol", "auto"))
    dns = "Default (provided by AdGuard VPN)" if state.get("dns", "default") == "default" else state.get("dns")
    return f"""Current configuration:
\tData directory: /home/andrei/.local/share/adguardvpn-cli
\tMode: {mode}
\tSOCKS port: {state.get('socks_port', '1080')}
\tSOCKS host: {state.get('socks_host', '127.0.0.1')}
\tSOCKS username:
\tDNS upstream: {dns}
\tTunnel routing mode: {state.get('routing', 'auto')}
\tChange system DNS: {'on' if state.get('system_dns') else 'off'}
\tCrash reporting: {'on' if state.get('crash') else 'off'}
\tSend anonymized usage data: {'on' if state.get('telemetry') else 'off'}
\tUpdate channel: {state.get('channel', 'release').upper()}
\tProtocol: {protocol}
\tPost-quantum cryptography: {'Default (on)' if state.get('post_quantum') else 'off'}
\tExclusions TCP early ACK: Default (off)
\tExclusions pre-resolve: Default (on)
\tExclusions pre-resolve max queries: Default (50)
\tExclusions scannable ports: Default (443,80,8080,8008,853)
\tShow hints: on
\tDebug logging: {'on' if state.get('debug') else 'Default (off)'}
\tShow notifications: Default (off)
\tOutbound interface override: not set
\tTUN interface name: not set
\tUse existing TUN interface: Default (off)
"""


def main(argv):
    args = argv[1:]
    state = load()
    joined = " ".join(args)
    if not args or args[0] in ("-h", "--help"):
        print("mock adguardvpn-cli")
        return 0
    if args[0] in ("-v", "--version"):
        print("AdGuard VPN CLI v1.8.3 (mock)")
        return 0
    if args[0] == "status":
        if state.get("connected"):
            print("VPN is connected")
            print(f"Connected to {state.get('location', 'Helsinki, Finland')}")
        else:
            print("VPN is disconnected")
            print("You can connect to a location by running `adguardvpn-cli connect -l 'city, country or ISO code'`")
        return 0
    if args[0] == "license":
        print(LICENSE, end="")
        return 0
    if args[0] == "list-locations":
        print(LOCATIONS, end="")
        return 0
    if args[0] == "check-update":
        print("You are using the latest version")
        return 0
    if args[0] == "connect":
        time.sleep(float(os.environ.get("AGVPN_MOCK_CONNECT_DELAY", "0.6")))
        city = "Helsinki"
        country = "Finland"
        if "-l" in args:
            query = args[args.index("-l") + 1].lower()
            for iso, country_name, city_name, _ping in LOC_ROWS:
                if query in (iso.lower(), country_name.lower(), city_name.lower()) or query in city_name.lower():
                    city, country = city_name, country_name
                    break
        if "-f" in args:
            city, country = "Vilnius", "Lithuania"
        state["connected"] = True
        state["city"] = city
        state["location"] = f"{city}, {country}"
        save(state)
        print(f"Connected to {state['location']}")
        print("You are now connected. You can check the connection status by running `adguardvpn-cli status`")
        return 0
    if args[0] == "disconnect":
        time.sleep(float(os.environ.get("AGVPN_MOCK_DISCONNECT_DELAY", "0.5")))
        state["connected"] = False
        save(state)
        print("VPN stopped")
        return 0
    if args[0] == "logout":
        print("Disconnecting on logout...")
        state["connected"] = False
        save(state)
        print("Logout completed")
        return 0
    if args[0] == "config":
        if len(args) >= 2 and args[1] == "show":
            print(config(state), end="")
            return 0
        if len(args) >= 3 and args[1].startswith("set-"):
            key = args[1][4:]
            value = args[2]
            mapping = {
                "mode": "mode", "protocol": "protocol", "dns": "dns",
                "change-system-dns": "system_dns", "tun-routing-mode": "routing",
                "post-quantum": "post_quantum", "telemetry": "telemetry",
                "crash-reporting": "crash", "debug-logging": "debug",
                "update-channel": "channel", "socks-port": "socks_port",
                "socks-host": "socks_host",
            }
            target = mapping.get(key)
            if target:
                if value in ("on", "off"):
                    state[target] = value == "on"
                else:
                    state[target] = value
                save(state)
            print("Config has been updated")
            print(config(state), end="")
            return 0
        print("mock config: unsupported", file=sys.stderr)
        return 1
    if args[0] == "site-exclusions":
        sub = args[1] if len(args) > 1 else "show"
        mode = state.get("exclusion_mode", "general")
        if "--for-mode" in args:
            mode = args[args.index("--for-mode") + 1]
        key = f"exclusions_{mode}"
        items = state.setdefault(key, [])
        if sub == "mode":
            if len(args) > 2:
                state["exclusion_mode"] = args[2]
                save(state)
                print(f"Exclusion mode is set to {args[2].upper()}")
            else:
                print(f"Current exclusion mode is {state.get('exclusion_mode', 'general').upper()}")
            return 0
        if sub == "show":
            print(f"Exclusions for {mode.upper()} mode:")
            for item in items:
                print(item)
            return 0
        if sub in ("add", "remove"):
            domains = args[2].split()
            if sub == "add":
                for domain in domains:
                    if domain not in items:
                        items.append(domain)
                print(f"Successfully added {len(domains)} exclusions")
            else:
                for domain in domains:
                    if domain in items:
                        items.remove(domain)
                print(f"Successfully removed {len(domains)} exclusions")
            save(state)
            return 0
        if sub == "clear":
            items.clear()
            save(state)
            print("Exclusions cleared")
            return 0
        return 1
    print(f"mock: unsupported command: {joined}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
    sys.exit(main(sys.argv))
