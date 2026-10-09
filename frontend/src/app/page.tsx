import { HomeHero } from "@/components/home/home-hero";
import { BrandValues, CateringSection, FeaturedMenuSection, GalleryPreview, VisitSection } from "@/components/home/home-sections";
import { SiteShell } from "@/components/home/site-shell";

export default function HomePage() {
  return <SiteShell><main className="home-landing" id="main-content">
    <HomeHero />
    <BrandValues />
    <FeaturedMenuSection />
    <VisitSection />
    <CateringSection />
    <GalleryPreview />
  </main></SiteShell>;
}
