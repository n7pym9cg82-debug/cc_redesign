import Link from "next/link";
import { previewDishes } from "./public-menu-data";

export function MenuPreview({ showLink = true }: { showLink?: boolean }) {
  return <div className="site-dish-grid">
    {previewDishes.map(({ name, category, price }, index) => <article className="site-dish-card" key={name}>
      <span className="site-dish-number">0{index + 1}</span>
      <div><p>{category}</p><h3>{name}</h3></div>
      <span className="site-dish-price">${price.toFixed(2)}</span>
    </article>)}
    {showLink && <p className="site-menu-note">A preview from our menu. <Link href="/menu">View full menu <span aria-hidden="true">↗</span></Link></p>}
  </div>;
}
