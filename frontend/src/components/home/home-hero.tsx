import Image from "next/image";
import Link from "next/link";

export function HomeHero() {
  return <section className="home-hero site-container" aria-labelledby="home-hero-title">
    <div className="home-hero-content">
      <h1 id="home-hero-title">Good Food Brings People Together</h1>
      <span className="home-hero-divider" aria-hidden="true" />
      <p className="home-hero-intro">Authentic South Indian flavors, timeless recipes, and warm hospitality inspired by Chennai.</p>
      <div className="home-hero-story">
        <p className="site-eyebrow">Our story</p>
        <h2>More than a meal.</h2>
        <p>Chennai Café draws inspiration from South Indian flavors and the simple pleasure of gathering around good food. We look forward to sharing more of our story here.</p>
        <Link className="site-text-link" href="/about">Learn more <span aria-hidden="true">↗</span></Link>
      </div>
      <div className="home-hero-actions">
        <Link className="home-hero-link home-hero-link-primary" href="/menu">Explore Menu <span aria-hidden="true">↗</span></Link>
        <Link className="home-hero-link home-hero-link-secondary" href="/order">Start Your Order <span aria-hidden="true">→</span></Link>
      </div>
    </div>
    <div className="home-hero-visual site-story-image">
      <Image src="/images/homePage.png" alt="Chennai Café artwork featuring its logo, Chennai skyline, and filter coffee"
        width={1086} height={1448} priority sizes="(max-width: 760px) 100vw, (max-width: 1199px) 48vw, 52vw" />
    </div>
  </section>;
}
