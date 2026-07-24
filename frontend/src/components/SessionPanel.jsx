import { useEffect, useState } from 'react'
import { Badge, Button, Empty } from './ui'
import { DifficultyBadge } from './ScenarioLauncher'

function Elapsed({ since }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])
  const total = Math.max(0, Math.floor((now - since) / 1000))
  const mm = String(Math.floor(total / 60)).padStart(2, '0')
  const ss = String(total % 60).padStart(2, '0')
  return <span className="font-mono tabular-nums">{mm}:{ss}</span>
}

/** Renders both validation shapes: the scripted one (single router vs. a
 *  minimum neighbor count) and the AI one (whole-topology sweep). */
function ValidationResult({ result }) {
  if (!result) return null

  const unhealthy = Object.entries(result.unhealthy_routers ?? {})
  const missing = Object.entries(result.missing_routes ?? {})
  const unreachable = result.unreachable_routers ?? []

  return (
    <div
      className={`rounded-lg border p-3 ${
        result.passed
          ? 'border-ok/40 bg-ok/10'
          : 'border-warn/40 bg-warn/10'
      }`}
    >
      <p className={`text-sm font-semibold ${result.passed ? 'text-ok' : 'text-warn'}`}>
        {result.passed ? 'Solved — topology is healthy' : 'Not solved yet'}
      </p>

      {result.reason && (
        <p className="mt-1 text-xs text-ink-200">{result.reason}</p>
      )}

      {typeof result.established_neighbors === 'number' && (
        <p className="mt-1 text-xs text-ink-200">
          {result.router}: {result.established_neighbors} established neighbor
          {result.established_neighbors === 1 ? '' : 's'} (needs {result.required})
        </p>
      )}

      {!result.passed && unreachable.length > 0 && (
        <p className="mt-1 text-xs text-ink-200">
          Unreachable: <span className="font-mono">{unreachable.join(', ')}</span>
        </p>
      )}

      {!result.passed && unhealthy.length > 0 && (
        <p className="mt-1 text-xs text-ink-200">
          Sessions down:{' '}
          <span className="font-mono">
            {unhealthy.map(([r, n]) => `${r} (${n})`).join(', ')}
          </span>
        </p>
      )}

      {!result.passed && missing.length > 0 && (
        <div className="mt-1 text-xs text-ink-200">
          Missing routes:
          <ul className="mt-0.5 ml-3 list-disc">
            {missing.map(([router, prefixes]) => (
              <li key={router} className="font-mono">
                {router} → {prefixes.join(', ')}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

/** Post-mortem shown only after a session is solved. */
function RevealBlock({ reveal }) {
  return (
    <div className="mt-2 rounded-lg border border-ink-700/70 bg-ink-850 p-3">
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
        <dt className="text-ink-400">Router</dt>
        <dd className="font-mono text-ink-50">{reveal.target_router ?? '—'}</dd>
        <dt className="text-ink-400">Category</dt>
        <dd className="font-mono text-ink-50">{reveal.category ?? '—'}</dd>
      </dl>

      {reveal.fault_commands?.length > 0 && (
        <>
          <p className="mt-3 mb-1 text-[11px] tracking-wide text-ink-400 uppercase">
            Injected config
          </p>
          <pre className="overflow-x-auto rounded-md bg-ink-950 p-3 text-xs leading-relaxed text-ink-200">
            <code className="font-mono">{reveal.fault_commands.join('\n')}</code>
          </pre>
        </>
      )}

      {reveal.internal_reasoning && (
        <>
          <p className="mt-3 mb-1 text-[11px] tracking-wide text-ink-400 uppercase">
            Why it broke
          </p>
          <p className="text-xs leading-relaxed text-ink-200">
            {reveal.internal_reasoning}
          </p>
        </>
      )}
    </div>
  )
}

export function SessionPanel({
  session,
  busy,
  onHint,
  onValidate,
  onReset,
  onReveal,
  onClear,
}) {
  const [showReveal, setShowReveal] = useState(false)

  // Collapse (and forget) the reveal whenever the session changes or is reset,
  // so a fresh fault never opens already showing the previous answer.
  const sessionId = session?.id
  const solved = session?.solved
  useEffect(() => {
    if (!solved) setShowReveal(false)
  }, [sessionId, solved])

  if (!session) {
    return (
      <Empty>
        No active session. Start a scripted scenario or generate one to inject a
        fault into the lab.
      </Empty>
    )
  }

  const { hints, validation, hintsExhausted, reveal } = session

  const toggleReveal = async () => {
    if (!showReveal && !reveal) await onReveal()
    setShowReveal((v) => !v)
  }

  return (
    <div className="flex flex-col gap-3">
      <div>
        <div className="flex items-start justify-between gap-2">
          <h3 className="text-base font-semibold text-ink-50">{session.title}</h3>
          {session.difficulty != null && (
            <DifficultyBadge level={session.difficulty} />
          )}
        </div>
        <p className="mt-1 text-sm leading-relaxed text-ink-200">
          {session.description}
        </p>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-ink-400">
          <Badge status={session.solved ? 'ok' : 'brand'}>
            {session.solved ? 'Solved' : 'In progress'}
          </Badge>
          <span>
            Elapsed <Elapsed since={session.startedAt} />
          </span>
          <span className="font-mono text-[11px] text-ink-600">
            {session.id.slice(0, 8)}
          </span>
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        <Button
          variant="primary"
          busy={busy === 'validate'}
          disabled={Boolean(busy)}
          onClick={onValidate}
        >
          Validate fix
        </Button>
        <Button
          busy={busy === 'hint'}
          disabled={Boolean(busy) || hintsExhausted}
          onClick={onHint}
        >
          {hintsExhausted ? 'No hints left' : 'Reveal hint'}
        </Button>
        <Button
          variant="danger"
          busy={busy === 'reset'}
          disabled={Boolean(busy)}
          onClick={onReset}
        >
          Reset lab
        </Button>
        {session.solved && (
          <Button disabled={Boolean(busy)} onClick={onClear}>
            Clear
          </Button>
        )}
      </div>

      {session.solved && (
        <div>
          <Button
            variant="ghost"
            busy={busy === 'reveal'}
            disabled={Boolean(busy)}
            onClick={toggleReveal}
            aria-expanded={showReveal}
            className="w-full"
          >
            {showReveal ? 'Hide the fault' : 'Show me what broke'}
          </Button>
          {showReveal && reveal && <RevealBlock reveal={reveal} />}
        </div>
      )}

      <ValidationResult result={validation} />

      {hints.length > 0 && (
        <ul className="flex flex-col gap-2">
          {hints.map((hint, i) => (
            <li
              key={i}
              className="rounded-lg border border-ink-700/70 bg-ink-850 p-3 text-sm text-ink-200"
            >
              <div className="mb-1 flex items-center justify-between text-xs text-ink-400">
                <span>Hint {i + 1}</span>
                {hint.cost != null && <span>−{hint.cost} pts</span>}
              </div>
              {hint.text}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
