import os
import time
import uuid
import random
import yaml
import anthropic
from dotenv import load_dotenv
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from netmiko import ConnectHandler

load_dotenv()


def _require_env(name):
    """Return env var `name`, or raise a clear error pointing at .env.example."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set. "
            "Copy .env.example to .env and fill in the values."
        )
    return value


app = FastAPI()

# The frontend runs on Vite's dev server; both spellings of localhost are listed
# because the browser matches the Origin header literally.
CORS_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

anthropic_client = anthropic.Anthropic(api_key=_require_env("ANTHROPIC_API_KEY"))

SESSIONS = {}


class StartSessionRequest(BaseModel):
    scenario_id: str


class SessionIdRequest(BaseModel):
    session_id: str


class GenerateScenarioRequest(BaseModel):
    difficulty: int

# Management IPs and login credentials come from the environment so nothing
# device-specific or secret is baked into source control. See .env.example.
ROUTERS = {
    name: _require_env(f"ROUTER_{name}_IP")
    for name in ("R1", "R2", "R3", "R4", "R5")
}

_router_password = _require_env("ROUTER_PASSWORD")
CREDS = {
    "username": _require_env("ROUTER_USERNAME"),
    "password": _router_password,
    # enable secret; falls back to the login password when unset, as on many labs
    "secret": os.environ.get("ROUTER_SECRET") or _router_password,
}

# Each router advertises its own /32 loopback via BGP; every other router
# should learn all four of the others'. Used for prefix-level reachability.
LOOPBACKS = {
    "R1": "10.0.0.1",
    "R2": "10.0.0.2",
    "R3": "10.0.0.3",
    "R4": "10.0.0.4",
    "R5": "10.0.0.5",
}

SCENARIOS_DIR = Path(__file__).parent / "scenarios"

# Seconds to wait after pushing fix/restore config before returning from a reset.
# Re-establishing a BGP session and reconverging OSPF is not instantaneous, so a
# validate fired immediately after a reset returns can legitimately read the
# topology as still broken. Holding the reset response until the control plane
# settles keeps that transient out of the user-visible result. Not a substitute
# for a correct reachability check -- purely a timing guard.
RESET_SETTLE_SECONDS = 5

# Seconds to wait after a soft clear before returning, so the refreshed updates
# have been exchanged and re-run through the policy by the time anything looks.
SOFT_CLEAR_SETTLE_SECONDS = 5

# Config keywords that mean a BGP route policy changed. IOS does not re-evaluate
# already-exchanged routes when a route-map/prefix-list is attached to (or
# removed from) a live neighbor, so the Adj-RIB stays as it was and the policy
# silently does nothing until the session is refreshed.
POLICY_COMMAND_SUBSTRINGS = ["route-map", "prefix-list"]

# Config we must never touch: the out-of-band management interface, the SSH/AAA
# stack we reach the lab over, and anything that would drop us off the box.
# Applied to AI-generated fault commands (validate_generated_commands) and to
# snapshot replay (snapshot_config_lines) alike -- reset used to bypass this,
# which let a replayed "line vty" block reset R5's vty 0-3 to
# "transport input none" and lock SSH out of four of its five lines.
FORBIDDEN_COMMAND_SUBSTRINGS = [
    "ethernet0/0", "ssh", "username", "line vty", "crypto", "no router bgp", "reload",
]

# Extra substrings that only matter on replay: they never appear in a generated
# fault, but do appear in a running-config and would rewrite the management
# plane if pushed back.
SNAPSHOT_EXTRA_FORBIDDEN_SUBSTRINGS = [
    "line con", "line aux", "ip ssh", "transport input", "aaa ", "enable secret",
    "enable password", "access-class",
]

SNAPSHOT_FORBIDDEN_SUBSTRINGS = (
    FORBIDDEN_COMMAND_SUBSTRINGS + SNAPSHOT_EXTRA_FORBIDDEN_SUBSTRINGS
)

# After pushing fault_commands, poll the topology to confirm the fault bit. Most
# faults (session down, filtered prefix) show up on the first sweep; others that
# depend on RIB reconvergence -- next-hop-self removed, a changed iBGP next hop --
# take longer, so we re-sweep every FAULT_POLL_INTERVAL_SECONDS until an effect
# appears or FAULT_POLL_TIMEOUT_SECONDS elapses. See fault_had_effect for what
# no amount of waiting can cover (e.g. a peer's hold timer, up to 180s).
FAULT_POLL_INTERVAL_SECONDS = 5
FAULT_POLL_TIMEOUT_SECONDS = 30


def load_scenarios():
    scenarios = {}
    for file in SCENARIOS_DIR.glob("*.yaml"):
        with open(file) as f:
            data = yaml.safe_load(f)
        if not data or "id" not in data:
            print(f"WARNING: skipping invalid scenario file {file}")
            continue
        scenarios[data["id"]] = data
    return scenarios

def commands_touch_policy(commands):
    """True if any config line changes a BGP route policy (see POLICY_COMMAND_SUBSTRINGS)."""
    return any(
        substring in line.lower()
        for line in commands
        for substring in POLICY_COMMAND_SUBSTRINGS
    )


def soft_clear_bgp(conn):
    """Refresh all BGP sessions so a newly changed route policy actually applies.

    `clear ip bgp * soft` re-sends outbound updates and asks peers to re-send
    inbound ones without tearing the sessions down, which is what forces the new
    inbound/outbound policy to be evaluated against the existing routes. Then
    sleep so the re-exchange has completed before we return.
    """
    output = conn.send_command("clear ip bgp * soft")
    time.sleep(SOFT_CLEAR_SETTLE_SECONDS)
    return output


def get_conn(ip):
    # 30s rather than the usual 15s: R5 is slow to emit its SSH banner and was
    # failing with "Error reading SSH protocol banner" while PuTTY connected to
    # it fine -- a slow device, not an unreachable one.
    device = {
        "device_type": "cisco_ios", "host": ip, **CREDS,
        "conn_timeout": 30, "auth_timeout": 30, "banner_timeout": 30,
    }
    conn = ConnectHandler(**device)
    conn.enable()
    return conn


def route_in_table(conn, prefix):
    """True if `prefix` has any route in the RIB, whatever protocol installed it.

    Deliberately protocol-agnostic. The loopbacks are advertised into both OSPF
    (for underlay reachability) and BGP (the routing-policy layer), so a /32 is
    routinely installed by OSPF (AD 110) rather than iBGP (AD 200). That is the
    expected, correct outcome here, so keying off "show ip route bgp" would flag
    perfectly reachable prefixes as missing. IOS answers a per-prefix lookup with
    "Routing entry for <prefix>" when the route exists and "% Network not in
    table" (or "% Subnet not in table") when it does not.
    """
    raw = conn.send_command(f"show ip route {prefix}")
    if not isinstance(raw, str):
        raw = str(raw)
    lowered = raw.lower()
    if "not in table" in lowered:
        return False
    return "routing entry for" in lowered


def missing_loopback_routes(conn, router_name):
    """Return (expected, missing) loopback routes for one already-open connection.

    Single source of truth for "which loopbacks can this router route to". Both
    verify_full_reachability (the topology-wide sweep behind /health and
    check_validation_topology) and check_router_clean (the pre-generation guard)
    go through here, so the two can never drift apart again.
    """
    expected = sorted(lb for r, lb in LOOPBACKS.items() if r != router_name)
    missing = [lb for lb in expected if not route_in_table(conn, lb)]
    return expected, missing


def verify_full_reachability():
    """Confirm every router can route to all other routers' loopbacks.

    A route-map / prefix-list fault can keep a BGP session Established while
    silently filtering prefixes, so session state alone is not enough. Each
    router should have a route to the four /32 loopbacks of the *other* routers
    (10.0.0.1-10.0.0.5, excluding its own). Each prefix is looked up
    individually with "show ip route <prefix>" and counts as reachable if it is
    in the RIB at all -- see route_in_table for why the sourcing protocol is
    intentionally ignored.
    """
    results = {}
    for name, ip in ROUTERS.items():
        expected = sorted(lb for r, lb in LOOPBACKS.items() if r != name)
        try:
            conn = get_conn(ip)
            try:
                expected, missing = missing_loopback_routes(conn, name)
            finally:
                conn.disconnect()

            results[name] = {
                "expected_loopbacks": expected,
                "missing_routes": missing,
                "reachable": len(missing) == 0,
            }
        except Exception as e:
            # Connection failure, not a routing failure. We learned nothing about
            # this router's RIB, so missing_routes stays empty -- claiming every
            # loopback is missing would be indistinguishable from a real
            # blackhole and would mislabel an SSH/auth outage as a BGP fault.
            # "error" plus reachable: False is what marks this router unknown.
            results[name] = {
                "expected_loopbacks": expected,
                "missing_routes": [],
                "reachable": False,
                "error": str(e),
            }
    return results


def run_health_check():
    results = {}
    for name, ip in ROUTERS.items():
        try:
            conn = get_conn(ip)
            parsed = conn.send_command("show ip bgp summary", use_textfsm=True)
            conn.disconnect()
            bad = [n for n in parsed if not str(n.get("state_or_prefixes_received", "")).isdigit()]
            results[name] = {"neighbors": len(parsed), "unhealthy": len(bad), "raw": parsed}
        except Exception as e:
            results[name] = {"error": str(e)}

    # Merge in prefix-level reachability so GET /health and validation can see
    # which specific loopbacks are filtered, not just whether sessions are up.
    reachability = verify_full_reachability()
    for name, reach in reachability.items():
        results.setdefault(name, {}).update({
            "expected_loopbacks": reach["expected_loopbacks"],
            "missing_routes": reach["missing_routes"],
            "reachable": reach["reachable"],
        })
    return results


@app.get("/scenarios")
def list_scenarios():
    scenarios = load_scenarios()
    return [
        {"id": s["id"], "title": s["title"], "description": s["description"], "difficulty": s["difficulty"]}
        for s in scenarios.values()
    ]


@app.get("/health")
def health():
    return run_health_check()


def inject_fault(scenario):
    ip = ROUTERS[scenario["target_router"]]
    conn = get_conn(ip)
    try:
        output = conn.send_config_set(scenario["fault_commands"])
    finally:
        conn.disconnect()
    return output


def summarize_topology_state(health):
    """Reduce a run_health_check result to just what a fault is meant to change.

    Per router: how many neighbors are not Established, which loopbacks are not
    in the RIB, and whether we could talk to the box at all. Everything else in
    the health payload (raw textfsm rows, counts that don't move) is noise for
    a before/after comparison.
    """
    return {
        name: {
            "unhealthy": h.get("unhealthy", 0),
            "missing": set(h.get("missing_routes") or []),
            "errored": "error" in h,
        }
        for name, h in health.items()
    }


def fault_had_effect(before_health, after_health):
    """Return (had_effect, details) by diffing two topology sweeps.

    A fault counts as effective if, on any router, the sweep found something
    broken that was NOT broken beforehand: more neighbors down than before, a
    loopback that used to be in the RIB and no longer is, or a router that has
    stopped answering entirely. Comparing against the "before" state rather than
    against perfect health matters -- if the lab was already missing a route,
    that pre-existing damage must not be mistaken for the new fault biting.

    Limits worth knowing. This only sees what the sweep sees. Its caller
    (poll_fault_effect) re-checks for up to FAULT_POLL_TIMEOUT_SECONDS, but a
    fault whose symptom depends on the BGP hold timer expiring on a *peer* (up to
    180s) can still read as inert within that window. Treating that as a false
    "inert" is the safe direction to be wrong: the scenario is rejected and
    regenerated rather than handed to a learner unverified.
    """
    before = summarize_topology_state(before_health)
    after = summarize_topology_state(after_health)

    new_unhealthy = {}
    new_missing = {}
    newly_unreachable = []

    for name, post in after.items():
        pre = before.get(name, {"unhealthy": 0, "missing": set(), "errored": False})

        if post["errored"]:
            if not pre["errored"]:
                newly_unreachable.append(name)
            # An unreachable router tells us nothing about its neighbors or RIB,
            # so skip the other two comparisons for it rather than reading the
            # zeroed-out fields as an improvement.
            continue

        if post["unhealthy"] > pre["unhealthy"]:
            new_unhealthy[name] = {
                "before": pre["unhealthy"], "after": post["unhealthy"]
            }

        appeared = sorted(post["missing"] - pre["missing"])
        if appeared:
            new_missing[name] = appeared

    details = {
        "new_unhealthy_neighbors": new_unhealthy,
        "new_missing_routes": new_missing,
        "newly_unreachable_routers": sorted(newly_unreachable),
    }
    had_effect = bool(new_unhealthy or new_missing or newly_unreachable)
    return had_effect, details


def poll_fault_effect(before_health):
    """Sweep the topology until the fault shows an effect, or the timeout expires.

    Returns (had_effect, details, elapsed_seconds). The first sweep runs
    immediately, so a fast fault (session down, filtered prefix) is confirmed
    with no added delay; only when a sweep comes back unchanged do we wait
    FAULT_POLL_INTERVAL_SECONDS and try again, up to FAULT_POLL_TIMEOUT_SECONDS
    total. elapsed_seconds is how long detection took, logged so slow-manifesting
    fault types (next-hop-self and other RIB-convergence cases) are visible.
    """
    start = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        after_health = run_health_check()
        had_effect, details = fault_had_effect(before_health, after_health)
        elapsed = time.monotonic() - start

        if had_effect:
            return True, details, elapsed
        if elapsed >= FAULT_POLL_TIMEOUT_SECONDS:
            return False, details, elapsed

        print(
            f"[ai/generate-scenario] no effect on sweep {attempt} "
            f"({elapsed:.1f}s elapsed); re-checking in {FAULT_POLL_INTERVAL_SECONDS}s"
        )
        time.sleep(FAULT_POLL_INTERVAL_SECONDS)


def check_validation(scenario):
    check_router = scenario["validation"]["router"]
    min_neighbors = scenario["validation"]["min_established_neighbors"]

    health = run_health_check()
    router_health = health.get(check_router, {})

    if "error" in router_health:
        return {"passed": False, "reason": f"Could not reach {check_router}"}

    established = router_health["neighbors"] - router_health["unhealthy"]
    passed = established >= min_neighbors

    return {
        "passed": passed,
        "router": check_router,
        "established_neighbors": established,
        "required": min_neighbors,
    }


def check_validation_topology(target_router=None):
    """Validate an AI-generated scenario by sweeping the whole topology.

    Two independent failure modes must both be clear to pass:

    1. Session state. A BGP fault injected on one router usually shows its
       symptom on a *peer* router. Example: removing R2's `neighbor 10.12.0.1`
       statement makes that neighbor vanish from R2's own summary (so R2 looks
       healthy) while R1 sits in Idle/Active toward R2. So we require every
       router to have zero unhealthy neighbors.
    2. Prefix reachability. A route-map / prefix-list fault can keep every
       session Established while filtering specific prefixes, which the
       session-state check cannot see. So we also require every router to have
       BGP routes to all other routers' loopbacks (via verify_full_reachability,
       whose data run_health_check merges in below).

    Overall "passed" requires BOTH: all sessions Established AND all loopbacks
    reachable from all routers.
    """
    health = run_health_check()

    unreachable = sorted(name for name, h in health.items() if "error" in h)
    unhealthy_routers = {
        name: h["unhealthy"]
        for name, h in health.items()
        if "error" not in h and h.get("unhealthy", 0) > 0
    }
    missing_routes = {
        name: h["missing_routes"]
        for name, h in health.items()
        if h.get("missing_routes")
    }

    passed = not unreachable and not unhealthy_routers and not missing_routes

    return {
        "passed": passed,
        "target_router": target_router,
        "checked_routers": sorted(health.keys()),
        "unreachable_routers": unreachable,
        "unhealthy_routers": unhealthy_routers,
        "missing_routes": missing_routes,
    }


def check_router_clean(target_router):
    """Return (is_clean, details) for a single router's current BGP health.

    Used before snapshotting an AI-generated scenario so we never capture an
    already-broken running-config (from a prior unresolved fault) as the
    baseline. A router is "clean" only if it has no unhealthy neighbors and is
    missing no routes to the other routers' loopbacks.

    The route half of that check goes through missing_loopback_routes -- the
    same per-prefix, protocol-agnostic lookup used by verify_full_reachability
    for /health and check_validation_topology. This used to keep its own
    "show ip route bgp" copy of the logic, which flagged loopbacks reached via
    OSPF (AD 110, beating iBGP's 200) as missing and blocked generation on a
    perfectly healthy router.
    """
    ip = ROUTERS[target_router]
    try:
        conn = get_conn(ip)
        try:
            summary = conn.send_command("show ip bgp summary", use_textfsm=True)
            _expected, missing = missing_loopback_routes(conn, target_router)
        finally:
            conn.disconnect()
    except Exception as e:
        return False, {"error": str(e)}

    if isinstance(summary, list):
        unhealthy = [
            n for n in summary
            if not str(n.get("state_or_prefixes_received", "")).isdigit()
        ]
    else:
        unhealthy = []

    details = {"unhealthy_neighbors": len(unhealthy), "missing_routes": missing}
    is_clean = not unhealthy and not missing
    return is_clean, details


def apply_fix(scenario):
    ip = ROUTERS[scenario["target_router"]]
    conn = get_conn(ip)
    try:
        output = conn.send_config_set(scenario["fix_commands"])
        if commands_touch_policy(scenario["fix_commands"]):
            soft_clear_bgp(conn)
    finally:
        conn.disconnect()
    return output


def snapshot_config_lines(snapshot_text):
    """Turn "show running-config" text into lines that are safe to push back.

    Two jobs:

    1. Drop banner/comment lines that IOS won't accept as config-mode commands.
    2. Drop management-plane config, per SNAPSHOT_FORBIDDEN_SUBSTRINGS. Replaying
       it rewrites the very access we manage the lab over: a snapshot's "line vty
       0 4" block reset R5's vty 0-3 to "transport input none", which is what
       produced the "Error reading SSH protocol banner" failures on R5.

    Filtering is BLOCK-AWARE, which matters more than the per-line match. IOS
    config is indented: dropping the header "line vty 0 4" while keeping its
    indented " transport input none" would not just fail to protect anything --
    the orphaned sub-command would land in whatever config context happened to be
    open, applying management config somewhere arbitrary. So when a top-level
    line is rejected, every indented line under it is dropped too, until the next
    top-level line. That is also what keeps "interface Ethernet0/0" and its
    addressing out of the replay as one unit.
    """
    lines = []
    skipping_block = False

    for raw in snapshot_text.splitlines():
        if not raw.strip():
            continue
        if (
            raw.startswith("!")
            or raw.startswith("Building configuration")
            or raw.startswith("Current configuration")
            or raw.strip() == "end"
        ):
            continue

        is_subcommand = raw[:1].isspace()
        if is_subcommand and skipping_block:
            continue

        forbidden = any(
            substring in raw.lower() for substring in SNAPSHOT_FORBIDDEN_SUBSTRINGS
        )
        if not is_subcommand:
            # A new top-level line ends any block we were skipping; whether we
            # start skipping a new one depends on this line alone.
            skipping_block = forbidden
        if forbidden:
            continue

        lines.append(raw)

    return lines


def restore_snapshot(target_router, snapshot_text, fix_commands=None, soft_clear=False):
    """Undo a fault: send fix_commands, then replay the pre-fault running-config.

    Order matters. The snapshot was captured BEFORE the fault, so it contains no
    negation lines for anything the fault added, and send_config_set MERGES
    rather than replaces -- replaying a config that merely lacks a route-map,
    prefix-list, or `neighbor ... route-map ... out` does not remove any of them.
    fix_commands holds the explicit `no ...` lines and is the only thing that can,
    so it goes first. The snapshot push follows as a safety net, restoring any
    baseline config the fault (or an incomplete fix) disturbed.

    Both pushes share one connection, and their outputs are concatenated so the
    caller's reset response shows the negations and not just the snapshot replay.

    Pass soft_clear=True when the fault being undone was a route policy: the
    same IOS behaviour that makes an added policy inert also makes a removed one
    linger, so the sessions need a refresh for the restored policy state to take.
    """
    ip = ROUTERS[target_router]
    conn = get_conn(ip)
    try:
        outputs = []
        if fix_commands:
            outputs.append(conn.send_config_set(fix_commands))
        outputs.append(conn.send_config_set(snapshot_config_lines(snapshot_text)))
        if soft_clear:
            soft_clear_bgp(conn)
    finally:
        conn.disconnect()
    return "\n".join(outputs)


TOPOLOGY_PROMPT = """You are designing lab exercises for a Cisco IOS BGP troubleshooting lab.

Topology (5 routers):
- R1 is in AS 65010.
- R2, R3, and R4 are in AS 65030 and run a full iBGP mesh with each other, peering via
  Loopback0 (update-source Loopback0).
- R5 is in AS 65020.
- R2 is the eBGP edge toward R1 (R2's neighbor 10.12.0.1, remote-as 65010).
- R4 is the eBGP edge toward R5.
- R3 sits in the middle of the iBGP mesh (e.g. neighbor 10.0.0.4 remote-as 65030 toward R4's loopback).

Interfaces: Ethernet0/1 and Ethernet0/2 are data-plane links between routers. Ethernet0/0 is
out-of-band management on every router and must NEVER be touched, shut down, or referenced in
any command.

ADDRESSING - these are the ONLY prefixes that exist in this lab. There are no others.

Loopback0 /32s (one per router, advertised into BGP; these are what reachability is measured on):
- 10.0.0.1/32  = R1's loopback
- 10.0.0.2/32  = R2's loopback
- 10.0.0.3/32  = R3's loopback
- 10.0.0.4/32  = R4's loopback
- 10.0.0.5/32  = R5's loopback

Point-to-point link subnets:
- 10.12.0.0/30 = R1 <-> R2
- 10.23.0.0/30 = R2 <-> R3
- 10.34.0.0/30 = R3 <-> R4
- 10.45.0.0/30 = R4 <-> R5

Every address or prefix you put in fault_commands or fix_commands MUST come from the lists
above. Do NOT invent subnets. A prefix-list, route-map, access-list, distribute-list, or any
other filter that matches an address not in those lists (for example 172.16.10.0/24,
192.168.x.x, or any other made-up range) matches nothing real, so the fault would not actually
break anything and the exercise is worthless. When the fault is meant to block "reachability to
router X's network," filter that router's Loopback0 /32 (e.g. `ip prefix-list BLOCK seq 5 deny
10.0.0.3/32` to blackhole R3), since the loopbacks are the prefixes carried in BGP.

ROUTE-MAP / PREFIX-LIST DIRECTION - get this right or the fault does nothing.

A route-map applied to a BGP neighbor statement is directional, and the direction decides which
half of the update flow it touches:
- `neighbor <peer-ip> route-map NAME out` filters what THIS router ADVERTISES TO that peer.
  Use "out" to stop the peer from LEARNING (and therefore reaching) a prefix.
- `neighbor <peer-ip> route-map NAME in` filters what THIS router ACCEPTS FROM that peer.
  Use "in" only to stop THIS router from learning a prefix THAT PEER ORIGINATES OR SENDS.

The single most common mistake is applying the filter "in" on the neighbor facing the router you
want to break. That denies a prefix in updates arriving FROM that neighbor - but if that neighbor
never sends the prefix in the first place, the filter matches nothing and the lab is not broken
at all. Ask yourself: "does the update carrying this prefix actually travel in the direction I am
filtering?" If the prefix flows from this router TOWARD the peer, the answer is "out".

Worked example - make R5 unable to reach R1's loopback (10.0.0.1/32):
10.0.0.1/32 originates on R1, enters AS 65030 at R2, which floods it directly to both R3 and R4
over the iBGP mesh, and R4 then advertises it OUT to R5. The last hop before R5 is R4, so the
filter belongs on R4, applied OUT toward R5:

    ip prefix-list BLOCK-R1 seq 5 deny 10.0.0.1/32
    ip prefix-list BLOCK-R1 seq 10 permit 0.0.0.0/0 le 32
    route-map TO-R5 permit 10
     match ip address prefix-list BLOCK-R1
    router bgp 65030
     neighbor 10.45.0.2 route-map TO-R5 out

Applying that same route-map `in` on R4's neighbor 10.45.0.2 would be WRONG: R5 never sends
10.0.0.1/32 to R4, so nothing would be filtered and R5 would still reach R1's loopback.
The mirror-image case: to stop R4 itself from accepting R5's loopback 10.0.0.5/32, you deny
10.0.0.5/32 in a route-map applied `in` on R4's `neighbor 10.45.0.2`, because that prefix really
does arrive from R5.

Also note the permit-everything-else line above. A route-map with only a deny-match and no
following permit clause drops ALL prefixes (implicit deny at the end), which breaks far more than
intended and makes the exercise unrealistic. Always let the other prefixes through.

THE iBGP FULL MESH - why "filter it on the middle router" never works.

R2, R3 and R4 are a FULL iBGP mesh: every one of them peers directly with the other two. There is
no transit router between them. On top of that, iBGP does NOT re-advertise a route learned from
one iBGP peer to another iBGP peer (the iBGP split-horizon / loop-prevention rule) - that is
exactly why the full mesh is required in the first place.

Put together, those two facts mean filtering a prefix OUTBOUND on a middle router accomplishes
nothing: the receiving router was never learning the prefix from that middle router anyway. It
has its own direct session with the router that injected the prefix into the AS, and it keeps
receiving the prefix over that session. You have filtered an advertisement that was never being
sent.

So a route-policy fault only bites if the filter sits at one of exactly two places:
1. The INJECTING router - the one that brings the prefix into AS 65030 from outside. That is R2
   for R1's 10.0.0.1/32, and R4 for R5's 10.0.0.5/32. Apply the filter "out" toward the specific
   iBGP peer that should lose the prefix.
2. The RECEIVING router itself - the router that should lose the prefix. Apply the filter "in" on
   the neighbor statement it actually learns that prefix from, i.e. the neighbor pointing at the
   injecting router's loopback.

Concretely, to make R2 lose 10.0.0.5/32 (R5's loopback), there are exactly two correct choices:
- on R4 (the injector, since R4 is the eBGP edge toward R5): filter outbound toward 10.0.0.2, or
- on R2 (the loser itself): filter inbound on `neighbor 10.0.0.4`.
Filtering anything on R3 does NOTHING here - R2 does not learn 10.0.0.5/32 from R3, it learns it
straight from R4. The same reasoning applies with the routers swapped for 10.0.0.1/32, which R2
injects: filter on R2 outbound toward 10.0.0.4, or on R4 inbound on `neighbor 10.0.0.2`.

Note that iBGP peerings use LOOPBACK addresses (update-source Loopback0), so an iBGP neighbor
statement names 10.0.0.2 / 10.0.0.3 / 10.0.0.4 - never a 10.23.0.x or 10.34.0.x link address.
The eBGP peerings are the ones that use link addresses (10.12.0.1 toward R1, 10.45.0.2 toward R5).

Before you commit to a route-policy fault, name the router that injects the prefix and the router
that should lose it. If your filter is on neither of them, it will not work - move it.

The human-facing `description` field may still use friendly, non-technical wording such as
"I can't reach R3's network from R1" - it does not need to name the prefix. That freedom applies
to the description text ONLY; the commands themselves must use the real prefixes listed above.

Invent exactly ONE realistic, fixable BGP fault appropriate for the requested difficulty.

EVERY fault, at every difficulty, MUST break reachability for at least one router - some other
router must end up unable to route to at least one loopback /32. Difficulty controls how hard the
fault is to DIAGNOSE, never how small its effect is. A fault that leaves every router still able
to reach every loopback is worthless and will be rejected.

- difficulty 1: a simple single command, easy to spot once you look, e.g. an interface shutdown
  or a missing/removed neighbor statement.
- difficulty 2: a moderate issue, e.g. a route-map or prefix-list that blocks routes.
- difficulty 3: HARD TO DIAGNOSE, not subtle in effect. It must still sever a path and break
  reachability for at least one router - it is just hard to spot in the running-config. Good
  difficulty-3 faults are ones that look almost correct at a glance:
  * a wrong update-source on an iBGP neighbor (peering never comes up / wrong source address),
  * next-hop-self removed from an edge router (iBGP peers get an unreachable next hop),
  * a prefix-list whose mask length or le/ge bounds are subtly wrong so it matches (or misses)
    the /32 it shouldn't,
  * a route-map that matches the wrong prefix,
  * a neighbor statement with the wrong remote-as (session won't establish).

DO NOT use AS-path prepending, local-preference, weight, or MED for ANY difficulty. This topology
has exactly ONE path between any pair of routers - there are no redundant or backup paths - so
changing path PREFERENCE changes nothing: the single available path still wins and reachability
is unaffected. A preference-tweak fault is inert here and will be rejected. Break the path itself
(session down, next hop unreachable, prefix filtered), do not merely make it less preferred.

Call the inject_fault tool exactly once with your chosen fault. The description you write must
read like an end-user complaint about a symptom (e.g. "I can't reach X from Y") and must NOT
reveal the underlying cause, the commands involved, or how to fix it.

Also fill in `internal_reasoning`. It is never shown to the learner - it is logged so an operator
can sanity-check the fault - so be blunt and technical there: name the exact prefix you are
blocking, the neighbor statement and direction (in vs out), the direction the update for that
prefix actually travels, and which router consequently loses reachability to what. Write it after
you have settled on the commands, and if writing it reveals that your direction is backwards
(the prefix never flows the way you filtered), fix the commands before calling the tool.

Finally, write exactly TWO `hints`, revealed to the learner one at a time as they get stuck:
- Hint 1 (cost 10): point at the general area without naming the mechanism. Example:
  "Check the outbound policy on the router that connects to AS 65020." It should narrow WHERE
  to look, not say what is wrong.
- Hint 2 (cost 25): be more specific about the nature of the problem - but do NOT hand over the
  fix. Never include the exact commands to type; the learner must find and correct the config
  themselves. Name the kind of misconfiguration and roughly where, and stop there."""

INJECT_FAULT_TOOL = {
    "name": "inject_fault",
    "description": "Inject a single realistic, fixable BGP fault into the lab topology.",
    "input_schema": {
        "type": "object",
        "properties": {
            "target_router": {
                "type": "string",
                "enum": ["R1", "R2", "R3", "R4", "R5"],
                "description": "The router to modify.",
            },
            "category": {
                "type": "string",
                "enum": ["bgp_neighbor", "route_policy", "interface"],
                "description": "The category of fault.",
            },
            "fault_commands": {
                "type": "array",
                "items": {"type": "string"},
                "description": "IOS config-mode lines that introduce the fault.",
            },
            "fix_commands": {
                "type": "array",
                "items": {"type": "string"},
                "description": "IOS config-mode lines that would fix the fault.",
            },
            "internal_reasoning": {
                "type": "string",
                "description": (
                    "INTERNAL ONLY - never shown to the learner. 1-3 sentences explaining the "
                    "mechanics of the fault you just wrote, for logging and sanity-checking. "
                    "For a route-map/prefix-list/distribute-list fault, state explicitly: the "
                    "exact prefix being blocked, which router injects that prefix into the AS, "
                    "which router should lose it, which neighbor statement the filter is on, "
                    "whether it is applied in or out, which direction the update for that "
                    "prefix actually flows, and why the filter sits on either the injecting "
                    "router (out) or the losing router (in) rather than a middle router. "
                    "For other fault types, state which session or path breaks and why."
                ),
            },
            "title": {
                "type": "string",
                "description": "A short, human-friendly scenario name.",
            },
            "description": {
                "type": "string",
                "description": (
                    "A 1-2 sentence plain-English description of the symptom, written like a "
                    "user complaint, without revealing the cause."
                ),
            },
            "hints": {
                "type": "array",
                "minItems": 2,
                "maxItems": 2,
                "description": (
                    "Exactly two progressive troubleshooting hints, shown to the learner one "
                    "at a time when they ask. Hint 1 (cost 10) points at the general area "
                    "without naming the mechanism, e.g. 'Check the outbound policy on the "
                    "router that connects to AS 65020.' Hint 2 (cost 25) is more specific but "
                    "still must NOT contain the exact fix commands - the learner has to find "
                    "and correct the config themselves."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "cost": {
                            "type": "integer",
                            "description": "Point cost: 10 for the first hint, 25 for the second.",
                        },
                        "text": {
                            "type": "string",
                            "description": "The hint text shown to the learner.",
                        },
                    },
                    "required": ["cost", "text"],
                },
            },
        },
        "required": [
            "target_router", "category", "fault_commands", "fix_commands",
            "internal_reasoning", "title", "description", "hints",
        ],
    },
}

def validate_generated_commands(commands):
    for line in commands:
        lowered = line.lower()
        if any(forbidden in lowered for forbidden in FORBIDDEN_COMMAND_SUBSTRINGS):
            return False
    return True


def choose_target_router(difficulty):
    """Weighted-random pick of the router to target, biased by difficulty.

    difficulty 1 faults (interface shutdown, missing neighbor) fit any router
    equally. difficulty 2-3 faults lean on the iBGP mesh and route-policy work,
    which live on the AS 65030 core routers R2/R3/R4; R1 and R5 are edge stubs,
    so they're kept as low-probability options rather than excluded outright.
    """
    routers = ["R1", "R2", "R3", "R4", "R5"]
    if difficulty == 1:
        weights = [1, 1, 1, 1, 1]
    else:
        weights = [1, 3, 3, 3, 1]
    return random.choices(routers, weights=weights, k=1)[0]


@app.post("/scenario/{scenario_id}/start")
def start_scenario(scenario_id: str):
    scenarios = load_scenarios()
    if scenario_id not in scenarios:
        raise HTTPException(status_code=404, detail="Scenario not found")
    scenario = scenarios[scenario_id]
    output = inject_fault(scenario)
    return {"status": "fault injected", "scenario": scenario_id, "output": output}


@app.post("/scenario/{scenario_id}/reset")
def reset_scenario(scenario_id: str):
    scenarios = load_scenarios()
    if scenario_id not in scenarios:
        raise HTTPException(status_code=404, detail="Scenario not found")
    scenario = scenarios[scenario_id]
    output = apply_fix(scenario)
    time.sleep(RESET_SETTLE_SECONDS)
    return {
        "status": "reset applied",
        "scenario": scenario_id,
        "output": output,
        "settle_seconds": RESET_SETTLE_SECONDS,
    }


@app.post("/scenario/{scenario_id}/validate")
def validate_scenario(scenario_id: str):
    scenarios = load_scenarios()
    if scenario_id not in scenarios:
        raise HTTPException(status_code=404, detail="Scenario not found")
    scenario = scenarios[scenario_id]
    return check_validation(scenario)


@app.post("/session/start")
def start_session(req: StartSessionRequest):
    scenarios = load_scenarios()
    if req.scenario_id not in scenarios:
        raise HTTPException(status_code=404, detail="Scenario not found")
    scenario = scenarios[req.scenario_id]
    output = inject_fault(scenario)

    session_id = str(uuid.uuid4())
    SESSIONS[session_id] = {
        "source": "yaml",
        "scenario_id": req.scenario_id,
        "hints_revealed": 0,
        "solved": False,
    }
    return {"session_id": session_id, "scenario_id": req.scenario_id, "output": output}


@app.post("/session/hint")
def session_hint(req: SessionIdRequest):
    session = SESSIONS.get(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.get("source") == "ai-generated":
        hints = session.get("hints", [])
    else:
        scenarios = load_scenarios()
        scenario = scenarios[session["scenario_id"]]
        hints = scenario.get("hints", [])
    revealed = session["hints_revealed"]

    if revealed >= len(hints):
        return {"hint": None, "hints_revealed": revealed, "total_hints": len(hints)}

    hint = hints[revealed]
    session["hints_revealed"] += 1
    return {
        "hint": hint["text"],
        "cost": hint["cost"],
        "hints_revealed": session["hints_revealed"],
        "total_hints": len(hints),
    }


@app.post("/session/validate")
def session_validate(req: SessionIdRequest):
    session = SESSIONS.get(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.get("source") == "ai-generated":
        result = check_validation_topology(session.get("target_router"))
    else:
        scenarios = load_scenarios()
        scenario = scenarios[session["scenario_id"]]
        result = check_validation(scenario)

    if result["passed"]:
        session["solved"] = True
    result["solved"] = session["solved"]
    return result


@app.post("/session/reset")
def session_reset(req: SessionIdRequest):
    session = SESSIONS.get(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.get("source") == "ai-generated":
        fix_commands = session["fix_commands"]
        output = restore_snapshot(
            session["target_router"],
            session["pre_fault_snapshot"],
            fix_commands=fix_commands,
            soft_clear=commands_touch_policy(fix_commands),
        )
    else:
        scenarios = load_scenarios()
        scenario = scenarios[session["scenario_id"]]
        output = apply_fix(scenario)

    # Hold the response until the control plane reconverges, so a validate the
    # caller fires the moment this returns sees settled state, not a transient.
    time.sleep(RESET_SETTLE_SECONDS)

    return {
        "status": "reset applied",
        "session_id": req.session_id,
        "output": output,
        "settle_seconds": RESET_SETTLE_SECONDS,
    }


@app.get("/session/{session_id}/reveal")
def session_reveal(session_id: str):
    """Reveal what the fault actually was -- but only once the session is solved.

    This is the post-mortem view: which router was touched, the category of
    fault, the exact config lines that were injected, and (for AI-generated
    faults) the internal_reasoning. Gated on session["solved"] so it can't be
    used to skip the troubleshooting -- an unsolved session gets a 403, not the
    answer. For scripted (yaml) scenarios the fields come from the scenario file;
    category / internal_reasoning aren't part of that schema, so they come back
    null.
    """
    session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    if not session.get("solved"):
        raise HTTPException(
            status_code=403,
            detail=(
                "The fault is only revealed once the scenario is solved. Fix the "
                "topology and pass validation first."
            ),
        )

    if session.get("source") == "ai-generated":
        source = session
    else:
        scenarios = load_scenarios()
        source = scenarios.get(session["scenario_id"], {})

    return {
        "title": source.get("title"),
        "target_router": source.get("target_router"),
        "category": source.get("category"),
        "fault_commands": source.get("fault_commands", []),
        "internal_reasoning": source.get("internal_reasoning"),
    }


@app.post("/ai/generate-scenario")
def generate_scenario(req: GenerateScenarioRequest):
    if req.difficulty not in (1, 2, 3):
        raise HTTPException(status_code=400, detail="difficulty must be 1, 2, or 3")

    chosen_router = choose_target_router(req.difficulty)

    # Refuse to generate on a router that isn't already clean: otherwise the
    # pre_fault_snapshot would capture leftover config from a prior unresolved
    # fault as the "baseline," and reset would restore to that broken state.
    is_clean, details = check_router_clean(chosen_router)
    if not is_clean:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"{chosen_router} is not in a clean state (likely leftover config "
                    f"from an unresolved fault). Reset or resolve the existing issue on "
                    f"{chosen_router} before generating a new scenario."
                ),
                "router": chosen_router,
                **details,
            },
        )

    response = anthropic_client.messages.create(
        model="claude-opus-4-8",
        # The inject_fault tool_use output has grown to eight fields -- two
        # command arrays, a verbose internal_reasoning, and two hint objects that
        # the prompt asks for LAST. At 1024 the JSON was truncated before hints
        # were emitted, so fault.get("hints") came back empty and AI sessions had
        # a permanently disabled hint button. Give the tool call room to finish.
        max_tokens=4096,
        system=TOPOLOGY_PROMPT,
        tools=[INJECT_FAULT_TOOL],
        tool_choice={"type": "tool", "name": "inject_fault"},
        messages=[
            {
                "role": "user",
                "content": (
                    f"Generate a difficulty {req.difficulty} BGP fault scenario. "
                    f"Target this specific router: {chosen_router}. You MUST set "
                    f"target_router to {chosen_router} and write every fault_commands "
                    f"and fix_commands line to be applied on {chosen_router}. Do not "
                    f"pick a different router. Within that constraint, invent the "
                    f"specific fault type, the exact IOS commands, and the symptom "
                    f"description yourself."
                ),
            }
        ],
    )

    # Fail loudly on a truncated tool call. A max_tokens stop leaves the tool_use
    # JSON cut off, so trailing fields (hints are emitted last) silently go
    # missing -- exactly the bug that disabled the hint button. Better a clear
    # 502 than a scenario built from a half-populated fault.
    if response.stop_reason == "max_tokens":
        raise HTTPException(
            status_code=502,
            detail=(
                "Model response hit the max_tokens limit; the inject_fault tool call was "
                "truncated and would be missing fields. Retry generation."
            ),
        )

    tool_use = next((block for block in response.content if block.type == "tool_use"), None)
    if tool_use is None:
        raise HTTPException(status_code=502, detail="Model did not call the inject_fault tool")

    fault = tool_use.input

    print(
        "[ai/generate-scenario] Claude inject_fault tool call: "
        f"requested_router={chosen_router!r}, "
        f"target_router={fault.get('target_router')!r}, "
        f"category={fault.get('category')!r}, "
        f"fault_commands={fault.get('fault_commands')!r}, "
        f"fix_commands={fault.get('fix_commands')!r}, "
        f"internal_reasoning={fault.get('internal_reasoning')!r}"
    )

    if fault.get("target_router") != chosen_router:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Model targeted {fault.get('target_router')!r} but was instructed to "
                f"target {chosen_router!r}"
            ),
        )

    commands = fault.get("fault_commands", []) + fault.get("fix_commands", [])
    if not validate_generated_commands(commands):
        raise HTTPException(status_code=400, detail="Generated commands failed safety validation")

    # Baseline for the inert-fault check below. Taken before the push and while
    # we hold no connection to the target, so the sweep isn't competing with our
    # own session for a vty line.
    before_health = run_health_check()

    ip = ROUTERS[fault["target_router"]]
    conn = get_conn(ip)
    try:
        pre_fault_snapshot = conn.send_command("show running-config")
        conn.send_config_set(fault["fault_commands"])
        if commands_touch_policy(fault["fault_commands"]):
            print(
                f"[ai/generate-scenario] policy fault on {fault['target_router']}: "
                "soft-clearing BGP so the new policy is applied to existing routes"
            )
            soft_clear_bgp(conn)
    finally:
        conn.disconnect()

    # Confirm the fault actually broke something. A filter applied in the wrong
    # direction, or at a filter point the iBGP full mesh routes around, config-
    # pushes cleanly and leaves the topology entirely healthy -- which would hand
    # the learner a scenario with no findable cause. Poll rather than sleep-once:
    # fast faults are confirmed on the first sweep, slow RIB-convergence ones get
    # up to FAULT_POLL_TIMEOUT_SECONDS to appear. Compare against the baseline
    # rather than against perfect health, so pre-existing damage isn't credited
    # to this fault.
    had_effect, effect_details, detect_seconds = poll_fault_effect(before_health)

    print(
        f"[ai/generate-scenario] fault effect check on {fault['target_router']}: "
        f"had_effect={had_effect}, detected_in={detect_seconds:.1f}s, {effect_details}"
    )

    if not had_effect:
        rollback_error = None
        try:
            # Same two-push rollback as /session/reset: fix_commands negates what
            # the fault added, then the (management-plane filtered) snapshot
            # sweeps up anything an incomplete fix left behind. That matters most
            # here -- leftovers from an inert fault are themselves inert, so
            # check_router_clean won't flag them on the next generation and they
            # would accumulate unnoticed.
            restore_snapshot(
                fault["target_router"],
                pre_fault_snapshot,
                fix_commands=fault["fix_commands"],
                soft_clear=commands_touch_policy(fault["fix_commands"]),
            )
        except Exception as e:
            rollback_error = str(e)
            print(
                f"[ai/generate-scenario] ROLLBACK FAILED on {fault['target_router']}: {e} "
                f"-- inert fault config may still be present"
            )

        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    "Generated fault had no effect on the topology: no new unhealthy "
                    "neighbors and no new missing routes after injection. The fault was "
                    "rolled back and no session was created. Retry generation."
                ),
                "router": fault["target_router"],
                "category": fault.get("category"),
                "fault_commands": fault.get("fault_commands"),
                "internal_reasoning": fault.get("internal_reasoning"),
                "effect_check": effect_details,
                "rollback_error": rollback_error,
            },
        )

    session_id = str(uuid.uuid4())
    # The fault is already on the device and confirmed effective by this point,
    # so a missing optional field must not raise: a KeyError here would 500
    # without creating a session or rolling back, orphaning a live fault with no
    # way to reset it. target_router is required and already validated above; the
    # rest fall back to sensible defaults. fix_commands defaulting to [] means a
    # later reset only replays the snapshot, which is still a valid (if less
    # surgical) recovery.
    SESSIONS[session_id] = {
        "source": "ai-generated",
        "title": fault.get("title", "Generated BGP fault"),
        "description": fault.get("description", ""),
        "target_router": fault["target_router"],
        "category": fault.get("category"),
        # Stored so a SOLVED session can reveal what was injected (see
        # /session/{id}/reveal). Never returned to a learner who hasn't solved it.
        "fault_commands": fault.get("fault_commands", []),
        "fix_commands": fault.get("fix_commands", []),
        # Server-side only: logged and available for sanity-checking the fault.
        # Never include this in any response returned to the learner - it gives
        # away the cause and the fix.
        "internal_reasoning": fault.get("internal_reasoning"),
        "pre_fault_snapshot": pre_fault_snapshot,
        "hints_revealed": 0,
        "hints": fault.get("hints", []),
        "solved": False,
        "difficulty": req.difficulty,
    }

    print(
        f"[ai/generate-scenario] stored session {session_id}: "
        f"target_router={SESSIONS[session_id]['target_router']!r} "
        f"(validation will sweep the full topology, not just this router)"
    )

    return {
        "session_id": session_id,
        "title": SESSIONS[session_id]["title"],
        "description": SESSIONS[session_id]["description"],
        "difficulty": req.difficulty,
    }