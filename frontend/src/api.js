// The FastAPI backend in ../../main.py. It only allows CORS from :5173, so the
// dev server has to stay on Vite's default port.
export const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000'

/** FastAPI's `detail` is a string for simple raises and an object for the
 *  structured ones (409 not-clean, 422 inert-fault). Carry both. */
export class ApiError extends Error {
  constructor(message, { status, detail } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

function messageFromDetail(detail, status) {
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object' && typeof detail.message === 'string') {
    return detail.message
  }
  return `Request failed (HTTP ${status})`
}

async function request(path, { method = 'GET', body, signal } = {}) {
  let res
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method,
      signal,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    })
  } catch (e) {
    if (e.name === 'AbortError') throw e
    throw new ApiError(`Cannot reach the lab API at ${API_BASE}. Is main.py running?`)
  }

  const payload = await res.json().catch(() => null)

  if (!res.ok) {
    const detail = payload?.detail
    throw new ApiError(messageFromDetail(detail, res.status), {
      status: res.status,
      detail,
    })
  }
  return payload
}

export const getHealth = (signal) => request('/health', { signal })
export const getScenarios = (signal) => request('/scenarios', { signal })

export const startSession = (scenario_id) =>
  request('/session/start', { method: 'POST', body: { scenario_id } })

export const generateScenario = (difficulty) =>
  request('/ai/generate-scenario', { method: 'POST', body: { difficulty } })

export const sessionHint = (session_id) =>
  request('/session/hint', { method: 'POST', body: { session_id } })

export const sessionValidate = (session_id) =>
  request('/session/validate', { method: 'POST', body: { session_id } })

export const sessionReset = (session_id) =>
  request('/session/reset', { method: 'POST', body: { session_id } })

// Post-mortem: backend returns the fault details only once the session is solved
// (403 otherwise), so this is safe to expose from the solved state in the UI.
export const sessionReveal = (session_id) =>
  request(`/session/${session_id}/reveal`)
