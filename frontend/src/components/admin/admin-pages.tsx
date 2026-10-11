"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { adminApi, AdminApiError, type AdminPhysicalTable, type AdminPrepItem, type AdminTableSession } from "@/lib/admin-api";
import { AdminShell } from "./admin-shell";

function time(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "—";
}

function money(value: number | null | undefined, currency = "USD"): string {
  if (value == null) return "Not available";
  try { return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(value); }
  catch { return value.toFixed(2); }
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : "The admin service is unavailable.";
}

function Status({ session }: { session: AdminTableSession }) {
  const labels = { NEW_ORDER: "New Order", IN_PROGRESS: "In Progress",
    ALL_DELIVERED: "All Delivered", CLOSED: "Closed", AWAITING_ORDER: "Awaiting Order" };
  if (session.status === "closed" && session.forced_close) return <span
    className="admin-badge admin-state-force-closed">Force Closed</span>;
  return <span className={`admin-badge admin-state-${session.kitchen_state.toLowerCase()}`}>
    {labels[session.kitchen_state]}
  </span>;
}

function ForceCloseDialog({ session, busy, onCancel, onConfirm }: {
  session: AdminTableSession; busy: boolean; onCancel: () => void;
  onConfirm: (reason: string) => void;
}) {
  const [reason, setReason] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  useEffect(() => { inputRef.current?.focus(); }, []);
  return <div className="admin-modal-backdrop" role="presentation">
    <div ref={dialogRef} className="admin-modal admin-force-modal" role="dialog" aria-modal="true"
      aria-label={`Force close ${session.display_name}`} onKeyDown={(event) => {
        if (event.key === "Escape" && !busy) { event.preventDefault(); onCancel(); }
        if (event.key !== "Tab") return;
        const controls = dialogRef.current?.querySelectorAll<HTMLElement>("input,button:not(:disabled)");
        if (!controls?.length) return;
        const first = controls[0], last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }}>
      <h2>Force close {session.display_name}?</h2>
      <p>{session.payment_state === "PAID" ?
        "Clover last reported Paid, but normal closing is blocked." :
        "Clover payment has not been confirmed."} This closes the local table session only.
        It will not modify or delete anything in Clover.</p>
      {session.reconciliation_pending && <p className="admin-force-warning">
        Warning: order sync is unresolved. A customer order write may still be ambiguous.
        This override will preserve that state in the audit record.</p>}
      <form onSubmit={(event) => { event.preventDefault(); onConfirm(reason.trim()); }}>
        <label htmlFor="force-close-reason">Reason for force close</label>
        <input ref={inputRef} id="force-close-reason" value={reason} required minLength={8}
          maxLength={500} onChange={(event) => setReason(event.target.value)}
          placeholder="e.g. Clover unavailable" />
        <div><button className="admin-button admin-button-light" type="button"
          onClick={onCancel} disabled={busy}>Cancel</button>
          <button className="admin-button admin-button-danger-outline" type="submit"
            disabled={busy || reason.trim().length < 8}>
            {busy ? "Force closing…" : "Force Close Table"}</button></div>
      </form>
    </div>
  </div>;
}

function Counts({ session }: { session: AdminTableSession }) {
  return <p className="admin-counts">{session.new_count} New · {session.ready_count} Ready · {session.delivered_count} Delivered
    <span>{session.open_count} open item{session.open_count === 1 ? "" : "s"} remaining</span></p>;
}

function Payment({ session }: { session: AdminTableSession }) {
  const label = { UNPAID: "Unpaid", PARTIALLY_PAID: "Partially paid",
    PAID: "Paid ✓", UNKNOWN: "Not checked", UNAVAILABLE: "Unavailable" }[session.payment_state];
  return <span className={`admin-payment admin-payment-${session.payment_state.toLowerCase()}`}>
    Payment: {label}</span>;
}

export function AdminEntry() {
  const router = useRouter();
  useEffect(() => {
    adminApi.me().then(() => router.replace("/admin/tables"))
      .catch(() => router.replace("/admin/login"));
  }, [router]);
  return <main className="admin-loading" role="status">Opening admin portal…</main>;
}

