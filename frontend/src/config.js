/**
 * Centralized runtime config for the frontend.
 *
 * Every backend URL the frontend uses is derived from one env var,
 * ``VITE_API_BASE_URL``. The fallback is the local dev backend
 * (``http://localhost:8000``) so a developer running ``npm run dev`` with
 * no .env still works. In production / staging, set the env var when
 * building:
 *
 *     VITE_API_BASE_URL=https://api.nokvo.com npm run build
 *
 * To override at runtime per-environment, create a ``.env``,
 * ``.env.production``, or ``.env.staging`` at the frontend root and Vite
 * loads it automatically. See ``.env.example`` in this directory for the
 * canonical key.
 *
 * WS URLs are derived from the HTTP base — https → wss, http → ws — so
 * there's no second env var to keep in sync.
 *
 * NEVER hard-code ``http://localhost:8000`` again. Import from here.
 */

const RAW_BASE = (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000').replace(/\/+$/, '');

/** Plain HTTP origin, e.g. ``https://api.nokvo.com`` (no trailing slash). */
export const API_ORIGIN = RAW_BASE;

/** WebSocket origin derived from the HTTP origin (https → wss, http → ws). */
export const WS_ORIGIN = RAW_BASE.replace(/^http(s?):\/\//, (_, s) => `ws${s}://`);

/** Top-level API mount: ``<origin>/api``. */
export const API_BASE_URL = `${API_ORIGIN}/api`;

/** Nokvo One tenant namespace: ``<origin>/api/nokvo-one``. */
export const NOKVO_ONE_API_BASE = `${API_ORIGIN}/api/nokvo-one`;

/** Nokvo One agents namespace: ``<origin>/api/nokvo-one/agents``. */
export const NOKVO_ONE_AGENTS_BASE = `${API_ORIGIN}/api/nokvo-one/agents`;

/** Nokvo One knowledge-base namespace. */
export const NOKVO_ONE_KB_BASE = `${API_ORIGIN}/api/nokvo-one/knowledge-base`;

/** Nokvo One clinic-services namespace. */
export const NOKVO_ONE_SERVICES_BASE = `${API_ORIGIN}/api/nokvo-one/services`;

/** Nokvo Connect public API namespace. */
export const NOKVO_CONNECT_API_BASE = `${API_ORIGIN}/api/nokvo-one/connect`;

/** Super-admin namespace (kept separate — different auth model). */
export const SUPERADMIN_API_BASE = `${API_ORIGIN}/superadmin`;

/**
 * Helper for WS endpoints. Pass the path-only portion (starts with /):
 *   wsUrl('/api/nokvo-one/notifications/ws?token=…')
 */
export function wsUrl(path) {
  if (!path) return WS_ORIGIN;
  return `${WS_ORIGIN}${path.startsWith('/') ? '' : '/'}${path}`;
}
