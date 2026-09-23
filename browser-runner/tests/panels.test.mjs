import {test} from "node:test";
import assert from "node:assert/strict";
import {mkdtemp, rm} from "node:fs/promises";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {chromium} from "playwright";
import {expandRequestedLayer, openPanel, exerciseHover} from "../server.mjs";

test("styling capture returns from Locations and waits for delayed drawer/dialog", async () => {
  const browser = await chromium.launch({headless: true});
  const directory = await mkdtemp(join(tmpdir(), "mapp-panels-"));
  try {
    const page = await browser.newPage();
    await page.setContent(`
      <style>
        .body {display:none} .expanded > .body {display:block}
        #layers, #dialog {display:none}
      </style>
      <button data-id="layers" onclick="setTimeout(() => {
        document.querySelector('#locations').hidden = true;
        document.querySelector('#layers').style.display = 'block';
      }, 100)">Layers</button>
      <section id="locations">Selected feature information</section>
      <section id="layers">
        <div class="drawer layer-group expandable" data-id="Group">
          <div class="header" onclick="setTimeout(() => this.parentElement.classList.add('expanded'), 150)">Group</div>
          <div class="body">
            <div class="drawer layer-view expandable" data-id="Areas">
              <div class="header" onclick="setTimeout(() => this.parentElement.classList.add('expanded'), 150)">Areas</div>
              <div class="body">
                <button data-id="style-drawer" onclick="setTimeout(() => document.querySelector('#dialog').style.display='block', 350)">Style</button>
              </div>
            </div>
          </div>
        </div>
      </section>
      <div id="dialog" data-id="style-drawer-dialog">Legend: Population</div>
    `);
    const input = {layer: "Areas", plan: {activeGroups: ["Group"]}, expectedPanelText: ["Population"]};
    const navigation = await expandRequestedLayer(page, input);
    assert.equal(navigation.layerOpened, true);
    const panel = await openPanel(page, "styling", input, directory);
    assert.equal(panel.passed, true, JSON.stringify(panel));
    assert.equal(panel.captured, true);
    assert.equal(panel.expectedTextFound.Population, true);
    const absent = await openPanel(page, "filtering", input, directory);
    assert.equal(absent.passed, false);
    assert.equal(absent.failureReason, "panel-not-found");
  } finally {
    await browser.close();
    await rm(directory, {recursive: true, force: true});
  }
});

test("overview automatic hover is skipped but explicit hover remains required", async () => {
  const input = {plan: {hover: {type: "hover-centre-feature", automatic: false, skipReason: "overview-has-no-feature-target"}}};
  const skipped = await exerciseHover({}, input, "/unused", 1000);
  assert.equal(skipped.skipped, true);
  assert.equal(skipped.requested, false);
  assert.equal(skipped.attempted, false);
  assert.equal(skipped.passed, true);
  const page = {locator: () => ({first: () => ({boundingBox: async () => null})})};
  for (const request of [{hover: true}, {expectedHoverText: ["Population"]}]) {
    const explicit = await exerciseHover(page, {...input, ...request}, "/unused", 1000);
    assert.equal(explicit.requested, true);
    assert.equal(explicit.skipped, false);
    assert.equal(explicit.passed, false);
  }
});
