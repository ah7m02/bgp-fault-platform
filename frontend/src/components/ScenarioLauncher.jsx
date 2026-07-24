import { useState } from 'react'
import { Badge, Button, Empty, Spinner } from './ui'

const DIFFICULTY_LABEL = { 1: 'Easy', 2: 'Moderate', 3: 'Subtle' }
const DIFFICULTY_STATUS = { 1: 'ok', 2: 'warn', 3: 'down' }

export function DifficultyBadge({ level }) {
  return (
    <Badge status={DIFFICULTY_STATUS[level] ?? 'idle'}>
      {DIFFICULTY_LABEL[level] ?? `Level ${level}`}
    </Badge>
  )
}

export function ScenarioLauncher({
  scenarios,
  loading,
  busy,
  disabled,
  onStart,
  onGenerate,
}) {
  const [difficulty, setDifficulty] = useState(1)

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h3 className="mb-2 text-xs font-semibold tracking-wide text-ink-400 uppercase">
          AI-generated fault
        </h3>
        <div className="rounded-lg border border-ink-700/70 bg-ink-850 p-3">
          <div className="flex gap-1.5" role="group" aria-label="Difficulty">
            {[1, 2, 3].map((level) => (
              <button
                key={level}
                type="button"
                aria-pressed={difficulty === level}
                onClick={() => setDifficulty(level)}
                className={`flex-1 rounded-md px-2 py-1.5 text-xs font-medium transition-colors ${
                  difficulty === level
                    ? 'bg-brand/15 text-brand ring-1 ring-brand/40 ring-inset'
                    : 'bg-ink-800 text-ink-400 hover:text-ink-200'
                }`}
              >
                {level} · {DIFFICULTY_LABEL[level]}
              </button>
            ))}
          </div>
          <Button
            variant="primary"
            busy={busy === 'generate'}
            disabled={disabled || Boolean(busy)}
            onClick={() => onGenerate(difficulty)}
            className="mt-3 w-full"
          >
            Generate &amp; inject fault
          </Button>
          <p className="mt-2 text-[11px] leading-relaxed text-ink-400">
            Claude invents the fault, pushes it, then re-sweeps the topology to
            confirm it actually bit. Takes ~30s and is rejected if inert.
          </p>
        </div>
      </div>

      <div>
        <h3 className="mb-2 text-xs font-semibold tracking-wide text-ink-400 uppercase">
          Scripted scenarios
        </h3>
        {loading ? (
          <div className="flex items-center gap-2 px-1 py-4 text-sm text-ink-400">
            <Spinner /> Loading scenarios…
          </div>
        ) : scenarios.length === 0 ? (
          <Empty>No scenarios found in scenarios/.</Empty>
        ) : (
          <ul className="flex flex-col gap-2">
            {scenarios.map((s) => (
              <li
                key={s.id}
                className="rounded-lg border border-ink-700/70 bg-ink-850 p-3"
              >
                <div className="flex items-start justify-between gap-2">
                  <h4 className="text-sm font-semibold text-ink-50">{s.title}</h4>
                  <DifficultyBadge level={s.difficulty} />
                </div>
                <p className="mt-1 text-xs leading-relaxed text-ink-400">
                  {s.description}
                </p>
                <Button
                  busy={busy === s.id}
                  disabled={disabled || Boolean(busy)}
                  onClick={() => onStart(s.id)}
                  className="mt-2 w-full"
                >
                  Start
                </Button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
