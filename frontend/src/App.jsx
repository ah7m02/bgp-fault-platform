import { useCallback, useEffect, useRef, useState } from 'react'
import * as api from './api'
import { API_BASE } from './api'
import { ROUTERS, summarizeHealth } from './topology'
import { Badge, Button, Dot, ErrorNote, Panel, Spinner } from './components/ui'
import { TopologyMap } from './components/TopologyMap'
import { RouterCard } from './components/RouterCard'
import { ScenarioLauncher } from './components/ScenarioLauncher'
import { SessionPanel } from './components/SessionPanel'

// A /health sweep SSHes into all five routers and does five lookups on each, so
// it takes tens of seconds. Auto-refresh is opt-in and deliberately slow.
const AUTO_REFRESH_MS = 60_000

function useHealth() {
  const [health, setHealth] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [lastUpdated, setLastUpdated] = useState(null)
  const inFlight = useRef(null)

  const refresh = useCallback(async () => {
    // Overlapping sweeps would fight over the routers' vty lines.
    if (inFlight.current) return inFlight.current
    const controller = new AbortController()
    setLoading(true)
    const run = api
      .getHealth(controller.signal)
      .then((data) => {
        setHealth(data)
        setLastUpdated(Date.now())
        setError(null)
      })
      .catch((e) => {
        if (e.name !== 'AbortError') setError(e)
      })
      .finally(() => {
        inFlight.current = null
        setLoading(false)
      })
    inFlight.current = run
    return run
  }, [])

  return { health, loading, error, lastUpdated, refresh, dismissError: () => setError(null) }
}

function StatusStrip({ summary, loading, lastUpdated }) {
  const items = [
    { label: 'Healthy', value: summary?.ok, status: 'ok' },
    { label: 'Degraded', value: summary?.degraded, status: 'warn' },
    { label: 'Unreachable', value: summary?.unreachable, status: 'down' },
  ]
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
      {items.map((item) => (
        <span key={item.label} className="flex items-center gap-2 text-sm">
          <Dot status={item.status} />
          <span className="font-mono text-ink-50 tabular-nums">
            {item.value ?? '—'}
          </span>
          <span className="text-ink-400">{item.label}</span>
        </span>
      ))}
      <span className="text-xs text-ink-400">
        {loading ? (
          <span className="flex items-center gap-1.5">
            <Spinner /> Sweeping topology…
          </span>
        ) : lastUpdated ? (
          `Updated ${new Date(lastUpdated).toLocaleTimeString()}`
        ) : (
          'Never swept'
        )}
      </span>
    </div>
  )
}

