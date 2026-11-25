# UI design

Status: approved direction (2026-11-24), **revised by ADR-0017** (Seekworthy design language: warm stone neutrals, role type scale, panel grammar). This is the source of truth for every screen, component, state and string in `web/`. If a screen needs something this document doesn't cover, extend this document first, then build it.

**References**

| Reference | What we take | What we don't take |
| --- | --- | --- |
| **Seekworthy** (owner-supplied codebase, MIT), primary since ADR-0017 | Warm stone neutral ramp, role type scale (10–14 px UI, 20 px page title), full-bleed panel grammar with hairlines, KPI strip, 200/60 px collapsible sidebar, `/join` auth card (badge, headline, icon-prefixed fields, live password hints, perks list) | Its name, logo, copy, marketing palette and assets. Its 1.4:1 input borders (we keep WCAG 1.4.11) and its password composition rules (we follow NIST, `core/domain/auth.py`) |
| **Answerline** (Framer template by firdavs.xyz) | Palette, type (Inter Tight), radii, hairline borders, buttons, status pills, the "live feed" table, the cited-answer block (numbered badge + source chip), accordion, the pulse dot, motion timings | Its images, copy, client wordmarks, logo mark and marketing layout. The template is sold for $49 (free under a launch offer at the time of writing). The owner should claim it so the design language is licensed |
| **Trakkr sign-up screen** (owner's screenshot) | Sign-in layout: a large rounded gradient panel, a centred display headline, an inline input bar with the button inside it, a row of check-mark reassurances, and a "what happens next" card overlapping the panel's bottom edge | Its green palette (we use our blue) and its copy |
| **Beautiful UI** (beautifului.dev, MIT, © 2026 Shane Levine) | Patterns: task rows, chat composer / prompt bar, streaming text, sidebar nav with a gliding hover, command search, records table | Nothing is copied verbatim. If code is ever copied, keep its MIT notice in the file header |
| **Thinking Orbs** (`thinking-orbs` on npm, MIT, © Jakub Antalik) | The "agent is thinking" indicator | Paid Libraries.dev Studio and Pro presets (not needed) |

**Decisions made with the owner**

- **Light theme only.** No dark mode at the demo. Tokens are structured so a dark set can be added later (Answerline's dark values are in Appendix B).
- **Connectors screen** shows the four in-scope connectors (Upload, GitHub, Discord, Gmail import) plus non-interactive **"Coming later"** tiles for Slack, Notion, Google Drive and Confluence (roadmap Phase 3). They are labelled clearly and can't be clicked.
- **Brand logos are always full colour**, official artwork, never recoloured.
- **Sign-in page** follows the Trakkr screenshot layout, adapted to our palette and to email + password sign-in and sign-up (ADR-0021). Copy approved as written in §4.1.
- **Product name: Slate.** The logo will be supplied by the owner; until then use the placeholder in §3.12.
- **Dependencies in §12 are approved.** Each is still added only in the milestone that first uses it.

**Assets from the Answerline export** (`answerline.framer.website.zip`). Copy the files we use into the repo, unchanged, and record their origin in `web/src/assets/README.md`.

| Asset in the zip | Use | Goes to |
| --- | --- | --- |
| Inter Tight `.woff2` files in `fonts.gstatic.com/s/intertight/v9/`: `…mjDw-qW…` = 400, `…mjPQ-qW…` = 500, `…mj0QiqW…` = 600 | Was the app's font until ADR-0017 (now Inter). The files were removed from the repo | — |
| Colour tokens, type presets, radii, shadow, spacing, motion config (inside `index.html` and the site modules) | Already transcribed into §2 and Appendix A | `web/src/styles/tokens.css` |
| AI-provider marks in `framerusercontent.com/images/`: OpenAI (`c4lGZvEW…svg`) and Gemini (`Fwu7qT1m…svg`) | Provider column of the Usage table (§4.6), at 16 px | `web/src/components/brand-icons/` |
| Answerline's inline UI icons (menu, arrow, check, plus/minus, crosshair): 24 px grid, round caps, stroke 1.6–2 | Not copied (only ~6 icons). Their stroke weight sets ours: Lucide at stroke 1.6 (§2.6). The crosshair is redrawn for the sign-in rails | — |

Not used, and why:

- **Season Mix** (`firdavs.xyz/fonts/`): a commercial font belonging to the template author's own site promo. Answerline itself doesn't use it, and the template purchase doesn't license it to us.
- **Photos, client wordmarks** (Northbeam, Tallyhaus, Corvid, …), **the Answerline wordmark and the author's portrait:** these are Answerline's marketing content and identity, not UI. Using them would make Slate look like a copy of the template.
- **Perplexity, Google and Copilot marks:** Slate doesn't use those providers.
- **`clsN31ER….png`:** actually a 710-byte AVIF favicon.

**Not in the zip:** logos for our connectors (GitHub, Discord, Gmail, and Slack, Notion, Google Drive and Confluence for the "Coming later" tiles), and Geist Mono. Get the logos from each brand's official press or brand kit (§2.6). Geist Mono comes from `next/font/google`.

---

## 1. Product feel

Calm, exact and trustworthy. The interface should feel like a well-made instrument, not a chatbot toy.

- **Quiet surfaces, loud evidence.** Almost everything is white and grey with hairline borders. Colour is reserved for things that matter: the accent for actions and citations, semantic colours for status, brand colours for sources.
- **Every claim is checkable.** Citations are the most designed element in the product. A user can get from any sentence to its exact passage in one click.
- **Honest states.** We show what the system is actually doing ("Searching 4 sources", "Queued, the worker usually starts within a minute"). We never fake progress and never hide failure.
- **Dense, not cramped.** App text is 14 px with generous line height. Rows are 40 to 56 px. Whitespace separates groups, and hairlines separate items.
- **Keyboard-first, mouse-friendly.** Every action can be reached by keyboard. The common ones have shortcuts.
- **Motion explains, never decorates.** Movement shows where something came from or that work is happening. All of it respects `prefers-reduced-motion`.

What "not vibe-coded" means here, concretely: no gradient text, no glassmorphism, no emoji in UI copy, no "Oops!", no spinners where a skeleton fits, no layout shift when data arrives, no default browser focus rings, no untruncated overflow, and no states left undesigned (see §11).

---

## 2. Foundations

### 2.1 Colour tokens

All colours are CSS custom properties in `web/src/styles/tokens.css`, mapped onto shadcn/ui's variable names (§2.8) so the primitives pick them up. **Never use a raw hex in a component.** The token column notes which Answerline token each value comes from (first 8 hex characters of its id).

**Neutrals** (warm stone since ADR-0017; the Answerline column records where each role came from, not its value)

| Token | Hex | Answerline | Use |
| --- | --- | --- | --- |
| `--bg` | `#FFFFFF` | `02ea51c7` | Page canvas, cards, inputs, menus |
| `--bg-subtle` | `#FAFAF9` | `e58286b7` | Row hover, question bar, featured surfaces, code blocks |
| `--bg-muted` | `#F5F5F4` | `4c9e3a74` | Selected row, kbd, skeleton base, sidebar active item |
| `--bg-emphasis` | `#E7E5E4` | `a25c931b` | Neutral pill fill, secondary button hover, avatar fill |
| `--border` | `#E7E5E4` | `6ff1d9b2` | Default hairline: cards, panels, dividers, icon-button ring |
| `--border-subtle` | `#F5F5F4` | `5f2cf5ca` | Dividers inside tables and lists |
| `--border-strong` | `#D6D3D1` | `af844af9` | Secondary button border, outline pills, dropzone |
| `--border-input` | `#8F8A85` | *new* | Input, checkbox, radio and select borders (3.4:1 on white, needed for WCAG 1.4.11) |
| `--fg` | `#1C1917` | `fc6ea28d` | Headings, primary text, titles, tooltips' background |
| `--fg-body` | `#292524` | `aef431cf` | Long-form text: answers, previews, descriptions |
| `--fg-muted` | `#57534E` | `17df2dac` | Secondary text, table cells, labels on `--bg-muted` |
| `--fg-subtle` | `#78716C` | `4e2d5370` | Meta text, timestamps, placeholders, captions. **Only on `--bg` or `--bg-subtle`** (4.4:1 on `--bg-muted`, which fails) |
| `--fg-disabled` | `#A8A29E` | `2c04ed3c` | Disabled text and decorative marks only (2.5:1, never for readable text) |

**Accent (blue)**

| Token | Hex | Answerline | Use |
| --- | --- | --- | --- |
| `--accent` | `#3050F0` | `5a57e92e` | Primary buttons, citation badges, links, focus ring, active indicators, progress |
| `--accent-strong` | `#2A45D6` | `0412aeed` | Accent text on tinted backgrounds (chips, tags) |
| `--accent-subtle` | `#EEF2FE` | `4dccf669` | Citation chip background, cited-passage highlight, selected filter chip, drag-over |
| `--accent-border` | `#C9D2FF` | `f6b2275f` | Tag borders, accent-tinted card borders |
| `--accent-fg` | `#FFFFFF` | `13fec117` | Text and icons on `--accent` |

**Semantic** (new; Answerline has none. Same recipe as the accent: tinted fill, darker text, soft border)

| Role | Text / icon | Fill | Border | Solid (buttons) |
| --- | --- | --- | --- | --- |
| Success | `#13734B` | `#E6F4EC` | `#BFE3CF` | — |
| Warning | `#8A5100` | `#FDF3E2` | `#F5DCAE` | — |
| Danger | `#B4231B` | `#FDECEA` | `#F6C9C4` | `#D1342B` (white text, 5.0:1) |
| Info | use accent tokens | | | |

**Special**

| Token | Value | Use |
| --- | --- | --- |
| `--mark` | `#FDF0C4` | Search term highlight (`<mark>`). Warm on purpose, so it never looks like a citation |
| `--overlay` | `rgb(13 15 18 / 0.40)` | Dialog and sheet backdrop. No blur |
| `--hover-wash` | `rgb(255 255 255 / 0.14)` | Overlay that wipes in on hover over accent and solid buttons (Answerline technique) |

**Contrast (checked, ADR-0017 values):** `--fg-muted` on white 7.6:1, `--fg-subtle` 4.8:1 on white and 4.6:1 on `--bg-subtle` (4.4:1 on `--bg-muted`, so never there), `--border-input` 3.4:1, white on `--accent` 5.95:1, `--accent` on `--accent-subtle` 5.3:1, every semantic text on its fill ≥ 5.2:1.

### 2.2 Typography

**Families** (both SIL OFL, `display: swap`, self-hosted with `next/font/local` from `web/src/fonts/`, the same Latin-subset files Seekworthy ships):

- **Inter**, variable weight 100–900 with italic, `--font-sans` (ADR-0017; replaces Inter Tight).
- **JetBrains Mono**, variable weight, `--font-mono` (replaces Geist Mono). For code, file paths, IDs, counters, request references and kbd.

Always use `tabular-nums` for numbers in tables, counts, times and badges.

**Scale (ADR-0017).** Each step is named for its job and is a Tailwind utility (`text-caption`, `text-page`…), defined in `globals.css`. Line height is a flat 1.5 except where noted. Nothing is bolder than 600.

| Role | Size | Weight | Where |
| --- | --- | --- | --- |
| `label` | 10 | 600, uppercase, `tracking-wider` | Panel eyebrows (`PanelLabel`), KPI labels, table headers, "Soon" tags |
| `data` | 11 | 400 / 500 | Dense meta, pills, table sub-lines, KPI captions |
| `caption` | 12 | 400 / 500 | **Default for controls:** buttons, inputs, nav items, table cells, form labels |
| `body` | 13 | 400 / 500 | Body text (the `<body>` default), row titles, descriptions |
| `ui` | 14 | 600 | Section and empty-state headings, large buttons |
| `dialog` | 15 | 600 | Dialog titles |
| `page` | 20 / 1.4 | 600, −0.02em | The one page title, in the page bar |
| `metric` | 24 / 1.2 | 600, `tracking-tight` | KPI figures |
| `display` | 36 / 1.2 (26 phone, 30 tablet) | 600, −0.025em | Auth headline only |
| `prose` | 15 / 26 | 400 | Answers and document previews (M3) |

Rules: sentence case everywhere (including buttons and titles). Never use weight 700. Body text is 13 px; 12 px is for controls, and 10–11 px is for labels and meta only. Prose measure is 60–75 characters (max width 720 px). Inputs are 16 px on touch devices (`pointer: coarse`) to stop iOS zooming in.

### 2.3 Spacing and layout

- 4 px base, using Tailwind's default scale. Common steps: 4, 6, 8, 10, 12, 16, 20, 24, 32, 40, 56. Answerline uses exactly these.
- **Component padding:** buttons 12–20 px horizontal; pills 4×10; tags 3×9; cards and dialogs 24; table cells 12×16; page gutters 32 desktop, 24 tablet, 16 phone.
- **Content widths:** chat column 720 px max; auth card 440 px; auth bar content 1120 px max. App pages are full-bleed (panel grammar, §4.8).
- **Grid lines.** Answerline frames its pages with hairline vertical "rails" and small crosshair marks where lines meet. We use rails on the sign-in page only. Inside the app, structure comes from hairline borders between regions (sidebar | main | panel), never from shadows.

### 2.4 Radius

| Token | Value | Use |
| --- | --- | --- |
| `--radius-xs` / `--radius-sm` | 4 px | Citation badge, kbd, pills, nav items |
| `--radius-md` | 6 px | Buttons, inputs, icon buttons, menu items |
| `--radius-lg` | 8 px | Auth card, popovers, toasts, dropzone, status list |
| `--radius-xl` | 12 px | Dialogs |
| `--radius-full` | 9999 px | Avatars, dots, progress bars, switches |

Nested radii: inner radius = outer radius − padding (for example, the button inside the 12 px composer with 6 px padding is 6 px).

### 2.5 Elevation

Answerline is flat. We keep it that way: **borders define surfaces, and shadows appear only on things that float.**

| Token | Value | Use |
| --- | --- | --- |
| `--shadow-none` | none | Everything in the page flow |
| `--shadow-sm` | `0 1px 2px -1px rgb(0 0 0 / .08), 0 1px 1px rgb(0 0 0 / .03)` | Inputs on the sign-in panel, the composer |
| `--shadow-md` | `0 0.6px 1.6px -1.5px rgb(0 0 0 / .17), 0 2.3px 6px -3px rgb(0 0 0 / .14), 0 10px 26px -4.5px rgb(0 0 0 / .06)` | Popovers, menus, toasts, hover cards (Answerline's composite shadow) |
| `--shadow-lg` | `--shadow-md` + `0 24px 48px -12px rgb(0 0 0 / .12)` | Dialogs, sheets, the command menu |

Z-index scale: `sidebar 20`, `sticky 30`, `overlay 40`, `dialog 50`, `popover 60`, `toast 70`, `tooltip 80`.

### 2.6 Iconography

- **UI icons:** `lucide-react` (shadcn's default), stroke 1.6 to match Answerline's icons (set once in the icon wrapper, not per use), `currentColor`. Sizes: 14 in pills and small buttons, 16 default, 20 in empty states and tiles. Icons that sit next to text are optically centred and use the text's colour.
- **Brand logos:** official artwork in **full colour, always** (owner decision). Stored as React SVG components in `web/src/components/brand-icons/`, one file per brand. Each file has a header comment with the official source URL and a trademark note. Don't recolour, outline, rotate, add effects or stretch them, as every brand's guidelines require. A logo shown at 20 px or larger sits in a **logo tile**: 1 px `--border`, `--bg`, `--radius-md`, logo at 55% of the tile size. Mixed shapes then line up in a list.

| Connector | Logo | Notes |
| --- | --- | --- |
| Upload | Lucide `upload` on an `--accent-subtle` tile, `--accent-strong` icon | Not a brand, so it uses our accent |
| GitHub | Invertocat mark, `#181717` | Black is GitHub's official colour on light backgrounds |
| Discord | Clyde mark, `#5865F2` | |
| Gmail | Official multicolour "M" | Label it "Gmail (Takeout import)" so no one expects a live connection |
| Slack, Notion, Google Drive, Confluence | Official marks | "Coming later" tiles only |

- A README "Credits" section lists Inter, Geist Mono, Lucide, Thinking Orbs, Beautiful UI and the brand trademark notice.

### 2.7 Motion

Values come from Answerline's Framer config: a spring with bounce 0.2 and duration 0.4 s, hover colour transitions of 0.2 s on `cubic-bezier(.44,0,.56,1)`, and reveal tweens on `cubic-bezier(.25,.1,.35,1)`.

| Token | Value | Use |
| --- | --- | --- |
| `--dur-instant` | 80 ms | Press feedback |
| `--dur-fast` | 120 ms | Tooltip and menu exit, checkbox tick |
| `--dur-base` | 200 ms | Hover colour and border changes, hover wash |
| `--dur-slow` | 300 ms | Panel and sheet slide, accordion |
| `--ease-standard` | `cubic-bezier(.44, 0, .56, 1)` | Colour transitions |
| `--ease-out` | `cubic-bezier(.25, .1, .35, 1)` | Things entering |
| `--ease-in` | `cubic-bezier(.4, 0, 1, 1)` | Things leaving (exits are always faster than entrances) |
| `spring` | `{ type: "spring", bounce: 0.2, duration: 0.4 }` | Dialogs, the sidebar's gliding highlight, the sources strip, sign-in card expansion |

**Signature micro-interactions** (use these consistently, add no others without updating this list):

1. **Hover wash** on primary and solid buttons: a `--hover-wash` layer wipes in top to bottom over `--dur-base` (a masked gradient, as in Answerline). Secondary buttons change fill to `--bg-emphasis`.
2. **Press:** `scale(0.98)` over `--dur-instant` on every button and clickable tile.
3. **Gliding highlight** in the sidebar and in tab bars: one `--bg-muted` pill slides between items on hover and focus with `spring`, instead of each item fading its own background (Beautiful UI's sidebar).
4. **Pulse dot** for live things (a sync running, a stream in progress): a 6 px `--accent` dot with a 4 px `--accent-subtle` ring, and a halo that scales 1→2.4 while fading out, every 1.6 s (Answerline's feed header).
5. **Stagger in:** when a set of items arrives at once (sources strip, search results), items fade in and rise 4 px, 30 ms apart, capped at 8 staggered items.
6. **Streaming text:** each new token chunk fades from opacity 0 to 1 over 120 ms. A 2 px `--fg` caret blinks at the end while streaming. No per-character animation.
7. **Arrow nudge:** links and buttons ending in `→` move the arrow 2 px right on hover.
8. **Copy confirm:** the copy icon morphs to a check for 1.5 s. The label is announced as "Copied".

9. **Illustration loops** (auth "How it works" only, §4.1): built from `lib/motion.ts` (`useCycle`, `useTypewriter`, `useCountUp`, `useInView`) and CSS transitions; entrances use `REVEAL` + `delay()` from `lib/reveal.ts`. Loops pause on hover/focus, offscreen and in hidden tabs.

**Reduced motion** (`prefers-reduced-motion: reduce`): illustration loops render their finished frame; remove every transform and spring, keep opacity fades at 150 ms or less, make the caret and pulse dot static, stop skeleton pulsing, and render orbs as a static frame (the library does this itself). Nothing may rely on motion to communicate.

### 2.8 Implementation mapping (Tailwind v4 + shadcn/ui)

Tokens live in `web/src/styles/tokens.css` and are exposed to Tailwind with `@theme inline`. **In code, every Slate token is prefixed `--sl-`** (`--sl-border`, `--sl-accent`, …), because several of our names collide with shadcn's (`--border`, `--accent`) and a variable can't refer to itself. This document uses the short names. shadcn variables map onto ours:

```css
:root {
  --background: var(--sl-bg);           --foreground: var(--sl-fg);
  --card: var(--sl-bg);                 --card-foreground: var(--sl-fg);
  --popover: var(--sl-bg);              --popover-foreground: var(--sl-fg);
  --primary: var(--sl-accent);          --primary-foreground: var(--sl-accent-fg);
  --secondary: var(--sl-bg);            --secondary-foreground: var(--sl-fg);
  --muted: var(--sl-bg-muted);          --muted-foreground: var(--sl-fg-subtle);
  --accent: var(--sl-bg-muted);         --accent-foreground: var(--sl-fg);   /* shadcn's "accent" = hover fill */
  --destructive: #D1342B;
  --border: var(--sl-border);           --input: var(--sl-border-input);
  --ring: var(--sl-accent);
  --radius: 8px;
}
```

shadcn's `--accent` means "hover fill", not brand colour. Our brand blue is `--primary` in shadcn terms. In Tailwind utilities our blue is exposed as `brand` (`bg-brand`, `text-brand-strong`) for the same reason.

---

## 3. Components

Every component lists its states. **A component isn't done until every listed state is built and has a Vitest test or a story.**

### 3.1 Buttons

| Size | Height | Padding x | Text | Icon | Use |
| --- | --- | --- | --- | --- | --- |
| `sm` | 32 | 12 | 13/500 | 14 | Table rows, toolbars, toasts |
| `md` (default) | 36 | 14 | 14/500 | 16 | Most actions |
| `lg` | 44 | 20 | 15/500 | 16 | Sign-in, empty states, dialog primaries on phone |

| Variant | Rest | Hover | Notes |
| --- | --- | --- | --- |
| `primary` | `--accent` fill, white text | Hover wash | One per view. The main forward action |
| `secondary` | `--bg`, 1 px `--border-strong`, `--fg` text | `--bg-emphasis` fill | The default for everything else |
| `ghost` | Transparent, `--fg-muted` | `--bg-muted` fill, `--fg` | Toolbars, menu triggers, low-emphasis actions |
| `tinted` | `--accent-subtle`, `--accent-strong` text | Fill darkens 4% | The sign-in "Continue" button inside the input bar (from the Trakkr screenshot) |
| `danger` | `#D1342B`, white | Hover wash | Only inside confirm dialogs |
| `link` | `--accent`, no padding | Underline (1 px, offset 3 px) | Inline actions in text |

- **Icon button:** square 32 / 36, `--radius-md`, transparent with an inset 1 px `--border` ring (Answerline's theme toggle), hover `--bg-muted`. **It must have an `aria-label` and a tooltip.**
- **States:** focus-visible ring (§9); disabled at opacity 0.5 with no hover and `cursor: not-allowed`; **loading** keeps the width locked, swaps the leading icon (or adds one) for a 14 px spinner, sets `aria-busy`, and ignores repeat clicks. Never replace the label with "Loading…".
- Labels are verbs: "Sync now", "Connect GitHub", "Delete source". Never "OK", "Submit" or "Yes".

### 3.2 Form controls

- **Text input:** height 36 (`lg` 44), `--radius-md`, 1 px `--border-input`, `--bg`, 14 px text, `--fg-subtle` placeholder, padding 12. Hover: border `--fg-subtle`. Focus: border `--accent` plus a 3 px `--accent-subtle` halo (the halo is decoration; the border is the 3:1 indicator). Error: border danger, halo `#FDECEA`, message below.
- **Field anatomy:** label (14/500, `--fg`) → 6 px → control → 6 px → hint (13, `--fg-subtle`) **or** error (13, danger, with a 14 px `circle-alert` icon). Errors replace the hint and never push layout: reserve the line when a field can fail. Use `aria-invalid` and `aria-describedby`. "Optional" goes after the label in `--fg-subtle`. Don't mark required fields with asterisks; most fields are required.
- **Secret input** (tokens): masked, a show/hide icon button inside, `autocomplete="off"`, `spellcheck=false`. After saving, show only `ghp_••••••••3f9a` and a "Replace" button. **The real value is never displayed again.**
- **Textarea:** same as input, auto-grows from 1 to 8 lines, then scrolls.
- **Select / combobox:** shadcn Select and Command. 32 px option rows, `--radius-sm`, check icon on the right for the selected item, type-ahead.
- **Checkbox** 16 px, `--radius-xs`, `--border-input`; checked fills `--accent` with a white check. **Radio** 16 px circle. **Switch** 34×20, track `--border-strong` off and `--accent` on, 16 px white thumb, `spring`.
- **Search field:** a leading 16 px `search` icon, a trailing kbd hint (`/` or `Ctrl K`) that becomes a clear (×) button once there's text.
- **Dropzone:** `--radius-lg`, 1.5 px dashed `--border-strong`, `--bg-subtle`, padding 32, centred icon, "Drop files here or **browse**", and a list of allowed types and the size limit in `small`. Drag-over: border `--accent`, fill `--accent-subtle`, icon nudges up 2 px. Invalid drag (wrong type, sniffed on drop): border danger for 1 s, then an inline error.

### 3.3 Pills, tags, badges, chips

| Element | Spec | Example |
| --- | --- | --- |
| **Status pill** | Padding 4×10, `--radius-sm`, 13/500, optional 14 px leading icon or 6 px dot | Variants below |
| **Tag** | Padding 3×9, `--radius-sm`, 1 px `--accent-border`, `--accent-strong` text, 12/500 | "Most chosen" style. Used for "Later", "Owner" |
| **Count badge** | Height 18, min-width 18, padding 0×5, `--radius-full`, `--bg-emphasis`, 12/500 tabular | "Sources 5" |
| **Citation badge** | See §3.10 | |
| **Filter chip** | Height 28, padding 0×10, `--radius-full`, 1 px `--border-strong`. Active: `--accent-subtle` fill, `--accent-border`, `--accent-strong` text, trailing × | Search filters |

Status pill variants (Answerline's "Cited / Fix queued / Missing" set, extended):

| Variant | Look | Used for |
| --- | --- | --- |
| `accent` | `--accent-subtle` fill, `--accent-strong` text | Syncing (with a 12 px spinner), Running |
| `success` | Success fill and text, `check` icon | Synced, Succeeded |
| `neutral` | `--bg-emphasis` fill, `--fg` text | Queued, Scheduled |
| `warning` | Warning fill and text | Synced with errors, Retrying |
| `danger` | Danger fill and text, `circle-alert` icon | Failed, Dead |
| `outline` | Transparent, 1 px `--border-strong`, `--fg-muted` text | Never synced, Paused, Disabled |

Pills always carry text. Colour is never the only signal.

### 3.4 Overlays

- **Tooltip:** `--fg` fill, white 12/500 text, `--radius-sm`, padding 4×8, no arrow, 6 px offset. Opens after 400 ms (instantly once another tooltip is open), closes immediately. Shows the shortcut in a kbd when there is one. Never put essential information only in a tooltip.
- **Popover / dropdown menu:** `--bg`, 1 px `--border`, `--radius-lg`, `--shadow-md`, padding 4. Items are 32 px, `--radius-sm`, padding 0×8, 14 px text, 16 px icon, hover and keyboard highlight `--bg-muted`, shortcut right-aligned in `--fg-subtle`. Destructive items sit last, after a separator, in danger text. Enter: opacity plus `scale(.96→1)` plus 4 px slide from the trigger side over 160 ms `--ease-out`. Exit 120 ms.
- **Hover card** (citation preview): a popover at 360 px width that opens after 300 ms hover or on focus, and stays open while the pointer is inside.
- **Dialog:** width 480 (forms) or 560 (connector setup), `--radius-lg`, padding 24, `--shadow-lg`, `--overlay` backdrop. Header: `h3` title plus a 14 px `--fg-muted` description and a close icon button. Footer: right-aligned, secondary then primary; on phone the buttons stack full width with the primary on top. Enter uses `spring` (opacity plus `scale(.97→1)` plus 8 px rise); exit is 150 ms opacity only. Esc and the close button close it; **the backdrop doesn't close dialogs that hold unsaved input.** Focus is trapped and returns to the trigger.
- **Confirm (destructive):** title "Delete *GitHub: slate*?", body stating exactly what is lost ("1,284 documents and their search index will be removed. This can't be undone after 7 days."), and an input "Type the source name to confirm" that enables the danger button.
- **Sheet:** a right-side panel, 480 px (full width on phone), `--dur-slow` slide. Used for document preview on narrow screens and for multi-step connector setup on phone.

### 3.5 Toasts

shadcn's Sonner wrapper, styled to our tokens.

- **Placement:** bottom right on desktop (24 px inset); top centre on phone, below the safe area. At most 3 visible; older ones stack behind, offset 8 px and scaled 0.96.
- **Anatomy:** width 360, `--bg`, 1 px `--border`, `--radius-lg`, `--shadow-md`, padding 12×14. A 16 px status icon (success check, danger alert, accent info, or spinner for promise toasts), a 14/500 title, an optional 13 `--fg-subtle` description, an optional `sm` secondary action, and a close button that appears on hover or focus.
- **Duration:** 4 s for success and info, 6 s with an action, **errors stay until dismissed**. Hovering or focusing pauses the timer.
- **Announcements:** `aria-live="polite"`; errors use `assertive`.
- **When to use:** confirming an action the user just took that has no other visible result ("Sync queued", "Link copied"), and background failures. **Don't** toast form validation errors (inline), page-load failures (inline error state), or anything the user must act on later (use a banner).
- **Copy:** past tense for done ("Source deleted"), present progressive for promises ("Uploading 3 files…" → "3 files uploaded"). Undo is offered where it's real (soft delete): "Source deleted. **Undo**".

### 3.6 Banners and inline alerts

- **Banner:** full width at the top of the main region, under the page header, 40 px tall, 13/500 text with a 16 px icon, an optional link action on the right, and optional dismissal (remembered for the session). Variants: `info` (accent tint), `warning`, `danger`. Used for budget at 80% or 100%, being offline and maintenance.
- **Inline alert (callout):** inside content, `--radius-md`, 1 px semantic border, semantic fill, padding 12×14, a 16 px icon, title 14/500, body 13. Used for a source's last sync failing, a connector setup error and the insufficient-evidence reply.

### 3.7 Lists, rows and tables

Built on Answerline's "live feed" table and Beautiful UI's records table and task rows.

- **Table:** no outer border inside a page (the page is the container); inside cards, the card's border holds it. The header row is 36 px, 12/500 `--fg-subtle`, sentence case, 1 px `--border-subtle` underneath, and sticky when the table scrolls. Body rows are 48 px (56 with a two-line cell), with 1 px `--border-subtle` dividers and cells padded 12×16. Numeric columns are right-aligned and tabular. Hover: `--bg-subtle`. Selected: `--bg-muted` plus a 2 px `--accent` bar on the left edge. A whole row that is a link uses a real `<a>` on the primary cell, stretched over the row, so middle-click and open-in-new-tab work.
- **Truncation:** single-line cells end with an ellipsis, and the full value shows in a tooltip (and in `title` for screen magnifiers). Titles may wrap to 2 lines and then clamp.
- **Relative time:** "14 min ago", "Yesterday, 16:20", "Oct 3" (adding the year if it isn't the current one). A tooltip shows the absolute local time with the timezone. Wrap it in `<time dateTime>`.
- **Task rows** (sync progress, §6.4): a 24 px number or status icon column, title 14/500, sub-line 13 `--fg-subtle` ("1,204 chunks"), and right-aligned status. The running step shows a spinner and its sub-line updates. Finished steps show a success check, and failed ones a danger icon with the error on the sub-line.
- **Phone:** tables become stacked rows: the primary text on top, meta joined with " · " below, and the status pill top right (Answerline's phone feed variant).

### 3.8 Feedback: loading, progress, empty

- **Skeletons** for any content that takes more than 300 ms and whose shape is known: `--bg-muted` blocks, `--radius-sm`, sized like the real content (no layout shift on arrival), with an opacity pulse of 1 → 0.6 over 1.2 s. Show them only after a 150 ms delay so fast loads don't flash.
- **Spinner** (14/16 px, 1.5 px ring, `--accent`, 0.8 s linear) for indeterminate waits on a specific control: button loading, syncing pills.
- **Progress bar:** 4 px, `--radius-full`, `--bg-emphasis` track, `--accent` fill, animated width. Used for uploads (determinate) and budget usage.
- **Orbs** are **only** for model work (retrieval and generation). They never stand in for page loading or syncs. See §5.
- **Empty states:** centred in their region, max width 400. A 40 px icon tile (1 px `--border`, `--radius-md`, 20 px `--fg-muted` icon), a 16/500 title, 14 `--fg-subtle` body (one or two sentences: why it's empty and what to do), and at most one primary action plus an optional link. No illustrations.

### 3.9 Navigation

**Sidebar** (Beautiful UI pattern):

- 200 px expanded, 60 px collapsed (icons only, labelled), toggled with the collapse button (`Ctrl/⌘ B` is planned). The state persists in `localStorage`. `--bg` background with a 1 px `--border` on the right (ADR-0017, §4.2).
- **Top:** the workspace block (24 px logo tile plus workspace name 14/500). There's a single workspace, so it isn't a switcher and has no chevron. Below it, a **New chat** button (`secondary`, full width, `square-pen` icon, kbd hint `Ctrl/⌘ Shift O`).
- **Primary nav** (items 32 px, `--radius-md`, 16 px icon, 14/500 label, the gliding highlight from §2.7): Chat, Search, Sources, Settings. Settings shows only for owners and admins. The active item has a `--bg` fill, a 1 px `--border` and `--fg` text. Inactive items use `--fg-muted`.
- **Recent chats:** a section label in `caption` style ("Today", "Yesterday", "Previous 7 days", "Older"), then items 32 px, single line with ellipsis. The current chat is highlighted. The list loads 30, then more on scroll (cursor pagination). Rename and delete wait until the API supports them; don't show a ⋯ menu that does nothing.
- **Bottom:** a user button (24 px avatar, name, and role in `caption`) that opens a menu: role shown, Keyboard shortcuts, Sign out.
- **Phone (< 768 px):** the sidebar becomes a left drawer (280 px, `--overlay` backdrop) opened from a menu icon in a 56 px top bar.

**Page header** (inside main): height 56, padding 0×32, `h2` title on the left, page actions on the right, 1 px `--border` underneath once the page scrolls (not at rest).

**Command menu** (`Ctrl/⌘ K`, shadcn Command): 560 px wide, positioned 20 vh from the top, `--shadow-lg`. A 48 px input, then groups: "Jump to" (pages), "Recent chats", "Documents" (live search results with source logos, debounced 200 ms, ACL-filtered by the API like everything else), "Actions" (New chat, Add source, Sync all). Arrow keys move, Enter runs, Esc closes. Empty: "No matches for "x"". Footer hints: `↑↓ navigate`, `↵ open`, `esc close`.

### 3.10 Citations (the signature component)

This adapts Answerline's "What do clients say" answer block.

- **Inline citation badge** `[n]` in answer text: a `<button>`, 16×16 minimum (expands for two digits), `--radius-xs`, `--accent` fill, white 11/600 tabular numerals, sitting 1 px above the baseline, with 2 px margin either side. Adjacent citations `[1][2]` sit 2 px apart. Hover or focus opens the **citation hover card**; click opens the sources panel at that source and highlights the passage. Accessible name: "Source 2: *Document title*".
- **Hover card:** a header with the logo (16 px), document title 14/500 (one line), and location in 12 `--fg-subtle` (repo › path, #channel, mail subject, file name); then the passage, 13/20 `--fg-body`, up to 5 lines, with a 2 px `--accent` left bar and `--accent-subtle` fill; then a footer with "Updated Oct 3" and "Open source ↗". The external link goes to the original (GitHub, Discord, and so on) through a re-checked URL from the API.
- **Source chip** (the strip under the question, and the "Sources" row under the answer): height 28, padding 3 / 10 / 3 / 3, `--radius-sm`, `--accent-subtle` fill, a 22 px number badge (`--accent`, `--radius-xs`, white 12/600), a 9 px gap, then the name in 13/500 `--accent-strong`, truncated at 180 px. Hover: fill darkens 4%. Click: the same as clicking the inline badge.
- **Integrity rules (from the architecture):** render only citation numbers the server validated. Unknown `[n]` text renders as plain text, never as a badge. Previews and links always go through the API's ACL-checked endpoints. The UI never builds a source URL on its own.

### 3.11 Code and rich text in answers

- Answers render as **Markdown without raw HTML**. Allowed: paragraphs, lists, bold and italic, inline code, fenced code, tables and links. Links get `rel="noopener noreferrer nofollow"` and `target="_blank"`, and only `http(s)` URLs become links; anything else renders as text. (Model output is untrusted; see `SECURITY.md`.)
- **Code block:** `--bg-subtle`, 1 px `--border`, `--radius-md`. A 36 px header with the language or file path (12 `--fg-subtle` mono) and a copy icon button. Body: mono 13/20, padding 12×16, horizontal scroll (no wrap), max height 400 px, then vertical scroll. Syntax highlighting is optional (see dependencies, §12).
- **Inline code:** mono 0.92em, `--bg-muted`, `--radius-xs`, padding 1×4.

### 3.12 Avatars and identity

- 24 px (lists, sidebar) and 32 px (settings). Circle, `--bg-emphasis` fill, initials in 12/600 `--fg-muted` (one or two letters).
- **Product name:** **Slate**, in UI copy, the page `<title>` ("Search · Slate"), metadata and the favicon.
- **Logo (owner-supplied, ADR-0017):** the Slate mark, traced by hand from the owner's PNG into one SVG path (`LogoMark`, 524 × 432 viewBox, even-odd fill, `currentColor`). `<Logo />` puts it in white at 62% width on a solid `--accent` tile (`--radius-md`, `--shadow-xs`, 24 px in the app, 28 px on the auth bar) beside the "Slate" wordmark in `ui` 600, −0.02em. The favicon (`app/icon.svg`) is the same tile.

---

## 4. Screens

Build order follows `ROADMAP.md`. Each screen lists layout, states and behaviour.

### 4.1 Sign in, Sign up, Verify, Reset (M1; open sign-up since ADR-0021; Seekworthy hero since ADR-0017)

All four screens share one frame (`web/src/app/(auth)/auth-frame.tsx`), modelled on Seekworthy's onboarding hero. **Anyone can sign up** (ADR-0021): each account gets its own private workspace, and is never asked to name it. There is no guest and no setup wizard.

```text
┌────────────────────────────────────────────────────────────────────┐
│ [S] Slate                                     [GitHub] Source on GitHub ↗ │  white bar, 56/64 px
│ ╭────────────────────────────────────────────────────────────────╮ │
│ │            (blue gradient + film grain, radius 24/28)          │ │
│ │                  Ask your knowledge.                           │ │  white 32/42/50, 600
│ │                  Get answers you can check.                    │ │
│ │      Search documents, code, chat and email, with every …      │ │  white 15/16
│ │        ╭──────────────────────────────────────────────╮        │ │
│ │        │ ✉  you@company.com              [Continue →] │        │ │  pill bar, rounded-2xl
│ │        ╰──────────────────────────────────────────────╯        │ │
│ │   ✓ Cited answers  ✓ Permission-aware  ✓ Your own workspace    │ │  white 13
│ │                 No account? Create one free →                  │ │
│ │     ┌──────────────────────────────────────────────────────┐   │ │
│ │     │ How it works          Built on free-tier infrastructure│   │ │  overlaps the bottom edge
│ │     │ 1 Connect sources │ 2 Ask a question │ 3 Check the answer│ │ │
╰─────┴──────────────────────────────────────────────────────┴─────╯
```

- **One screen, always (owner requirement).** The frame is exactly the viewport tall (`h-svh`, no page scroll at 1366×768, 1440×900, 1536×864, 1920×1080 or a phone). Spacing scales with height via `clamp()`; spare height is split 2:3 above and below the hero, so the form never floats at the top of a tall screen. The subtitle hides below 680 px tall and the card below 640 px; the form never hides. The card is ~250 px tall and runs off the panel's bottom edge, as in Seekworthy.
- **Panel (`GrainPanel`):** inset 12 px (20 from `sm`), radius 24 (28). Background: an 8% white radial wash top-left over `linear-gradient(180deg, --hero-deep 0%, --hero-mid 55%, --hero-light 100%)` (`#3050F0` → `#4260F1` → `#8396F6`). **Grain:** Seekworthy's `noise.png` (an opaque mid-grey tile) at 160 px with `mix-blend-overlay`, in two layers: the base at `--grain-base` 0.45, and a second, offset copy at `--grain-boost` 0.4 masked to fade out from top to bottom. Overlay grain is ~2.5× weaker on dark blue than on light, so the second layer makes the texture read evenly from top to bottom. Mid-grey averages to no change under overlay, so the average colour and contrast are unaffected. **Contrast:** white on `--hero-mid` is 5.05:1 and on `--hero-deep` 5.95:1; no white text may sit below the 55% stop.
- **Entrance:** headline, then subtitle (+80 ms), then form (+160 ms), each fading up 8 px; the card slides up 24 px (+320 ms). Off under reduced motion.
- **Sign-in bar:** a white `rounded-2xl` pill with a white ring and a deep blue shadow; on focus the ring widens to 4 px of white/45. Inside: a 20 px `mail` icon, a 16 px input, and Continue. **Continue is tinted (`--accent-subtle`) until the email looks valid, then solid `--accent` with a glow** (`data-state="idle|ready"`). It still submits while tinted, so an invalid email gets a message, never a dead button. On phones its label is visually hidden and the arrow remains.
- **Two steps, one `<form>`:** after Continue the pill grows into a white card (fade + scale from 0.98): the email read-only with "Change", the password (autofocused) with "Forgot password?" beside its label, and a full-width "Sign in". An unverified account goes to Verify after signing in. The email input stays mounted (`autocomplete="username"`), and the password uses `current-password`. A failed login always says "Email or password is incorrect." (`SECURITY.md`).
- **Below the bar:** an email-format error replaces the trust row as a white pill (`role=alert`). Otherwise the trust row reads "Cited answers · Permission-aware · Your own private workspace". Then "No account? **Create one free →**" (to Sign up).
- **Session expired** (`?reason=expired`) or **password changed** (`?reason=reset`): a white pill above the bar.
- **Sign up: steps in the pill bar** (owner decision). The same bar as sign-in shows one question at a time: What's your name? → Your email → Create a password → Enter the setup code (only when the server says the address is `OWNER_EMAIL`'s). Above the bar, "Step N of M" in mono; below it, the live password rules (on the password step), else the step's hint, else its question, all in white; then a Back link and progress dots (the current one stretched). Enter continues. Each step is checked locally (`stepProblem`) before moving on, and Continue is tinted until the step is valid. It's one `<form>`: answered inputs stay mounted (visually hidden, `tabIndex=-1`, `aria-hidden`) with their `autocomplete`, so autofill and password managers work. A server field error jumps back to the step that owns it, with the message as a white pill. "Already have an account? Sign in" sits in the top bar (`AuthFrame aside`). **Bot defences:** the Cloudflare Turnstile widget (`components/turnstile.tsx`, interaction-only, so most people never see it) mounts with the form so the check is done by the last step, and a honeypot `website` field sits off-screen (`aria-hidden`, `tabIndex=-1`). When sign-up is closed, the form is replaced by "Sign-ups are closed right now. Sign in".
- **Verify** (`/verify`): "Check your inbox." The pill bar takes the 6-digit code (`inputMode=numeric`, `autocomplete=one-time-code`, mono with wide tracking); "Sent to *email*" above, "The code expires 15 minutes after it was sent." below. Then "Send a new code" (needs a fresh bot check) and "Use a different account" (signs out). The app shell sends any unverified account here.
- **Forgot password** (`/forgot-password`): a white card asks for the email (prefilled from sign-in), then for the code and a new password with the live rules. The request step always says "If *email* has an account, we sent it a code", so it can't be used to find accounts. Success goes to Sign in with `?reason=reset`.

**How it works** (`(auth)/how-it-works/`): a white card, `rounded-t-2xl`, max 1060 px, with an upward shadow and a 48 px header ("How it works" / "Built on free-tier infrastructure"). Three columns. Each has a real `h3`, a screen-reader-only sentence and an `aria-hidden` illustration of fixed height (148 px; no layout shift). The columns tell **one story on one clock** (`useClock`, 18 s loop, 50 ms ticks; every beat is a constant in `how-it-works/timeline.ts`): sources are connected → someone asks with @ → Slate thinks → a cited answer streams in.

1. **Connect anything:** an infinite marquee of integration logos in two rows scrolling in opposite directions (40 s and 48 s), edges faded with a mask. Each track holds its list twice and slides by −50%, so the wrap has no seam. Hovering pauses it. Under it, the tagline **"One search box for {tool}"**: the tool name rolls up every 1.5 s in `--accent` (hovering a logo locks it to that tool), then "N tools and counting". The logos are every Simple Icons connector that's plausible for Slate (owner's choice: no Live/Soon labels). Not shown: Slack and Microsoft (not in Simple Icons, never approximated), GitBook and Intercom (too pale on white).
2. **Ask with @:** a mini composer (placeholder "Ask anything", paperclip, mic, an Enter key in a tinted square). The question types itself out, then "@" opens a source menu: Handbook.pdf · #hr-policies · **Exec notes 🔒 "No access"** (greyed). A pretend pointer glides to Handbook.pdf, highlights and clicks it, and a mono source chip drops in. The Enter key turns solid and pulses, then the question becomes a sent message bubble. Below it, the **thinking orb** (`ThinkingOrb` 20, `searching`) with "Thinking…", and steps appearing one by one along a left rule: Reading Handbook.pdf · Checking who can see it · Ranking passages *24 found* · Writing the answer. When done, the header becomes "Thought for 3 seconds ⌃".
3. **Get a cited answer:** three overlapping source avatars with "3 sources" slide in, the answer streams word by word with a block caret ("New parents get **16 weeks** of paid leave…"), then an inline mono source chip pops in with a slight overshoot, followed by the actions row (copy, retry, 👍, 👎) and a follow-up ("↳ How do I request it?"). **Hovering the chip** opens a preview of the cited passage (Handbook.pdf · p. 4) with the `.sl-sweep` highlight.

**Phones (< md):** one column at a time. The header shows 1 · 2 · 3 tabs (`aria-pressed`) that follow the story; tapping one jumps the clock to that column's scene (under reduced motion, it just switches column).

Loop rules (`lib/motion.ts`): the whole card pauses while hovered or focused, while offscreen and in a hidden tab. Under reduced motion every column shows its finished frame. No animation library is used; the orb is the one package (§5).

### 4.2 App shell

`sidebar (200, collapsible to 60) | main (fluid) | [panel (400), only on Chat and Search]`, split by hairlines, with the page on white.

- **Sidebar** (`components/app-shell.tsx`): a 56 px header with the logo, whose hairline lines up with the page bar. Then "New chat", a divider, and the nav: Home, Chat, Search, Sources. Items are `caption` with 15 px icons and radius-sm; the active item has an `--accent-subtle` fill and `--accent` text. The footer holds System status, the account (20 px initials avatar, name, role, sign-out icon button) and the collapse toggle. Collapsed, it's a 60 px icon rail labelled with `title` and `aria-label`. The state is kept in `localStorage` (`slate.sidebar.collapsed`) and read with `useSyncExternalStore`, so the server renders it expanded and hydration never mismatches.
- **Below `lg` (1024 px):** a 56 px top bar with a menu button opens the same sidebar as a 260 px drawer over an `--overlay` backdrop. Escape or a backdrop click closes it.
- **Page bar** (`PageHeader`): at least 56 px tall, full-bleed, with a hairline below. Title in `page`, an optional one-line `caption` description, and actions on the right.

### 4.3 Chat (M3)

**Empty (new chat):**

- Vertically centred column, 640 px wide. A 64 px orb in `breathing` (the assistant's idle presence), then `h1` "What do you want to know?", then 14 `--fg-subtle` "Searching 4 sources · 1,284 documents you can access" (from the API; each account sees its own numbers).
- The composer (below), then 3–4 **suggested questions** as `secondary` `sm` buttons that wrap. Suggestions come from the workspace's own documents; an empty workspace suggests uploading or loading sample data instead.

**Composer** (Beautiful UI prompt bar):

- Sticky at the bottom of the chat column, 16 px from the bottom edge (plus the safe-area inset), with a gradient fade from `--bg` above it so text doesn't collide with it. Container: `--bg`, 1 px `--border-strong`, `--radius-lg`, `--shadow-sm`, padding 12; focus-within: border `--accent`.
- Inside: an auto-growing textarea (15 px, 1–8 lines), placeholder "Ask anything about your sources…". A bottom row: on the left, filter chips ("All sources ▾" multi-select with logos, and "Any time ▾"); on the right, a hint ("↵ to send, ⇧↵ for a new line", `caption`, hidden on phone) and the **send button** (32 px, `--accent`, `--radius-md`, `arrow-up` icon). It's disabled while the input is empty.
- While streaming, the send button becomes a **Stop** button (`square` icon, `secondary`). Stop cancels the SSE request and keeps the partial answer with a "Stopped" note.
- Length limit: 4,000 characters. A counter appears at 3,600 ("3,612 / 4,000") and turns danger at the limit. Sending is blocked over the limit.
- Keyboard: Enter sends, Shift+Enter adds a new line, `/` focuses the composer from anywhere in Chat, ↑ in an empty composer recalls the last question for editing.

**Conversation:**

- Each turn is a **question bar** followed by an **answer block** (Answerline's "Prompt bar" plus "Answer").
- **Question bar:** full column width, `--bg-subtle`, 1 px `--border` top and bottom, padding 14×16, the question in 15/500 `--fg`. On the right, once sources arrive: "5 sources" (13 `--fg-subtle`, with a `layers` icon), which opens the panel.
- **Source strip:** directly under the question bar, a horizontal scroll row of source chips (§3.10) that stagger in when the `sources` event arrives.
- **Answer:** `prose`, max 720, 16 px below the strip. Inline citation badges.
- **Answer footer** (after `done`): `ghost` `sm` icon buttons for Copy (copies the answer with citations turned into footnotes: `[1] Title — URL`), thumbs up and thumbs down. Down opens a small popover with reasons ("Wrong", "Missing information", "Bad sources", "Other") and an optional comment. Submitting shows "Thanks, feedback saved" as a toast. To the right, 12 `--fg-subtle` meta: "4.2 s · gpt-x-mini". Not shown: token or cost numbers (those belong in Settings).
- **Scrolling:** auto-scroll follows the stream only while the user is at the bottom. If they scroll up, it stops, and a floating "↓ Jump to latest" pill appears above the composer.

**Answer lifecycle, mapped to the SSE contract** (`ARCHITECTURE.md` §8; no invented events):

| Moment | What the user sees |
| --- | --- |
| Sent, before `meta` | The question bar appears immediately (optimistic). Under it: a 20 px orb (`searching`) and "Searching your sources…" |
| `meta` received | No visual change. The URL updates to the session ID (history `replace`) |
| `sources` received | The strip staggers in. The orb switches to `connecting` and the label to "Reading 5 sources…" |
| First `token` | The orb and label fade out over 150 ms; text streams in with the caret. The answer region has `aria-busy="true"` |
| `citation` | The matching `[n]` in the text becomes a live badge (until then it renders as a muted placeholder badge, so the text doesn't reflow) |
| `done` | The caret disappears, the footer fades in, `aria-busy` clears, and screen readers hear "Answer ready" |
| `error` mid-stream | Partial text stays. An inline alert (danger) below it: "The answer stopped before it finished." with a **Retry** button and "Ref: req_8f2c…" (mono, copyable) |
| No first token after 20 s | The label changes to "Still working… this is taking longer than usual." No timeout UI before the server's own timeout |

The stage label is in an `aria-live="polite"` region; it announces stage changes, never individual tokens.

**Insufficient evidence** (a designed outcome, not an error):

- An inline alert in the **neutral** style (`--bg-subtle`, `--border`, `circle-help` icon): title "Not enough evidence to answer this", body "I couldn't find passages that answer this question in the sources available to you."
- Then "Closest matches" with up to 3 compact source rows (logo, title, location), each opening the preview.
- Then suggestions as `ghost` buttons: "Rephrase", "Search instead" (opens Search with the query), and "Clear filters" if filters are active.
- **The wording is the same for every user.** It must never hint that private documents exist that the user can't see.

**Sources panel** (right, 400 px; a sheet below 1280 px):

- Header: "Sources" plus a count badge and a close icon button. Below it, the list of sources for the selected answer: number badge, logo, title (2 lines), location, relative date, and a 3-line snippet with the cited passage highlighted.
- Clicking a source switches the panel to the **document preview**: back arrow, title, location and an "Open in GitHub ↗" style link. Then the document text in `prose` (code in a code block), with every cited passage highlighted (`--accent-subtle` fill, 2 px `--accent` left bar) and the selected one scrolled into view with a brief pulse of the bar.
- Loading shows a preview skeleton. A 404 or 403 from the API shows the empty state "This document isn't available" (same wording for both, so access isn't leaked).

### 4.4 Search (M2)

- **Header:** `h2` "Search" and a large search field (height 44, 16 px text, autofocused, `/` focuses it from anywhere on the page). The query is kept in the URL (`?q=&source=&date=&type=`), so results are shareable and back/forward works.
- **Filter row:** filter chips for Source (multi-select with logos and per-source counts), Date (Any time, Past week, Past month, Past year, Custom range), Type (Documents, Code, Issues and PRs, Messages, Email). "Clear all" appears when any filter is active.
- **Result meta:** "24 results · 180 ms" in 13 `--fg-subtle`.
- **Results list** (split view on ≥ 1280 px: list 1fr, preview 480 px):
  - Row: a 16 px logo and location line (12 `--fg-subtle`: "slate › core › services › search.py"), a title (15/500 `--fg`, 1 line), a 2-line snippet (14 `--fg-body`) with query terms in `<mark>` (`--mark`), and on the right a relative date and author (12 `--fg-subtle`). Padding 12×16, `--radius-md`, hover `--bg-subtle`, selected `--bg-muted` plus the left accent bar.
  - Keyboard: ↑/↓ move the selection (and update the preview), Enter opens the preview on narrow screens, `Ctrl/⌘ Enter` opens the original, Esc returns focus to the field.
  - Pagination: "Show more results" button (cursor-based), not infinite scroll.
- **States:**
  - Before searching: "Recent searches" (last 5, stored locally per user, with a clear button) and "Try" example queries.
  - Loading: 6 skeleton rows after 150 ms. Previous results stay visible at 60% opacity while a new query loads (no flash to empty).
  - No results: empty state "No results for "query"", body "Try fewer words, a different spelling, or removing filters.", and a "Clear filters" action if any are set.
  - Error: an inline alert with Retry and the reference ID.

### 4.5 Sources (M1 skeleton → M4–M6 full)

**List page:**

- Header: `h2` "Sources", plus a live indicator when anything is syncing (pulse dot and "Syncing 1 source"), and a `primary` "Add source" button (owner and admin only).
- **Table** (Answerline feed style): Source (logo tile, name 14/500, location 12 `--fg-subtle`, such as "aashu/slate" or "Demo server · 6 channels"), Visibility (`outline` pill with an icon: `lock` Only me / `users` Workspace / `globe` Public), Status (pill, §3.3), Documents (right, tabular), Last synced (relative time), and a row ⋯ menu (Sync now, Edit, Pause or Resume, separator, Delete).
- "Sync now" sets the pill to `neutral` "Queued" immediately, with the toast "Sync queued. The worker usually starts within a minute." Then it becomes `accent` "Syncing" once the run starts. (The worker is GitHub Actions; it is honestly not instant.)
- **Viewers** see the table read-only, without the Add button, row menus or credentials.
- **Empty:** icon `plug`, "No sources yet", "Connect GitHub, Discord or Gmail, or upload files, to start asking questions.", with the action "Add source".

**Add source dialog** (560 px; a sheet on phone): title "Add a source".

- **Available** grid (2 columns, 12 px gap): connector tiles. A tile is a card (1 px `--border`, `--radius-lg`, padding 16) containing a 40 px logo tile, name 15/500, a one-line description 13 `--fg-subtle`, and capability tags in 12 `--fg-subtle` joined by " · ". The whole tile is a button. Hover: border `--border-strong` and `--bg-subtle` fill. Focus ring. Press scale.
  - Upload files: "PDF, Word, Markdown, HTML, CSV and text, up to 25 MB each"
  - GitHub: "Code, docs, issues and pull requests"
  - Discord: "Channels and threads from a server"
  - Gmail: "Import a Google Takeout export (.mbox). Always private"
- **Coming later** section: a `caption` label, then the same tile layout for Slack, Notion, Google Drive and Confluence, **not interactive**: rendered as list items (not buttons), the logo in full colour, name and description in `--fg-subtle`, a "Later" tag in the top-right corner, and no hover or press effect. Screen readers hear "Slack, planned for a later release". There's no waitlist or email capture.

**Connector setup** (inside the same dialog; a step indicator in the header "Step 2 of 4"; Back and Continue in the footer; state kept when going back):

- **Upload:** a dropzone, then a visibility radio ("Only me" default, "Everyone in the workspace"). After dropping, a file list where each row shows the type icon, name, size and status:
  - "Uploading 42%" (determinate progress)
  - "Queued", "Processing" (spinner)
  - "Ready" (success check)
  - "Already indexed" (neutral, for the checksum no-op, so a duplicate isn't presented as an error)
  - Failures, inline with a reason: "File type not supported", "Larger than 25 MB", "Couldn't read this PDF" (with a Retry button where retryable).
  Files are validated in the browser (type and size) before upload, and again on the server, which is the authority.
- **GitHub:** 1) Access: a fine-grained personal access token (secret input), a "Create a token ↗" link, and the required permissions listed in a small checklist; "Check access" calls `validate` and shows "Connected as @user · 12 repositories" (success) or the specific failure. 2) Repositories: a searchable checklist with visibility tags. 3) Content: switches for Code, Docs and READMEs, Issues, Pull requests. 4) Review: a summary, then "Connect and start sync".
- **Discord:** 1) Bot: "Add the bot to your server ↗", the bot token (secret input), Check. 2) Channels: a checklist grouped by category, with notes on which channels the bot can't read. 3) Visibility. 4) Review.
- **Gmail:** 1) A short Takeout guide (3 numbered steps with a "Open Google Takeout ↗" link, and the note "Select only Mail, in .mbox format"). 2) A dropzone for `.mbox`. 3) Visibility is fixed at "Only me" (a `lock` icon and the line "Email is always private to you"). No choice is offered.

**Source detail page:**

- Header: a 40 px logo tile, the name as `h2`, the location, a status pill, a `secondary` "Sync now" and a ⋯ menu. An inline danger alert appears under the header if the last run failed, with a human summary and a "View errors" link.
- **Stat row:** four stats with a 12 `caption` label and a 22/500 tabular value: Documents, Chunks, Last synced, Next sync ("in 38 min", hourly).
- **Tabs** (gliding underline indicator, `spring`): Runs, Errors, Settings.
  - **Runs:** table with Started, Trigger (Manual, Scheduled, Webhook), Duration, Seen / Changed / Failed (tabular), Status. An active run sits at the top as an expanded **task-row card**: "1 Fetching items · 140 found", "2 Parsing · 38 of 140", "3 Embedding · 1,204 chunks", "4 Saving", each with a spinner, check or error.
  - **Errors:** table with Item (title or ID, mono), Stage (pill: fetch, parse, chunk, embed, store), Message, "Retryable" tag, and time. Empty: "No errors in recent runs."
  - **Settings:** name, visibility, content options, credential ("Replace token") and a **Danger zone** card at the bottom (1 px danger border) with "Delete source" (§3.4 confirm).
- **Live updates and the Neon budget:** poll run status every 5 s **only** while a run is queued or running **and** the tab is visible. Back off to 15 s after 2 minutes, and stop when the run ends. Never poll in the background or on other pages.

### 4.6 Settings (M7; owner and admin)

- Left sub-nav (Members, Usage). Content max width 800.
- **Members:** a table (avatar, name, email, role select, joined). Owners can change roles; the select is disabled for oneself with the tooltip "You can't change your own role". Inviting users waits until there's an API for it; don't add a dead button.
- **Usage (budget):** a card with the month-to-date spend shown big ("$1.84 of $5.00"), a progress bar (turns warning at 80% and danger at 100%), and "Resets Dec 1". Below it, a small daily-spend bar chart for the month (follow the `dataviz` guidance when building it), then a breakdown table: Provider (16 px mark: OpenAI and Gemini from the Answerline export; Groq and Cloudflare from their brand kits), Model, Kind (embed, chat, rewrite), Tokens in/out, Cost. At 80% and above, the matching banner from §3.6 shows app-wide for the owner.

### 4.7 System pages

| Page | Content |
| --- | --- |
| 404 | `h1` "Page not found", body "The page may have moved, or the link is wrong.", button "Go to Chat" |
| Error (500 / error boundary) | `h1` "Something went wrong", body "This is on our side. Try again, and if it keeps happening, share this reference:", the request ID in a mono copyable field, buttons "Try again" and "Go to Chat" |
| Offline | A banner (warning): "You're offline. Changes will resume when you reconnect." Sending is disabled with the tooltip "You're offline" |
| Forbidden action | Not a page. Hide controls the role can't use. If the API still returns 403, toast "You don't have permission to do that." |

---

### 4.8 Home (M1, ADR-0017)

The landing page after sign-in (`/home`), and the reference implementation of the **panel grammar** (`components/panel.tsx`): full-bleed cells, never floating cards; 1 px hairlines drawn by rows (bottom) and cells (right); `PanelLabel` eyebrows; `tabular-nums` figures. Search and Chat must be built the same way.

- **Page bar:** "Home", with "Welcome back, {first name}" , and an "Add source" `primary` button for members who can add. It links to `/sources?add=1`, which opens the Add source dialog; closing the dialog clears the parameter.
- **KPI strip** (`PanelRow cols={4}` of `Kpi`; 2×2 on mobile): Sources ("N need attention" when any failed), Documents, Processing (a live pulse dot while above zero) and Last sync (hover for the absolute time). Every figure comes from `GET /api/v1/sources`. There is **no extra endpoint**, and polling follows the Sources rule: only while something is queued or syncing, and only while the tab is visible.
- **Row 2:** "Recent sources" (2/3 width) lists the five most recently synced, each with logo tile, name, document count, time and status pill, plus "View all ›". Empty: a dashed `--bg-subtle` call to action, "Add your first source" with "Upload files" (or "Nothing shared with you yet" for viewers). "Get started" (1/3) has three steps: Connect a source (ticked once one exists), Search everything (M2) and Ask with citations (M3), the last two tagged "Soon".
- **States:** skeletons in place of each figure and row while loading; an inline danger alert with Retry if the list fails.

## 5. Thinking orbs

- Package: `thinking-orbs` (MIT, zero dependencies, 2D canvas, SSR-safe, pauses offscreen and in hidden tabs, renders a static frame under reduced motion). Wrap it once in `web/src/components/thinking-orb.tsx` (a client component), so the rest of the app never imports the package directly and it can be swapped.
- Always `theme="light"` (we ship light only).
- **Only two sizes exist** in the package (they're separately tuned presets): `64` and `20`. Don't scale them with CSS.

| Where | Size | State | Meaning |
| --- | --- | --- | --- |
| Chat empty state | 64 | `breathing` | The assistant is ready (idle presence) |
| After sending, before `sources` | 20 | `searching` | Retrieving |
| After `sources`, before the first token | 20 | `connecting` | Linking passages into an answer |
| Sign-in "How it works", column 2, while thinking | 20 | `searching` | Decorative (`decorative` prop), `aria-hidden`, paused with the card |

- Orbs are **never** used for page loads, syncs, uploads or button loading. Those use skeletons, spinners and progress bars. This keeps the orb meaning "the AI is working".
- Accessibility: each orb has an `aria-label` ("Searching your sources") unless it's decorative. The adjacent text label carries the meaning for screen readers through the live region.

---

## 6. Content and voice

- **Plain, specific, calm.** Say what happened and what to do next. Use the user's words (source names, file names), not ours (no "chunk", "embedding", "principal" or "generation" in the UI; "passage" is the user-facing word for a chunk).
- **Sentence case** everywhere. No exclamation marks, no emoji, no "Oops", "Uh-oh" or "Whoops". Don't blame the user ("invalid input" → "Enter an email like name@company.com").
- **Numbers:** thousands separators ("1,284"), units with a space ("25 MB"), durations "4.2 s" and "3 min", money "$1.84".
- **Buttons:** verb + noun ("Connect GitHub", "Delete source"). Destructive confirms repeat the verb ("Delete source", not "Confirm").

**Error messages by domain error** (`core/domain/errors.py` → problem `type`):

| Error | Message | Where |
| --- | --- | --- |
| `ValidationFailed` | The field-specific message from the API's problem details, under the field | Inline |
| `NotFound` / `PermissionDenied` on documents | "This document isn't available. It may have been removed, or you may not have access." (identical for both) | Panel or page empty state |
| `PermissionDenied` (action) | "You don't have permission to do that." | Toast |
| `Conflict` | "This was changed by someone else. Reload to see the latest version." + Reload | Inline alert |
| `RateLimited` | "You're sending questions quickly. Try again in {n} s." (countdown from `Retry-After`) | Toast or composer hint |
| `UpstreamUnavailable` | "The answer service isn't responding. Try again in a moment." + Retry | Inline alert |
| `BudgetExceeded` | "This month's AI budget is used up. Search still works; answers resume on {date}." | Banner + composer disabled |
| `ConnectorError` | Connector-specific and actionable, e.g. "GitHub rejected the token. Check that it hasn't expired and can read the selected repositories." | Setup step or source alert |
| Network failure | "Couldn't reach the server. Check your connection and try again." | Inline alert or toast |
| Unexpected (500) | "Something went wrong on our side. Reference: {request_id}" | Inline alert or error page |

Never show stack traces, raw problem `type` URIs, SQL or provider error text to users. Always show the request ID for 5xx errors.

---

## 7. Keyboard shortcuts

Platform-aware labels (⌘ on macOS, Ctrl elsewhere). The `?` key opens a shortcuts dialog listing all of them.

| Keys | Action | Scope |
| --- | --- | --- |
| `Ctrl/⌘ K` | Command menu | Global |
| `Ctrl/⌘ Shift O` | New chat | Global |
| `Ctrl/⌘ B` | Toggle sidebar | Global |
| `/` | Focus the composer or search field | Chat, Search |
| `Enter` / `Shift Enter` | Send / new line | Composer |
| `↑` | Edit last question (empty composer) | Composer |
| `↑` `↓` `Enter` | Move / open | Lists, results, menus |
| `Ctrl/⌘ Enter` | Open the original source | Search result |
| `Esc` | Close the panel, dialog or menu; stop editing | Global |
| `?` | Keyboard shortcuts | Global (not while typing) |

Single-key shortcuts never fire while focus is in an input.

---

## 8. Responsive behaviour

| Width | Layout |
| --- | --- |
| ≥ 1280 | Sidebar + main + panel side by side |
| 1024–1279 | Sidebar + main. The sources panel and search preview open as a right sheet |
| 768–1023 | Collapsed sidebar (56 px icons) + main. Sheets as above |
| < 768 | Top bar with a menu button (sidebar drawer). Single column. The composer is fixed to the bottom with the safe-area inset. Dialogs become full-width bottom sheets. Tables become stacked rows |

- Minimum supported width: 320 px with no horizontal page scroll.
- Touch (`pointer: coarse`): interactive targets at least 44×44 (hit area may extend past the visual), inputs at 16 px, hover-only affordances (tooltips, hover cards, row menus) get a tap equivalent. A citation tap opens the panel instead of the hover card.

---

## 9. Accessibility (WCAG 2.2 AA)

- **Contrast:** use only the token pairs checked in §2.1. `--fg-disabled` is never used for readable text. `--fg-subtle` is never placed on `--bg-muted`.
- **Focus:** every interactive element shows `outline: 2px solid var(--accent); outline-offset: 2px` on `:focus-visible` (inputs use their border plus halo instead). Never remove focus styles without a replacement. Focus is never hidden under sticky bars (2.4.11): add `scroll-padding` for the sticky header and the composer.
- **Keyboard:** everything is reachable and operable. Dialogs trap focus and restore it. A skip link "Skip to content" is the first focusable element. Roving tabindex in lists and menus (Radix provides this).
- **Semantics:** one `h1` per page (visually it may be `h2`-styled), landmarks (`nav`, `main`, `aside` for the panel), real `<button>` and `<a>`, labelled icon buttons, table headers with `scope`.
- **Live regions:** stream stage changes and "Answer ready" (polite), toasts (polite, errors assertive), search result counts (polite, after typing settles).
- **Target size:** at least 24×24 everywhere (2.5.8), at least 44×44 on touch.
- **Reduced motion:** §2.7. **Zoom:** layouts work at 200% zoom and with text spacing overrides. **Colour is never the only signal:** pills have text, errors have icons and text, cited passages have a bar as well as a fill.
- **Testing:** `@axe-core/playwright` in the e2e smoke flows (zero violations), plus a manual keyboard-only pass per screen.

---

## 10. Implementation notes

- **File layout:** `web/src/styles/tokens.css` (tokens), `web/src/styles/globals.css` (base, `@theme inline`), `web/src/components/ui/` (shadcn primitives restyled to this spec), `web/src/components/brand-icons/`, `web/src/components/` (product components: `citation-badge`, `source-chip`, `composer`, `question-bar`, `thinking-orb`, `status-pill`, `task-row`, `connector-tile`, …), `web/src/app/` (routes).
- **Restyle, don't fork:** start from shadcn primitives and change classes to match this document. Keep behaviour (Radix) intact.
- **Variants** through `class-variance-authority` (shadcn's default), with names matching this document (`variant="tinted"`, `size="sm"`).
- **Fonts:** Inter and JetBrains Mono through `next/font/local` (files in `web/src/fonts/`, see `OFL.txt`; ADR-0017), exposing `--font-sans` and `--font-mono`.
- **No raw hex values, pixel radii or ad-hoc shadows in components.** Lint for it if it becomes a problem.
- **Data and streaming:** TanStack Query for requests. The chat stream uses a small SSE client that parses the versioned event schema with zod and feeds a reducer (`idle → searching → reading → streaming → done | error | insufficient`). The UI states in §4.3 are that reducer's states, so they're unit-testable.

---

## 11. Polish checklist (definition of done for any UI work)

- [ ] Every state is designed and built: loading (skeleton after 150 ms), empty, error (with retry and reference), partial, success, disabled, and the viewer and unverified-account versions.
- [ ] No layout shift when data, fonts or images load. Skeletons match the real dimensions.
- [ ] Long content handled: 200-character titles, long file paths, 4-digit counts, empty strings, non-Latin names.
- [ ] Keyboard-only pass done. Focus order is logical, focus is visible, Esc works.
- [ ] Reduced motion checked. Nothing depends on animation.
- [ ] 320 px, 768 px and 1280 px checked. No horizontal scroll.
- [ ] Copy follows §6: sentence case, specific, no jargon, verbs on buttons.
- [ ] Only tokens used, with no stray hex, radius or shadow values.
- [ ] Numbers are tabular and formatted. Times are relative with an absolute tooltip.
- [ ] Hover, focus, active and disabled exist for every interactive element.
- [ ] axe reports zero violations. Icon buttons have labels and tooltips.
- [ ] Nothing leaks access: wording identical for "not found" and "no access", and no counts of hidden content.

---

## 12. Dependencies this design needs (approved 2026-11-24)

The owner approved this list. Each package is added only in the milestone that first needs it, with the reason in the commit body (`CONVENTIONS.md`). Anything not on this list still needs approval.

| Package | License | Why | Milestone | Alternative without it |
| --- | --- | --- | --- | --- |
| `thinking-orbs` | MIT | Thinking indicator (§5) | M1 (sign-in illustration; pulled forward, ADR-0017) | A CSS three-dot pulse (weaker) |
| `sonner` (via shadcn) | MIT | Toasts (§3.5) | M1 | — (shadcn's recommended toast) |
| `cmdk` (via shadcn Command) | MIT | Command menu, comboboxes | M2 | Plain listbox |
| `motion` | MIT | Springs, gliding highlight, layout animation of the sources strip | M3 | CSS transitions plus a measured indicator (more code, no springs) |
| `react-markdown` + `remark-gfm` | MIT | Answer rendering without raw HTML (§3.11) | M3 | A hand-rolled renderer (risky) |
| `shiki` | MIT | Syntax highlighting in code answers and previews | M4 (optional) | Unhighlighted mono blocks |
| `@axe-core/playwright` | MPL-2.0 | Accessibility checks in e2e | M1 | Manual checks only |

`lucide-react`, `class-variance-authority`, `tw-animate-css` and the Radix primitives arrive with `shadcn init` in M0.

---

## Appendix A. Answerline token map

Kept so values can be traced back. Light values as shipped in this design.

| Answerline token (prefix) | Light | Our token |
| --- | --- | --- |
| `02ea51c7` | `#FFFFFF` | `--bg` |
| `e58286b7` | `#FAFAFB` | `--bg-subtle` |
| `4c9e3a74` | `#F1F2F4` | `--bg-muted` |
| `a25c931b` | `#EEF0F2` | `--bg-emphasis` |
| `6ff1d9b2` | `#E4E6EA` | `--border` |
| `5f2cf5ca` | `#EEF0F2` | `--border-subtle` |
| `af844af9` | `#D5D8DD` | `--border-strong` |
| `fc6ea28d` | `#0D0F12` | `--fg` |
| `aef431cf` | `#2A2D31` | `--fg-body` |
| `17df2dac` | `#4A4E55` | `--fg-muted` |
| `4e2d5370` | `#6B7078` | `--fg-subtle` |
| `2c04ed3c` | `#A0A5AD` | `--fg-disabled` |
| `5a57e92e` / `1a7eaff3` | `#3050F0` | `--accent` |
| `0412aeed` | `#2440D8` | `--accent-strong` |
| `4dccf669` | `#E8ECFF` | `--accent-subtle` |
| `f6b2275f` | `#C9D2FF` | `--accent-border` |
| `13fec117` | `#FFFFFF` | `--accent-fg` |

## Appendix B. Dark values (not shipped)

Recorded for a future dark theme so nobody has to re-extract them. Answerline's dark set: `--bg #0A0B0D`, `--bg-subtle #0E1013`, `--bg-muted #121417`, `--bg-emphasis #22252A`, `--border #24272C`, `--border-subtle #1C1F23`, `--border-strong #2E3238`, `--fg #EDEFF2`, `--fg-body #D9DCE1`, `--fg-muted #B4B8BF`, `--fg-subtle #9EA3AB`, `--fg-disabled #6B7078`, `--accent #4362FF` (text-on-dark accent `#8EA0FF`), `--accent-strong #A3B2FF`, `--accent-subtle #1A2150`, `--accent-border #2F3A80`. A dark theme would also need semantic colours, an input-border check and new contrast checks, plus an ADR.
