import type { Metadata } from "next";
import Image from "next/image";
import Link from "next/link";
import { InteriorPage } from "@/components/home/site-shell";

export const metadata: Metadata = {
  title: "About | Chennai Café",
  description: "Learn about the South Indian inspiration and welcoming spirit behind Chennai Café.",
};

export default function AboutPage() {
  return <InteriorPage eyebrow="Our story" title="About Chennai Café."
    intro="Good food brings people together. Chennai Café takes its inspiration from South Indian flavors and the warmth of a shared table.">
    <div className="site-editorial-split">
      <div className="site-editorial-image"><Image src="/images/homePage.png" alt="Chennai Café artwork with filter coffee"
        width={1086} height={1448} sizes="(max-width: 760px) 100vw, 50vw" /></div>
      <div><p className="site-eyebrow">South Indian inspiration</p><h2>Food with a sense of place.</h2>
        <p>Our menu brings together dishes and drinks inspired by Chennai and beyond. From dosa to filter coffee, there is always something to discover and share.</p>
        <Link className="site-text-link" href="/menu">Explore the menu <span aria-hidden="true">↗</span></Link></div>
    </div>
    <blockquote className="site-pullquote">“Good food brings people together.”</blockquote>
    <div className="site-editorial-columns">
      <section><p className="site-eyebrow">Hospitality</p><h2>Make room for connection.</h2><p>Whether it is a quick visit or time around a table, we want the experience to feel welcoming.</p></section>
      <section><p className="site-eyebrow">Food philosophy</p><h2>Let the menu speak.</h2><p>Browse the dishes we serve, then use your table’s QR code for current dine-in availability and ordering.</p></section>
    </div>
  </InteriorPage>;
}
