import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { customerApi, type MenuResponse } from "@/lib/api";
import { PublicMenuBrowser } from "./public-menu-browser";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const menu: MenuResponse = { categories: [
  { id: "dosa", name: "Dosa", display_order: 1, items: [{
    id: "item-1", name: "Masala Dosa", price: 13.99, currency: "USD",
    availability: { type: "always" }, modifier_groups: [],
  }] },
  { id: "drinks", name: "Beverages", display_order: 2, items: [{
    id: "item-2", name: "Filter Coffee", price: 3.99, currency: "USD",
    availability: { type: "always" }, modifier_groups: [],
  }] },
] };

describe("public menu browser", () => {
  it("browses the catalog without QR ordering controls", async () => {
    vi.spyOn(customerApi, "menu").mockResolvedValue(menu);
    render(<PublicMenuBrowser />);
    await waitFor(() => expect(screen.getByRole("heading", { name: "Filter Coffee" })).toBeInTheDocument());
    expect(screen.getByText("$13.99")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Dosa" }));
    expect(screen.queryByRole("heading", { name: "Filter Coffee" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "All" }));
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "coffee" } });
    expect(screen.getByRole("heading", { name: "Filter Coffee" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Masala Dosa" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /add to cart/i })).not.toBeInTheDocument();
  });

  it("labels the static preview when the catalog is unavailable", async () => {
    vi.spyOn(customerApi, "menu").mockRejectedValue(new Error("offline"));
    render(<PublicMenuBrowser />);
    await waitFor(() => expect(screen.getByText(/Showing a small menu preview/)).toBeInTheDocument());
    expect(screen.getByRole("heading", { name: "Masala Dosa" })).toBeInTheDocument();
  });
});
