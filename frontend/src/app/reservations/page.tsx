import type { Metadata } from "next";
import Link from "next/link";
import { InteriorPage } from "@/components/home/site-shell";

export const metadata: Metadata = {
  title: "Reservations | Chennai Café",
  description: "Reservations are coming soon at Chennai Café. Visit details will be shared when available.",
};

export default function ReservationsPage() {
  return <InteriorPage eyebrow="Plan ahead" title="Reservations coming soon."
    intro="Reservations are not available online yet. We’ll share booking details here when they’re ready.">
    <div className="site-interior-panel"><p>Looking for visit details?</p>
      <Link className="site-text-link" href="/hours-location">Hours &amp; location <span aria-hidden="true">↗</span></Link></div>
  </InteriorPage>;
}
