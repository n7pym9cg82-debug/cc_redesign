"use client";

import { useEffect, useMemo, useState } from "react";
import { customerApi, type MenuCategory } from "@/lib/api";
import { previewDishes } from "./public-menu-data";

type PublicDish = { id: string; name: string; category: string; price: number; currency: string; weekend: boolean; modifiers: boolean };

function fromCatalog(categories: MenuCategory[]): PublicDish[] {
  return categories.flatMap((category) => category.items.map((item) => ({
    id: item.id, name: item.name, category: category.name, price: item.price,
    currency: item.currency, weekend: item.availability.type === "weekend",
    modifiers: item.modifier_groups.length > 0,
  })));
}

const preview: PublicDish[] = previewDishes.map((dish) => ({
  id: dish.name, name: dish.name, category: dish.category, price: dish.price,
  currency: "USD", weekend: false, modifiers: false,
}));

export function PublicMenuBrowser() {
  const [catalog, setCatalog] = useState<PublicDish[] | null>(null);
  const [category, setCategory] = useState("All");
  const [query, setQuery] = useState("");

  useEffect(() => {
    let active = true;
    customerApi.menu().then((menu) => {
      if (active) setCatalog(fromCatalog(menu.categories));
    }).catch(() => { if (active) setCatalog([]); });
    return () => { active = false; };
  }, []);

  const usingPreview = catalog !== null && catalog.length === 0;
  const dishes = catalog && catalog.length > 0 ? catalog : preview;
  const categories = useMemo(() => ["All", ...new Set(dishes.map((dish) => dish.category))], [dishes]);
  const visible = dishes.filter((dish) => (category === "All" || dish.category === category) &&
    `${dish.name} ${dish.category}`.toLowerCase().includes(query.trim().toLowerCase()));

  return <div className="public-menu-browser">
    <p className="site-interior-note">{usingPreview
      ? "Showing a small menu preview. Scan your table’s QR code for the current dine-in menu and availability."
      : "Browse the menu here. Ordering at a table starts with its QR code."}</p>
    <label className="public-menu-search">Search the menu
      <input type="search" value={query} onChange={(event) => setQuery(event.target.value)}
        placeholder="Search dishes or categories" />
    </label>
    <div className="public-menu-categories" role="group" aria-label="Menu categories">
      {categories.map((name) => <button key={name} type="button" aria-pressed={category === name}
        onClick={() => setCategory(name)}>{name}</button>)}
    </div>
    {visible.length > 0 ? <div className="public-menu-grid">
      {visible.map((dish) => <article className="public-menu-item" key={`${dish.category}-${dish.id}`}>
        <div><p>{dish.category}</p><h2>{dish.name}</h2>
          {dish.weekend && <span className="public-menu-tag">Weekend availability</span>}
          {dish.modifiers && <span className="public-menu-tag">Choices available</span>}
        </div>
        <span className="public-menu-price">{new Intl.NumberFormat("en-US", { style: "currency", currency: dish.currency }).format(dish.price)}</span>
      </article>)}
    </div> : <p className="public-menu-empty">No dishes match that search.</p>}
  </div>;
}
