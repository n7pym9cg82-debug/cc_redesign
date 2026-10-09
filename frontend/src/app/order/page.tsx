import type { Metadata } from "next";
import Link from "next/link";
import { InteriorPage } from "@/components/home/site-shell";

export const metadata: Metadata = {
  title: "Order Online | Chennai Café",
  description: "General online ordering is coming soon. Dine-in guests can use their table’s QR code.",
};

export default function GeneralOrderPage() {
  return <InteriorPage eyebrow="Ordering" title="Online ordering coming soon."
    intro="General online ordering is not available yet. Dine-in guests can scan the QR code at their table to begin a separate, secure ordering session.">
    <div className="site-interior-panel"><h2>Already at Chennai Café?</h2>
      <p>Scan your table’s QR code to open its secure ordering page and current menu.</p>
      <Link className="site-text-link" href="/menu">Browse the public menu <span aria-hidden="true">↗</span></Link></div>
  </InteriorPage>;
}
