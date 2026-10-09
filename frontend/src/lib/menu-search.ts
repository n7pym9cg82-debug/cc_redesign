import type { MenuCategory, MenuItem } from "@/lib/api";

export function searchMenu(categories: MenuCategory[], query: string): MenuItem[] {
  const term = query.trim().toLocaleLowerCase();
  if (!term) return [];
  const matches = new Map<string, MenuItem>();
  for (const category of categories) {
    const categoryMatches = category.name.toLocaleLowerCase().includes(term);
    for (const item of category.items) {
      if (categoryMatches || [item.name, item.description ?? ""].some((value) =>
        value.toLocaleLowerCase().includes(term))) matches.set(item.id, item);
    }
  }
  return [...matches.values()];
}
