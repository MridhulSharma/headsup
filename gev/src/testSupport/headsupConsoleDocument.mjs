/**
 * A document just large enough to mount the Heads Up console into.
 *
 * The console's whole job is to add its surfaces to chrome upstream already
 * built, so a test needs that chrome present and needs to be able to read
 * back what landed in it. This fixture supplies the four upstream hosts —
 * `#data-panel`, `#scene-panel`, `#style-indicator` and the HUD's bottom
 * right corner — plus the slice of the DOM the console actually uses:
 * creation, append, class and attribute reads, a descendant selector, and
 * click and keydown dispatch.
 *
 * Deliberately NOT a general DOM: anything the console does not call throws
 * or is absent, so a future change that reaches for more of the platform
 * shows up here as a failure rather than passing against a fake that quietly
 * accepts everything.
 * @module testSupport/headsupConsoleDocument
 */

/** Match one simple compound selector (`tag`, `.class`, `#id`) to a node. */
function matchesSimple(node, selector) {
  if (selector.startsWith('#')) return node.id === selector.slice(1);
  if (selector.startsWith('.'))
    return node.classList.contains(selector.slice(1));
  return node.tagName === selector.toUpperCase();
}

/** Match a descendant selector (`#intel-hud .hud-bottom-right`). */
function matches(node, selector) {
  const parts = selector.trim().split(/\s+/);
  if (!matchesSimple(node, parts.at(-1))) return false;
  let ancestor = node.parent;
  for (const part of parts.slice(0, -1).reverse()) {
    while (ancestor && !matchesSimple(ancestor, part))
      ancestor = ancestor.parent;
    if (!ancestor) return false;
    ancestor = ancestor.parent;
  }
  return true;
}

function createNode(doc, tag) {
  const listeners = new Map();
  const classes = new Set();
  const attributes = new Map();
  const node = {
    tagName: String(tag).toUpperCase(),
    ownerDocument: doc,
    children: [],
    parent: null,
    dataset: {},
    style: {
      properties: new Map(),
      setProperty(name, value) {
        this.properties.set(name, value);
      },
    },
    hidden: false,
    textContent: '',
    title: '',
    type: '',
    id: '',
    classList: {
      add: (...names) => names.forEach((name) => classes.add(name)),
      remove: (...names) => names.forEach((name) => classes.delete(name)),
      contains: (name) => classes.has(name),
      toggle: (name, force) => {
        const next = force ?? !classes.has(name);
        if (next) classes.add(name);
        else classes.delete(name);
        return next;
      },
    },
    get className() {
      return [...classes].join(' ');
    },
    set className(value) {
      classes.clear();
      for (const name of String(value).split(/\s+/).filter(Boolean))
        classes.add(name);
    },
    get isConnected() {
      let current = node;
      while (current.parent) current = current.parent;
      return current === doc.documentElement || current === doc.body;
    },
    setAttribute(name, value) {
      attributes.set(name, String(value));
    },
    getAttribute(name) {
      return attributes.get(name) ?? null;
    },
    removeAttribute(name) {
      attributes.delete(name);
    },
    append(...nodes) {
      for (const child of nodes) {
        child.parent?.children.splice(child.parent.children.indexOf(child), 1);
        child.parent = node;
        node.children.push(child);
      }
    },
    replaceChildren(...nodes) {
      for (const child of node.children) child.parent = null;
      node.children = [];
      node.append(...nodes);
    },
    remove() {
      if (!node.parent) return;
      node.parent.children.splice(node.parent.children.indexOf(node), 1);
      node.parent = null;
    },
    addEventListener(type, listener) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(listener);
    },
    removeEventListener(type, listener) {
      listeners.get(type)?.delete(listener);
    },
    dispatch(type, event = {}) {
      const payload = {
        type,
        target: node,
        defaultPrevented: false,
        preventDefault() {
          this.defaultPrevented = true;
        },
        stopPropagation() {},
        ...event,
      };
      for (const listener of [...(listeners.get(type) ?? [])])
        listener(payload);
      return payload;
    },
    click() {
      return node.dispatch('click');
    },
    focus() {
      doc.activeElement = node;
    },
    descendants() {
      return node.children.flatMap((child) => [child, ...child.descendants()]);
    },
    querySelector(selector) {
      return (
        node.descendants().find((child) => matches(child, selector)) ?? null
      );
    },
    querySelectorAll(selector) {
      return node.descendants().filter((child) => matches(child, selector));
    },
  };
  return node;
}

/**
 * Build the fixture.
 * @returns {{document: object, body: object, hudContent: object,
 *   dataToggles: object, scenePanel: object, styleIndicator: object}}
 */
export function consoleTestDocument() {
  const doc = {
    activeElement: null,
    createElement: (tag) => createNode(doc, tag),
    createTextNode: (text) => ({ tagName: '#TEXT', textContent: text }),
    getElementById: (id) =>
      doc.documentElement.descendants().find((node) => node.id === id) ?? null,
    querySelector: (selector) => doc.documentElement.querySelector(selector),
    listeners: new Map(),
    addEventListener(type, listener) {
      if (!doc.listeners.has(type)) doc.listeners.set(type, new Set());
      doc.listeners.get(type).add(listener);
    },
    removeEventListener(type, listener) {
      doc.listeners.get(type)?.delete(listener);
    },
    /** Dispatch a document-level event, as a key press reaches the console. */
    dispatch(type, event = {}) {
      const payload = {
        type,
        defaultPrevented: false,
        preventDefault() {
          this.defaultPrevented = true;
        },
        stopPropagation() {},
        ...event,
      };
      for (const listener of [...(doc.listeners.get(type) ?? [])])
        listener(payload);
      return payload;
    },
  };
  doc.documentElement = createNode(doc, 'html');
  doc.documentElement.dataset = {};
  doc.body = createNode(doc, 'body');
  doc.documentElement.append(doc.body);

  const panel = (id, innerClass, titleText) => {
    const host = createNode(doc, 'div');
    host.id = id;
    const inner = createNode(doc, 'div');
    inner.className = innerClass;
    const header = createNode(doc, 'div');
    header.className = 'panel-header';
    const title = createNode(doc, 'span');
    title.className = 'panel-title';
    title.textContent = titleText;
    header.append(title);
    inner.append(header);
    host.append(inner);
    doc.body.append(host);
    return { host, inner, title };
  };

  const data = panel('data-panel', 'data-panel-inner', 'DATA LAYERS');
  const dataToggles = createNode(doc, 'div');
  dataToggles.id = 'data-toggles';
  data.inner.append(dataToggles);
  const scene = panel('scene-panel', 'scene-panel-inner', 'SCENES');

  const styleIndicator = createNode(doc, 'div');
  styleIndicator.id = 'style-indicator';
  const activeStyle = createNode(doc, 'span');
  activeStyle.id = 'active-style-name';
  activeStyle.textContent = 'NORMAL';
  styleIndicator.append(activeStyle);
  doc.body.append(styleIndicator);

  const hud = createNode(doc, 'div');
  hud.id = 'intel-hud';
  const corner = createNode(doc, 'div');
  corner.className = 'hud-corner hud-bottom-right';
  const hudContent = createNode(doc, 'div');
  hudContent.className = 'hud-content';
  corner.append(hudContent);
  hud.append(corner);
  doc.body.append(hud);

  return {
    document: doc,
    body: doc.body,
    hudContent,
    dataToggles,
    dataTitle: data.title,
    scenePanel: scene.host,
    sceneInner: scene.inner,
    sceneTitle: scene.title,
    styleIndicator,
    activeStyle,
  };
}