export function AdminLogin() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    adminApi.me().then(() => router.replace("/admin/tables")).catch(() => undefined);
  }, [router]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await adminApi.login(username, password);
      setPassword("");
      router.replace("/admin/tables");
    } catch (failure) {
      setError(failure instanceof AdminApiError && failure.status === 401
        ? "Incorrect username or password." : message(failure));
    } finally { setBusy(false); }
  }

  return <main className="admin-login-page">
    <div className="admin-login-card">
      <p className="admin-kicker">Staff access</p>
      <h1>Chennai Café Admin</h1>
      <p>Sign in to manage active table sessions.</p>
      <form onSubmit={submit}>
        <label>Username<input autoComplete="username" value={username}
          onChange={(event) => setUsername(event.target.value)} required /></label>
        <label>Password<input type="password" autoComplete="current-password" value={password}
          onChange={(event) => setPassword(event.target.value)} required /></label>
        {error && <p className="admin-alert" role="alert">{error}</p>}
        <button className="admin-button" type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
      <Link href="/">Return to public site</Link>
    </div>
  </main>;
}

export function ActiveTables({ initialView = "tables" }: { initialView?: "tables" | "pending" }) {
  return <AdminShell><ActiveTablesContent initialView={initialView} /></AdminShell>;
}

function ActiveTablesContent({ initialView }: { initialView: "tables" | "pending" }) {
  const [tables, setTables] = useState<AdminPhysicalTable[] | null>(null);
  const [items, setItems] = useState<AdminPrepItem[] | null>(null);
  const [view, setView] = useState<"tables" | "pending">(initialView);
  const [selectedTableId, setSelectedTableId] = useState<string | null>(null);
  const [selected, setSelected] = useState<AdminTableSession | null>(null);
  const [error, setError] = useState("");
  const [panelError, setPanelError] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [paymentBusy, setPaymentBusy] = useState(false);
  const [transitioning, setTransitioning] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [forceConfirming, setForceConfirming] = useState(false);
  const [closing, setClosing] = useState(false);
  const tileRef = useRef<HTMLButtonElement | null>(null);
  const panelCloseRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLElement | null>(null);
  const load = useCallback(async () => {
    try {
      const [floor, prep] = await Promise.all([adminApi.tables(), adminApi.prep()]);
      setError("");
      setTables(floor.tables);
      setItems(prep.items);
      setSelected((current) => current ? floor.tables.find((table) =>
        table.session?.session_id === current.session_id)?.session || null : null);
    }
    catch (failure) { setError(message(failure)); }
    finally { setRefreshing(false); }
  }, []);
  useEffect(() => {
    let active = true;
    Promise.all([adminApi.tables(), adminApi.prep()]).then(([floor, prep]) => {
      if (active) { setTables(floor.tables); setItems(prep.items); }
    }).catch((failure) => { if (active) setError(message(failure)); });
    const timer = window.setInterval(() => { void load(); }, 15000);
    return () => { active = false; window.clearInterval(timer); };
  }, [load]);
  const selectedId = selectedTableId;
  useEffect(() => {
    if (!selectedId) return;
    panelCloseRef.current?.focus();
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape" && !confirming && !forceConfirming) {
        setSelectedTableId(null);
        setSelected(null);
        tileRef.current?.focus();
      }
      if (event.key !== "Tab" || confirming || forceConfirming ||
          !window.matchMedia("(max-width: 1050px)").matches) return;
      const controls = panelRef.current?.querySelectorAll<HTMLElement>(
        "button:not(:disabled), a[href]",
      );
      if (!controls?.length) return;
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [selectedId, confirming, forceConfirming]);

  const selectedTable = tables?.find((table) => table.table_id === selectedTableId);
  const physicalIds = new Set(tables?.map((table) => table.table_id) || []);
  const floorItems = items?.map((item) => {
    const references = item.tables.filter((table) => physicalIds.has(table.table));
    const newQuantity = references.reduce((sum, table) => sum + table.new_quantity, 0);
    const readyQuantity = references.reduce((sum, table) => sum + table.ready_quantity, 0);
    return { ...item, tables: references, new_quantity: newQuantity,
      ready_quantity: readyQuantity, remaining_quantity: newQuantity + readyQuantity };
  }).filter((item) => item.remaining_quantity > 0);
  const pendingCount = floorItems?.reduce((sum, item) => sum + item.remaining_quantity, 0) || 0;
  function switchView(next: "tables" | "pending") {
    setView(next);
    window.history.replaceState(null, "", next === "pending" ? "/admin/tables?view=pending" : "/admin/tables");
  }
  function updateSession(updated: AdminTableSession) {
    setSelected(updated);
    setTables((rows) => rows?.map((table) => table.table_id === updated.table ? {
      ...table, session: updated,
      kitchen_state: updated.total_item_lines ? updated.kitchen_state as AdminPhysicalTable["kitchen_state"] : "AVAILABLE",
      new_count: updated.new_count, ready_count: updated.ready_count,
      delivered_count: updated.delivered_count, open_count: updated.open_count,
    } : table) || null);
  }
  async function openTable(table: AdminPhysicalTable, button: HTMLButtonElement) {
    tileRef.current = button;
    setSelectedTableId(table.table_id);
    setSelected(table.session);
    setPanelError("");
    if (!table.session) return;
    const session = table.session;
    try {
      const detail = await adminApi.detail(session.session_id);
      setSelected((current) => current?.session_id === detail.session_id ? detail : current);
      if (detail.clover_order_ref) {
        setPaymentBusy(true);
        const refreshed = await adminApi.refreshPayment(session.session_id);
        setSelected((current) => current?.session_id === refreshed.session_id ? refreshed : current);
        setTables((rows) => rows?.map((row) => row.table_id === refreshed.table ?
          { ...row, session: refreshed } : row) || null);
      }
    } catch (failure) {
      setPanelError(message(failure));
      setSelected((current) => current?.session_id === session.session_id ?
        { ...current, payment_state: "UNKNOWN", payment_checked_at: null, safe_to_close: false } : current);
    } finally { setPaymentBusy(false); }
  }

  async function refreshPayment() {
    if (!selected) return;
    setPaymentBusy(true);
    setPanelError("");
    try {
      const updated = await adminApi.refreshPayment(selected.session_id);
      updateSession(updated);
    } catch (failure) {
      setPanelError(message(failure));
      setSelected((current) => current && { ...current, payment_state: "UNKNOWN",
        payment_checked_at: null, safe_to_close: false });
    } finally { setPaymentBusy(false); }
  }

  async function advance(lineId: string, status: "NEW" | "READY") {
    setTransitioning(lineId);
    setPanelError("");
    try {
      const updated = status === "NEW" ? await adminApi.ready(lineId) : await adminApi.delivered(lineId);
      updateSession(updated);
      void adminApi.prep().then((page) => setItems(page.items)).catch(() => undefined);
    } catch (failure) { setPanelError(message(failure)); }
    finally { setTransitioning(null); }
  }

  async function closeTable(forceReason?: string) {
    if (!selected) return;
    setClosing(true);
    setPanelError("");
    try {
      if (forceReason === undefined) await adminApi.close(selected.session_id);
      else await adminApi.forceClose(selected.session_id, forceReason);
      setTables((rows) => rows?.map((table) => table.table_id === selected.table ? {
        ...table, session: null, kitchen_state: "AVAILABLE" as const,
        new_count: 0, ready_count: 0, delivered_count: 0, open_count: 0,
      } : table) || null);
      setSelectedTableId(null);
      setSelected(null);
      setConfirming(false);
      setForceConfirming(false);
      tileRef.current?.focus();
    } catch (failure) {
      setPanelError(message(failure));
      setConfirming(false);
      setForceConfirming(false);
      const updated = await adminApi.detail(selected.session_id).catch(() => null);
      if (updated) setSelected(updated);
    } finally { setClosing(false); }
  }

  return <><div className="admin-title-row admin-floor-title">
    <div><p className="admin-kicker">Kitchen operations</p><h1>Tables</h1>
      </div>
    <button className="admin-button admin-button-light" type="button" onClick={() => { setRefreshing(true); void load(); }} disabled={refreshing}>
      {refreshing ? "Refreshing…" : "Refresh tables"}</button>
  </div>
  <div className="admin-view-switch" aria-label="Tables workspace view">
    <button type="button" aria-pressed={view === "tables"} onClick={() => switchView("tables")}>Tables</button>
    <button type="button" aria-pressed={view === "pending"} onClick={() => switchView("pending")}
      className={pendingCount > 0 ? "admin-pending-urgent" : "admin-pending-neutral"}>
      Items Pending <span>{pendingCount}</span></button>
  </div>
  {error && <p className="admin-alert" role="alert">{error}</p>}
  {tables === null && !error && <p role="status">Loading tables…</p>}
  {view === "tables" && <div className={`admin-workspace ${selectedTableId ? "admin-workspace-selected" : ""}`}>
    {tables && <div className="admin-table-grid" aria-label="Physical tables">
      {tables.map((table) => <button type="button"
        className={`admin-table-tile admin-accent-${table.kitchen_state.toLowerCase()}`}
        key={table.table_id} aria-haspopup="dialog"
        aria-label={`${table.display_name}, ${table.kitchen_state.replaceAll("_", " ").toLowerCase()}`}
        aria-expanded={selectedTableId === table.table_id}
        onClick={(event) => void openTable(table, event.currentTarget)}>
        <span className="admin-tile-top"><strong>{table.table_id}</strong>
          <span className="admin-tile-state">{({ AVAILABLE: "Available", NEW_ORDER: "New Order",
            IN_PROGRESS: "In Progress", ALL_DELIVERED: "All Delivered" })[table.kitchen_state]}</span></span>
        <span className="admin-tile-counts">{table.session && table.session.total_item_lines > 0 ?
          <>{table.new_count} New <b>·</b> {table.ready_count} Ready <b>·</b> {table.open_count} Open
            {table.delivered_count > 0 && <small>{table.delivered_count} Delivered</small>}</> :
          <small>No current order</small>}</span>
        {table.session?.reconciliation_pending && <span className="admin-sync-warning">Order sync needs review</span>}
      </button>)}
    </div>}
    {selectedTable && <div className="admin-panel-backdrop" onClick={() => { setSelectedTableId(null); setSelected(null); tileRef.current?.focus(); }}>
      <aside ref={panelRef} className="admin-selected-panel" role="dialog" aria-label={`${selectedTable.display_name} details`}
        onClick={(event) => event.stopPropagation()}>
        <div className="admin-panel-heading"><div><p className="admin-kicker">Selected table</p>
          <h2>{selectedTable.display_name}</h2>
          {selectedTable.kitchen_state === "AVAILABLE" ?
            <span className="admin-badge admin-state-available">Available</span> :
            selected && <Status session={selected} />}</div>
          <button ref={panelCloseRef} className="admin-panel-dismiss" type="button"
            aria-label="Close table details" onClick={() => { setSelectedTableId(null); setSelected(null); tileRef.current?.focus(); }}>×</button></div>
        {panelError && <p className="admin-alert" role="alert">{panelError}</p>}
        {!selected ? <p className="admin-available-message">No active table session.</p> :
          selectedTable.kitchen_state === "AVAILABLE" ?
          <p className="admin-available-message">No current guest order.</p> : <>
        <Counts session={selected} />
        {selected.reconciliation_pending && <p className="admin-warning">Order sync needs review. Kitchen changes and normal closing are paused. Force close requires an explicit override.</p>}
        <h3>Kitchen items</h3>
        {selected.kitchen_lines.length ? <ul className="admin-kitchen-lines">
          {selected.kitchen_lines.map((line) => <li key={line.kitchen_line_id}
            className={line.status === "DELIVERED" ? "admin-line-complete" : ""}>
            <div><strong>{line.item_name} ×{line.quantity}</strong>
              {line.modifiers.map((modifier) => <span key={modifier.id}>{modifier.name}</span>)}
              <small>{line.status}</small></div>
            {line.status !== "DELIVERED" ? <button className="admin-button admin-button-light"
              type="button" disabled={selected.reconciliation_pending || transitioning !== null}
              onClick={() => void advance(line.kitchen_line_id, line.status as "NEW" | "READY")}>
              {transitioning === line.kitchen_line_id ? "Updating…" : line.status === "NEW" ? "Mark Ready" : "Mark Delivered"}
            </button> : <span className="admin-delivered">Delivered</span>}
          </li>)}
        </ul> : <p>No confirmed kitchen items yet.</p>}
        {selected.kitchen_state === "ALL_DELIVERED" && <p className="admin-complete">All items delivered. {selected.payment_state === "PAID" ? "Paid in Clover." :
          selected.payment_state === "UNAVAILABLE" ? "Payment unavailable in Clover." : "Payment pending in Clover."}</p>}
        {selected.payment_warning && <p className="admin-warning">{selected.payment_warning}. You may refresh payment or force close this table.</p>}
        <dl className="admin-panel-facts">
          <div><dt>Current total</dt><dd>{money(selected.order?.total, selected.order?.currency || "USD")}</dd></div>
          <div><dt>Payment</dt><dd><Payment session={selected} /></dd></div>
          <div><dt>Opened</dt><dd>{time(selected.opened_at)}</dd></div>
          <div><dt>Last activity</dt><dd>{time(selected.last_activity_at)}</dd></div>
        </dl>
        <button className="admin-button admin-button-light admin-payment-refresh" type="button"
          onClick={() => void refreshPayment()} disabled={paymentBusy || !selected.clover_order_ref}>
          {paymentBusy ? "Checking Clover…" : "Refresh payment"}</button>
        <button className="admin-button admin-close-table" type="button"
          disabled={!selected.safe_to_close || closing} onClick={() => setConfirming(true)}>Close Table</button>
        {selected.safe_to_force_close && !selected.safe_to_close &&
          <button className="admin-button admin-button-danger-outline admin-force-close-table"
            type="button" disabled={closing} onClick={() => setForceConfirming(true)}>
            Force Close Table</button>}
        </>}
      </aside>
    </div>}
  </div>}
  {view === "pending" && <div className="admin-prep-list">
    {floorItems?.length === 0 && <div className="admin-empty">No outstanding kitchen items.</div>}
    {floorItems?.map((item) => <section className="admin-prep-card" key={item.item_id}>
      <div className="admin-prep-heading"><div><strong>{item.name}</strong>
        <small>NEW {item.new_quantity} · READY {item.ready_quantity}</small></div>
        <strong>{item.remaining_quantity} remaining</strong></div>
      <div className="admin-prep-references">Tables: {item.tables.map((table) => <button type="button"
        key={table.session_id} onClick={() => {
          switchView("tables");
          const physical = tables?.find((row) => row.table_id === table.table);
          if (physical) { setSelectedTableId(physical.table_id); setSelected(physical.session); }
        }}>{table.table} ×{table.remaining_quantity}</button>)}</div>
    </section>)}
  </div>}
  {confirming && selected && <div className="admin-modal-backdrop" role="presentation">
    <div className="admin-modal" role="dialog" aria-modal="true" aria-label="Confirm close table">
      <h2>Close {selected.display_name}?</h2>
      <p>All kitchen items are delivered and Clover payment will be checked again before closing this local session.</p>
      <div><button className="admin-button admin-button-light" type="button" onClick={() => setConfirming(false)} disabled={closing}>Cancel</button>
        <button className="admin-button" type="button" onClick={() => void closeTable()} disabled={closing}>
          {closing ? "Closing…" : "Confirm Close Table"}</button></div>
    </div>
  </div>}
  {forceConfirming && selected && <ForceCloseDialog session={selected} busy={closing}
    onCancel={() => setForceConfirming(false)} onConfirm={(reason) => void closeTable(reason)} />}
  </>;
}

