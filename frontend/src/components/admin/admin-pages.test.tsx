import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { adminApi, AdminApiError, type AdminPhysicalTable, type AdminTableSession } from "@/lib/admin-api";
import { ActiveTables, AdminEntry, AdminLogin, PrepSummaryPage, SessionHistory, TableDetail } from "./admin-pages";

const navigation = vi.hoisted(() => ({
  pathname: "/admin/tables", params: { sessionId: "a".repeat(32) },
  replace: vi.fn(),
}));
vi.mock("next/navigation", () => ({
  usePathname: () => navigation.pathname,
  useParams: () => navigation.params,
  useRouter: () => ({ replace: navigation.replace }),
}));

const sample: AdminTableSession = {
  session_id: "a".repeat(32), table: "T18", display_name: "Table 18",
  status: "open", opened_at: "2026-10-09T09:00:00Z",
  last_activity_at: "2026-10-09T09:10:00Z", closed_at: null,
  clover_order_ref: "…1234", clover_order_status: null,
  mutation_count: 2, reconciliation_pending: false, healthy: true,
  safe_to_close: true, safe_to_force_close: true,
  forced_close: false, forced_close_reason: null, forced_close_at: null,
  forced_close_by: null, forced_close_payment_state: null,
  forced_close_unresolved: false,
  order: { currency: "USD", total: 21.98,
    items: [{ item_id: "ITEM1", name: "Chicken Biryani", quantity: 1,
      unit_price: 16.99, modifiers: [{ id: "SWEET", name: "Sweet", price_delta: 0 }] }] },
  kitchen_state: "ALL_DELIVERED", new_count: 0, ready_count: 0,
  delivered_count: 1, open_count: 0, total_item_lines: 1,
  kitchen_lines: [{ kitchen_line_id: "b".repeat(32), item_id: "ITEM1",
    item_name: "Chicken Biryani", quantity: 1,
    modifiers: [{ id: "SWEET", name: "Sweet", price_delta: 0 }],
    status: "DELIVERED", created_at: "2026-10-09T09:10:00Z",
    ready_at: "2026-10-09T09:15:00Z", delivered_at: "2026-10-09T09:20:00Z" }],
  payment_state: "PAID", payment_checked_at: "2026-10-09T09:22:00Z",
  payment_warning: null,
};

function floor(...sessions: AdminTableSession[]): { tables: AdminPhysicalTable[] } {
  return { tables: Array.from({ length: 4 }, (_, row) =>
    "ABCD".split("").map((column) => {
      const tableId = `${column}${row + 1}`;
      const session = sessions.find((entry) => entry.table === tableId) || null;
      return { table_id: tableId, display_name: `Table ${tableId}`,
        column: column as AdminPhysicalTable["column"], position: row + 1, session,
        kitchen_state: session?.total_item_lines ? session.kitchen_state as AdminPhysicalTable["kitchen_state"] : "AVAILABLE" as const,
        new_count: session?.new_count || 0, ready_count: session?.ready_count || 0,
        delivered_count: session?.delivered_count || 0, open_count: session?.open_count || 0 };
    })).flat() };
}

const at = (table: string, session: AdminTableSession = sample): AdminTableSession =>
  ({ ...session, table, display_name: `Table ${table}` });

beforeEach(() => {
  navigation.replace.mockReset();
  vi.spyOn(adminApi, "me").mockResolvedValue({ username: "operator" });
  vi.spyOn(adminApi, "refreshPayment").mockResolvedValue(sample);
  vi.spyOn(adminApi, "tables").mockResolvedValue(floor());
  vi.spyOn(adminApi, "prep").mockResolvedValue({ items: [] });
});
afterEach(() => { cleanup(); window.history.replaceState(null, "", "/admin/tables"); vi.restoreAllMocks(); });

