import { ROUTERS, LINKS, routerStatus } from '../topology'
import { Dot } from './ui'

const NODE_STATUS_RING = {
  ok: 'border-ok/50 bg-ok/5',
  warn: 'border-warn/60 bg-warn/5',
  down: 'border-down/60 bg-down/5',
  idle: 'border-ink-700 bg-ink-850',
}

/**
 * The chain runs R1—R2—R3—R4—R5. A link is drawn as degraded when either
 * endpoint is unhealthy: /health reports per-router state, not per-adjacency,
 * so this is an indication of where to look rather than a claim about the wire.
 */
function linkStatus(health, from, to) {
  const ends = [routerStatus(health?.[from]), routerStatus(health?.[to])]
  if (ends.includes('idle')) return 'idle'
  if (ends.includes('down')) return 'down'
  if (ends.includes('warn')) return 'warn'
  return 'ok'
}

const LINK_COLOR = {
  ok: 'bg-ok/40',
  warn: 'bg-warn/50',
  down: 'bg-down/50',
  idle: 'bg-ink-700',
}

export function TopologyMap({ health, selected, onSelect }) {
  return (
    <div className="overflow-x-auto">
      <div className="flex min-w-[640px] items-start">
        {ROUTERS.map((router, i) => {
          const status = routerStatus(health?.[router.name])
          const link = LINKS[i]
          const isSelected = selected === router.name
          return (
            <div key={router.name} className="flex flex-1 items-start">
              <button
                type="button"
                onClick={() => onSelect?.(isSelected ? null : router.name)}
                aria-pressed={isSelected}
                className={`flex w-24 shrink-0 flex-col items-center gap-1 rounded-lg border px-2 py-3 transition-colors hover:border-ink-600 ${NODE_STATUS_RING[status]} ${isSelected ? 'ring-2 ring-brand/60' : ''}`}
              >
                <Dot status={status} />
                <span className="font-mono text-sm font-semibold text-ink-50">
                  {router.name}
                </span>
                <span className="font-mono text-[11px] text-ink-400">
                  AS {router.as}
                </span>
              </button>

              {link && (
                <div className="flex flex-1 flex-col items-center gap-1 pt-6">
                  <div className={`h-0.5 w-full rounded ${LINK_COLOR[linkStatus(health, link.from, link.to)]}`} />
                  <span className="font-mono text-[10px] whitespace-nowrap text-ink-400">
                    {link.prefix}
                  </span>
                  <span className="text-[10px] tracking-wide text-ink-600 uppercase">
                    {link.kind}
                  </span>
                </div>
              )}
            </div>
          )
        })}
      </div>

      {/* R2/R3/R4 also peer directly with each other over Loopback0, which the
          linear chain above cannot show. */}
      <div className="mt-3 flex min-w-[640px] items-center gap-2 pl-[calc(20%+0.5rem)]">
        <div className="h-3 w-[52%] rounded-b border-x border-b border-ink-700" />
        <span className="text-[11px] whitespace-nowrap text-ink-400">
          AS 65030 · iBGP full mesh via Loopback0
        </span>
      </div>
    </div>
  )
}