export function TableDetail() {
  return <AdminShell><TableDetailContent /></AdminShell>;
}

function TableDetailContent() {
  const params = useParams<{ sessionId: string }>();
  const router = useRouter();
  const [session, setSession] = useState<AdminTableSession | null>(null);
  const [error, setError] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [forceConfirming, setForceConfirming] = useState(false);
  const [closing, setClosing] = useState(false);
  const [transitioning, setTransitioning] = useState<string | null>(null);
  const [paymentBusy, setPaymentBusy] = useState(false);
  const closeTriggerRef = useRef<HTMLButtonElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    let active = true;
    adminApi.detail(params.sessionId).then(async (result) => {
      if (!active) return;
      setSession(result);
      if (result.status === "open" && result.clover_order_ref) {
        setPaymentBusy(true);
        try {
          const refreshed = await adminApi.refreshPayment(result.session_id);
          if (active) setSession(refreshed);
        } catch (failure) {
          if (active) {
            setError(message(failure));
            setSession((current) => current && { ...current, payment_state: "UNKNOWN",
              payment_checked_at: null, safe_to_close: false });
          }
        } finally { if (active) setPaymentBusy(false); }
      }
    })
      .catch((failure) => { if (active) setError(message(failure)); });
    return () => { active = false; };
  }, [params.sessionId]);

  useEffect(() => {
    if (!confirming) return;
    const trigger = closeTriggerRef.current;
    cancelRef.current?.focus();
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape" && !closing) {
        event.preventDefault();
        setConfirming(false);
      }
      if (event.key !== "Tab") return;
      const buttons = document.querySelectorAll<HTMLButtonElement>(".admin-modal button:not(:disabled)");
      if (buttons.length < 2) return;
      if (event.shiftKey && document.activeElement === buttons[0]) {
        event.preventDefault();
        buttons[buttons.length - 1].focus();
      } else if (!event.shiftKey && document.activeElement === buttons[buttons.length - 1]) {
        event.preventDefault();
        buttons[0].focus();
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => { document.removeEventListener("keydown", onKeyDown); trigger?.focus(); };
  }, [confirming, closing]);

  async function close(forceReason?: string) {
    if (!session) return;
    setClosing(true);
    setError("");
    try {
      if (forceReason === undefined) await adminApi.close(session.session_id);
      else await adminApi.forceClose(session.session_id, forceReason);
      router.replace("/admin/tables");
    } catch (failure) {
      setError(message(failure));
      setConfirming(false);
      setForceConfirming(false);
    } finally { setClosing(false); }
  }

  async function advance(lineId: string, status: "NEW" | "READY") {
    setTransitioning(lineId);
    setError("");
    try {
      const result = status === "NEW" ? await adminApi.ready(lineId) : await adminApi.delivered(lineId);
      setSession(result);
    } catch (failure) { setError(message(failure)); }
    finally { setTransitioning(null); }
  }

  async function refreshPayment() {
    if (!session) return;
    setPaymentBusy(true);
    setError("");
    try { setSession(await adminApi.refreshPayment(session.session_id)); }
    catch (failure) {
      setError(message(failure));
      setSession((current) => current && { ...current, payment_state: "UNKNOWN",
        payment_checked_at: null, safe_to_close: false });
    } finally { setPaymentBusy(false); }
  }

  return <>
    <Link className="admin-back" href="/admin/tables">← Tables</Link>
    {error && <p className="admin-alert" role="alert">{error}</p>}
    {!session && !error && <p role="status">Loading table session…</p>}
    {session && <>
      <div className="admin-title-row"><div><p className="admin-kicker">Table session</p>
        <h1>{session.display_name}</h1><Status session={session} /></div>
        {session.status === "open" && <button ref={closeTriggerRef} className="admin-button" type="button"
          disabled={!session.safe_to_close} onClick={() => setConfirming(true)}>Close Table</button>}
      </div>
      {session.reconciliation_pending && <p className="admin-warning">Order sync needs review. Kitchen changes and normal closing are paused. Force close requires an explicit override.</p>}
      <div className="admin-detail-grid">
        <section className="admin-panel"><h2>Kitchen progress</h2>
          <Counts session={session} />
          {session.kitchen_state === "ALL_DELIVERED" && <p className="admin-complete">All items delivered. {session.payment_state === "PAID" ? "Paid in Clover. Close Table when service is complete." :
            session.payment_state === "UNAVAILABLE" ? "Payment unavailable in Clover. Force Close remains available." :
              "Payment pending in Clover. Close Table is disabled."}</p>}
          {session.payment_warning && <p className="admin-warning">{session.payment_warning}. You may refresh payment or force close this table.</p>}
          {session.kitchen_lines.length ? <ul className="admin-kitchen-lines">
            {session.kitchen_lines.map((line) => <li key={line.kitchen_line_id}>
              <div><strong>{line.item_name} ×{line.quantity}</strong>
                {line.modifiers.map((modifier) => <span key={modifier.id}>{modifier.name}</span>)}
                <small>{line.status}</small></div>
              {session.status === "open" && line.status !== "DELIVERED" && <button
                className="admin-button admin-button-light" type="button"
                disabled={session.reconciliation_pending || transitioning !== null}
                onClick={() => void advance(line.kitchen_line_id, line.status as "NEW" | "READY")}>
                {transitioning === line.kitchen_line_id ? "Updating…" : line.status === "NEW" ? "Mark Ready" : "Mark Delivered"}
              </button>}
              {line.status === "DELIVERED" && <span className="admin-delivered">Delivered</span>}
            </li>)}
          </ul> : <p>No confirmed kitchen items yet.</p>}
          {session.status === "open" && !session.safe_to_close && !session.reconciliation_pending &&
            <p>Close Table requires delivery of every item and confirmed Clover payment.</p>}
          {session.status === "open" && session.safe_to_force_close && !session.safe_to_close &&
            <button className="admin-button admin-button-danger-outline admin-force-close-table"
              type="button" disabled={closing} onClick={() => setForceConfirming(true)}>
              Force Close Table</button>}
        </section>
        <section className="admin-panel"><h2>Current check</h2>
          {session.order?.items.length ? <ul className="admin-order-lines">
            {session.order.items.map((line, index) => <li key={`${line.item_id}-${index}`}>
              <div><strong>{line.quantity} × {line.name || "Item"}</strong>
                {line.modifiers?.map((modifier) => <span key={modifier.id}>{modifier.name}</span>)}</div>
              <span>{money(line.unit_price, session.order?.currency || "USD")}</span>
            </li>)}
          </ul> : <p>No confirmed item readback is stored for this session yet.</p>}
          <div className="admin-total"><span>Last confirmed total</span>
            <strong>{money(session.order?.total, session.order?.currency || "USD")}</strong></div>
        </section>
        <section className="admin-panel"><h2>Session details</h2>
          <dl className="admin-facts">
            <div><dt>Status</dt><dd>{session.status}</dd></div>
            <div><dt>Opened</dt><dd>{time(session.opened_at)}</dd></div>
            <div><dt>Last activity</dt><dd>{time(session.last_activity_at)}</dd></div>
            <div><dt>Closed</dt><dd>{time(session.closed_at)}</dd></div>
            <div><dt>Submissions</dt><dd>{session.mutation_count}</dd></div>
            <div><dt>Clover order</dt><dd>{session.clover_order_ref || "Not created"}</dd></div>
            <div><dt>Clover status</dt><dd>{session.clover_order_status || "Not checked"}</dd></div>
            <div><dt>Payment</dt><dd><Payment session={session} /></dd></div>
            <div><dt>Reconciliation</dt><dd>{session.reconciliation_pending ? "Pending" : "Clear"}</dd></div>
            {session.forced_close && <>
              <div><dt>Close type</dt><dd>Force Closed</dd></div>
              <div><dt>Override reason</dt><dd>{session.forced_close_reason}</dd></div>
              <div><dt>Closed by</dt><dd>{session.forced_close_by}</dd></div>
            </>}
          </dl>
          {session.status === "open" && <button className="admin-button admin-button-light" type="button"
            disabled={paymentBusy || !session.clover_order_ref} onClick={() => void refreshPayment()}>
            {paymentBusy ? "Checking Clover…" : "Refresh payment"}</button>}
        </section>
      </div>
      {confirming && <div className="admin-modal-backdrop" role="presentation">
        <div className="admin-modal" role="dialog" aria-modal="true" aria-label="Confirm close table">
          <h2>Close {session.display_name}?</h2>
          <p>The next guest scanning this table will start a new session. The existing Clover order and history will be preserved.</p>
          <div><button ref={cancelRef} type="button" className="admin-button admin-button-light" onClick={() => setConfirming(false)} disabled={closing}>Cancel</button>
            <button type="button" className="admin-button" onClick={() => void close()} disabled={closing}>
              {closing ? "Closing…" : "Confirm Close Table"}</button></div>
        </div>
      </div>}
      {forceConfirming && <ForceCloseDialog session={session} busy={closing}
        onCancel={() => setForceConfirming(false)} onConfirm={(reason) => void close(reason)} />}
    </>}
  </>;
}

