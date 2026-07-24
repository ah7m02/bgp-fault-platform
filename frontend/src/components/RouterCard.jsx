import { LOOPBACK_OWNER, routerStatus } from '../topology'
import { Badge, Dot } from './ui'

const STATUS_LABEL = {
  ok: 'Healthy',
  warn: 'Degraded',
  down: 'Unreachable',
  idle: 'Unknown',
}

function NeighborRow({ neighbor }) {
  // main.py treats a numeric state_or_prefixes_received as Established: the
  // column holds a prefix count when the session is up and a state name
  // (Idle/Active/Connect) when it is not.
  const value = String(neighbor.state_or_prefixes_received ?? '')
  const established = /^\d+$/.test(value)
  return (
    <tr className="border-t border-ink-800">
      <td className="py-1 pr-2 font-mono text-ink-200">{neighbor.bgp_neighbor}</td>
      <td className="py-1 pr-2 font-mono text-ink-400">{neighbor.neighbor_as}</td>
      <td className="py-1 pr-2 font-mono text-ink-400">{neighbor.up_down}</td>
      <td className="py-1 text-right">
        <span className={established ? 'text-ok' : 'text-down'}>
          {established ? `${value} pfx` : value || '—'}
        </span>
      </td>
    </tr>
  )
}

export function RouterCard({ router, health, expanded, onToggle, blind = false }) {
  const status = routerStatus(health)
  const neighbors = health?.raw ?? []
  const missing = health?.missing_routes ?? []

  // Blind mode: the badge is the only status signal. Everything that would let
  // you read the specific symptom off the card -- counts, the SSH error, the
  // missing-route chips, and the expandable neighbor table -- is withheld so the
  // learner has to go find it on the routers themselves.
  return (
    <article
      className={`rounded-xl border bg-ink-850 transition-colors ${
        status === 'down'
          ? 'border-down/40'
          : status === 'warn'
            ? 'border-warn/40'
            : 'border-ink-700/70'
      }`}
    >
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={!blind && expanded}
        className="flex w-full items-center gap-3 px-4 py-3 text-left"
      >
        <Dot status={status} pulse={status !== 'ok' && status !== 'idle'} />
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline gap-2">
            <span className="font-mono text-base font-semibold text-ink-50">
              {router.name}
            </span>
            <span className="font-mono text-xs text-ink-400">{router.mgmt}</span>
          </div>
          <p className="truncate text-xs text-ink-400">
            AS {router.as} · {router.role} · lo0 {router.loopback}
          </p>
        </div>
        <Badge status={status === 'idle' ? 'idle' : status}>
          {STATUS_LABEL[status]}
        </Badge>
      </button>

      {!blind && (
        <div className="flex flex-wrap gap-x-5 gap-y-1 border-t border-ink-800 px-4 py-2 text-xs">
          <span className="text-ink-400">
            Neighbors <span className="font-mono text-ink-50">{health?.neighbors ?? '—'}</span>
          </span>
          <span className="text-ink-400">
            Not established{' '}
            <span className={`font-mono ${health?.unhealthy ? 'text-down' : 'text-ink-50'}`}>
              {health?.unhealthy ?? '—'}
            </span>
          </span>
          <span className="text-ink-400">
            Missing routes{' '}
            <span className={`font-mono ${missing.length ? 'text-warn' : 'text-ink-50'}`}>
              {health?.error ? '?' : missing.length}
            </span>
          </span>
        </div>
      )}

      {!blind && health?.error && (
        <p className="border-t border-ink-800 px-4 py-2 font-mono text-xs break-words text-down">
          {health.error}
        </p>
      )}

      {!blind && missing.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 border-t border-ink-800 px-4 py-2">
          <span className="text-xs text-ink-400">Cannot route to:</span>
          {missing.map((prefix) => (
            <span
              key={prefix}
              className="rounded bg-warn/10 px-1.5 py-0.5 font-mono text-[11px] text-warn"
            >
              {prefix}
              {LOOPBACK_OWNER[prefix] && (
                <span className="text-warn/60"> ({LOOPBACK_OWNER[prefix]})</span>
              )}
            </span>
          ))}
        </div>
      )}

      {!blind && expanded && (
        <div className="border-t border-ink-800 px-4 py-3">
          {neighbors.length === 0 ? (
            <p className="text-xs text-ink-400">No BGP neighbor data.</p>
          ) : (
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-[11px] tracking-wide text-ink-400 uppercase">
                  <th className="pb-1 font-medium">Neighbor</th>
                  <th className="pb-1 font-medium">AS</th>
                  <th className="pb-1 font-medium">Up/Down</th>
                  <th className="pb-1 text-right font-medium">State</th>
                </tr>
              </thead>
              <tbody>
                {neighbors.map((n) => (
                  <NeighborRow key={n.bgp_neighbor} neighbor={n} />
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </article>
  )
}
