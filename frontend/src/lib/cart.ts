import type { MenuItem, OrderItem } from "./api";

export type Cart = Record<string, number>;

export function variantKey(itemId: string, modifierIds: string[] = []): string {
  return modifierIds.length ? `${itemId}|${[...modifierIds].sort().join(",")}` : itemId;
}

export function parseVariantKey(value: string): { itemId: string; modifierIds: string[] } | null {
  const [itemId, encoded, extra] = value.split("|");
  if (extra !== undefined || !/^[A-Za-z0-9_-]{1,64}$/.test(itemId)) return null;
  if (encoded === undefined) return { itemId, modifierIds: [] };
  const modifierIds = encoded.split(",");
  if (!modifierIds.length || modifierIds.some((id) => !/^[A-Za-z0-9_-]{1,64}$/.test(id)) ||
      modifierIds.join(",") !== [...new Set(modifierIds)].sort().join(",")) return null;
  return { itemId, modifierIds };
}

export function changeQuantity(cart: Cart, itemId: string, delta: number): Cart {
  const next = { ...cart };
  const quantity = Math.max(0, Math.min(10, (next[itemId] ?? 0) + delta));
  if (quantity === 0) delete next[itemId];
  else next[itemId] = quantity;
  return next;
}

export function subtotalCents(cart: Cart, menu: MenuItem[]): number {
  const byId = new Map(menu.map((item) => [item.id, item]));
  return Object.entries(cart).reduce((sum, [identity, quantity]) => {
    const variant = parseVariantKey(identity);
    const item = variant && byId.get(variant.itemId);
    if (!item) return sum;
    const options = item.modifier_groups?.flatMap((group) => group.options) ?? [];
    const delta = variant.modifierIds.reduce((amount, id) =>
      amount + Math.round((options.find((option) => option.id === id)?.price_delta ?? 0) * 100), 0);
    return sum + (Math.round(item.price * 100) + delta) * quantity;
  }, 0);
}

export function cartCount(cart: Cart): number {
  return Object.values(cart).reduce((sum, quantity) => sum + quantity, 0);
}

export function orderItems(cart: Cart): OrderItem[] {
  return Object.entries(cart).map(([identity, quantity]) => {
    const variant = parseVariantKey(identity);
    if (!variant) throw new Error("Invalid cart identity");
    return { item_id: variant.itemId, quantity,
      ...(variant.modifierIds.length ? { modifier_ids: variant.modifierIds } : {}) };
  });
}

export function cartStorageKey(token: string, sessionId: string): string {
  return `restaurant-cart:${token}:${sessionId}`;
}

export function pendingStorageKey(token: string, sessionId: string): string {
  return `restaurant-pending:${token}:${sessionId}`;
}

export type PendingSubmission = { key: string; items: OrderItem[]; confirmationPending?: boolean };
