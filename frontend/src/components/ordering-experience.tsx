"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, customerApi, type MenuCategory, type MenuItem, type OrderConfirmation, type PublicSession } from "@/lib/api";
import { cartCount, cartStorageKey, changeQuantity, orderItems, pendingStorageKey,
  parseVariantKey, subtotalCents, variantKey, type Cart, type PendingSubmission } from "@/lib/cart";
import { searchMenu } from "@/lib/menu-search";
import { CategoryNav, CheckDrawer, CustomerErrorState, DishDetailSheet, MenuItemCard, MenuSearch, MenuSection,
  MenuSkeleton, OrderConfirmationView, RestaurantHeader, StickyCheckBar, type CheckLine } from "./customer/experience-ui";

type View = "menu" | "check" | "confirmation";

function readStoredCart(key: string): Cart {
  try {
    const value: unknown = JSON.parse(sessionStorage.getItem(key) ?? "{}");
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    return Object.fromEntries(Object.entries(value).filter(([id, quantity]) =>
      parseVariantKey(id) !== null && Number.isInteger(quantity) &&
      Number(quantity) >= 1 && Number(quantity) <= 10)) as Cart;
  } catch { return {}; }
}

function readPending(key: string): PendingSubmission | null {
  try {
    const value: unknown = JSON.parse(sessionStorage.getItem(key) ?? "null");
    if (!value || typeof value !== "object" || !("key" in value) ||
        !("items" in value) || typeof value.key !== "string" ||
        !Array.isArray(value.items)) return null;
    return value as PendingSubmission;
  } catch { return null; }
}

