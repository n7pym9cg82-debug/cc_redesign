import Image from "next/image";
import Link from "next/link";
import type { ReactNode } from "react";
import { HomeNavigation } from "./home-navigation";

export function SiteFooter() {
  return <footer className="site-footer">
    <div className="site-container site-footer-grid">
      <div>
        <Link className="site-footer-brand" href="/" aria-label="Chennai Café home">
          <Image src="/images/chennai-cafe-logo.png" alt="Chennai Café" width={1774} height={887} sizes="180px" />
        </Link>
        <p>Good food brings people together.</p>
      </div>
      <div><h2>Explore</h2><Link href="/menu">Menu</Link><Link href="/about">About</Link>
        <Link href="/gallery">Gallery</Link><Link href="/catering">Catering</Link></div>
      <div><h2>Visit</h2><Link href="/hours-location">Hours &amp; location</Link>
        <Link href="/contact">Contact</Link><p>Address and hours to be confirmed.</p></div>
      <div><h2>Stay connected</h2><p>Phone, email, and social links to be confirmed.</p>
        <Link href="/order">Order online</Link><Link href="/reservations">Reservations</Link></div>
    </div>
    <div className="site-container site-footer-bottom">© {new Date().getFullYear()} Chennai Café</div>
  </footer>;
}

export function SiteShell({ children }: { children: ReactNode }) {
  return <div className="site-shell"><HomeNavigation />{children}<SiteFooter /></div>;
}

export function InteriorPage({ eyebrow, title, intro, children }: {
  eyebrow: string; title: string; intro: string; children: ReactNode;
}) {
  return <SiteShell><main id="main-content" className="site-interior site-container">
    <p className="site-eyebrow">{eyebrow}</p>
    <h1>{title}</h1>
    <p className="site-interior-intro">{intro}</p>
    {children}
  </main></SiteShell>;
}