export default function App() {
  const { health, loading, error: healthError, lastUpdated, refresh, dismissError } =
    useHealth()

  const [scenarios, setScenarios] = useState([])
  const [scenariosLoading, setScenariosLoading] = useState(true)
  const [session, setSession] = useState(null)
  const [busy, setBusy] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [expanded, setExpanded] = useState(null)
  const [autoRefresh, setAutoRefresh] = useState(false)
  // On by default: the point of the console is to practise finding the fault,
  // not to read it off the cards.
  const [blind, setBlind] = useState(true)

  useEffect(() => {
    const controller = new AbortController()
    api
      .getScenarios(controller.signal)
      .then(setScenarios)
      .catch((e) => {
        if (e.name !== 'AbortError') setActionError(e)
      })
      .finally(() => setScenariosLoading(false))
    refresh()
    return () => controller.abort()
  }, [refresh])

  useEffect(() => {
    if (!autoRefresh) return
    const id = setInterval(refresh, AUTO_REFRESH_MS)
    return () => clearInterval(id)
  }, [autoRefresh, refresh])

  /** Every session action is one in-flight request with a shared busy key. */
  const run = useCallback(async (key, fn) => {
    setBusy(key)
    setActionError(null)
    try {
      return await fn()
    } catch (e) {
      setActionError(e)
      return null
    } finally {
      setBusy(null)
    }
  }, [])

  const handleStart = (scenarioId) =>
    run(scenarioId, async () => {
      const scenario = scenarios.find((s) => s.id === scenarioId)
      const res = await api.startSession(scenarioId)
      setSession({
        id: res.session_id,
        title: scenario?.title ?? scenarioId,
        description: scenario?.description ?? '',
        difficulty: scenario?.difficulty,
        startedAt: Date.now(),
        hints: [],
        hintsExhausted: false,
        validation: null,
        solved: false,
      })
      refresh()
    })

  const handleGenerate = (difficulty) =>
    run('generate', async () => {
      const res = await api.generateScenario(difficulty)
      setSession({
        id: res.session_id,
        title: res.title,
        description: res.description,
        difficulty: res.difficulty,
        startedAt: Date.now(),
        hints: [],
        // AI-generated sessions are stored with an empty hints list server-side.
        hintsExhausted: true,
        validation: null,
        solved: false,
      })
      refresh()
    })

  const handleHint = () =>
    run('hint', async () => {
      const res = await api.sessionHint(session.id)
      setSession((prev) =>
        res.hint == null
          ? { ...prev, hintsExhausted: true }
          : {
              ...prev,
              hints: [...prev.hints, { text: res.hint, cost: res.cost }],
              hintsExhausted: res.hints_revealed >= res.total_hints,
            },
      )
    })

  const handleValidate = () =>
    run('validate', async () => {
      const result = await api.sessionValidate(session.id)
      setSession((prev) => ({ ...prev, validation: result, solved: result.solved }))
      refresh()
    })

  const handleReset = () =>
    run('reset', async () => {
      await api.sessionReset(session.id)
      setSession((prev) => ({ ...prev, validation: null, solved: false, reveal: null }))
      refresh()
    })

  const handleReveal = () =>
    run('reveal', async () => {
      const data = await api.sessionReveal(session.id)
      setSession((prev) => ({ ...prev, reveal: data }))
    })

  const summary = summarizeHealth(health)

  return (
    <div className="mx-auto flex min-h-full max-w-7xl flex-col gap-5 px-4 py-6 sm:px-6">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-ink-50">BGP Lab Console</h1>
          <p className="mt-0.5 text-sm text-ink-400">
            Five-router Cisco IOS topology ·{' '}
            <span className="font-mono text-xs">{API_BASE}</span>
          </p>
        </div>
        <div className="flex items-center gap-2">
          {summary && (
            <Badge status={summary.healthy ? 'ok' : 'warn'}>
              {summary.healthy ? 'All clear' : 'Fault present'}
            </Badge>
          )}
          <label className="flex cursor-pointer items-center gap-2 text-xs text-ink-400">
            <input
              type="checkbox"
              checked={blind}
              onChange={(e) => setBlind(e.target.checked)}
              className="size-3.5 accent-brand"
            />
            Blind mode
          </label>
          <label className="flex cursor-pointer items-center gap-2 text-xs text-ink-400">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
              className="size-3.5 accent-brand"
            />
            Auto 60s
          </label>
          <Button busy={loading} onClick={refresh}>
            Refresh health
          </Button>
        </div>
      </header>

      <p className="-mt-2 text-xs text-ink-400">
        {blind
          ? 'Blind mode is on: each router shows only its health badge — the topology still colours by status, but neighbour counts, missing routes, and per-session detail are hidden so you have to find the symptom yourself. Turn it off to reveal full diagnostics.'
          : 'Blind mode is off: full diagnostics are shown. Turn it on to hide the symptom and troubleshoot from the routers themselves.'}
      </p>

      <ErrorNote error={healthError} onDismiss={dismissError} />
      <ErrorNote error={actionError} onDismiss={() => setActionError(null)} />

      <div className="grid gap-5 lg:grid-cols-[minmax(0,2fr)_minmax(320px,1fr)]">
        <div className="flex flex-col gap-5">
          <Panel
            title="Topology"
            subtitle="Click a router to expand its BGP neighbors below"
          >
            <StatusStrip
              summary={summary}
              loading={loading}
              lastUpdated={lastUpdated}
            />
            <div className="mt-4">
              <TopologyMap
                health={health}
                selected={expanded}
                onSelect={setExpanded}
              />
            </div>
          </Panel>

          <Panel title="Routers" subtitle="BGP session state and loopback reachability">
            <div className="grid gap-3 sm:grid-cols-2">
              {ROUTERS.map((router) => (
                <RouterCard
                  key={router.name}
                  router={router}
                  health={health?.[router.name]}
                  blind={blind}
                  expanded={expanded === router.name}
                  onToggle={() =>
                    setExpanded((prev) => (prev === router.name ? null : router.name))
                  }
                />
              ))}
            </div>
          </Panel>
        </div>

        <div className="flex flex-col gap-5">
          <Panel title="Active session">
            <SessionPanel
              session={session}
              busy={busy}
              onHint={handleHint}
              onValidate={handleValidate}
              onReset={handleReset}
              onReveal={handleReveal}
              onClear={() => setSession(null)}
            />
          </Panel>

          <Panel title="Inject a fault">
            <ScenarioLauncher
              scenarios={scenarios}
              loading={scenariosLoading}
              busy={busy}
              disabled={Boolean(session) && !session.solved}
              onStart={handleStart}
              onGenerate={handleGenerate}
            />
            {session && !session.solved && (
              <p className="mt-3 text-[11px] text-ink-400">
                Reset or solve the active session before injecting another fault —
                stacked faults make the baseline snapshot unreliable.
              </p>
            )}
          </Panel>
        </div>
      </div>
    </div>
  )
}
