// Mirrors the lab described in main.py's TOPOLOGY_PROMPT. R2/R3/R4 form a full
// iBGP mesh in AS 65030; R1 and R5 are eBGP stubs at either end of the chain.
export const ROUTERS = [
  { name: 'R1', mgmt: '192.168.0.241', loopback: '10.0.0.1', as: '65010', role: 'eBGP stub' },
  { name: 'R2', mgmt: '192.168.0.242', loopback: '10.0.0.2', as: '65030', role: 'eBGP edge → R1' },
  { name: 'R3', mgmt: '192.168.0.243', loopback: '10.0.0.3', as: '65030', role: 'iBGP core' },
  { name: 'R4', mgmt: '192.168.0.244', loopback: '10.0.0.4', as: '65030', role: 'eBGP edge → R5' },
  { name: 'R5', mgmt: '192.168.0.245', loopback: '10.0.0.5', as: '65020', role: 'eBGP stub' },
]

export const LINKS = [
  { from: 'R1', to: 'R2', prefix: '10.12.0.0/30', kind: 'eBGP' },
  { from: 'R2', to: 'R3', prefix: '10.23.0.0/30', kind: 'iBGP' },
  { from: 'R3', to: 'R4', prefix: '10.34.0.0/30', kind: 'iBGP' },
  { from: 'R4', to: 'R5', prefix: '10.45.0.0/30', kind: 'eBGP' },
]

/** Which router owns a loopback, so missing-route chips can be labelled. */
export const LOOPBACK_OWNER = Object.fromEntries(
  ROUTERS.map((r) => [r.loopback, r.name]),
)

/**
 * Collapse one router's /health entry to a single status.
 *
 * `down` means we learned nothing (SSH/auth failure) — main.py deliberately
 * leaves missing_routes empty in that case, so an unreachable box must not be
 * read as "no missing routes". `warn` is a router we did reach that has
 * neighbors out of Established or loopbacks absent from its RIB.
 */
export function routerStatus(health) {
  if (!health) return 'idle'
  if (health.error) return 'down'
  const missing = health.missing_routes?.length ?? 0
  if ((health.unhealthy ?? 0) > 0 || missing > 0) return 'warn'
  return 'ok'
}

export function summarizeHealth(health) {
  if (!health) return null
  const entries = ROUTERS.map((r) => [r.name, routerStatus(health[r.name])])
  return {
    ok: entries.filter(([, s]) => s === 'ok').length,
    degraded: entries.filter(([, s]) => s === 'warn').length,
    unreachable: entries.filter(([, s]) => s === 'down').length,
    healthy: entries.every(([, s]) => s === 'ok'),
  }
}
