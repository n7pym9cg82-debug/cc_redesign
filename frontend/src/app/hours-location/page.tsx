import type { Metadata } from "next";
import { InteriorPage } from "@/components/home/site-shell";

export const metadata: Metadata = {
  title: "Visit | Chennai Café",
  description: "Plan a visit to Chennai Café. Address, hours, and directions will be shared when confirmed.",
};

export default function HoursLocationPage() {
  return <InteriorPage eyebrow="Plan your visit" title="Come visit us."
    intro="We’ll share confirmed opening hours, address, phone, and directions here soon.">
    <div className="site-visit-page-grid">
      <div className="site-interior-panel"><h2>Visit details</h2><dl className="site-detail-list">
        <div><dt>Address</dt><dd>To be confirmed</dd></div>
        <div><dt>Opening hours</dt><dd>To be confirmed</dd></div>
        <div><dt>Phone</dt><dd>To be confirmed</dd></div>
      </dl><button className="site-button site-button-dark" type="button" disabled>Directions coming soon</button></div>
      <div className="site-map-placeholder" role="img" aria-label="Map will be available once the address is confirmed">
        <span aria-hidden="true">⌖</span><p>Map and directions coming soon</p>
      </div>
    </div>
  </InteriorPage>;
}
