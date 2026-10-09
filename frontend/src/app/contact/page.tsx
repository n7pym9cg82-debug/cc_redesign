import type { Metadata } from "next";
import Link from "next/link";
import { InteriorPage } from "@/components/home/site-shell";

export const metadata: Metadata = {
  title: "Contact | Chennai Café",
  description: "Find contact and visit information for Chennai Café. Verified details are coming soon.",
};

export default function ContactPage() {
  return <InteriorPage eyebrow="Get in touch" title="Contact Chennai Café."
    intro="We look forward to hearing from you. Verified contact details and a direct inquiry form will appear here soon.">
    <div className="site-contact-grid">
      <div className="site-interior-panel"><h2>Reach us</h2><dl className="site-detail-list">
        <div><dt>Phone</dt><dd>To be confirmed</dd></div>
        <div><dt>Email</dt><dd>To be confirmed</dd></div>
        <div><dt>Address</dt><dd>To be confirmed</dd></div>
        <div><dt>Social</dt><dd>Links coming soon</dd></div>
      </dl><Link className="site-text-link" href="/hours-location">Plan your visit <span aria-hidden="true">↗</span></Link></div>
      <div className="site-interior-panel site-contact-form" role="group" aria-label="Contact form coming soon">
        <h2>Send a note</h2><p>The contact form is not active yet.</p>
        <label>Your name<input type="text" placeholder="Coming soon" disabled /></label>
        <label>Email address<input type="email" placeholder="Coming soon" disabled /></label>
        <label>Message<textarea placeholder="Coming soon" disabled /></label>
        <button className="site-button site-button-dark" type="button" disabled>Send message</button>
      </div>
    </div>
  </InteriorPage>;
}
