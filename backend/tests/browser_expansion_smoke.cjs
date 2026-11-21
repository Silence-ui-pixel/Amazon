// 本地真实前后端联调；ABA 传输替身，禁止误称线上验收。
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const {
  chromium,
} = require("C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright");
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 1000 },
    });
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    const seed = await (
      await page.request.get("http://127.0.0.1:8014/__test__/seed")
    ).json();
    await page.addInitScript((data) => {
      if (!sessionStorage.getItem("expansion-seeded")) {
        localStorage.setItem(
          "keyword-library-active-run-v1",
          JSON.stringify(data),
        );
        sessionStorage.setItem("expansion-seeded", "1");
      }
    }, seed);
    const writes = [];
    await page.route("**/api/v1/**", async (route) => {
      const req = route.request();
      if (req.method() !== "GET") writes.push(req.url());
      const response = await route.fetch({
        url: req.url().replace("127.0.0.1:5175", "127.0.0.1:8014"),
      });
      await route.fulfill({ response });
    });
    await page.goto("http://127.0.0.1:5175/");
    await page.locator('.step-nav-item[data-step="3"]').click();
    const panel = page.getByRole("region", {
      name: "ABA 正向 / 反向拓词",
      exact: true,
    });
    await panel.waitFor({ timeout: 15000 });
    const forward = panel.getByRole("button", {
      name: "开始正向采集 / 重试",
      exact: true,
    });
    await forward.click();
    await panel.getByText("采集完整", { exact: true }).waitFor();
    await panel
      .getByRole("button", { name: "开始反向采集 / 重试", exact: true })
      .click();
    await panel
      .getByText("本周正反向均已完整采集，第四步已完成。", { exact: true })
      .waitFor();
    assert.match(
      await panel.locator(".comparison").innerText(),
      /交集 23 · 仅正向 1 · 仅反向 1/,
    );
    await panel
      .getByRole("button", { name: "查看反向词表", exact: true })
      .click();
    await panel.getByText("共 24 个去重关键词", { exact: false }).waitFor();
    assert.equal(await panel.locator("tbody tr").count(), 20);
    await panel.locator("tbody tr").first().locator("summary").click();
    await panel
      .locator("tbody tr")
      .first()
      .getByText("B012345678,B087654321", { exact: true })
      .waitFor();
    await panel.getByRole("button", { name: "下一页", exact: true }).click();
    await page.waitForFunction(
      () => document.querySelectorAll(".expansion-panel tbody tr").length === 4,
    );
    const downloadEvent = page.waitForEvent("download");
    await panel
      .getByRole("button", { name: "下载反向 CSV", exact: true })
      .click();
    const download = await downloadEvent;
    const csv = await fs.readFile(await download.path(), "utf8");
    assert.match(csv, /B012345678/);
    assert.match(csv, /B087654321/);
    assert.match(csv, /2026-10-17/);
    assert.equal(await page.locator(".step-nav-item.complete").count(), 4);
    const before = writes.length;
    await page.reload();
    await page.locator('.step-nav-item[data-step="3"]').click();
    await panel
      .getByText("本周正反向均已完整采集，第四步已完成。", { exact: true })
      .waitFor();
    assert.equal(writes.length, before);
    assert.equal(
      (
        await (
          await page.request.get("http://127.0.0.1:8014/__test__/calls")
        ).json()
      ).aba,
      2,
    );
    await page.addStyleTag({ content: ".topbar{visibility:hidden!important}" });
    await panel.screenshot({ path: "../outputs/aba-expansion-desktop.png" });
    await page.setViewportSize({ width: 390, height: 844 });
    await panel.screenshot({ path: "../outputs/aba-expansion-mobile.png" });
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth > innerWidth,
      ),
      false,
    );
    // 草稿改选立即撤下完成；保存新竞对版本后旧反向快照只能作历史证据。
    await page.locator('.step-nav-item[data-step="2"]').click();
    const competitors = page.getByRole("region", {
      name: "筛选竞对 ASIN",
      exact: true,
    });
    await competitors
      .locator("article.candidate")
      .first()
      .getByLabel("相关性 / 排除理由")
      .fill("再次核对规格一致");
    await page.waitForFunction(
      () => !document.querySelector(".expansion-panel .success"),
    );
    await competitors
      .getByRole("button", { name: "保存竞对选择", exact: true })
      .click();
    await competitors
      .getByRole("button", { name: "竞对选择已保存", exact: true })
      .waitFor();
    await page.waitForFunction(() =>
      document
        .querySelector(".expansion-panel .report-heading")
        ?.textContent.includes("上游已变更 · 历史证据"),
    );
    assert.equal(await panel.locator(".success").count(), 0);
    assert.equal(
      (
        await (
          await page.request.get("http://127.0.0.1:8014/__test__/calls")
        ).json()
      ).aba,
      2,
    );
    assert.deepEqual(errors, []);
    await page.waitForLoadState("networkidle");
    console.log(
      JSON.stringify({
        forward: true,
        reverse: true,
        provenance: true,
        pagination: true,
        csvDownload: true,
        fourStepsDone: true,
        refreshNoCollection: true,
        upstreamInvalidation: true,
        mobileNoOverflow: true,
        pageErrors: errors,
      }),
    );
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
