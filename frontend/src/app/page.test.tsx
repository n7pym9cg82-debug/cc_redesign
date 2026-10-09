import { beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import HomePage from "./page";
import GeneralOrderPage from "./order/page";

vi.mock("next/navigation", () => ({ usePathname: () => "/" }));
beforeEach(() => cleanup());

describe("general website", () => {
  it("renders real homepage content and keeps the public navigation drawer", () => {
    render(<HomePage />);
    expect(screen.getByRole("heading", { name: "Good Food Brings People Together", level: 1 })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /artwork featuring its logo/i })).toHaveAttribute("src",
      expect.stringContaining("homePage.png"));
    const intro = screen.getByText(/Authentic South Indian flavors/);
    const story = screen.getByRole("heading", { name: "More than a meal." });
    expect(story.closest(".home-hero-content")).toContainElement(intro);
    expect(intro.compareDocumentPosition(story) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Fresh Ingredients" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Made for the table." })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "A table awaits." })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Explore Menu/ })).toHaveAttribute("href", "/menu");
    expect(screen.getByRole("link", { name: /Start Your Order/ })).toHaveAttribute("href", "/order");
    expect(screen.getByRole("navigation", { name: "Main navigation" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Website pages" })).not.toBeInTheDocument();
    const menuButton = screen.getByRole("button", { name: "Open navigation" });
    expect(menuButton).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(menuButton);
    expect(menuButton).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("dialog", { name: "Website navigation" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Home" })).toHaveAttribute("aria-current", "page");
    for (const destination of ["/about", "/menu", "/contact", "/hours-location", "/catering", "/gallery", "/order", "/reservations"]) {
      expect(screen.getAllByRole("link").some((link) => link.getAttribute("href") === destination)).toBe(true);
    }
    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(screen.getByRole("contentinfo")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Masala Dosa" })).toBeInTheDocument();
  });

  it("closes the drawer with Escape or outside click and keeps keyboard focus inside", async () => {
    render(<HomePage />);
    const menuButton = screen.getByRole("button", { name: "Open navigation" });
    fireEvent.click(menuButton);
    const closeButton = screen.getByRole("button", { name: "Close navigation" });
    expect(closeButton).toHaveFocus();
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(screen.getByRole("dialog", { name: "Website navigation" }).querySelector('a[href="/reservations"]')).toHaveFocus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(closeButton).toHaveFocus();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Website navigation" })).not.toBeInTheDocument();
    await waitFor(() => expect(menuButton).toHaveFocus());
    fireEvent.click(menuButton);
    fireEvent.click(document.querySelector(".home-drawer-backdrop")!);
    expect(screen.queryByRole("dialog", { name: "Website navigation" })).not.toBeInTheDocument();
  });

  it("keeps the general order entry separate from QR table ordering", () => {
    render(<GeneralOrderPage />);
    expect(screen.getByRole("heading", { name: "Online ordering coming soon.", level: 1 })).toBeInTheDocument();
    expect(screen.getByText(/Scan your table’s QR code/)).toBeInTheDocument();
    expect(screen.getByText(/General online ordering is not available yet/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start Your Order" })).not.toBeInTheDocument();
  });
});
