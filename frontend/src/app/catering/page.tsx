import type { Metadata } from "next";
import Link from "next/link";
import { InteriorPage } from "@/components/home/site-shell";

export const metadata: Metadata = {
  title: "Catering | Chennai Café",
  description: "Explore catering possibilities with Chennai Café. Service details and inquiries are being prepared.",
};

export default function CateringPage() {
  return <InteriorPage eyebrow="Gather together" title="Catering for your occasion."
    intro="Bring the spirit of Chennai Café to a gathering. Catering availability, formats, and inquiry details will be confirmed here soon.">
    <div className="site-occasion-grid">
      {[
        ["Corporate", "A meal shared with colleagues."],
        ["Family gatherings", "Time together around the table."],
        ["Celebrations", "Food for moments worth sharing."],
      ].map(([title, copy]) => <article className="site-occasion-card" key={title}><span aria-hidden="true">✦</span><h2>{title}</h2><p>{copy}</p></article>)}
    </div>
    <div className="site-editorial-columns site-catering-details">
      <section><p className="site-eyebrow">Service overview</p><h2>Details are on the way.</h2><p>We have not published packages, minimums, or pricing. Please check back for confirmed service details.</p></section>
      <section><p className="site-eyebrow">A taste of the menu</p><h2>Explore the possibilities.</h2><p>Browse our public menu for a sense of the dishes at Chennai Café. Catering selections may differ.</p><Link className="site-text-link" href="/menu">Browse menu <span aria-hidden="true">↗</span></Link></section>
    </div>
    <div className="site-page-cta"><p>Catering inquiries will open when contact details are confirmed.</p>
      <Link className="site-button site-button-dark" href="/contact">Contact us about catering <span aria-hidden="true">↗</span></Link></div>
  </InteriorPage>;
}
