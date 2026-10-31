// End-to-end walk-through in a real browser at phone size: two new users and the admin use every feature.
// Usage: node e2e_phone.js <base url> <sqlite db path> <admin password> <photo dir>
// Fails (exit 1) on any wrong result, server error, browser console error, broken image or sideways scrolling.
const { chromium } = require('playwright');
const { execFileSync } = require('child_process');
const [U, DB, ADMIN_PW, PICS] = process.argv.slice(2);
const fails = [], pagesSeen = new Set();
const ok = (cond, msg) => { if (!cond) fails.push(msg); };
const sql = (q) => execFileSync('python3', ['-c', 'import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);r=c.execute(sys.argv[2]).fetchall();c.commit();print(r)', DB, q]).toString().trim();
const stamp = Date.now().toString(36).slice(-5);

async function user(browser, name, pw, signup = true) {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
  const p = await ctx.newPage();
  await p.route(/fonts\.(googleapis|gstatic)\.com/, r => r.abort());   // sandbox has no internet; real phones load these
  p.on('dialog', d => d.accept());
  p.on('console', m => { if (m.type() === 'error' && !/fonts\.g|ERR_FAILED/.test(m.text())) fails.push(`${name}: console error ${m.text()}`); });
  p.on('response', r => { if (r.status() >= 500) fails.push(`${name}: HTTP ${r.status()} ${r.url()}`); });
  p.on('load', async () => {
    try {
      const u = new URL(p.url()); pagesSeen.add(u.pathname.replace(/\d+/g, ':id'));
      const wide = await p.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      if (wide > 1) fails.push(`${name}: page scrolls sideways by ${wide}px on ${u.pathname}`);
      const broken = await p.evaluate(() => [...document.images].filter(i => i.complete && i.naturalWidth === 0 && !/fonts/.test(i.src)).map(i => i.src));
      if (broken.length) fails.push(`${name}: broken image(s) on ${u.pathname}: ${broken.join(', ')}`);
    } catch (e) { /* page navigated away mid-check */ }
  });
  await p.goto(U + (signup ? '/signup' : '/login'));
  await p.fill('[name=username]', name); await p.fill('[name=password]', pw);
  await p.click('form button.primary'); await p.waitForLoadState();
  ok(await p.isVisible('a[href="/wallet"]'), `${name}: not logged in after ${signup ? 'signup' : 'login'}`);
  return p;
}
const wallet = async (p) => parseFloat((await p.textContent('a[href="/wallet"]')).replace(/[^0-9.]/g, ''));
const idFrom = (p) => parseInt(p.url().split('/').pop());