export function OrderingExperience({ tableToken }: { tableToken: string }) {
  const [session, setSession] = useState<PublicSession | null>(null);
  const [categories, setCategories] = useState<MenuCategory[]>([]);
  const [activeCategory, setActiveCategory] = useState<string | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [selecting, setSelecting] = useState<MenuItem | null>(null);
  const [selectedModifiers, setSelectedModifiers] = useState<Record<string, string[]>>({});
  const [cart, setCart] = useState<Cart>({});
  const [pending, setPending] = useState<PendingSubmission | null>(null);
  const [confirmation, setConfirmation] = useState<OrderConfirmation | null>(null);
  const [view, setView] = useState<View>("menu");
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadErrorKind, setLoadErrorKind] = useState<"table" | "menu" | "ordering">("table");
  const [locked, setLocked] = useState(false);
  const submittingRef = useRef(false);
  const searchButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    let active = true;
    async function load() {
      let stage: "table" | "menu" | "ordering" = "table";
      setLoading(true);
      setError(null);
      setSession(null);
      setCategories([]);
      setSearchOpen(false);
      setSearchQuery("");
      setView("menu");
      setConfirmation(null);
      setPending(null);
      setCart({});
      try {
        await customerApi.resolveTable(tableToken);
        stage = "ordering";
        const [sessionResult, menuResult] = await Promise.allSettled([
          customerApi.createSession(tableToken), customerApi.menu(),
        ]);
        if (sessionResult.status === "rejected") throw sessionResult.reason;
        stage = "menu";
        if (menuResult.status === "rejected") throw menuResult.reason;
        const currentSession = sessionResult.value;
        const currentMenu = menuResult.value;
        if (!active) return;
        setSession(currentSession);
        setCategories(currentMenu.categories);
        setActiveCategory(currentMenu.categories[0]?.id ?? null);
        setCart(readStoredCart(cartStorageKey(tableToken, currentSession.session_id)));
        const saved = readPending(pendingStorageKey(tableToken, currentSession.session_id));
        setPending(saved);
        if (saved) setView("check");
      } catch (cause) {
        if (active) {
          setLoadErrorKind(stage);
          setError(cause instanceof Error ? cause.message : "Ordering is unavailable.");
        }
      } finally {
        if (active) setLoading(false);
      }
    }
    void load();
    return () => { active = false; };
  }, [tableToken]);

  useEffect(() => {
    if (loading || !session || view === "confirmation" || searchQuery.trim() ||
        typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver((entries) => {
      const visible = entries.filter((entry) => entry.isIntersecting)
        .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
      if (visible[0]) setActiveCategory(visible[0].target.getAttribute("data-category-section"));
    }, { rootMargin: "-140px 0px -65% 0px", threshold: 0 });
    document.querySelectorAll<HTMLElement>("[data-category-section]").forEach((section) => observer.observe(section));
    return () => observer.disconnect();
  }, [categories, loading, searchQuery, session, view]);

  function updateCart(next: Cart) {
    if (!session || pending || submitting) return;
    setCart(next);
    sessionStorage.setItem(cartStorageKey(tableToken, session.session_id), JSON.stringify(next));
  }

  function addItem(item: MenuItem) {
    if (item.modifier_groups.length) {
      setSelectedModifiers({});
      setSelecting(item);
    } else {
      updateCart(changeQuantity(cart, item.id, 1));
    }
  }

  function toggleModifier(groupId: string, optionId: string, maxSelect: number) {
    setSelectedModifiers((previous) => {
      const current = previous[groupId] ?? [];
      const next = maxSelect === 1 ? [optionId] : current.includes(optionId) ?
        current.filter((id) => id !== optionId) : current.length < maxSelect ?
          [...current, optionId] : current;
      return { ...previous, [groupId]: next };
    });
  }

  function addSelectedItem() {
    if (!selecting || selecting.modifier_groups.some((group) => {
      const chosen = (selectedModifiers[group.id] ?? []).length;
      return chosen < group.min_select || chosen > group.max_select;
    })) return;
    const ids = selecting.modifier_groups.flatMap((group) => selectedModifiers[group.id] ?? []);
    updateCart(changeQuantity(cart, variantKey(selecting.id, ids), 1));
    setSelecting(null);
  }

  async function placeOrder() {
    if (!session || submittingRef.current || locked) return;
    const items = pending?.items ?? orderItems(cart);
    if (items.length === 0) return;
    const submission = pending ?? { key: crypto.randomUUID(), items };
    if (!pending) {
      setPending(submission);
      sessionStorage.setItem(pendingStorageKey(tableToken, session.session_id), JSON.stringify(submission));
    }
    submittingRef.current = true;
    setSubmitting(true);
    setError(null);
    try {
      const result = await customerApi.placeOrder(
        session.session_id, tableToken, submission.items, submission.key,
      );
      setConfirmation(result);
      setCart({});
      setPending(null);
      setLocked(false);
      sessionStorage.removeItem(cartStorageKey(tableToken, session.session_id));
      sessionStorage.removeItem(pendingStorageKey(tableToken, session.session_id));
      setView("confirmation");
    } catch (cause) {
      const failure = cause instanceof ApiError ? cause : new ApiError(0, "Please try again.");
      if (failure.status === 409) {
        setError(failure.message);
        setLocked(true);
      } else if (failure.code === "ORDER_CONFIRMATION_PENDING" || submission.confirmationPending) {
        const waiting = { ...submission, confirmationPending: true };
        setPending(waiting);
        sessionStorage.setItem(pendingStorageKey(tableToken, session.session_id), JSON.stringify(waiting));
        setError(null);
      } else {
        setError(failure.message);
      }
      if ((failure.status === 400 || failure.status === 422) &&
          !submission.confirmationPending && failure.code !== "ORDER_CONFIRMATION_PENDING") {
        setPending(null);
        sessionStorage.removeItem(pendingStorageKey(tableToken, session.session_id));
      }
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  }

  const closeCheck = useCallback(() => { if (!submitting) setView("menu"); }, [submitting]);
  const closeSheet = useCallback(() => setSelecting(null), []);
  const count = cartCount(cart);
  const menu = categories.flatMap((category) => category.items);
  const uniqueItems = [...new Map(menu.map((item) => [item.id, item])).values()];
  const itemById = new Map(uniqueItems.map((item) => [item.id, item]));
  const cartLines: CheckLine[] = Object.entries(cart).flatMap(([identity, quantity]) => {
    const variant = parseVariantKey(identity);
    const item = variant && itemById.get(variant.itemId);
    if (!variant || !item) return [];
    const options = item.modifier_groups.flatMap((group) => group.options).filter(
      (option) => variant.modifierIds.includes(option.id));
    return [{ identity, quantity, item, options }];
  });
  const displayName = session?.table.display_name ?? "Your table";
  const subtotal = subtotalCents(cart, uniqueItems);
  const results = searchMenu(categories, searchQuery);
  const searching = Boolean(searchQuery.trim());
  const closeSearch = useCallback(() => { setSearchOpen(false); setSearchQuery(""); }, []);

  function selectCategory(id: string) {
    setActiveCategory(id);
    if (searching) {
      closeSearch();
      requestAnimationFrame(() => document.getElementById(`category-${id}`)?.scrollIntoView({ behavior: "smooth" }));
    }
  }

  if (loading) return <MenuSkeleton />;
  if (!session) return <CustomerErrorState kind={loadErrorKind}
    onRetry={() => window.location.reload()} />;

  return <main className="page-shell">
    <RestaurantHeader table={displayName} />
    {view === "confirmation" && confirmation ?
      <OrderConfirmationView confirmation={confirmation} table={displayName} onAnotherCourse={() => {
        setConfirmation(null); setView("menu"); setError(null);
      }} /> : <>
      <section className="arrival content-width"><p className="eyebrow">WELCOME TO YOUR TABLE</p>
        <h1 className="display-heading">A little time to <em>savour.</em></h1>
        <p className="body-copy">Browse our menu and send your selections directly to the kitchen.</p>
        <div className="arrival-rule" aria-hidden="true"><span>✦</span></div>
      </section>
      <CategoryNav categories={categories} active={activeCategory} onSelect={selectCategory}
        onSearch={() => setSearchOpen(true)} searchButtonRef={searchButtonRef} searchOpen={searchOpen} />
      {searchOpen ? <MenuSearch query={searchQuery} onChange={setSearchQuery} onClose={closeSearch}
        returnFocusRef={searchButtonRef} /> : null}
      <div className="content-width menu-content" aria-label="Menu">
        {menu.length === 0 ? <div className="menu-unavailable"><h2>Our menu is temporarily unavailable.</h2>
          <p>Please try again in a moment.</p></div> : searching ? <section className="menu-section search-results"
            aria-label="Search results"><div className="section-title-row"><h2>Search results</h2><span aria-hidden="true" /></div>
            <p className="search-count" role="status" aria-live="polite">{results.length} {results.length === 1 ? "dish" : "dishes"} found</p>
            {results.length ? <div className="dish-grid">{results.map((item) => <MenuItemCard key={item.id}
              item={item} quantity={cart[item.id] ?? 0} disabled={Boolean(pending) || submitting}
              onAdd={() => addItem(item)} onIncrement={() => updateCart(changeQuantity(cart, item.id, 1))}
              onDecrement={() => updateCart(changeQuantity(cart, item.id, -1))} />)}</div> :
              <div className="search-empty"><h3>No dishes found</h3><p>Try another search.</p></div>}
          </section> : categories.map((category) =>
          <MenuSection key={category.id} category={category} disabled={Boolean(pending) || submitting}
            quantities={cart} onAdd={addItem}
            onIncrement={(item) => updateCart(changeQuantity(cart, item.id, 1))}
            onDecrement={(item) => updateCart(changeQuantity(cart, item.id, -1))} />)}
      </div>
      {view === "menu" && count > 0 ? <StickyCheckBar count={count} subtotal={subtotal}
        onReview={() => setView("check")} /> : null}
      {view === "check" ? <CheckDrawer table={displayName} lines={cartLines} count={count}
        subtotal={subtotal} pendingConfirmation={Boolean(pending?.confirmationPending)}
        submitting={submitting} locked={locked} error={error} hasPending={Boolean(pending)}
        onClose={closeCheck} onChange={(identity, amount) => updateCart(changeQuantity(cart, identity, amount))}
        onSubmit={() => void placeOrder()} /> : null}
    </>}
    {selecting ? <DishDetailSheet item={selecting} selected={selectedModifiers} onClose={closeSheet}
      onToggle={(group, id) => toggleModifier(group.id, id, group.max_select)}
      onConfirm={addSelectedItem} /> : null}
    <footer className="page-footer content-width"><span>CHENNAI CAFÉ</span><span>Dine well. Stay awhile.</span></footer>
  </main>;
}
