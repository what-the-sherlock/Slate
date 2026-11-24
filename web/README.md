# Slate web

Next.js (App Router) frontend. Design spec: [docs/UI_DESIGN.md](../docs/UI_DESIGN.md).

```bash
pnpm install
pnpm dev            # http://localhost:3000; /api/* is proxied to API_ORIGIN (default http://127.0.0.1:8000)
pnpm gen:api        # regenerate src/lib/api/schema.d.ts from ../openapi.json
pnpm lint && pnpm typecheck && pnpm test
```

Fonts: Inter Tight (SIL OFL, see `src/fonts/OFL.txt`) and Geist Mono.
