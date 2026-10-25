import assert from 'node:assert/strict';
import test from 'node:test';
import { consoleTestDocument } from '../testSupport/headsupConsoleDocument.mjs';
import {
  PRODUCT_NAME,
  WORDMARK_HEAD,
  WORDMARK_TAIL,
  applyHeadsupBrand,
} from './brand.js';

/**
 * The two upstream hosts, shaped as the templates build them: a title bar
 * whose heading carries a gaze-tracking logo beside the wordmark, and a
 * loading cover whose logo is a sibling of its heading.
 */
function chrome() {
  const fixture = consoleTestDocument();
  const doc = fixture.document;

  const bar = doc.createElement('div');
  bar.id = 'title-bar';
  const heading = doc.createElement('h1');
  const titleLogo = doc.createElement('span');
  titleLogo.className = 'title-logo brand-logo';
  titleLogo.setAttribute('data-logo-gaze', '');
  const titleWord = doc.createElement('span');
  titleWord.setAttribute('translate', 'no');
  titleWord.append(doc.createTextNode("GOD'S EYE "));
  heading.append(titleLogo, titleWord);
  const subtitle = doc.createElement('p');
  subtitle.className = 'subtitle';
  subtitle.textContent = 'NO PLACE LEFT BEHIND';
  bar.append(heading, subtitle);

  const cover = doc.createElement('div');
  cover.id = 'loading-screen';
  const content = doc.createElement('div');
  content.className = 'loader-content';
  const coverLogo = doc.createElement('span');
  coverLogo.className = 'loader-logo brand-logo';
  coverLogo.setAttribute('data-logo-gaze', '');
  const coverHeading = doc.createElement('h2');
  coverHeading.append(doc.createTextNode("GOD'S EYE "));
  const status = doc.createElement('p');
  status.className = 'loader-status';
  status.textContent = 'Initializing photorealistic world...';
  content.append(coverLogo, coverHeading, status);
  cover.append(content);

  fixture.body.append(bar, cover);
  return { doc, bar, heading, cover, coverHeading, subtitle, status };
}

/** The wordmark as two children: the head as text, the tail as the accent. */
function readWordmark(host) {
  return {
    count: host.children.length,
    head: host.children[0]?.textContent,
    tailClass: host.children[1]?.className,
    tail: host.children[1]?.textContent,
  };
}

test('the title bar carries the product name, in two tones', () => {
  const { doc, heading } = chrome();

  const found = applyHeadsupBrand(doc);

  assert.equal(found.title, true);
  assert.deepEqual(readWordmark(heading), {
    count: 2,
    head: WORDMARK_HEAD,
    tailClass: 'title-accent',
    tail: WORDMARK_TAIL,
  });
  assert.equal(heading.getAttribute('translate'), 'no');
});

test("the fork's name is off the chrome, and so is the gaze logo", () => {
  const { doc, bar, cover } = chrome();

  applyHeadsupBrand(doc);

  for (const host of [bar, cover]) {
    assert.equal(host.querySelector('.brand-logo'), null);
    assert.equal(host.querySelector('.title-logo'), null);
    assert.equal(host.querySelector('.loader-logo'), null);
  }
});

test('the loading cover says the same thing the title bar does', () => {
  const { doc, coverHeading } = chrome();

  const found = applyHeadsupBrand(doc);

  assert.equal(found.loader, true);
  assert.equal(found.logos, 1);
  assert.deepEqual(readWordmark(coverHeading), {
    count: 2,
    head: WORDMARK_HEAD,
    tailClass: 'title-accent',
    tail: WORDMARK_TAIL,
  });
});

test('the tab is named, and the document title is the readable spelling', () => {
  const { doc } = chrome();

  applyHeadsupBrand(doc);

  assert.equal(doc.title, PRODUCT_NAME);
  assert.equal(PRODUCT_NAME, 'Signum');
});

test('nothing upstream owns beside the headings is touched', () => {
  // The tagline is hidden by the stylesheet rather than removed, and the
  // status line is upstream's to write while the world loads. A rename must
  // not quietly take either away.
  const { doc, subtitle, status } = chrome();

  applyHeadsupBrand(doc);

  assert.equal(subtitle.textContent, 'NO PLACE LEFT BEHIND');
  assert.equal(subtitle.parent?.id, 'title-bar');
  assert.equal(status.textContent, 'Initializing photorealistic world...');
  assert.equal(status.parent?.className, 'loader-content');
});

test('a document without the chrome is branded as far as it goes', () => {
  // This runs before the scene is built, against markup expanded at build
  // time. A missing heading is a reason to carry on, not to take the
  // application down.
  const { document: doc } = consoleTestDocument();

  const found = applyHeadsupBrand(doc);

  assert.deepEqual(found, { title: false, loader: false, logos: 0 });
  assert.equal(doc.title, PRODUCT_NAME);
  assert.doesNotThrow(() => applyHeadsupBrand(null));
});
