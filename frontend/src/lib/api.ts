export type MenuItem = {
  id: string;
  name: string;
  price: number;
  currency: string;
  description?: string | null;
  image_url?: string | null;
  availability: { type: "always" | "weekend" };
  modifier_groups: MenuModifierGroup[];
};

export type MenuModifierOption = { id: string; name: string; price_delta: number };
export type MenuModifierGroup = {
  id: string; name: string; required: boolean; min_select: number;
  max_select: number; options: MenuModifierOption[];
};
export type MenuCategory = { id: string; name: string; display_order: number; items: MenuItem[] };
export type MenuResponse = { categories: MenuCategory[] };

export type TableResolution = { table: string; display_name: string; valid: boolean };
export type PublicSession = {
  session_id: string;
  table: { display_name: string };
  status: string;
};
export type OrderLine = {
  item_id: string | null;
  name: string | null;
  quantity: number;
  unit_price: number | null;
  modifiers?: { id: string; name: string; price_delta: number }[];
};
export type OrderConfirmation = PublicSession & {
  order: { currency: string | null; total: number | null; items: OrderLine[] };
};
export type OrderItem = { item_id: string; quantity: number; modifier_ids?: string[] };

export class ApiError extends Error {
  constructor(public status: number, message: string, public code?: string) { super(message); }
}

function apiBase(): string {
  const configured = process.env.NEXT_PUBLIC_API_BASE_URL ??
    (process.env.NODE_ENV === "development" ? "http://127.0.0.1:8000" : "");
  if (!configured) throw new ApiError(0, "Ordering is not configured yet.");
  const url = new URL(configured);
  if (url.pathname !== "/" || url.search || url.hash || url.username || url.password ||
      (url.protocol !== "https:" && !(url.protocol === "http:" &&
        ["localhost", "127.0.0.1"].includes(url.hostname)))) {
    throw new ApiError(0, "Ordering is not configured yet.");
  }
  return url.origin;
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    const base = apiBase();
    const headers = new Headers(init?.headers);
    if (!headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    const host = new URL(base).hostname;
    if (process.env.NODE_ENV === "development" &&
        process.env.NEXT_PUBLIC_NGROK_BYPASS_WARNING === "true" &&
        (host.endsWith(".ngrok-free.dev") || host.endsWith(".ngrok-free.app"))) {
      headers.set("ngrok-skip-browser-warning", "true");
    }
    response = await fetch(`${base}${path}`, {
      ...init,
      cache: "no-store",
      headers,
    });
  } catch {
    throw new ApiError(0, "We can’t connect right now. Please try again.");
  }
  if (!response.ok) {
    const problem: unknown = await response.json().catch(() => null);
    if (response.status === 503 && problem && typeof problem === "object" &&
        "code" in problem && problem.code === "ORDER_CONFIRMATION_PENDING") {
      throw new ApiError(503, "Your order was sent. We’re confirming it with the kitchen.",
        "ORDER_CONFIRMATION_PENDING");
    }
    if (response.status === 404) throw new ApiError(404, "This table link or session is no longer active.");
    if (response.status === 503) throw new ApiError(503, "Ordering is temporarily unavailable. Please ask a team member.");
    if (response.status === 409) throw new ApiError(409, "We’re checking your order. Please ask a team member before trying again.");
    if (response.status === 400 || response.status === 422) {
      throw new ApiError(response.status, "An item may have changed. Please review your cart.");
    }
    throw new ApiError(response.status, "Something went wrong. Please try again.");
  }
  return response.json() as Promise<T>;
}

export const customerApi = {
  resolveTable(token: string) {
    return api<TableResolution>(`/api/public/table/${encodeURIComponent(token)}`);
  },
  createSession(token: string) {
    return api<PublicSession>("/api/public/table-sessions", {
      method: "POST", body: JSON.stringify({ table_token: token }),
    });
  },
  async menu() {
    return api<MenuResponse>("/api/menu");
  },
  placeOrder(sessionId: string, token: string, items: OrderItem[], key: string) {
    return api<OrderConfirmation>(
      `/api/public/table-sessions/${encodeURIComponent(sessionId)}/orders`,
      { method: "POST", headers: { "Idempotency-Key": key },
        body: JSON.stringify({ table_token: token, items }) },
    );
  },
};