describe("admin portal", () => {
  it("renders login and navigates after successful authentication", async () => {
    vi.mocked(adminApi.me).mockRejectedValue(new AdminApiError(401, "Sign in"));
    vi.spyOn(adminApi, "login").mockResolvedValue({ username: "operator" });
    render(<AdminLogin />);
    expect(screen.getByRole("heading", { name: "Chennai Café Admin" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "operator" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "synthetic" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(adminApi.login).toHaveBeenCalledWith("operator", "synthetic"));
    await waitFor(() => expect(navigation.replace).toHaveBeenCalledWith("/admin/tables"));
  });

  it("shows a safe failed-login message", async () => {
    vi.mocked(adminApi.me).mockRejectedValue(new AdminApiError(401, "Sign in"));
    vi.spyOn(adminApi, "login").mockRejectedValue(new AdminApiError(401, "Sign in"));
    render(<AdminLogin />);
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "operator" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "wrong" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Incorrect username or password.");
  });

  it("always renders the fixed 16-table floor and supports refresh", async () => {
    const tables = vi.mocked(adminApi.tables).mockResolvedValue(floor());
    render(<ActiveTables />);
    expect(await screen.findByRole("button", { name: /Table A1, available/i })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /Table [ABCD][1-4], available/i })).toHaveLength(16);
    expect(screen.getByRole("button", { name: /Table D4, available/i })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /Table [ABCD][1-4], available/i })
      .map((button) => button.textContent?.slice(0, 2))).toEqual([
        "A1", "B1", "C1", "D1", "A2", "B2", "C2", "D2",
        "A3", "B3", "C3", "D3", "A4", "B4", "C4", "D4",
      ]);
    fireEvent.click(screen.getByRole("button", { name: /Table A1, available/i }));
    expect(screen.getByRole("dialog", { name: "Table A1 details" })).toHaveTextContent("No active table session.");
    fireEvent.click(screen.getByRole("button", { name: "Refresh tables" }));
    await waitFor(() => expect(tables).toHaveBeenCalledTimes(2));
  });

  it("prioritizes green, amber, and red kitchen states with counts and secondary sync warning", async () => {
    vi.mocked(adminApi.tables).mockResolvedValue(floor(
      { ...at("A1"), kitchen_state: "NEW_ORDER", new_count: 1, ready_count: 0,
        delivered_count: 0, open_count: 1, reconciliation_pending: true },
      { ...at("B1"), session_id: "c".repeat(32),
        kitchen_state: "IN_PROGRESS", new_count: 0, ready_count: 1,
        delivered_count: 1, open_count: 1, payment_state: "PARTIALLY_PAID" },
      { ...at("C1"), session_id: "d".repeat(32) },
    ));
    render(<ActiveTables />);
    expect(await screen.findByText("New Order")).toBeInTheDocument();
    expect(screen.getByText("In Progress")).toBeInTheDocument();
    expect(screen.getByText("All Delivered")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Table A1, new order/i })).toHaveClass("admin-accent-new_order");
    expect(screen.getByRole("button", { name: /Table B1, in progress/i })).toHaveClass("admin-accent-in_progress");
    expect(screen.getByRole("button", { name: /Table C1, all delivered/i })).toHaveClass("admin-accent-all_delivered");
    expect(screen.getByText("Order sync needs review")).toBeInTheDocument();
  });

  it("advances kitchen lines one step and enables closing only after delivery", async () => {
    const newSession: AdminTableSession = { ...sample, kitchen_state: "NEW_ORDER",
      new_count: 1, delivered_count: 0, open_count: 1, safe_to_close: false,
      safe_to_force_close: false,
      kitchen_lines: [{ ...sample.kitchen_lines[0], status: "NEW", ready_at: null,
        delivered_at: null }] };
    const readySession: AdminTableSession = { ...newSession, kitchen_state: "IN_PROGRESS",
      new_count: 0, ready_count: 1,
      kitchen_lines: [{ ...newSession.kitchen_lines[0], status: "READY" }] };
    vi.spyOn(adminApi, "detail").mockResolvedValue(newSession);
    vi.mocked(adminApi.refreshPayment).mockResolvedValue(newSession);
    const ready = vi.spyOn(adminApi, "ready").mockResolvedValue(readySession);
    const delivered = vi.spyOn(adminApi, "delivered").mockResolvedValue(sample);
    render(<TableDetail />);
    expect(await screen.findByRole("button", { name: "Mark Ready" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close Table" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Mark Ready" }));
    await waitFor(() => expect(ready).toHaveBeenCalledWith(sample.kitchen_lines[0].kitchen_line_id));
    fireEvent.click(await screen.findByRole("button", { name: "Mark Delivered" }));
    await waitFor(() => expect(delivered).toHaveBeenCalledWith(sample.kitchen_lines[0].kitchen_line_id));
    await waitFor(() => expect(screen.getByRole("button", { name: "Close Table" })).toBeEnabled());
    expect(screen.getByText(/All items delivered. Paid in Clover/)).toBeInTheDocument();
  });

  it("shows neutral pending control when no work is outstanding", async () => {
    render(<ActiveTables />);
    expect(await screen.findByRole("button", { name: "Items Pending 0" })).toHaveClass("admin-pending-neutral");
  });

  it("opens and switches in-place table panels with payment gating", async () => {
    const unpaid = { ...at("A3"), payment_state: "UNPAID" as const, safe_to_close: false };
    const other = { ...at("B3", unpaid), session_id: "c".repeat(32) };
    vi.mocked(adminApi.tables).mockResolvedValue(floor(unpaid, other));
    vi.spyOn(adminApi, "detail").mockImplementation(async (id) => id === sample.session_id ? unpaid : other);
    vi.mocked(adminApi.refreshPayment).mockImplementation(async (id) => id === sample.session_id ? unpaid : other);
    render(<ActiveTables />);
    fireEvent.click(await screen.findByRole("button", { name: /Table A3, all delivered/i }));
    expect(await screen.findByRole("dialog", { name: "Table A3 details" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close Table" })).toBeDisabled();
    expect(screen.getByText(/Payment pending in Clover/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close table details" }));
    fireEvent.click(screen.getByRole("button", { name: /Table B3, all delivered/i }));
    expect(await screen.findByRole("dialog", { name: "Table B3 details" })).toBeInTheDocument();
    expect(adminApi.refreshPayment).toHaveBeenCalledWith(other.session_id);
  });

  it("advances a selected table line without navigating and shows paid closure", async () => {
    const fresh: AdminTableSession = { ...at("A3"), kitchen_state: "NEW_ORDER", new_count: 1,
      delivered_count: 0, open_count: 1, safe_to_close: false, safe_to_force_close: false,
      kitchen_lines: [{ ...sample.kitchen_lines[0], status: "NEW", ready_at: null, delivered_at: null }] };
    const readyState: AdminTableSession = { ...fresh, kitchen_state: "IN_PROGRESS",
      new_count: 0, ready_count: 1, kitchen_lines: [{ ...fresh.kitchen_lines[0], status: "READY" }] };
    vi.mocked(adminApi.tables).mockResolvedValue(floor(fresh));
    vi.spyOn(adminApi, "detail").mockResolvedValue(fresh);
    vi.mocked(adminApi.refreshPayment).mockResolvedValue(fresh);
    const ready = vi.spyOn(adminApi, "ready").mockResolvedValue(readyState);
    const delivered = vi.spyOn(adminApi, "delivered").mockResolvedValue(at("A3"));
    render(<ActiveTables />);
    fireEvent.click(await screen.findByRole("button", { name: /Table A3, new order/i }));
    fireEvent.click(await screen.findByRole("button", { name: "Mark Ready" }));
    await waitFor(() => expect(ready).toHaveBeenCalled());
    fireEvent.click(await screen.findByRole("button", { name: "Mark Delivered" }));
    await waitFor(() => expect(delivered).toHaveBeenCalled());
    expect(await screen.findByText(/All items delivered. Paid in Clover/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close Table" })).toBeEnabled();
    expect(navigation.replace).not.toHaveBeenCalled();
  });

  it("returns a paid, delivered physical table to Available after confirmed close", async () => {
    const delivered = at("D4");
    vi.mocked(adminApi.tables).mockResolvedValue(floor(delivered));
    vi.spyOn(adminApi, "detail").mockResolvedValue(delivered);
    vi.mocked(adminApi.refreshPayment).mockResolvedValue(delivered);
    const close = vi.spyOn(adminApi, "close").mockResolvedValue({ ...delivered, status: "closed" });
    render(<ActiveTables />);
    fireEvent.click(await screen.findByRole("button", { name: /Table D4, all delivered/i }));
    fireEvent.click(await screen.findByRole("button", { name: "Close Table" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm Close Table" }));
    await waitFor(() => expect(close).toHaveBeenCalledWith(delivered.session_id));
    expect(screen.getByRole("button", { name: /Table D4, available/i })).toBeInTheDocument();
    expect(navigation.replace).not.toHaveBeenCalled();
  });

  it("keeps a delivered table usable when Clover reports its order unavailable", async () => {
    const unpaid = { ...at("A1"), payment_state: "UNAVAILABLE" as const,
      payment_warning: "Clover order could not be found",
      safe_to_close: false, safe_to_force_close: true, reconciliation_pending: true };
    vi.mocked(adminApi.tables).mockResolvedValue(floor(unpaid));
    vi.spyOn(adminApi, "detail").mockResolvedValue(unpaid);
    vi.mocked(adminApi.refreshPayment).mockResolvedValue(unpaid);
    const force = vi.spyOn(adminApi, "forceClose").mockResolvedValue({
      ...unpaid, status: "closed", forced_close: true,
      forced_close_reason: "Clover unavailable", forced_close_by: "operator",
      forced_close_at: "2026-10-11T10:00:00Z", forced_close_unresolved: true,
    });
    const normal = vi.spyOn(adminApi, "close");
    render(<ActiveTables />);
    fireEvent.click(await screen.findByRole("button", { name: /Table A1, all delivered/i }));
    expect(await screen.findByRole("button", { name: "Force Close Table" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Close Table" })).toBeDisabled();
    expect(screen.getByText("Payment: Unavailable")).toBeInTheDocument();
    expect(screen.getByText(/Clover order could not be found/)).toBeInTheDocument();
    expect(screen.queryByText("The admin service is temporarily unavailable.")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Refresh payment" }));
    await waitFor(() => expect(adminApi.refreshPayment).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole("button", { name: "Force Close Table" }));
    const dialog = screen.getByRole("dialog", { name: "Force close Table A1" });
    expect(dialog).toHaveTextContent("order sync is unresolved");
    const confirm = within(dialog).getByRole("button", { name: "Force Close Table" });
    expect(confirm).toBeDisabled();
    fireEvent.change(within(dialog).getByLabelText("Reason for force close"),
      { target: { value: "Clover unavailable" } });
    fireEvent.click(confirm);
    await waitFor(() => expect(force).toHaveBeenCalledWith(unpaid.session_id, "Clover unavailable"));
    expect(normal).not.toHaveBeenCalled();
    expect(await screen.findByRole("button", { name: /Table A1, available/i })).toBeInTheDocument();
  });

  it("keeps a true admin-service failure distinct from Clover unavailability", async () => {
    const delivered = { ...at("B2"), safe_to_close: false, safe_to_force_close: true };
    vi.mocked(adminApi.tables).mockResolvedValue(floor(delivered));
    vi.spyOn(adminApi, "detail").mockRejectedValue(
      new AdminApiError(503, "The admin service is temporarily unavailable."));
    render(<ActiveTables />);
    fireEvent.click(await screen.findByRole("button", { name: /Table B2, all delivered/i }));
    expect(await screen.findByText("The admin service is temporarily unavailable.")).toBeInTheDocument();
    expect(screen.queryByText(/Clover order could not be found/)).not.toBeInTheDocument();
  });

  it("does not offer force close while NEW or READY kitchen work remains", async () => {
    const pending = { ...at("A2"), kitchen_state: "NEW_ORDER" as const,
      new_count: 1, delivered_count: 0, open_count: 1,
      safe_to_close: false, safe_to_force_close: false };
    vi.mocked(adminApi.tables).mockResolvedValue(floor(pending));
    vi.spyOn(adminApi, "detail").mockResolvedValue(pending);
    vi.mocked(adminApi.refreshPayment).mockResolvedValue(pending);
    render(<ActiveTables />);
    fireEvent.click(await screen.findByRole("button", { name: /Table A2, new order/i }));
    expect(screen.queryByRole("button", { name: "Force Close Table" })).not.toBeInTheDocument();
  });

  it("shows expandable prep quantities and excludes delivered groups", async () => {
    vi.spyOn(adminApi, "prep").mockResolvedValue({ items: [{ item_id: "BIRYANI",
      name: "Chicken Biryani", new_quantity: 4, ready_quantity: 3,
      remaining_quantity: 7, tables: [
        { session_id: sample.session_id, table: "T18", new_quantity: 4,
          ready_quantity: 0, remaining_quantity: 4 },
        { session_id: "c".repeat(32), table: "T19", new_quantity: 0,
          ready_quantity: 3, remaining_quantity: 3 },
      ] }] });
    render(<PrepSummaryPage />);
    expect(await screen.findByText("7 remaining")).toBeInTheDocument();
    expect(screen.queryByText("Delivered")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Chicken Biryani/ }));
    expect(screen.getByText("T18")).toBeInTheDocument();
    expect(screen.getByText("T19")).toBeInTheDocument();
  });

  it("uses a red quantity badge and drills from pending work into the physical table", async () => {
    const newOrder = { ...at("A1"), kitchen_state: "NEW_ORDER" as const,
      new_count: 1, delivered_count: 0, open_count: 1, safe_to_close: false };
    vi.mocked(adminApi.tables).mockResolvedValue(floor(newOrder));
    vi.mocked(adminApi.prep).mockResolvedValue({ items: [{ item_id: "TEA",
      name: "Indian Masala Tea", new_quantity: 9, ready_quantity: 0,
      remaining_quantity: 9, tables: [{ session_id: newOrder.session_id, table: "A1",
        new_quantity: 4, ready_quantity: 0, remaining_quantity: 4 },
        { session_id: "z".repeat(32), table: "T18", new_quantity: 5,
          ready_quantity: 0, remaining_quantity: 5 }] }] });
    render(<ActiveTables />);
    const pending = await screen.findByRole("button", { name: "Items Pending 4" });
    expect(pending).toHaveClass("admin-pending-urgent");
    fireEvent.click(pending);
    expect(screen.getByText("Indian Masala Tea")).toBeInTheDocument();
    expect(screen.getByText("4 remaining")).toBeInTheDocument();
    expect(screen.queryByText("T18")).not.toBeInTheDocument();
    expect(screen.queryByText("Delivered")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "A1 ×4" }));
    expect(screen.getByRole("dialog", { name: "Table A1 details" })).toBeInTheDocument();
    expect(navigation.replace).not.toHaveBeenCalled();
  });

  it("shows details, requires close confirmation, and returns to Active Tables", async () => {
    vi.spyOn(adminApi, "detail").mockResolvedValue(sample);
    const close = vi.spyOn(adminApi, "close").mockResolvedValue({ ...sample, status: "closed" });
    render(<TableDetail />);
    expect(await screen.findByRole("heading", { name: "Table 18" })).toBeInTheDocument();
    expect(screen.getAllByText(/Chicken Biryani/)).toHaveLength(2);
    expect(screen.getAllByText("Sweet")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Close Table" }));
    expect(screen.getByRole("dialog", { name: "Confirm close table" })).toHaveTextContent(
      "The next guest scanning this table will start a new session.",
    );
    expect(close).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Confirm Close Table" }));
    await waitFor(() => expect(close).toHaveBeenCalledWith(sample.session_id));
    await waitFor(() => expect(navigation.replace).toHaveBeenCalledWith("/admin/tables"));
  });

  it("keeps the standalone table detail usable after a deleted Clover order", async () => {
    const unavailable: AdminTableSession = { ...sample, payment_state: "UNAVAILABLE",
      payment_warning: "Clover order could not be found", safe_to_close: false,
      safe_to_force_close: true };
    vi.spyOn(adminApi, "detail").mockResolvedValue(unavailable);
    vi.mocked(adminApi.refreshPayment).mockResolvedValue(unavailable);
    render(<TableDetail />);
    expect(await screen.findByText("Payment: Unavailable")).toBeInTheDocument();
    expect(screen.getByText(/Clover order could not be found/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close Table" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Force Close Table" }));
    expect(screen.getByRole("dialog", { name: "Force close Table 18" })).toBeInTheDocument();
  });

  it("disables closure of a session needing reconciliation", async () => {
    const pending: AdminTableSession = {
      ...sample, reconciliation_pending: true, safe_to_close: false,
    };
    vi.spyOn(adminApi, "detail").mockResolvedValue(pending);
    vi.mocked(adminApi.refreshPayment).mockResolvedValue(pending);
    render(<TableDetail />);
    expect(await screen.findByRole("button", { name: "Close Table" })).toBeDisabled();
    expect(screen.getByText(/Order sync needs review/)).toBeInTheDocument();
  });

  it("shows closed history and protects navigation", async () => {
    vi.spyOn(adminApi, "history").mockResolvedValue({
      sessions: [{ ...sample, status: "closed", kitchen_state: "CLOSED",
        closed_at: "2026-10-09T10:00:00Z" }],
      next_offset: null,
    });
    render(<SessionHistory />);
    expect(await screen.findByRole("link", { name: "Table 18" })).toBeInTheDocument();
    expect(screen.getByText("$21.98")).toBeInTheDocument();
    cleanup();
    vi.mocked(adminApi.me).mockRejectedValue(new AdminApiError(401, "Sign in"));
    render(<AdminEntry />);
    await waitFor(() => expect(navigation.replace).toHaveBeenCalledWith("/admin/login"));
  });

  it("distinguishes a force-closed visit and its reason in History", async () => {
    vi.spyOn(adminApi, "history").mockResolvedValue({
      sessions: [{ ...sample, status: "closed", kitchen_state: "CLOSED",
        forced_close: true, forced_close_reason: "Clover unavailable",
        forced_close_by: "operator", forced_close_at: "2026-10-11T10:00:00Z",
        forced_close_unresolved: true, closed_at: "2026-10-11T10:00:00Z" }],
      next_offset: null,
    });
    render(<SessionHistory />);
    expect(await screen.findByText("Force Closed")).toBeInTheDocument();
    expect(screen.getByText(/Reason: Clover unavailable/)).toBeInTheDocument();
    expect(screen.getByText(/Order sync unresolved at close/)).toBeInTheDocument();
  });

  it("logs out through the backend", async () => {
    const logout = vi.spyOn(adminApi, "logout").mockResolvedValue({ ok: true });
    render(<ActiveTables />);
    fireEvent.click(await screen.findByRole("button", { name: "Logout" }));
    await waitFor(() => expect(logout).toHaveBeenCalled());
    await waitFor(() => expect(navigation.replace).toHaveBeenCalledWith("/admin/login"));
  });
});
