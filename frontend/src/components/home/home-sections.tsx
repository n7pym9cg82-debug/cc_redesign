import Image from "next/image";
import Link from "next/link";
import { MenuPreview } from "./menu-preview";

const values = [
  { title: "Fresh Ingredients", icon: <><path d="M26 5C12 5 6 11 6 20a6 6 0 0 0 6 6c9 0 14-6 14-21Z" /><path d="M5 28c5-8 11-12 19-16" /></> },
  { title: "Timeless Recipes", icon: <path d="M16 27 5.6 16.6A6.7 6.7 0 0 1 15 7.1l1 1 1-1a6.7 6.7 0 0 1 9.4 9.5L16 27Z" /> },
  { title: "Great Together", icon: <><circle cx="16" cy="9" r="4" /><path d="M7 27v-3a9 9 0 0 1 18 0v3H7ZM5 11a3 3 0 1 0 0 6m22-6a3 3 0 1 1 0 6" /></> },
];

export function BrandValues() {
  return <section className="site-section site-values site-container" aria-labelledby="values-title">
    <p className="site-eyebrow">What brings us together</p>
    <h2 id="values-title">A little more to savor.</h2>
    <div className="site-highlight-grid">
      {values.map(({ title, icon }) => <article className="site-highlight" key={title}>
        <svg aria-hidden="true" viewBox="0 0 32 32" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">{icon}</svg>
        <h3>{title}</h3>
      </article>)}
    </div>
  </section>;
}

export function FeaturedMenuSection() {
  return <section className="site-section site-section-muted" aria-labelledby="featured-title">
    <div className="site-container">
      <div className="site-section-heading-row">
        <div><p className="site-eyebrow">From the menu</p><h2 id="featured-title">Made for the table.</h2></div>
        <Link className="site-text-link" href="/menu">View full menu <span aria-hidden="true">↗</span></Link>
      </div>
      <MenuPreview showLink={false} />
    </div>
  </section>;
}

export function VisitSection() {
  return <section className="site-section site-section-muted" aria-labelledby="visit-title">
    <div className="site-container site-visit-grid">
      <div><p className="site-eyebrow">Visit us</p><h2 id="visit-title">A table awaits.</h2>
        <p className="site-section-intro">We’ll share confirmed visit details here as soon as they’re available.</p>
        <Link className="site-text-link" href="/hours-location">Plan your visit <span aria-hidden="true">↗</span></Link>
      </div>
      <dl className="site-visit-details">
        <div><dt>Address</dt><dd>To be confirmed</dd></div>
        <div><dt>Opening hours</dt><dd>To be confirmed</dd></div>
        <div><dt>Phone</dt><dd>To be confirmed</dd></div>
      </dl>
    </div>
  </section>;
}

export function CateringSection() {
  return <section className="site-section site-catering" aria-labelledby="catering-title">
    <div className="site-container site-catering-grid">
      <div><p className="site-eyebrow">Catering</p><h2 id="catering-title">Bring Chennai Café to your event.</h2>
        <p>Planning a gathering? Explore our catering page for the latest information and ways to get in touch.</p>
        <Link className="site-button site-button-dark" href="/catering">Explore catering <span aria-hidden="true">↗</span></Link>
      </div>
      <div className="site-catering-image"><Image src="/images/homePage.png" alt="Chennai Café artwork with filter coffee"
        width={1086} height={1448} sizes="(max-width: 760px) 100vw, 45vw" /></div>
    </div>
  </section>;
}

export function GalleryPreview() {
  return <section className="site-section site-container" aria-labelledby="gallery-title">
    <div className="site-section-heading-row">
      <div><p className="site-eyebrow">Gallery</p><h2 id="gallery-title">A closer look.</h2></div>
      <Link className="site-text-link" href="/gallery">View gallery <span aria-hidden="true">↗</span></Link>
    </div>
    <div className="site-gallery-grid">
      <div className="site-gallery-tile"><Image src="/images/homePage.png" alt="Chennai Café illustrated brand artwork" fill sizes="(max-width: 760px) 100vw, 33vw" /></div>
      <div className="site-gallery-placeholder">Restaurant photography coming soon</div>
      <div className="site-gallery-tile"><Image src="/images/welcomePage.png" alt="Chennai Café illustrated welcome artwork" fill sizes="(max-width: 760px) 100vw, 33vw" /></div>
    </div>
    <p className="site-gallery-note">More restaurant photography will be added as it becomes available.</p>
  </section>;
}
