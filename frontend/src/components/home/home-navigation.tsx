"use client";

import Image from "next/image";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

const destinations = [
  { label: "Menu", href: "/menu" },
  { label: "About", href: "/about" },
  { label: "Catering", href: "/catering" },
  { label: "Gallery", href: "/gallery" },
  { label: "Visit", href: "/hours-location" },
];

const drawerDestinations = [
  { label: "Home", href: "/" },
  ...destinations,
  { label: "Contact", href: "/contact" },
  { label: "Order Online", href: "/order" },
  { label: "Reservations", href: "/reservations" },
];

export function HomeNavigation() {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);

  const close = useCallback(() => {
    setOpen(false);
    triggerRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!open) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeRef.current?.focus();
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        close();
      }
      if (event.key !== "Tab" || !panelRef.current) return;
      const focusable = panelRef.current.querySelectorAll<HTMLElement>("button, a[href]");
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [close, open]);

  return <>
    <header className="site-header">
      <div className="site-container site-header-inner">
        <Link className="site-brand" href="/" aria-label="Chennai Café home">
          <Image src="/images/chennai-cafe-logo.png" alt="" width={1774} height={887}
            sizes="(max-width: 760px) 146px, 174px" />
        </Link>
        <nav className="site-desktop-nav" aria-label="Main navigation">
          {destinations.map(({ label, href }) => <Link key={href} href={href}
            aria-current={pathname === href ? "page" : undefined}>{label}</Link>)}
        </nav>
        <Link className="site-header-cta" href="/order">Order Online <span aria-hidden="true">↗</span></Link>
        <button ref={triggerRef} className="home-nav-trigger" type="button"
          aria-label="Open navigation" aria-expanded={open} aria-controls="home-navigation-panel"
          onClick={() => setOpen(true)}>
          <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            strokeWidth="1.8" strokeLinecap="round"><path d="M4 6h16M4 12h16M4 18h16" /></svg>
        </button>
      </div>
    </header>
    {open && <>
      <div className="home-drawer-backdrop" aria-hidden="true" onClick={close} />
      <div ref={panelRef} className="home-drawer" id="home-navigation-panel" role="dialog"
        aria-modal="true" aria-label="Website navigation">
        <div className="home-drawer-top"><span>Chennai Café</span>
          <button ref={closeRef} type="button" aria-label="Close navigation" onClick={close}>
            <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor"
              strokeWidth="1.8" strokeLinecap="round"><path d="M5 5 19 19M19 5 5 19" /></svg>
          </button>
        </div>
        <nav aria-label="Website pages">
          {drawerDestinations.map(({ label, href }) => <Link key={href} href={href}
            aria-current={pathname === href ? "page" : undefined} onClick={close}>
            {label}<span aria-hidden="true">↗</span>
          </Link>)}
        </nav>
      </div>
    </>}
  </>;
}
