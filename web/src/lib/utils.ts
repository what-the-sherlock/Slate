import { createCn } from "cn/config";

/**
 * Class merging that knows our theme. Without this, `cn` reads custom sizes such as `text-ui`
 * or `text-caption` as text *colours*, so `cn("text-white", "text-ui")` silently dropped the
 * white (primary buttons showed dark labels) and pills lost their size. The role type scale
 * (globals.css @theme, UI_DESIGN §2.2) is registered as font sizes here. Import `cn` from
 * this file, never from the "cn" package directly (enforced by ESLint).
 */
export const cn = createCn({
  extend: {
    classGroups: {
      "font-size": [{ text: ["label", "data", "caption", "body", "ui", "dialog", "page", "metric", "display"] }],
    },
  },
});
