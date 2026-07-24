const STATUS_STYLES = {
  ok: 'bg-ok/10 text-ok ring-ok/30',
  warn: 'bg-warn/10 text-warn ring-warn/30',
  down: 'bg-down/10 text-down ring-down/30',
  idle: 'bg-ink-700/50 text-ink-400 ring-ink-600',
  brand: 'bg-brand/10 text-brand ring-brand/30',
}

export function Badge({ status = 'idle', children, className = '' }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset ${STATUS_STYLES[status]} ${className}`}
    >
      {children}
    </span>
  )
}

export function Dot({ status = 'idle', pulse = false }) {
  const color = { ok: 'bg-ok', warn: 'bg-warn', down: 'bg-down', idle: 'bg-ink-600', brand: 'bg-brand' }[status]
  return (
    <span className="relative inline-flex size-2">
      {pulse && (
        <span className={`absolute inline-flex size-full animate-ping rounded-full opacity-60 ${color}`} />
      )}
      <span className={`relative inline-flex size-2 rounded-full ${color}`} />
    </span>
  )
}

export function Panel({ title, subtitle, actions, children, className = '' }) {
  return (
    <section
      className={`flex flex-col rounded-xl border border-ink-700/70 bg-ink-900 ${className}`}
    >
      {(title || actions) && (
        <header className="flex items-start justify-between gap-3 border-b border-ink-700/70 px-4 py-3">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold tracking-wide text-ink-50 uppercase">
              {title}
            </h2>
            {subtitle && <p className="mt-0.5 text-xs text-ink-400">{subtitle}</p>}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className="min-h-0 flex-1 p-4">{children}</div>
    </section>
  )
}

const BUTTON_VARIANTS = {
  primary: 'bg-brand text-ink-950 hover:bg-brand/85 focus-visible:outline-brand',
  ghost:
    'bg-ink-800 text-ink-200 ring-1 ring-inset ring-ink-700 hover:bg-ink-700 focus-visible:outline-ink-400',
  danger:
    'bg-down/15 text-down ring-1 ring-inset ring-down/30 hover:bg-down/25 focus-visible:outline-down',
}

export function Button({
  variant = 'ghost',
  busy = false,
  disabled = false,
  className = '',
  children,
  ...props
}) {
  return (
    <button
      type="button"
      disabled={disabled || busy}
      className={`inline-flex items-center justify-center gap-2 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 disabled:cursor-not-allowed disabled:opacity-50 ${BUTTON_VARIANTS[variant]} ${className}`}
      {...props}
    >
      {busy && <Spinner />}
      {children}
    </button>
  )
}

export function Spinner({ className = '' }) {
  return (
    <span
      role="presentation"
      className={`size-3.5 animate-spin rounded-full border-2 border-current border-t-transparent ${className}`}
    />
  )
}

export function ErrorNote({ error, onDismiss }) {
  if (!error) return null
  return (
    <div className="flex items-start justify-between gap-3 rounded-lg border border-down/30 bg-down/10 px-3 py-2 text-sm text-down">
      <span className="min-w-0">{error.message}</span>
      {onDismiss && (
        <button
          type="button"
          onClick={onDismiss}
          aria-label="Dismiss error"
          className="shrink-0 text-down/70 hover:text-down"
        >
          ✕
        </button>
      )}
    </div>
  )
}

export function Empty({ children }) {
  return (
    <p className="rounded-lg border border-dashed border-ink-700 px-3 py-6 text-center text-sm text-ink-400">
      {children}
    </p>
  )
}
