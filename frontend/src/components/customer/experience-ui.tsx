"use client";

import { useEffect, useRef, type RefObject } from "react";
import Image from "next/image";
import type { MenuCategory, MenuItem, MenuModifierGroup, OrderConfirmation } from "@/lib/api";

export function money(value: number, currency = "USD"): string {
  return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(value);
}

function Icon({ name }: { name: "search" | "plus" | "minus" | "close" | "arrow" }) {
  const paths = {
    search: <><circle cx="10.8" cy="10.8" r="6.5" /><path d="m16 16 4.5 4.5" /></>,
    plus: <path d="M12 5v14M5 12h14" />,
    minus: <path d="M5 12h14" />,
    close: <path d="M5 5 19 19M19 5 5 19" />,
    arrow: <path d="M4 12h16m-6-6 6 6-6 6" />,
  };
  return <svg className="ui-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}

function imageUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

function useDialogFocus(onClose: () => void) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const dialog = dialogRef.current;
    const oldOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    dialog?.querySelector<HTMLElement>("[data-autofocus]")?.focus();
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") { event.preventDefault(); onClose(); }
      if (event.key !== "Tab" || !dialog) return;
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
      ));
      if (!focusable.length) return;
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (!dialog.contains(document.activeElement)) {
        event.preventDefault(); first.focus();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault(); first.focus();
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => { document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = oldOverflow; previous?.focus(); };
  }, [onClose]);
  return dialogRef;
}

export function RestaurantHeader({ table }: { table: string }) {
  return <header className="restaurant-header">
    <div className="content-width header-inner">
      <Image className="brand-logo" src="/images/chennai-cafe-logo.png" alt="Chennai Café"
        width={160} height={80} priority />
      <div className="table-indicator"><span className="table-indicator-label">Dine-in</span><strong>{table}</strong></div>
    </div>
  </header>;
}

export function MenuSkeleton() {
  return <main className="page-shell"><div className="loading-top" />
    <div className="content-width loading-layout" role="status" aria-label="Loading menu">
      <div className="skeleton skeleton-wordmark" /><div className="skeleton skeleton-title" />
      <div className="skeleton skeleton-intro" /><div className="skeleton skeleton-rail" />
      <div className="skeleton-list">{Array.from({ length: 5 }, (_, index) =>
        <div className="skeleton-menu-item" key={index}><div className="skeleton skeleton-dish" />
          <div className="skeleton skeleton-detail" /></div>)}</div>
    </div></main>;
}

export function CustomerErrorState({ kind, onRetry }: { kind: "table" | "menu" | "ordering"; onRetry: () => void }) {
  const heading = kind === "table" ? "This table link is no longer active." :
    kind === "menu" ? "Our menu is temporarily unavailable." :
      "Ordering is temporarily unavailable.";
  const message = kind === "menu" ? "Please try again in a moment." :
    kind === "ordering" ? "We couldn't open your check right now." :
      "Please ask a member of our team for assistance.";
  return <main className="page-shell error-page"><div className="content-width">
    <p className="eyebrow">CHENNAI CAFÉ · DINE-IN</p>
    <h1 className="display-heading">{heading}</h1>
    <p role="alert" className="body-copy">{message}</p>
    {kind !== "table" ? <p className="body-copy muted">Please ask a member of our team if you need help.</p> : null}
    <button className="primary-button" onClick={onRetry}>Try Again <Icon name="arrow" /></button>
  </div></main>;
}

export function CategoryNav({ categories, active, onSelect, onSearch, onCloseSearch, searchButtonRef,
  searchOpen, searchQuery, onSearchChange }: {
  categories: MenuCategory[]; active: string | null; onSelect: (id: string) => void;
  onSearch: () => void; onCloseSearch: () => void; searchButtonRef: RefObject<HTMLButtonElement | null>;
  searchOpen: boolean; searchQuery: string; onSearchChange: (query: string) => void;
}) {
  const railRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const rail = railRef.current;
    const selected = railRef.current?.querySelector<HTMLElement>('[aria-current="location"]');
    if (!searchOpen && rail && selected && typeof rail.scrollTo === "function") {
      rail.scrollTo({ left: selected.offsetLeft - rail.offsetLeft - (rail.clientWidth - selected.clientWidth) / 2,
        behavior: "smooth" });
    }
  }, [active, searchOpen]);
  return <nav className="category-nav" aria-label="Menu categories"><div className="content-width category-nav-inner">
    <button className={`category-search${searchOpen ? " is-active" : ""}`} ref={searchButtonRef}
      aria-label="Search menu" aria-expanded={searchOpen} onClick={onSearch}><Icon name="search" /></button>
    {searchOpen ? <MenuSearch query={searchQuery} onChange={onSearchChange} onClose={onCloseSearch}
      returnFocusRef={searchButtonRef} /> : null}
    <div className="category-links" ref={railRef} hidden={searchOpen}>
    {categories.map((category) => <a href={`#category-${category.id}`} key={category.id}
      aria-current={active === category.id ? "location" : undefined}
      className={`category-link${active === category.id ? " is-active" : ""}`}
      onClick={() => onSelect(category.id)}>{category.name}</a>)}
    </div>
  </div></nav>;
}

