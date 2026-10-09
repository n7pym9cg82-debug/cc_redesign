import type { Metadata } from "next";
import Link from "next/link";
import { InteriorPage } from "@/components/home/site-shell";
import { PublicMenuBrowser } from "@/components/home/public-menu-browser";

export const metadata: Metadata = {
  title: "Menu | Chennai Café",
  description: "Browse the Chennai Café menu. Dine-in ordering starts with your table’s QR code.",
};

export default function MenuPage() {
  return <InteriorPage eyebrow="Browse" title="The menu."
    intro="Explore the flavors of Chennai Café. This public menu is for browsing; table ordering remains on your QR link.">
    <PublicMenuBrowser />
    <div className="site-page-cta"><p>Ready to order?</p><Link className="site-button site-button-dark" href="/order">Start Your Order <span aria-hidden="true">↗</span></Link></div>
  </InteriorPage>;
}