export function SessionHistory() {
  return <AdminShell><SessionHistoryContent /></AdminShell>;
}

export function PrepSummaryPage() {
  return <AdminShell><PrepSummaryContent /></AdminShell>;
}

function PrepSummaryContent() {
  const [items, setItems] = useState<AdminPrepItem[] | null>(null);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const load = useCallback(async () => {
    setRefreshing(true);
    try { setItems((await adminApi.prep()).items); setError(""); }
    catch (failure) { setError(message(failure)); }
    finally { setRefreshing(false); }
  }, []);
  useEffect(() => {
    adminApi.prep().then((page) => setItems(page.items))
      .catch((failure) => setError(message(failure)));
    const timer = window.setInterval(() => { void load(); }, 15000);
    return () => window.clearInterval(timer);
  }, [load]);

  const remaining = items?.reduce((total, item) => total + item.remaining_quantity, 0) || 0;
  return <><div className="admin-title-row"><div>
    <p className="admin-kicker">Across open tables</p><h1>Prep Summary</h1>
    <p>{remaining} outstanding portion{remaining === 1 ? "" : "s"} · Delivered lines excluded</p>
  </div><button className="admin-button admin-button-light" type="button" onClick={load}
    disabled={refreshing}>{refreshing ? "Refreshing…" : "Refresh prep"}</button></div>
  {error && <p className="admin-alert" role="alert">{error}</p>}
  {items === null && !error && <p role="status">Loading prep summary…</p>}
  {items?.length === 0 && <div className="admin-empty">No outstanding kitchen items.</div>}
  {items && items.length > 0 && <div className="admin-prep-list">
    {items.map((item) => <section className="admin-prep-card" key={item.item_id}>
      <button type="button" aria-expanded={expanded === item.item_id}
        aria-controls={`prep-${item.item_id.replace(/[^A-Za-z0-9_-]/g, "-")}`}
        onClick={() => setExpanded((current) => current === item.item_id ? null : item.item_id)}>
        <span><strong>{item.name}</strong><small>{item.new_quantity} New · {item.ready_quantity} Ready</small></span>
        <span>{item.remaining_quantity} remaining <span aria-hidden="true">{expanded === item.item_id ? "−" : "+"}</span></span>
      </button>
      {expanded === item.item_id && <div className="admin-prep-tables"
        id={`prep-${item.item_id.replace(/[^A-Za-z0-9_-]/g, "-")}`}>
        {item.tables.map((table) => <p key={table.session_id}>
          <strong>{table.table}</strong><span>{table.new_quantity} New · {table.ready_quantity} Ready · {table.remaining_quantity} portions</span>
        </p>)}
      </div>}
    </section>)}
  </div>}
  </>;
}

