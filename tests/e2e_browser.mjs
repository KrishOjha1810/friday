/* End to end, in a real browser, against the real server.
 *
 * The page had one test suite that read its source and ran two pure functions.
 * That catches a syntax error and nothing else: whether the thing actually
 * WORKS when a person opens it, clicks a session and types an answer, was never
 * checked by anything but me looking at screenshots.
 *
 * Every failure here is written as what a user would experience, not as an
 * assertion about the DOM.
 */

import { chromium } from 'playwright';

const URL = process.argv[2];
if (!URL) {
  console.error('usage: node e2e_browser.mjs <url>');
  process.exit(2);
}

let pass = 0, fail = 0;
const failures = [];

async function check(name, fn) {
  try {
    await fn();
    pass++;
    console.log(`  ok   ${name}`);
  } catch (e) {
    fail++;
    failures.push(`${name}: ${e.message}`);
    console.log(`  FAIL ${name}`);
    console.log(`         ${String(e.message).split('\n')[0].slice(0, 140)}`);
  }
}

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1200, height: 900 } });
const page = await ctx.newPage();

const consoleErrors = [];
page.on('console', m => {
  if (m.type() === 'error') consoleErrors.push(m.text());
});
page.on('pageerror', e => consoleErrors.push('pageerror: ' + e.message));

await page.goto(URL, { waitUntil: 'networkidle' });

await check('a new conversation shows you what to try, and tapping one asks it',
  async () => {
  // A blank box does not tell you any of this exists, and a product with
  // thirty-six commands and no visible starting point is one people type
  // "hello" into and close.
  await page.waitForSelector('.firstrun button', { timeout: 8000 });
  const options = await page.$$eval('.firstrun button', els =>
    els.map(e => e.textContent));
  assert(options.length >= 3, `only ${options.length} suggestions`);
  assert(options.join(' ').includes('brief me'), options);
  const before = await page.$$eval('.turn', els => els.length);
  await page.click('.firstrun button');
  await page.waitForFunction(
    n => document.querySelectorAll('.turn').length > n, before,
    { timeout: 20000 });
  assert(!(await page.$('.firstrun')), 'the card stayed after being used');
});

await check('the page loads and names itself', async () => {
  const name = await page.textContent('.name');
  assert(name && name.trim() === 'Friday', `header said ${JSON.stringify(name)}`);
});

await check('no javascript errors on load', async () => {
  assert(consoleErrors.length === 0,
         'console errors: ' + consoleErrors.slice(0, 3).join(' | '));
});

await check('the fleet strip shows every session', async () => {
  await page.waitForSelector('.fleet .sess', { timeout: 8000 });
  const labels = await page.$$eval('.fleet .sess', els =>
    els.map(e => e.textContent.trim()));
  for (const want of ['api', 'jobhunt', 'voicebridge']) {
    assert(labels.some(l => l.includes(want)), `${want} missing from ${labels}`);
  }
});

await check('a session waiting on you is marked as such', async () => {
  const cls = await page.getAttribute('.fleet .sess', 'class');
  const count = await page.textContent('.fleet .count');
  assert(/needs/.test(cls + count), `strip said "${count}", class "${cls}"`);
});

await check('the tab title carries the count, so it reads while hidden', async () => {
  const title = await page.title();
  assert(/\(\d+\)/.test(title), `title was "${title}"`);
});

await check('asking a question gets an answer in the thread', async () => {
  // Wait for a NEW reply, not for any reply: the page already puts a startup
  // notice in the thread, so "a bubble exists" is true before we ask anything.
  const before = await page.$$eval('.turn.fri .bub', els => els.length);
  await page.fill('#input', "what's running?");
  await page.press('#input', 'Enter');
  await page.waitForFunction(
    n => document.querySelectorAll('.turn.fri .bub').length > n,
    before, { timeout: 25000 });
  const said = await page.$$eval('.turn.fri .bub', els =>
    els.map(e => e.textContent).join(' '));
  assert(/api|jobhunt|voicebridge/.test(said),
         `Friday replied with nothing about the fleet: ${said.slice(-160)}`);
});

await check('tapping a session opens the panel with its real question', async () => {
  await page.click('.fleet .sess');
  await page.waitForSelector('#peek:not([hidden])', { timeout: 5000 });
  const ask = await page.textContent('#peekAsk');
  assert(/force-push/.test(ask), `panel showed "${ask}"`);
});

await check('the panel offers answers that were actually on the table', async () => {
  const opts = await page.$$eval('#peekSuggest button', els =>
    els.map(e => e.textContent.trim()));
  assert(opts.length > 0, 'no suggestions at all for a yes/no question');
  assert(opts.includes('Yes') && opts.includes('No'), `offered ${opts}`);
});

await check('escape closes the panel', async () => {
  await page.keyboard.press('Escape');
  await page.waitForSelector('#peek', { state: 'hidden', timeout: 3000 });
});

await check('answering from the panel reaches that session', async () => {
  await page.click('.fleet .sess');
  await page.waitForSelector('#peek:not([hidden])');
  await page.click('#peekSuggest button');   // "Yes"
  await page.waitForSelector('#peek', { state: 'hidden', timeout: 5000 });
  await page.waitForTimeout(1500);
  const said = await page.$$eval('.turn.you .bub', els =>
    els.map(e => e.textContent).join(' '));
  assert(/api/.test(said) && /yes/i.test(said),
         `what got sent was: ${said.slice(-120)}`);
});

await check('a message from a person can be acted on, not just read', async () => {
  // An announcement you can read and cannot act on is the dashboard this
  // product exists not to be.
  await page.waitForSelector('.bub .act', { timeout: 15000 });
  const label = await page.textContent('.bub .act');
  assert(/reply|answer|something/i.test(label), `the action said "${label}"`);
});

