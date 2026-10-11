export type AdminOrderLine = {
  item_id: string | null;
  name: string | null;
  quantity: number;
  unit_price: number | null;
  modifiers: { id: string; name: string; price_delta: number }[];
};

export type AdminKitchenLine = {
  kitchen_line_id: string;
  item_id: string | null;
  item_name: string;
  quantity: number;
  modifiers: { id: string; name: string; price_delta: number }[];
  status: "NEW" | "READY" | "DELIVERED";
  created_at: string;
  ready_at: string | null;
  delivered_at: string | null;
};

export type AdminTableSession = {
  session_id: string;
  table: string;
  display_name: string;
  status: "open" | "closed";
  opened_at: string;
  last_activity_at: string;
  closed_at: string | null;
  clover_order_ref: string | null;
  clover_order_status: string | null;
  mutation_count: number;
  reconciliation_pending: boolean;
  healthy: boolean;
  safe_to_close: boolean;
  safe_to_force_close: boolean;
  forced_close: boolean;
  forced_close_reason: string | null;
  forced_close_at: string | null;
  forced_close_by: string | null;
  forced_close_payment_state: string | null;
  forced_close_unresolved: boolean;
  order: { currency: string | null; total: number | null; items: AdminOrderLine[] } | null;
  kitchen_state: "NEW_ORDER" | "IN_PROGRESS" | "ALL_DELIVERED" | "CLOSED" | "AWAITING_ORDER";
  new_count: number;
  ready_count: number;
  delivered_count: number;
  open_count: number;
  total_item_lines: number;
  kitchen_lines: AdminKitchenLine[];
  payment_state: "UNPAID" | "PARTIALLY_PAID" | "PAID" | "UNKNOWN" | "UNAVAILABLE";
  payment_checked_at: string | null;
  payment_warning: string | null;
};

export type AdminPrepItem = {
  item_id: string;
  name: string;
  new_quantity: number;
  ready_quantity: number;
  remaining_quantity: number;
  tables: { session_id: string; table: string; new_quantity: number;
    ready_quantity: number; remaining_quantity: number }[];
};

export type AdminPhysicalTable = {
  table_id: string;
  display_name: string;
  column: "A" | "B" | "C" | "D";
  position: number;
  session: AdminTableSession | null;
  kitchen_state: "AVAILABLE" | "NEW_ORDER" | "IN_PROGRESS" | "ALL_DELIVERED";
  new_count: number;
  ready_count: number;
  delivered_count: number;
  open_count: number;
};

type SessionPage = { sessions: AdminTableSession[]; next_offset: number | null };

export class AdminApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

async function adminRequest<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/admin${path}`, {
      ...init,
      credentials: "same-origin",
      cache: "no-store",
      headers: init?.body ? { "Content-Type": "application/json", ...init.headers } : init?.headers,
    });
  } catch {
    throw new AdminApiError(0, "Cannot reach the admin service. Please try again.");
  }
  if (!response.ok) {
    if (response.status === 401) throw new AdminApiError(401, "Please sign in to continue.");
    if (response.status === 403) throw new AdminApiError(403, "This admin request was not allowed.");
    if (response.status === 404) throw new AdminApiError(404, "Table session not found.");
    if (response.status === 409) {
      const payload = await response.json().catch(() => null);
      throw new AdminApiError(409, typeof payload?.detail === "string" ? payload.detail :
        "This table action is not available until kitchen work and order sync are complete.");
    }
    if (response.status === 429) throw new AdminApiError(429, "Too many sign-in attempts. Please try again later.");
    throw new AdminApiError(response.status, "The admin service is temporarily unavailable.");
  }
  return response.json() as Promise<T>;
}

export const adminApi = {
  me: () => adminRequest<{ username: string }>("/me"),
  login: (username: string, password: string) => adminRequest<{ username: string }>("/login", {
    method: "POST", body: JSON.stringify({ username, password }),
  }),
  logout: () => adminRequest<{ ok: boolean }>("/logout", { method: "POST" }),
  active: () => adminRequest<SessionPage>("/table-sessions"),
  tables: () => adminRequest<{ tables: AdminPhysicalTable[] }>("/tables"),
  history: (offset = 0) => adminRequest<SessionPage>(`/table-sessions/history?limit=25&offset=${offset}`),
  prep: () => adminRequest<{ items: AdminPrepItem[] }>("/prep-summary"),
  detail: (sessionId: string) => adminRequest<AdminTableSession>(
    `/table-sessions/${encodeURIComponent(sessionId)}`,
  ),
  close: (sessionId: string) => adminRequest<AdminTableSession>(
    `/table-sessions/${encodeURIComponent(sessionId)}/close`, { method: "POST" },
  ),
  forceClose: (sessionId: string, reason: string) => adminRequest<AdminTableSession>(
    `/table-sessions/${encodeURIComponent(sessionId)}/force-close`, {
      method: "POST", body: JSON.stringify({ reason }),
    },
  ),
  refreshPayment: (sessionId: string) => adminRequest<AdminTableSession>(
    `/table-sessions/${encodeURIComponent(sessionId)}/refresh-payment`, { method: "POST" },
  ),
  ready: (lineId: string) => adminRequest<AdminTableSession>(
    `/kitchen-items/${encodeURIComponent(lineId)}/ready`, { method: "POST" },
  ),
  delivered: (lineId: string) => adminRequest<AdminTableSession>(
    `/kitchen-items/${encodeURIComponent(lineId)}/delivered`, { method: "POST" },
  ),
};
