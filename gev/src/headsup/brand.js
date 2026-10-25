/**
 * The name this console wears on screen.
 *
 * The viewer in `gev/` is a fork of God's Eye View and says so in
 * `gev/NOTICE`, `gev/LICENSE` and the root README. The MIT licence requires
 * that attribution and none of it moves: what changes here is the product
 * name in the chrome, because the tool is Signum and a viewer reads a name
 * in two places — the title bar and the cover shown while the world loads.
 *
 * Upstream writes its own name into both from static markup
 * (`src/ui/templates/scene-chrome.html` and `hud-loading.html`), so neither
 * template is edited: this module rewrites the two headings at runtime and
 * `?mode=full` never calls it, which leaves upstream's own appearance
 * byte-for-byte what upstream built and a `git subtree pull` a merge of
 * upstream's files.
 *
 * Deliberately not part of `markHeadsupMode`, which is contracted to apply
 * the CSS hook and nothing else — `mode.test.mjs` passes it a document stub
 * with no `querySelector` precisely to hold that line.
 * @module headsup/brand
 */

/** What the tab, and anything reading `document.title`, calls this. */
export const PRODUCT_NAME = 'Signum';

/** The wordmark, split so upstream's `.title-accent` keeps its two tones. */
export const WORDMARK_HEAD = 'SIG';
export const WORDMARK_TAIL = 'NUM';

/**
 * Write the wordmark into one heading, replacing whatever upstream put there.
 *
 * Replacing the children rather than the text is what takes the gaze-tracking
 * logo out of the title bar: it is a child of the same heading, and an eye
 * that follows the cursor is the fork's motif rather than this console's.
 * @param {object} doc Document the heading belongs to.
 * @param {object|null|undefined} host Heading to rewrite.
 * @returns {boolean} Whether a heading was there to rewrite.
 */
function setWordmark(doc, host) {
  if (!host) return false;
  const accent = doc.createElement('span');
  accent.className = 'title-accent';
  accent.textContent = WORDMARK_TAIL;
  host.replaceChildren(doc.createTextNode(WORDMARK_HEAD), accent);
  // A proper noun: no locale has a different spelling of it.
  host.setAttribute('translate', 'no');
  return true;
}

/**
 * Put the product name on the chrome, and take the fork's name off it.
 *
 * Every lookup is guarded: this runs before the scene is built, against
 * markup expanded at build time, and a console that threw here would take
 * the whole application down over a heading.
 * @param {object} doc The live document.
 * @returns {{title: boolean, loader: boolean, logos: number}} What was found.
 */
export function applyHeadsupBrand(doc) {
  const found = { title: false, loader: false, logos: 0 };
  if (!doc) return found;
  try {
    doc.title = PRODUCT_NAME;
  } catch {
    /* A document that will not take a title is still worth branding. */
  }
  const bar = doc.getElementById?.('title-bar') ?? null;
  const cover = doc.getElementById?.('loading-screen') ?? null;
  // The cover's logo is a sibling of its heading, so replacing the heading
  // does not reach it. Taken off first: every selector below runs over markup
  // nobody has rewritten yet, which keeps the order of these three
  // independent of what each one leaves behind.
  for (const logo of cover?.querySelectorAll('.brand-logo') ?? []) {
    logo.remove();
    found.logos += 1;
  }
  found.title = setWordmark(doc, bar?.querySelector('h1'));
  found.loader = setWordmark(doc, cover?.querySelector('h2'));
  return found;
}
