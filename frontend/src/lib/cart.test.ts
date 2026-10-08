import { describe, expect, it } from "vitest";
import { cartStorageKey, changeQuantity, orderItems, subtotalCents, variantKey } from "./cart";

describe("cart", () => {
  it("increments, decrements, removes and caps quantity", () => {
    let cart = changeQuantity({}, "A", 1);
    cart = changeQuantity(cart, "A", 1);
    expect(cart.A).toBe(2);
    cart = changeQuantity(cart, "A", -1);
    expect(cart.A).toBe(1);
    expect(changeQuantity(cart, "A", -1)).toEqual({});
    expect(changeQuantity({}, "A", 100).A).toBe(10);
  });

  it("scopes storage to both table token and session and computes display subtotal", () => {
    expect(cartStorageKey("token-a", "session")).not.toBe(cartStorageKey("token-b", "session"));
    expect(subtotalCents({ A: 2 }, [{ id: "A", name: "Dish", price: 4.99,
      currency: "USD", availability: { type: "always" }, modifier_groups: [] }])).toBe(998);
  });

  it("distinguishes and submits modifier variants with price deltas", () => {
    const plain = variantKey("LASSI", ["PLAIN"]);
    const sweet = variantKey("LASSI", ["SWEET"]);
    const cart = { [plain]: 1, [sweet]: 2 };
    expect(plain).not.toBe(sweet);
    expect(orderItems(cart)).toEqual([
      { item_id: "LASSI", quantity: 1, modifier_ids: ["PLAIN"] },
      { item_id: "LASSI", quantity: 2, modifier_ids: ["SWEET"] },
    ]);
    expect(subtotalCents(cart, [{ id: "LASSI", name: "Lassi", price: 3.99,
      currency: "USD", availability: { type: "always" }, modifier_groups: [{
        id: "G1", name: "Style", required: true, min_select: 1, max_select: 1,
        options: [{ id: "PLAIN", name: "Plain", price_delta: 0 },
          { id: "SWEET", name: "Sweet", price_delta: 0.25 }],
      }] }])).toBe(1247);
  });
});