(async () => {
  const browser = await chromium.launch();
  const buyer = await user(browser, 'wife' + stamp, 'pass12345'), seller = await user(browser, 'hubby' + stamp, 'pass12345');

  // Wallet: new accounts start at $0; pretend top-up
  ok(await wallet(buyer) === 0, 'new buyer should start at $0');
  await buyer.goto(U + '/wallet'); await buyer.fill('form[action="/wallet/deposit"] [name=amount]', '2,000'); await buyer.click('text=Add funds');
  ok(await wallet(buyer) === 2000, `buyer wallet after deposit is ${await wallet(buyer)}, expected 2000`);

  // Sell with a big phone photo + a PNG, condition, price above the old cap
  await seller.goto(U + '/sell');
  await seller.fill('[name=title]', 'Bass boat 2019 ' + stamp); await seller.fill('[name=price]', '1,500');
  await seller.selectOption('[name=category]', 'Sports'); await seller.selectOption('#condition', 'Used - Like new');
  await seller.fill('[name=location]', 'Lafayette, LA'); await seller.fill('[name=body]', 'Trolling motor, trailer included.');
  await seller.setInputFiles('#photos', [PICS + '/p0.jpg', PICS + '/p1.jpg']); await seller.click('button:has-text("Publish listing")');
  await seller.waitForURL(/\/listing\/\d+$/); const lid = idFrom(seller);
  ok((await seller.textContent('.notice')).includes('live'), 'no "listing is live" message');
  ok(await seller.locator('.strip button').count() === 2, 'listing should show 2 photo thumbnails');
  const big = await seller.evaluate(() => { const i = document.getElementById('mainphoto'); return [i.naturalWidth, i.naturalHeight]; });
  ok(big[0] === 1600, `main photo should be resized to 1600px wide, got ${big}`);

  // Browse, list view, filters, search
  await buyer.goto(U + '/');
  ok(await buyer.isVisible(`a[href="/listing/${lid}"] img`), 'new listing photo not on browse grid');
  await buyer.goto(U + '/?view=list'); ok(await buyer.isVisible(`a.rowitem[href="/listing/${lid}"]`), 'not in list view');
  await buyer.goto(U + '/?min=1000&max=2000&cond=Used+-+Like+new'); ok(await buyer.isVisible(`a[href="/listing/${lid}"]`), 'filters hide matching item');
  ok(!(await buyer.content()).includes('LEGO'), 'price filter let a $95 item through');
  await buyer.goto(U + '/?q=bass+boat'); ok(await buyer.isVisible(`a[href="/listing/${lid}"]`), 'search did not find item');
  await buyer.goto(U + '/?sort=price_desc'); const order = await buyer.$$eval('.tile .price', els => els.map(e => parseFloat(e.firstChild.textContent.replace(/[^0-9.]/g, ''))));
  ok(order.every((v, i) => i === 0 || order[i - 1] >= v), `sort high-low wrong: ${order}`);

  // Listing page: gallery switch, message the seller with a quick reply
  await buyer.goto(U + `/listing/${lid}`);
  const first = await buyer.getAttribute('#mainphoto', 'src'); await buyer.click('.strip button >> nth=1');
  ok(await buyer.getAttribute('#mainphoto', 'src') !== first, 'tapping 2nd thumbnail did not switch photo');
  ok((await buyer.textContent('body')).includes('Used - Like new'), 'condition not shown');
  await buyer.click('button.chip:has-text("When can I pick it up?")');
  ok(await buyer.inputValue('#firstmsg') === 'When can I pick it up?', 'quick reply did not fill message');
  await buyer.click('form[action$="/message"] button.primary'); await buyer.waitForURL(/\/inbox\/\d+$/); const cid = idFrom(buyer);

  // Seller: unread badge, reads, replies with an off-site payment ask -> buyer sees warning
  await seller.goto(U + '/'); ok((await seller.textContent('nav')).match(/Inbox\s*1/), 'seller has no unread badge');
  await seller.goto(U + '/inbox'); await seller.click(`a[href="/inbox/${cid}"]`);
  ok((await seller.textContent('.bubbles')).includes('When can I pick it up?'), 'seller cannot see buyer message');
  await seller.fill('#reply', 'Saturday 9am. Or Zelle me and I will drop it off'); await seller.click('button:has-text("Send")');
  await seller.goto(U + '/'); ok(!(await seller.textContent('nav')).match(/Inbox\s*\d/), 'unread badge did not clear');
  await buyer.goto(U + `/inbox/${cid}`);
  ok(await buyer.isVisible('.warn'), 'no off-site payment warning for buyer');

  // Seller drops the price; buyer saves it and sees the drop
  await seller.goto(U + `/listing/${lid}/edit`); await seller.fill('#p', '1250'); await seller.click('text=Save changes');
  await buyer.goto(U + `/listing/${lid}`);
  ok((await buyer.textContent('.price')).includes('$1,250.00') && await buyer.isVisible('s.was'), 'price drop not shown');
  await buyer.click('form[action$="/save"] button'); await buyer.goto(U + '/saved');
  ok(await buyer.isVisible(`a[href="/listing/${lid}"]`), 'item not on Saved page');

  // Buy, both approve, return it inside the 1-day window, full refund, item back on sale
  await buyer.goto(U + `/listing/${lid}`); await buyer.click('button:has-text("Buy with Safe Transfer")');
  await buyer.waitForURL(/\/order\/\d+$/); let oid = idFrom(buyer);
  ok(await wallet(buyer) === 750, `buyer wallet after paying should be 750, got ${await wallet(buyer)}`);
  await buyer.goto(U + '/'); ok(!(await buyer.content()).includes(`/listing/${lid}"`), 'sold-pending item still on browse');
  await seller.goto(U + `/order/${oid}`); await seller.click('text=I handed it over, approve');
  await buyer.goto(U + `/order/${oid}`); await buyer.click('text=I received it, approve');
  ok((await buyer.textContent('body')).includes('Return window'), 'no return window after both approved');
  await buyer.click('summary:has-text("Return this item")'); await buyer.fill('#rr', 'Too big for my garage'); await buyer.click('text=Start return');
  await seller.goto(U + `/order/${oid}`); ok((await seller.textContent('body')).includes('Too big for my garage'), 'seller cannot see return reason');
  await seller.click('text=I got it back, refund the buyer');
  ok(await wallet(buyer) === 2000 || (await buyer.reload(), await wallet(buyer)) === 2000, 'buyer not fully refunded');
  ok(await wallet(seller) === 0, 'seller should not be paid on a return');
  await buyer.goto(U + '/'); ok(await buyer.isVisible(`a[href="/listing/${lid}"]`), 'returned item not back on sale');

  // Buy again, both approve, the day passes -> seller paid minus 5%; both rate each other
  await buyer.goto(U + `/listing/${lid}`); await buyer.click('button:has-text("Buy with Safe Transfer")'); await buyer.waitForURL(/\/order\/\d+$/); oid = idFrom(buyer);
  await seller.goto(U + `/order/${oid}`); await seller.click('text=I handed it over, approve');
  await buyer.goto(U + `/order/${oid}`); await buyer.click('text=I received it, approve');
  await seller.goto(U + '/wallet'); ok((await seller.textContent('main')).includes('waiting to be paid out'), 'seller wallet does not show pending payout');
  sql(`UPDATE orders SET return_until='2000-01-01T00:00:00' WHERE id=${oid}`);   // stand-in for 24 hours passing
  await seller.goto(U + `/order/${oid}`);
  ok(await wallet(seller) === 1187.5, `seller should get $1,187.50 (5% fee), got ${await wallet(seller)}`);
  ok((await seller.textContent('body')).includes('Completed'), 'order not completed after window');
  await seller.selectOption('#stars', '5'); await seller.fill('#rc', 'Easy buyer'); await seller.click('text=Leave rating');
  await buyer.goto(U + `/order/${oid}`); await buyer.selectOption('#stars', '4'); await buyer.fill('#rc', 'Boat as described'); await buyer.click('text=Leave rating');
  await buyer.goto(U + `/u/hubby${stamp}`); const prof = await buyer.textContent('main');
  ok(prof.includes('4.0 from 1 rating') && prof.includes('Boat as described') && prof.includes('1 completed sale'), 'seller profile/rating wrong');

  // Dispute: buyer disputes a seeded item, admin refunds
  await buyer.goto(U + '/listing/3'); await buyer.click('button:has-text("Buy with Safe Transfer")'); await buyer.waitForURL(/\/order\/\d+$/); const did = idFrom(buyer);
  const before = await wallet(buyer);
  await buyer.click('summary:has-text("Open a dispute")'); await buyer.fill('[name=reason]', 'Seller never showed up'); await buyer.click('button:has-text("Open dispute")');
  const admin = await user(browser, 'admin', ADMIN_PW, false);
  await admin.goto(U + '/admin'); ok((await admin.textContent('main')).includes('Seller never showed up'), 'admin cannot see dispute');
  await admin.click(`form[action="/admin/resolve/${did}"] button:has-text("Refund buyer")`);
  await buyer.reload(); ok(await wallet(buyer) === before + 380, 'dispute refund wrong');

  // Neighbors: free stuff with photo, reply, mark picked up
  await seller.goto(U + '/neighbors/new'); await seller.selectOption('#kind', 'free');
  await seller.fill('#title', 'Free firewood ' + stamp); await seller.fill('#location', 'Lafayette, LA'); await seller.setInputFiles('#image', PICS + '/p1.jpg');
  await seller.click('form button.primary'); await seller.waitForURL(/\/neighbors\/\d+$/); const nid = idFrom(seller);
  await buyer.goto(U + `/neighbors/${nid}`); await buyer.fill('#reply', 'I will take it'); await buyer.click('text=Send reply');
  await seller.goto(U + `/neighbors/${nid}`); ok((await seller.textContent('main')).includes('I will take it'), 'neighbors reply missing');
  await seller.click("text=It's been picked up"); ok((await seller.textContent('main')).includes('Done'), 'not marked done');

  // Visit every remaining page once, log out and back in
  for (const path of ['/orders', '/wallet', '/saved', '/inbox', '/neighbors', '/u/' + 'wife' + stamp, '/sell']) await buyer.goto(U + path);
  await buyer.click('nav button:has-text("Log out")'); ok(await buyer.isVisible('a[href="/login"]'), 'log out failed');
  await buyer.goto(U + '/login'); await buyer.fill('[name=username]', 'wife' + stamp); await buyer.fill('[name=password]', 'pass12345');
  await buyer.click('form button.primary'); ok(await wallet(buyer) > 0, 'could not log back in');

  await browser.close();
  console.log(`pages checked: ${[...pagesSeen].sort().join(' ')}`);
  if (fails.length) { console.log('FAILURES:\n- ' + fails.join('\n- ')); process.exit(1); }
  console.log('E2E PASSED');
})().catch(e => { console.log('CRASHED: ' + e.message.split('\n')[0]); if (fails.length) console.log('- ' + fails.join('\n- ')); process.exit(1); });
