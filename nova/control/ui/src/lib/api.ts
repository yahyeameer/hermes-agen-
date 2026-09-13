/* The Control API, and nothing else.
 *
 * The dashboard talks to `/platform/v1/*` and never to a runtime endpoint — the boundary
 * `docs/platform/ARCHITECTURE_BOUNDARIES.md` §5 exists to keep. Every panel loads
 * independently: one 403 (a viewer reading an admin route) must blank its own card and
 * nothing else, which is why there is no single `Promise.all` over the whole page.
 */
export const API = "/platform/v1";

export type Loaded<T> =
  | { state: "loading" }
  | { state: "ok"; data: T }
  | { state: "forbidden" }
  | { state: "error"; message: string };

export async function load<T>(path: string): Promise<Loaded<T>> {
  try {
    const response = await fetch(`${API}${path}`, { headers: { Accept: "application/json" } });
    if (response.status === 403) return { state: "forbidden" };
    if (!response.ok) {
      let message = `HTTP ${response.status}`;
      try {
        const body = await response.json();
        message = body?.error?.message ?? message;
      } catch {
        /* a non-JSON error body is still an error worth showing */
      }
      return { state: "error", message };
    }
    return { state: "ok", data: (await response.json()) as T };
  } catch (cause) {
    return { state: "error", message: cause instanceof Error ? cause.message : "network error" };
  }
}

export function plural(n: number, one: string, many = `${one}s`) {
  return `${n} ${n === 1 ? one : many}`;
}
