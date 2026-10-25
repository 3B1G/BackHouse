// Adapted from the "menu hover effects" nav: each link fills with ink from the top while two borders close in above and
// below it. Recolored to the app's tokens and given its links as props; the current page keeps its borders, and keyboard
// focus gets the same effect as hover. Small screens use the header's menu button, so this nav has no toggle of its own.

export type NavItem = { label: string; href: string; current?: boolean };

export default function NavMenu({ items, className = "" }: { items: NavItem[]; className?: string }) {
  return (
    <nav aria-label="Main" className={className}>
      <ul className="flex items-center gap-1 xl:gap-3">
        {items.map((item) => (
          <li key={item.href} className="list-none">
            <a
              href={item.href}
              aria-current={item.current ? "page" : undefined}
              className="group relative inline-block no-underline focus-visible:outline-offset-4"
            >
              {/* Link text */}
              <span className="relative z-10 block px-3 py-2 text-[0.9375rem] font-bold text-ink transition-colors duration-300 group-hover:text-paper group-focus-visible:text-paper">
                {item.label}
              </span>
              {/* Top and bottom borders: they close in on hover; the current page keeps them */}
              <span
                aria-hidden="true"
                className={`absolute inset-0 origin-center border-y-2 border-ink transition-all duration-300 ${
                  item.current
                    ? "scale-y-100 opacity-100"
                    : "scale-y-[2] opacity-0 group-hover:scale-y-100 group-hover:opacity-100 group-focus-visible:scale-y-100 group-focus-visible:opacity-100"
                }`}
              />
              {/* Ink fill from the top */}
              <span
                aria-hidden="true"
                className="absolute left-0 top-[2px] h-full w-full origin-top scale-0 bg-ink opacity-0 transition-all duration-300 group-hover:scale-100 group-hover:opacity-100 group-focus-visible:scale-100 group-focus-visible:opacity-100"
              />
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}
