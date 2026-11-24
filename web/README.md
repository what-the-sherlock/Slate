# Slate web

Next.js (App Router) frontend. Design spec: [docs/UI_DESIGN.md](../docs/UI_DESIGN.md).

```bash
pnpm install
pnpm dev            # http://localhost:3000; /api/* is proxied to API_ORIGIN (default http://127.0.0.1:8000)
pnpm gen:api        # regenerate src/lib/api/schema.d.ts from ../openapi.json
pnpm lint && pnpm typecheck && pnpm test
```

Fonts: Inter and JetBrains Mono (both SIL OFL, see `src/fonts/OFL.txt`), self-hosted with `next/font/local`. The font files and `public/brand/noise.png` (the hero's film grain) come unchanged from the Seekworthy codebase (MIT). The Slate logo (`components/brand.tsx` `LogoMark`, `app/icon.svg`) is the owner's artwork, traced to SVG. Integration logos come from Simple Icons (CC0); the trademarks belong to their owners and are shown only to name integrations. The thinking orb is `thinking-orbs` (MIT, © Jakub Antalik).
