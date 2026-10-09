import { beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { OrderingExperience } from "./ordering-experience";
import { ApiError, customerApi } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, customerApi: {
    resolveTable: vi.fn(), createSession: vi.fn(), menu: vi.fn(), placeOrder: vi.fn(),
  } };
});

const menu = { categories: [
  { id: "C1", name: "Biryani", display_order: 7, items: [
    { id: "BIRYANI", name: "Chicken Biryani", price: 16.99, currency: "USD",
      availability: { type: "always" as const }, modifier_groups: [] },
  ] },
  { id: "C2", name: "Beverages", display_order: 15, items: [
    { id: "LASSI", name: "Mango Lassi", price: 4.99, currency: "USD",
      availability: { type: "always" as const }, modifier_groups: [] },
    { id: "STYLE", name: "Lassi", price: 3.99, currency: "USD",
      availability: { type: "always" as const }, modifier_groups: [{ id: "G1", name: "Lassi Style",
        required: true, min_select: 1, max_select: 1, options: [
          { id: "PLAIN", name: "Plain", price_delta: 0 },
          { id: "SWEET", name: "Sweet", price_delta: 0 },
        ] }] },
  ] },
] };

beforeEach(() => {
  cleanup();
  sessionStorage.clear();
  vi.clearAllMocks();
  vi.mocked(customerApi.resolveTable).mockResolvedValue({ table: "T12", display_name: "Table 12", valid: true });
  vi.mocked(customerApi.createSession).mockResolvedValue({ session_id: "session-1", table: { display_name: "Table 12" }, status: "open" });
  vi.mocked(customerApi.menu).mockResolvedValue(menu);
  vi.mocked(customerApi.placeOrder).mockResolvedValue({ session_id: "session-1",
    table: { display_name: "Table 12" }, status: "open",
    order: { currency: "USD", total: 21.98, items: [
      { item_id: "BIRYANI", name: "Chicken Biryani", quantity: 1, unit_price: 16.99 },
      { item_id: "LASSI", name: "Mango Lassi", quantity: 1, unit_price: 4.99 },
    ] } });
  Object.defineProperty(globalThis.crypto, "randomUUID", { configurable: true,
    value: () => "test-idempotency-key" });
});

