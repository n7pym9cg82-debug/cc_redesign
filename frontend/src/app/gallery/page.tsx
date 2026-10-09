import type { Metadata } from "next";
import Image from "next/image";
import { InteriorPage } from "@/components/home/site-shell";

export const metadata: Metadata = {
  title: "Gallery | Chennai Café",
  description: "Explore the visual world of Chennai Café. More restaurant photography is coming soon.",
};

export default function GalleryPage() {
  return <InteriorPage eyebrow="A closer look" title="Gallery."
    intro="A glimpse of the Chennai Café visual world. Restaurant and dish photography will be added as it becomes available.">
    <div className="site-gallery-grid site-gallery-full">
      <figure className="site-gallery-tile"><Image src="/images/homePage.png" alt="Chennai Café artwork with its logo, skyline, and filter coffee" fill sizes="(max-width: 760px) 100vw, 33vw" /><figcaption>Chennai Café artwork</figcaption></figure>
      <div className="site-gallery-placeholder">Restaurant photography coming soon</div>
      <figure className="site-gallery-tile"><Image src="/images/welcomePage.png" alt="Chennai Café illustrated welcome artwork" fill sizes="(max-width: 760px) 100vw, 33vw" /><figcaption>Welcome artwork</figcaption></figure>
      {Array.from({ length: 3 }, (_, index) => <div className="site-gallery-placeholder" key={index}><span>More moments coming soon</span></div>)}
    </div>
  </InteriorPage>;
}