function SessionHistoryContent() {
  const [sessions, setSessions] = useState<AdminTableSession[]>([]);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async (offset: number) => {
    setLoading(true);
    setError("");
    try {
      const page = await adminApi.history(offset);
      setSessions((prior) => offset === 0 ? page.sessions : [...prior, ...page.sessions]);
      setNextOffset(page.next_offset);
    } catch (failure) { setError(message(failure)); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => {
    adminApi.history(0).then((page) => {
      setSessions(page.sessions);
      setNextOffset(page.next_offset);
    }).catch((failure) => setError(message(failure)))
      .finally(() => setLoading(false));
  }, []);

  return <><div className="admin-title-row"><div>
    <p className="admin-kicker">Archive</p><h1>Session History</h1>
    <p>Closed visits, newest first. Totals are the last locally confirmed readback.</p>
  </div></div>
  {error && <p className="admin-alert" role="alert">{error}</p>}
  {loading && sessions.length === 0 && <p role="status">Loading history…</p>}
  {!loading && sessions.length === 0 && !error && <div className="admin-empty">No closed sessions yet.</div>}
  {sessions.length > 0 && <div className="admin-history-table"><table>
    <thead><tr><th>Table</th><th>Opened</th><th>Closed</th><th>Kitchen lines</th><th>Final known total</th><th>Payment</th><th>Clover order</th><th>Status</th></tr></thead>
    <tbody>{sessions.map((session) => <tr key={session.session_id}>
      <td><Link href={`/admin/tables/${session.session_id}`}>{session.display_name}</Link></td>
      <td>{time(session.opened_at)}</td><td>{time(session.closed_at)}</td>
      <td>{session.total_item_lines} ({session.delivered_count} delivered)</td>
      <td>{money(session.order?.total, session.order?.currency || "USD")}</td>
      <td><Payment session={session} /></td>
      <td>{session.clover_order_ref || "—"}</td><td><Status session={session} />
        {session.forced_close && <small className="admin-history-reason">
          Reason: {session.forced_close_reason}
          {session.forced_close_unresolved && " · Order sync unresolved at close"}
        </small>}</td>
    </tr>)}</tbody>
  </table></div>}
  {nextOffset !== null && <button className="admin-button admin-button-light" type="button"
    disabled={loading} onClick={() => void load(nextOffset)}>Load more</button>}
  </>;
}