export function MenuSearch({ query, onChange, onClose, returnFocusRef }: {
  query: string; onChange: (query: string) => void; onClose: () => void;
  returnFocusRef: RefObject<HTMLButtonElement | null>;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const returnFocus = returnFocusRef.current;
    inputRef.current?.focus();
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") { event.preventDefault(); onClose(); }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => { document.removeEventListener("keydown", onKeyDown); returnFocus?.focus(); };
  }, [onClose, returnFocusRef]);
  return <div className="menu-search-panel" role="search">
    <label className="search-field"><Icon name="search" /><span className="sr-only">Search the menu</span>
      <input ref={inputRef} type="search" value={query} onChange={(event) => onChange(event.target.value)}
        placeholder="Search the menu" aria-label="Search the menu" />
      {query ? <button type="button" className="search-clear" aria-label="Clear search"
        onClick={() => { onChange(""); inputRef.current?.focus(); }}><Icon name="close" /></button> : null}
    </label>
    <button type="button" className="search-close" aria-label="Close search" onClick={onClose}>Close</button>
  </div>;
}

export function MenuItemCard({ item, quantity, disabled, onAdd, onIncrement, onDecrement,
  presentation = "standard" }: {
  item: MenuItem; quantity: number; disabled: boolean; onAdd: () => void;
  onIncrement: () => void; onDecrement: () => void; presentation?: "standard" | "featured";
}) {
  const image = imageUrl(item.image_url);
  const customizable = item.modifier_groups.length > 0;
  return <article className={`dish-card${presentation === "featured" ? " dish-card-featured" : ""}`}>
    <div className="dish-content"><div className="dish-title-row"><h3 className="dish-title">{item.name}</h3>
      <span className="dish-price">{money(item.price, item.currency)}</span></div>
      {item.description ? <p className="dish-description">{item.description}</p> : null}
      <div className="dish-footer"><div className="dish-meta">
        {item.availability.type === "weekend" ? <span className="availability-note">Weekend availability</span> : null}
        {customizable ? <span className="availability-note">Choose options</span> : null}
      </div>
      {!customizable && quantity > 0 ? <div className="quantity-stepper" aria-label={`${item.name} quantity`}>
        <button aria-label={`Remove one ${item.name}`} onClick={onDecrement} disabled={disabled}><Icon name="minus" /></button>
        <span aria-live="polite">{quantity}</span>
        <button aria-label={`Add one ${item.name}`} onClick={onIncrement} disabled={disabled || quantity >= 10}><Icon name="plus" /></button>
      </div> : <button className="add-to-check" onClick={onAdd} disabled={disabled}
        aria-label={customizable ? `Choose options for ${item.name}` : `Add ${item.name} to check`}>
        <Icon name="plus" /></button>}
      </div></div>
    {image ? <div className="dish-image">
      {/* Optional Clover-hosted images have no fixed Next.js remote host. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={image} alt="" loading="lazy" />
    </div> : null}
  </article>;
}

export function MenuSection({ category, disabled, quantities, onAdd, onIncrement, onDecrement }: {
  category: MenuCategory; disabled: boolean; quantities: Record<string, number>;
  onAdd: (item: MenuItem) => void; onIncrement: (item: MenuItem) => void;
  onDecrement: (item: MenuItem) => void;
}) {
  return <section className="menu-section" id={`category-${category.id}`} data-category-section={category.id}
    aria-label={category.name}><div className="section-title-row"><h2>{category.name}</h2><span aria-hidden="true" /></div>
    <div className="dish-grid">{category.items.map((item) => <MenuItemCard key={item.id} item={item}
      quantity={quantities[item.id] ?? 0} disabled={disabled} onAdd={() => onAdd(item)}
      onIncrement={() => onIncrement(item)} onDecrement={() => onDecrement(item)} />)}</div>
  </section>;
}

function ModifierGroup({ group, selected, onToggle }: {
  group: MenuModifierGroup; selected: string[]; onToggle: (id: string) => void;
}) {
  return <fieldset className="modifier-group"><legend>{group.name}</legend>
    <p className="modifier-help">{group.required ? "Required" : "Optional"} · Choose {group.min_select === group.max_select ?
      group.min_select : `${group.min_select}–${group.max_select}`}</p>
    <div className="modifier-options">{group.options.map((option) => <label className="modifier-option" key={option.id}>
      <input type={group.max_select === 1 ? "radio" : "checkbox"} name={group.id} value={option.id}
        checked={selected.includes(option.id)} onChange={() => onToggle(option.id)} />
      <span className="option-indicator" aria-hidden="true" /><span className="option-name">{option.name}</span>
      {option.price_delta !== 0 ? <span className="option-price">{option.price_delta > 0 ? "+" : ""}{money(option.price_delta)}</span> : null}
    </label>)}</div>
  </fieldset>;
}

export function DishDetailSheet({ item, selected, onToggle, onClose, onConfirm }: {
  item: MenuItem; selected: Record<string, string[]>;
  onToggle: (group: MenuModifierGroup, id: string) => void;
  onClose: () => void; onConfirm: () => void;
}) {
  const ref = useDialogFocus(onClose);
  const incomplete = item.modifier_groups.some((group) => {
    const count = (selected[group.id] ?? []).length;
    return count < group.min_select || count > group.max_select;
  });
  const selectedPrice = item.modifier_groups.reduce((sum, group) => sum + group.options
    .filter((option) => (selected[group.id] ?? []).includes(option.id))
    .reduce((groupSum, option) => groupSum + option.price_delta, 0), item.price);
  return <div className="dialog-scrim" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <div className="dialog-panel dish-sheet" role="dialog" aria-modal="true" aria-labelledby="dish-sheet-title" ref={ref}>
      <div className="sheet-grip" aria-hidden="true" /><button className="icon-close" aria-label="Close options"
        data-autofocus onClick={onClose}><Icon name="close" /></button>
      <p className="eyebrow">MAKE IT YOURS</p><h2 id="dish-sheet-title" className="sheet-title">{item.name}</h2>
      <p className="sheet-price">{money(item.price, item.currency)}</p>
      {item.description ? <p className="body-copy">{item.description}</p> : null}
      <div className="sheet-groups">{item.modifier_groups.map((group) => <ModifierGroup key={group.id}
        group={group} selected={selected[group.id] ?? []} onToggle={(id) => onToggle(group, id)} />)}</div>
      {incomplete ? <p className="selection-note">Choose the required options to continue.</p> : null}
      <button className="primary-button full-width" onClick={onConfirm} disabled={incomplete}>
        Add to Check <span aria-hidden="true">·</span> {money(selectedPrice, item.currency)}</button>
    </div>
  </div>;
}

export type CheckLine = { identity: string; quantity: number; item: MenuItem;
  options: { name: string; price_delta: number }[] };

export function StickyCheckBar({ count, subtotal, onReview }: {
  count: number; subtotal: number; onReview: () => void;
}) {
  return <div className="check-dock"><button className="check-dock-button" onClick={onReview}
    aria-label={`Review check, ${count} items`}><span className="check-dock-copy"><small>Your Check · {count} {count === 1 ? "item" : "items"}</small>
      <strong>{money(subtotal / 100)}</strong></span>
      <span className="check-dock-action">Review <Icon name="arrow" /></span></button></div>;
}

export function CheckDrawer({ table, lines, count, subtotal, pendingConfirmation, submitting,
  locked, error, hasPending, onClose, onChange, onSubmit }: {
  table: string; lines: CheckLine[]; count: number; subtotal: number; pendingConfirmation: boolean;
  submitting: boolean; locked: boolean; error: string | null; hasPending: boolean;
  onClose: () => void; onChange: (identity: string, amount: number) => void; onSubmit: () => void;
}) {
  const ref = useDialogFocus(onClose);
  return <div className="dialog-scrim check-scrim" onMouseDown={(event) => {
    if (event.target === event.currentTarget && !submitting) onClose();
  }}><div className="dialog-panel check-panel" role="dialog" aria-modal="true"
    aria-labelledby="check-title" ref={ref}>
    <div className="check-panel-head"><div><p className="eyebrow">{table} · DINE-IN</p>
      <h2 id="check-title" className="sheet-title">Your Check</h2></div>
      <button className="icon-close" aria-label="Close check" data-autofocus onClick={onClose}
        disabled={submitting}><Icon name="close" /></button></div>
    {pendingConfirmation ? <div className="pending-panel" role="status">
      <span className="pending-symbol" aria-hidden="true">✦</span>
      <h3>Your order is with the kitchen</h3><p>We&apos;re confirming the final check.</p>
    </div> : null}
    {count === 0 ? <div className="empty-check"><p>Your next course awaits.</p>
      <button className="quiet-button" onClick={onClose}>Browse the Menu</button></div> :
      <div className="check-lines">{lines.map(({ identity, quantity, item, options }) =>
        <div className="check-line" key={identity}><div className="check-line-copy"><h3>{item.name}</h3>
          {options.length ? <p>{options.map((option) => option.name).join(", ")}</p> : null}
          <small>{quantity} × {money(item.price + options.reduce((sum, option) => sum + option.price_delta, 0), item.currency)}</small>
        </div><div className="check-line-actions"><strong>{money((item.price + options.reduce((sum, option) => sum + option.price_delta, 0)) * quantity, item.currency)}</strong>
          {!hasPending ? <div className="quantity-stepper" aria-label={`${item.name} quantity`}>
            <button aria-label={`Remove one ${item.name}`} onClick={() => onChange(identity, -1)}><Icon name="minus" /></button>
            <span aria-live="polite">{quantity}</span>
            <button aria-label={`Add one ${item.name}`} disabled={quantity >= 10}
              onClick={() => onChange(identity, 1)}><Icon name="plus" /></button></div> : null}
        </div></div>)}</div>}
    <div className="check-panel-bottom"><div className="check-total"><span>Current estimate</span>
      <strong>{money(subtotal / 100)}</strong></div>
      <p className="estimate-note">Final prices, taxes and your check total are confirmed by the restaurant.</p>
      {error && !pendingConfirmation ? <p className="inline-error" role="alert">{error}</p> : null}
      {count > 0 ? <button className="primary-button full-width" disabled={submitting || locked}
        onClick={onSubmit}>{submitting ? "Sending your order to the kitchen…" :
          pendingConfirmation ? "Check Order Status" : hasPending ? "Try the Same Order Again" : "Send to Kitchen"}
        {!submitting ? <Icon name="arrow" /> : null}</button> : null}
      <button className="quiet-button full-width" onClick={onClose} disabled={submitting}>Continue Browsing</button>
    </div>
  </div></div>;
}

export function OrderConfirmationView({ confirmation, table, onAnotherCourse }: {
  confirmation: OrderConfirmation; table: string; onAnotherCourse: () => void;
}) {
  return <section className="confirmation content-width"><div className="confirmation-mark" aria-hidden="true">✓</div>
    <p className="eyebrow">SENT TO THE KITCHEN</p>
    <h1 className="display-heading">Your order is with<br />the kitchen.</h1>
    <p className="body-copy">Your selections have been sent to the kitchen.</p>
    <div className="confirmation-check"><div className="confirmation-check-head"><span>{table}</span>
      <span>Current Check</span></div><ul>{confirmation.order.items.map((line, index) =>
        <li key={`${line.item_id ?? "custom"}-${index}`}><div><strong>{line.name ?? "Item"}</strong>
          {line.modifiers?.length ? <small>{line.modifiers.map((modifier) => modifier.name).join(", ")}</small> : null}
        </div><span>×{line.quantity}</span></li>)}</ul>
      <div className="confirmation-total"><span>Current Check</span><strong>{confirmation.order.total === null ? "—" :
        money(confirmation.order.total, confirmation.order.currency ?? "USD")}</strong></div>
    </div>
    <button className="primary-button confirmation-action" onClick={onAnotherCourse}>Add Another Course <Icon name="arrow" /></button>
  </section>;
}