describe("customer ordering", () => {
  it("shows loading then a Clover menu and lets a customer change quantities", async () => {
    render(<OrderingExperience tableToken="token-a" />);
    expect(screen.getByRole("status", { name: "Loading menu" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Chicken Biryani" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Chennai Café" })).toHaveAttribute("src", expect.stringContaining("chennai-cafe-logo.png"));
    expect(screen.getByText("Table 12")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Menu categories" })).toHaveTextContent("Biryani");
    expect(screen.getByText("$16.99")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add Chicken Biryani to check" }));
    fireEvent.click(screen.getByRole("button", { name: "Add one Chicken Biryani" }));
    expect(screen.getByRole("button", { name: "Review check, 2 items" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove one Chicken Biryani" }));
    expect(screen.getByRole("button", { name: "Review check, 1 items" })).toBeInTheDocument();
  });

  it("uses dynamic categories and marks the selected category", async () => {
    render(<OrderingExperience tableToken="token-a" />);
    const navigation = await screen.findByRole("navigation", { name: "Menu categories" });
    const biryani = within(navigation).getByRole("link", { name: "Biryani" });
    const beverages = within(navigation).getByRole("link", { name: "Beverages" });
    expect(biryani).toHaveAttribute("aria-current", "location");
    fireEvent.click(beverages);
    expect(beverages).toHaveAttribute("href", "#category-C2");
    expect(beverages).toHaveAttribute("aria-current", "location");
    expect(biryani).not.toHaveAttribute("aria-current");
  });

  it("opens search, filters full and partial names case-insensitively, and restores focus on Escape", async () => {
    render(<OrderingExperience tableToken="token-a" />);
    const searchButton = await screen.findByRole("button", { name: "Search menu" });
    searchButton.focus();
    fireEvent.click(searchButton);
    const navigation = screen.getByRole("navigation", { name: "Menu categories" });
    const input = within(navigation).getByRole("searchbox", { name: "Search the menu" });
    expect(within(navigation).queryByRole("link", { name: "Biryani" })).not.toBeInTheDocument();
    expect(input).toHaveFocus();
    fireEvent.change(input, { target: { value: "CHICKEN BIRYANI" } });
    expect(screen.getByRole("heading", { name: "Search results" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("1 dish found");
    expect(screen.getByRole("heading", { name: "Chicken Biryani" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Mango Lassi" })).not.toBeInTheDocument();
    fireEvent.change(input, { target: { value: "  mAnGo  " } });
    expect(screen.getByRole("heading", { name: "Mango Lassi" })).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();
    expect(searchButton).toHaveFocus();
    expect(within(navigation).getByRole("link", { name: "Biryani" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Biryani" })).toBeInTheDocument();
  });

  it("matches category names, clears to categorized browsing, and shows an empty state", async () => {
    render(<OrderingExperience tableToken="token-a" />);
    fireEvent.click(await screen.findByRole("button", { name: "Search menu" }));
    const input = screen.getByRole("searchbox", { name: "Search the menu" });
    fireEvent.change(input, { target: { value: "beverages" } });
    expect(screen.getByRole("status")).toHaveTextContent("2 dishes found");
    expect(screen.getByRole("heading", { name: "Mango Lassi" })).toBeInTheDocument();
    fireEvent.change(input, { target: { value: "not on menu" } });
    expect(screen.getByRole("heading", { name: "No dishes found" })).toBeInTheDocument();
    expect(screen.getByText("Try another search.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear search" }));
    expect(input).toHaveFocus();
    expect(screen.getByRole("heading", { name: "Beverages" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Biryani" })).toBeInTheDocument();
  });

  it("keeps the cart unchanged while searching and still opens modifier options", async () => {
    render(<OrderingExperience tableToken="token-a" />);
    fireEvent.click(await screen.findByRole("button", { name: "Add Chicken Biryani to check" }));
    fireEvent.click(screen.getByRole("button", { name: "Search menu" }));
    fireEvent.change(screen.getByRole("searchbox", { name: "Search the menu" }),
      { target: { value: "lassi" } });
    expect(screen.getByRole("button", { name: "Review check, 1 items" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Choose options for Lassi" }));
    expect(screen.getByRole("dialog", { name: "Lassi" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close options" }));
    fireEvent.click(screen.getByRole("button", { name: "Close search" }));
    expect(screen.getByRole("button", { name: "Review check, 1 items" })).toBeInTheDocument();
  });

  it("closes the modifier sheet with Escape and restores focus", async () => {
    render(<OrderingExperience tableToken="token-a" />);
    const open = await screen.findByRole("button", { name: "Choose options for Lassi" });
    open.focus();
    fireEvent.click(open);
    expect(screen.getByRole("dialog", { name: "Lassi" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close options" })).toHaveFocus();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Lassi" })).not.toBeInTheDocument();
    expect(open).toHaveFocus();
  });

  it("shows a restaurant check with line details and quantity controls", async () => {
    render(<OrderingExperience tableToken="token-a" />);
    fireEvent.click(await screen.findByRole("button", { name: "Add Chicken Biryani to check" }));
    const bar = screen.getByRole("button", { name: "Review check, 1 items" });
    expect(bar).toHaveTextContent("Your Check");
    expect(bar).toHaveTextContent("$16.99");
    fireEvent.click(bar);
    const check = screen.getByRole("dialog", { name: "Your Check" });
    expect(within(check).getByRole("heading", { name: "Chicken Biryani" })).toBeInTheDocument();
    expect(within(check).getByText("1 × $16.99")).toBeInTheDocument();
    fireEvent.click(within(check).getByRole("button", { name: "Add one Chicken Biryani" }));
    expect(within(check).getByText("2 × $16.99")).toBeInTheDocument();
    fireEvent.click(within(check).getByRole("button", { name: "Remove one Chicken Biryani" }));
    expect(within(check).getByText("1 × $16.99")).toBeInTheDocument();
  });

  it("requires a Lassi option and keeps different modifier choices as separate cart lines", async () => {
    render(<OrderingExperience tableToken="token-a" />);
    fireEvent.click(await screen.findByRole("button", { name: "Choose options for Lassi" }));
    expect(within(screen.getByRole("dialog", { name: "Lassi" })).getByRole("button", { name: /Add to Check/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("radio", { name: "Plain" }));
    fireEvent.click(within(screen.getByRole("dialog", { name: "Lassi" })).getByRole("button", { name: /Add to Check/ }));
    fireEvent.click(screen.getByRole("button", { name: "Choose options for Lassi" }));
    fireEvent.click(screen.getByRole("radio", { name: "Sweet" }));
    fireEvent.click(within(screen.getByRole("dialog", { name: "Lassi" })).getByRole("button", { name: /Add to Check/ }));
    fireEvent.click(screen.getByRole("button", { name: "Review check, 2 items" }));
    expect(within(screen.getByRole("dialog", { name: "Your Check" })).getAllByRole("heading", { name: "Lassi" })).toHaveLength(2);
    expect(screen.getByText("Plain")).toBeInTheDocument();
    expect(screen.getByText("Sweet")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Send to Kitchen" }));
    await waitFor(() => expect(customerApi.placeOrder).toHaveBeenCalledWith(
      "session-1", "token-a", [
        { item_id: "STYLE", quantity: 1, modifier_ids: ["PLAIN"] },
        { item_id: "STYLE", quantity: 1, modifier_ids: ["SWEET"] },
      ], "test-idempotency-key"));
  });

  it("does not carry a cart to a different table token", async () => {
    const view = render(<OrderingExperience tableToken="token-a" />);
    fireEvent.click(await screen.findByRole("button", { name: "Add Chicken Biryani to check" }));
    expect(screen.getByRole("button", { name: "Review check, 1 items" })).toBeInTheDocument();
    view.rerender(<OrderingExperience tableToken="token-b" />);
    await waitFor(() => expect(customerApi.resolveTable).toHaveBeenCalledWith("token-b"));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Review check, 1 items" })).not.toBeInTheDocument());
  });

  it("restores a cart for the same table session after refresh", async () => {
    const view = render(<OrderingExperience tableToken="token-a" />);
    fireEvent.click(await screen.findByRole("button", { name: "Add Mango Lassi to check" }));
    view.unmount();
    render(<OrderingExperience tableToken="token-a" />);
    expect(await screen.findByRole("button", { name: "Review check, 1 items" })).toBeInTheDocument();
  });

  it("submits once, confirms the Clover total, then allows more items", async () => {
    render(<OrderingExperience tableToken="token-a" />);
    fireEvent.click(await screen.findByRole("button", { name: "Add Chicken Biryani to check" }));
    fireEvent.click(screen.getByRole("button", { name: "Review check, 1 items" }));
    const place = screen.getByRole("button", { name: "Send to Kitchen" });
    fireEvent.click(place);
    fireEvent.click(place);
    expect(await screen.findByRole("heading", { name: /Your order is with.*the kitchen/i, level: 1 })).toBeInTheDocument();
    expect(screen.getByText("$21.98")).toBeInTheDocument();
    expect(customerApi.placeOrder).toHaveBeenCalledTimes(1);
    expect(customerApi.placeOrder).toHaveBeenCalledWith("session-1", "token-a",
      [{ item_id: "BIRYANI", quantity: 1 }], "test-idempotency-key");
    fireEvent.click(screen.getByRole("button", { name: /Add Another Course/ }));
    expect(screen.getByRole("heading", { name: "Mango Lassi" })).toBeInTheDocument();
  });

  it("shows the backend-normalized confirmation name instead of reusing the menu name", async () => {
    vi.mocked(customerApi.placeOrder).mockResolvedValue({ session_id: "session-1",
      table: { display_name: "Table 12" }, status: "open",
      order: { currency: "USD", total: 16.99, items: [
        { item_id: "BIRYANI", name: "Clover Chicken Biryani", quantity: 1, unit_price: 16.99 },
      ] } });
    render(<OrderingExperience tableToken="token-a" />);
    fireEvent.click(await screen.findByRole("button", { name: "Add Chicken Biryani to check" }));
    fireEvent.click(screen.getByRole("button", { name: "Review check, 1 items" }));
    fireEvent.click(screen.getByRole("button", { name: "Send to Kitchen" }));
    expect(await screen.findByRole("heading", { name: /Your order is with.*the kitchen/i, level: 1 })).toBeInTheDocument();
    expect(screen.getByText(/Clover Chicken Biryani/)).toBeInTheDocument();
  });

  it("shows a friendly invalid-link error", async () => {
    vi.mocked(customerApi.resolveTable).mockRejectedValue(new Error("This table link is no longer active."));
    render(<OrderingExperience tableToken="bad-token" />);
    expect(await screen.findByRole("heading", { name: "This table link is no longer active." })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("member of our team");
  });

  it("shows a distinct calm menu-unavailable state", async () => {
    vi.mocked(customerApi.menu).mockRejectedValue(new ApiError(503, "Unavailable"));
    render(<OrderingExperience tableToken="token-a" />);
    expect(await screen.findByRole("heading", { name: "Our menu is temporarily unavailable." })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("try again");
  });

  it("retries a failed submission with its original key and locks reconciliation", async () => {
    vi.mocked(customerApi.placeOrder).mockRejectedValueOnce(new ApiError(0, "Connection lost."))
      .mockRejectedValueOnce(new ApiError(409, "We’re checking your order."));
    render(<OrderingExperience tableToken="token-a" />);
    fireEvent.click(await screen.findByRole("button", { name: "Add Mango Lassi to check" }));
    fireEvent.click(screen.getByRole("button", { name: "Review check, 1 items" }));
    fireEvent.click(screen.getByRole("button", { name: "Send to Kitchen" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Connection lost");
    fireEvent.click(screen.getByRole("button", { name: /Try the Same Order Again/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("checking your order");
    expect(screen.getByRole("button", { name: /Try the Same Order Again/ })).toBeDisabled();
    expect(customerApi.placeOrder).toHaveBeenCalledTimes(2);
    expect(vi.mocked(customerApi.placeOrder).mock.calls[0][3]).toBe(
      vi.mocked(customerApi.placeOrder).mock.calls[1][3]);
  });

  it("keeps the cart and same key while confirmation is pending, then clears on success", async () => {
    const newKey = vi.fn(() => "test-idempotency-key");
    Object.defineProperty(globalThis.crypto, "randomUUID", { configurable: true, value: newKey });
    vi.mocked(customerApi.placeOrder).mockRejectedValueOnce(new ApiError(
      503, "Your order was sent. We’re confirming it with the kitchen.",
      "ORDER_CONFIRMATION_PENDING",
    ));
    const view = render(<OrderingExperience tableToken="token-a" />);
    fireEvent.click(await screen.findByRole("button", { name: "Add Chicken Biryani to check" }));
    fireEvent.click(screen.getByRole("button", { name: "Review check, 1 items" }));
    fireEvent.click(screen.getByRole("button", { name: "Send to Kitchen" }));
    expect(await screen.findByRole("button", { name: /Check Order Status/ })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("confirming the final check");
    expect(sessionStorage.getItem("restaurant-cart:token-a:session-1")).toContain("BIRYANI");
    expect(sessionStorage.getItem("restaurant-pending:token-a:session-1")).toContain("test-idempotency-key");
    view.unmount();
    render(<OrderingExperience tableToken="token-a" />);
    fireEvent.click(await screen.findByRole("button", { name: /Check Order Status/ }));
    expect(await screen.findByRole("heading", { name: /Your order is with.*the kitchen/i, level: 1 })).toBeInTheDocument();
    expect(customerApi.placeOrder).toHaveBeenCalledTimes(2);
    expect(vi.mocked(customerApi.placeOrder).mock.calls[0][3]).toBe(
      vi.mocked(customerApi.placeOrder).mock.calls[1][3]);
    expect(newKey).toHaveBeenCalledTimes(1);
    expect(sessionStorage.getItem("restaurant-cart:token-a:session-1")).toBeNull();
    expect(sessionStorage.getItem("restaurant-pending:token-a:session-1")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Add Another Course/ }));
    expect(screen.getByRole("button", { name: "Add Chicken Biryani to check" })).toBeInTheDocument();
  });

  it("keeps definite rejection in the ordinary failure flow", async () => {
    vi.mocked(customerApi.placeOrder).mockRejectedValueOnce(new ApiError(502, "The order could not be sent."));
    render(<OrderingExperience tableToken="token-a" />);
    fireEvent.click(await screen.findByRole("button", { name: "Add Chicken Biryani to check" }));
    fireEvent.click(screen.getByRole("button", { name: "Review check, 1 items" }));
    fireEvent.click(screen.getByRole("button", { name: "Send to Kitchen" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("could not be sent");
    expect(screen.getByRole("button", { name: /Try the Same Order Again/ })).toBeEnabled();
    expect(screen.queryByRole("button", { name: /Check Order Status/ })).not.toBeInTheDocument();
  });

  it("never drops a confirmation-pending key after a later client error", async () => {
    vi.mocked(customerApi.placeOrder)
      .mockRejectedValueOnce(new ApiError(503, "Pending", "ORDER_CONFIRMATION_PENDING"))
      .mockRejectedValueOnce(new ApiError(422, "Request unavailable"));
    render(<OrderingExperience tableToken="token-a" />);
    fireEvent.click(await screen.findByRole("button", { name: "Add Chicken Biryani to check" }));
    fireEvent.click(screen.getByRole("button", { name: "Review check, 1 items" }));
    fireEvent.click(screen.getByRole("button", { name: "Send to Kitchen" }));
    fireEvent.click(await screen.findByRole("button", { name: /Check Order Status/ }));
    await waitFor(() => expect(screen.getByRole("button", { name: /Check Order Status/ })).toBeEnabled());
    expect(sessionStorage.getItem("restaurant-pending:token-a:session-1")).toContain("test-idempotency-key");
    expect(vi.mocked(customerApi.placeOrder).mock.calls[0][3]).toBe(
      vi.mocked(customerApi.placeOrder).mock.calls[1][3]);
  });
});