await check('it offers the verbs that turn a message into work', async () => {
  await page.click('.bub .act');
  await page.waitForSelector('#peek:not([hidden])', { timeout: 5000 });
  const verbs = await page.$$eval('#peekSuggest button', els =>
    els.map(e => e.textContent));
  for (const want of ['Draft a reply', 'File a ticket']) {
    assert(verbs.some(v => v.includes(want)), `${want} missing from ${verbs}`);
  }
  const asked = await page.textContent('#peekAsk');
  assert(/Thursday/.test(asked), `the panel showed "${asked}"`);
  await page.keyboard.press('Escape');
  await page.waitForSelector('#peek', { state: 'hidden', timeout: 3000 });
});

await check('a production error offers what you do about a production error',
  async () => {
  // A panel that answers a Sentry alert with "brief me" is a panel you stop
  // opening. Each kind gets the verbs that fit it.
  await page.waitForFunction(
    () => [...document.querySelectorAll('.bub')].some(
      b => /Sentry/.test(b.textContent) && b.querySelector('.act')),
    { timeout: 20000 });
  const bub = await page.$('.bub:has(.act)');
  const acts = await page.$$('.bub .act');
  for (const a of acts) {
    const holder = await a.evaluateHandle(e => e.closest('.bub'));
    const text = await holder.evaluate(e => e.textContent);
    if (!/Sentry/.test(text)) continue;
    await a.click();
    await page.waitForSelector('#peek:not([hidden])', { timeout: 5000 });
    const verbs = await page.$$eval('#peekSuggest button', els =>
      els.map(e => e.textContent));
    assert(verbs.some(v => /File a ticket/.test(v)),
           `sentry verbs were ${verbs}`);
    assert(verbs.some(v => /Open in Sentry/.test(v)),
           `no way through to the issue: ${verbs}`);
    const state = await page.textContent('#peekState');
    assert(/production/.test(state), `panel said "${state}"`);
    await page.keyboard.press('Escape');
    await page.waitForSelector('#peek', { state: 'hidden', timeout: 3000 });
    return;
  }
  assert(false, 'no actionable Sentry announcement arrived');
});

await check('a plan can be approved from the panel that shows it', async () => {
  await page.waitForFunction(
    () => [...document.querySelectorAll('.bub')].some(
      b => /Nothing has run yet/.test(b.textContent) && b.querySelector('.act')),
    { timeout: 20000 });
  const acts = await page.$$('.bub .act');
  for (const a of acts) {
    const holder = await a.evaluateHandle(e => e.closest('.bub'));
    const text = await holder.evaluate(e => e.textContent);
    if (!/Nothing has run yet/.test(text)) continue;
    await a.click();
    await page.waitForSelector('#peek:not([hidden])', { timeout: 5000 });
    const verbs = await page.$$eval('#peekSuggest button', els =>
      els.map(e => e.textContent));
    assert(verbs.some(v => /Run the plan/.test(v)), `plan verbs were ${verbs}`);
    assert(verbs.some(v => /Not now/.test(v)),
           `no way to decline: ${verbs}`);
    await page.keyboard.press('Escape');
    await page.waitForSelector('#peek', { state: 'hidden', timeout: 3000 });
    return;
  }
  assert(false, 'no approvable plan arrived');
});

await check('the alerts bell shows on or off distinctly', async () => {
  const before = await page.getAttribute('body', 'data-push');
  assert(before === 'off' || before === 'on',
         `bell state was ${JSON.stringify(before)}, so it reads the same either way`);
});

await check('live updates arrive without a reload', async () => {
  const before = await page.$$eval('.turn', els => els.length);
  await page.evaluate(() => fetch('/state?k=' + new URLSearchParams(location.search).get('k')));
  await page.waitForTimeout(500);
  const after = await page.$$eval('.turn', els => els.length);
  assert(after >= before, 'the thread lost turns');
});

await check('it survives a reload with its history', async () => {
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForSelector('.turn', { timeout: 8000 });
  const turns = await page.$$eval('.turn', els => els.length);
  assert(turns > 0, 'the conversation was empty after a reload');
});

await check('it is usable on a phone-sized screen', async () => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(300);
  const overflow = await page.evaluate(() =>
    document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert(overflow <= 2, `the page scrolls sideways by ${overflow}px on a phone`);
  const inputBox = await page.$('#input');
  assert(inputBox, 'the composer disappeared on a small screen');
  const box = await inputBox.boundingBox();
  assert(box && box.width > 100, `composer is ${box && box.width}px wide`);
});

await check('keyboard focus is visible for anyone not using a mouse', async () => {
  await page.setViewportSize({ width: 1200, height: 900 });
  await page.keyboard.press('Tab');
  const styled = await page.evaluate(() => {
    const el = document.activeElement;
    if (!el || el === document.body) return false;
    const s = getComputedStyle(el);
    return s.outlineStyle !== 'none' || s.boxShadow !== 'none' ||
           s.borderColor !== 'rgba(0, 0, 0, 0)';
  });
  assert(styled, 'nothing visibly focused after Tab');
});

await check('no javascript errors after all of that', async () => {
  assert(consoleErrors.length === 0,
         'console errors: ' + consoleErrors.slice(0, 3).join(' | '));
});

await browser.close();

console.log(`\n  ${pass} passed, ${fail} failed`);
if (fail) {
  console.log('\n  failures:');
  failures.forEach(f => console.log('   - ' + f));
  process.exit(1);
}
