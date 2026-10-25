"use strict";

const { after, before, test } = require("node:test");
const assert = require("node:assert/strict");
const { spawn, execFileSync } = require("node:child_process");
const { once } = require("node:events");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const net = require("node:net");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "..");
const python = process.env.PYTHON_BIN || "python3";
let temporaryDirectory;
let databasePath;
let browser;
let localServer;
let betaServer;
let localUrl;
let betaUrl;

async function freePort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => server.listen(0, "127.0.0.1", error => error ? reject(error) : resolve()));
  const port = server.address().port;
  await new Promise(resolve => server.close(resolve));
  return port;
}

function startServer(port, privateBeta) {
  const child = spawn(python, ["app.py"], {
    cwd: root,
    env: { ...process.env, APP_ENV: "development", APP_HOST: "127.0.0.1", APP_PORT: String(port), EXPENSE_DB_PATH: databasePath, PRIVATE_BETA: privateBeta ? "1" : "0" },
    stdio: ["ignore", "ignore", "ignore"]
  });
  child.on("error", error => { child.startupError = error; });
  return child;
}

async function waitForServer(url, child) {
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`App server exited with status ${child.exitCode}.`);
    if (child.startupError) throw child.startupError;
    try {
      const response = await fetch(`${url}/healthz`);
      if (response.ok) return;
    } catch {}
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  throw new Error(`App server did not become healthy at ${url}.`);
}

function issueInvitation(hours = 72) {
  const result = execFileSync(python, ["app.py", "invite", "create", "--expires-hours", String(hours)], {
    cwd: root,
    env: { ...process.env, APP_ENV: "development", PRIVATE_BETA: "1", EXPENSE_DB_PATH: databasePath },
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"]
  });
  const [metadata, token] = result.trim().split(/\r?\n/);
  const id = Number(metadata.split(" ")[1]);
  assert.ok(Number.isInteger(id));
  assert.ok(token && token.length >= 32);
  return { id, token };
}

function databaseCommand(script, ...args) {
  execFileSync(python, ["-c", script, databasePath, ...args], { cwd: root, stdio: "ignore" });
}

async function newPage(url = localUrl) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  await page.goto(url);
  await page.getByRole("heading", { name: "Kontrol sizde." }).waitFor({ state: "visible", timeoutMs: 10_000 });
  return { context, page };
}

async function register(page, email, token) {
  await page.locator("#toggle-auth").click();
  await page.getByLabel("E-posta").fill(email);
  await page.getByLabel("Parola").fill("phase-eleven-temporary-password");
  if (token !== undefined) await page.getByLabel("Davet kodu").fill(token);
  await page.getByRole("button", { name: "Hesap oluştur" }).click();
}

async function waitForDashboard(page) {
  await page.getByRole("link", { name: /Genel Bakış/ }).click();
  await page.getByRole("heading", { name: "Genel Bakış" }).waitFor({ state: "visible", timeoutMs: 10_000 });
}

async function stopServer(child) {
  if (!child || child.exitCode !== null) return;
  child.kill("SIGTERM");
  await Promise.race([once(child, "exit"), new Promise(resolve => setTimeout(resolve, 3000))]);
  if (child.exitCode === null) child.kill("SIGKILL");
}

before(async () => {
  temporaryDirectory = await fs.mkdtemp(path.join(os.tmpdir(), "expense-tracker-e2e-"));
  databasePath = path.join(temporaryDirectory, "isolated.sqlite3");
  const localPort = await freePort();
  const betaPort = await freePort();
  if (localPort === betaPort) throw new Error("Could not allocate separate local and beta ports.");
  localUrl = `http://127.0.0.1:${localPort}`;
  betaUrl = `http://127.0.0.1:${betaPort}`;
  localServer = startServer(localPort, false);
  await waitForServer(localUrl, localServer);
  betaServer = startServer(betaPort, true);
  await waitForServer(betaUrl, betaServer);
  const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
  browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
});

after(async () => {
  await browser?.close().catch(() => {});
  await Promise.all([stopServer(localServer), stopServer(betaServer)]);
  if (temporaryDirectory) await fs.rm(temporaryDirectory, { recursive: true, force: true });
});

test("local signup, expense/category/budget/recurring/CSV flows, account switching, expiry and deletion", async () => {
  const { context, page } = await newPage();
  try {
    assert.match(await page.locator("body").innerText(), /Giriş yap/);
    await page.locator("#toggle-auth").focus();
    await page.keyboard.press("Enter");
    assert.equal(await page.locator('input[name="invitation_token"]').count(), 0);
    await page.getByLabel("E-posta").fill("e2e-owner@example.invalid");
    await page.getByLabel("Parola").fill("phase-eleven-temporary-password");
    await page.getByRole("button", { name: "Hesap oluştur" }).click();
    await waitForDashboard(page);
    assert.equal(await page.locator("[data-section]").count(), 4);
    assert.equal(await page.locator("#page-overview #expense-form").count(), 0);
    await page.getByRole("link", { name: /Kredi Kartlarım/ }).click();
    await page.locator('#card-form input[name="bank_name"]').fill("E2E Banka");
    await page.locator('#card-form input[name="display_name"]').fill("E2E Kart");
    await page.locator('#card-form input[name="closing_day"]').fill("15");
    await page.locator('#card-form input[name="due_day"]').fill("20");
    await page.locator("#card-form button.primary").click();
    await page.locator("#cards b").filter({ hasText: /E2E Banka · E2E Kart/ }).waitFor({ state: "visible" });
    await page.getByRole("link", { name: /Genel Bakış/ }).click();
    await page.getByText("E2E Kart", { exact: true }).waitFor({ state: "visible" });
    assert.equal(await page.locator("#card-form").isVisible(), false);
    await page.getByRole("link", { name: "Harcamalarım" }).click();
    assert.match(await page.locator("#expenses").innerText(), /Bu ay henüz harcama yok\./);

    await page.getByRole("button", { name: /Kategori ekle/ }).click();
    await page.locator('#category-form input[name="name"]').fill("E2E Test");
    await page.locator("#category-form button.primary").click();
    await page.locator('#expense-form select[name="category_id"] option').filter({ hasText: "E2E Test" }).waitFor({ state: "attached" });

    const currentMonth = new Date().toISOString().slice(0, 7);
    const today = new Date().toISOString().slice(0, 10);
    await page.locator("#new-expense").click();
    await page.locator('#expense-form input[name="amount"]').fill("25.00");
    await page.locator('#expense-form input[name="spent_on"]').fill(today);
    await page.locator('#expense-form input[name="description"]').fill("E2E market harcaması");
    await page.locator('#expense-form select[name="category_id"]').selectOption({ label: "E2E Test" });
    await page.locator('#expense-form select[name="payment_method_id"]').selectOption({ label: "Kredi kartı" });
    await page.locator('#expense-form select[name="card_id"]').selectOption({ label: "E2E Banka · E2E Kart" });
    await page.locator('#expense-form select[name="installments_count"]').selectOption("3");
    await page.locator('#expense-form input[name="first_installment_on"]').fill(today);
    await page.locator('#expense-form button.primary').click();
    const expenseRow = page.locator("#expenses tbody tr").filter({ hasText: "E2E market harcaması" });
    await expenseRow.waitFor({ state: "visible" });
    await page.locator("#expense-search").fill("eşleşme yok");
    await page.getByText("Filtrelerle eşleşen harcama yok.", { exact: true }).waitFor({ state: "visible" });
    await page.locator("#expense-search").fill("E2E market");
    await page.locator("#expense-category-filter").selectOption({ label: "E2E Test" });
    await page.locator("#expense-method-filter").selectOption({ label: "Kredi kartı" });
    await expenseRow.waitFor({ state: "visible" });
    await page.locator("#expense-search").fill("");
    await page.locator("#expense-category-filter").selectOption("");
    await page.locator("#expense-method-filter").selectOption("");
    await page.getByRole("link", { name: /Genel Bakış/ }).click();
    assert.match(await page.locator(".stat.spend strong").innerText(), /25,00/);
    await page.getByRole("link", { name: "Harcamalarım" }).click();

    await expenseRow.getByRole("button", { name: "Düzenle" }).click();
    await page.locator('#expense-form input[name="description"]').fill("E2E market harcaması güncellendi");
    await page.locator('#expense-form button.primary').click();
    await page.locator("#expense-dialog").waitFor({ state: "hidden" });
    await page.getByRole("link", { name: /Genel Bakış/ }).click();
    assert.match(await page.locator(".stat.spend strong").innerText(), /25,00/);
    await page.getByRole("link", { name: "Harcamalarım" }).click();

    await page.locator('#budget-form select[name="category_id"]').selectOption({ label: "E2E Test" });
    await page.locator('#budget-form input[name="amount"]').fill("28.00");
    await page.locator('#budget-form button[type="submit"]').click();
    await page.getByText("Bütçeye yaklaşıyor", { exact: true }).waitFor({ state: "visible" });

    const previousDate = new Date();
    previousDate.setMonth(previousDate.getMonth() - 1);
    const previousMonth = `${previousDate.getFullYear()}-${String(previousDate.getMonth() + 1).padStart(2, "0")}`;
    await page.locator("#month").fill(previousMonth);
    const previousLabel = new Intl.DateTimeFormat("tr-TR", { month: "long", year: "numeric" }).format(new Date(`${previousMonth}-01T12:00:00`));
    assert.equal(await page.locator("#month").inputValue(), previousMonth);
    await page.getByRole("link", { name: /Genel Bakış/ }).click();
    assert.match(await page.locator(".stat.spend strong").innerText(), /0,00/);
    await page.locator("#month").fill(currentMonth);
    const currentLabel = new Intl.DateTimeFormat("tr-TR", { month: "long", year: "numeric" }).format(new Date(`${currentMonth}-01T12:00:00`));
    assert.equal(await page.locator("#month").inputValue(), currentMonth);
    assert.match(await page.locator(".stat.spend strong").innerText(), /25,00/);
    await page.getByRole("link", { name: "Harcamalarım" }).click();

    await expenseRow.getByRole("button", { name: "Sil" }).click();
    await page.getByRole("dialog").getByText(/emin misiniz/).waitFor({ state: "visible" });
    await page.locator("#cancel-delete").click();
    await expenseRow.waitFor({ state: "visible" });
    await expenseRow.getByRole("button", { name: "Sil" }).click();
    await page.locator("#confirm-delete").click();
    await expenseRow.waitFor({ state: "detached" });
    await page.getByRole("link", { name: /Genel Bakış/ }).click();
    assert.match(await page.locator(".stat.spend strong").innerText(), /0,00/);

    await page.locator("#new-recurring").click();
    await page.locator('#recurring-form input[name="description"]').fill("E2E düzenli gider");
    await page.locator('#recurring-form input[name="amount"]').fill("12.50");
    await page.locator('#recurring-form select[name="category_id"]').selectOption({ label: "E2E Test" });
    await page.locator('#recurring-form select[name="frequency"]').selectOption("monthly");
    await page.locator('#recurring-form input[name="start_on"]').fill(today);
    await page.locator('#recurring-form button[type="submit"]').click();
    const planned = page.locator(".planned-row").filter({ hasText: "E2E düzenli gider" });
    await planned.waitFor({ state: "visible" });
    assert.match(await page.locator(".stat.spend strong").innerText(), /0,00/);
    await planned.getByRole("button", { name: "Gerçekleşti / ödendi" }).click();
    await page.getByRole("link", { name: /Genel Bakış/ }).click();
    assert.match(await page.locator(".stat.spend strong").innerText(), /12,50/);
    await page.getByRole("link", { name: "Harcamalarım" }).click();

    await page.locator("#import-csv").click();
    const csvPath = path.join(temporaryDirectory, "import.csv");
    await fs.writeFile(csvPath, `Tarih,Tutar,Açıklama,Kategori\n${today},5,CSV içe aktarım,E2E Test\nnot-a-date,not-a-number,Hatalı satır,E2E Test\n`, "utf8");
    await page.locator('#import-form input[name="csv_file"]').setInputFiles(csvPath);
    await page.locator("#preview-import").click();
    await page.getByText("Hatalı satır", { exact: true }).waitFor({ state: "visible" });
    await page.getByText("Kontrol gerekli", { exact: true }).waitFor({ state: "visible" });
    await page.locator("#save-import").click();
    await page.getByText(/1 harcama içe aktarıldı/).waitFor({ state: "visible" });

    const downloadPromise = page.waitForEvent("download");
    await page.getByRole("link", { name: "CSV dışa aktar" }).click();
    const download = await downloadPromise;
    const exported = await fs.readFile(await download.path(), "utf8");
    assert.match(exported, /CSV içe aktarım/);

    await page.locator("#new-expense").click();
    await page.locator('#expense-form input[name="amount"]').fill("8.00");
    await page.locator('#expense-form input[name="spent_on"]').fill(today);
    await page.locator('#expense-form input[name="description"]').fill("E2E statement purchase");
    await page.locator('#expense-form select[name="payment_method_id"]').selectOption({ label: "Kredi kartı" });
    await page.locator('#expense-form select[name="card_id"]').selectOption({ label: "E2E Banka · E2E Kart" });
    await page.locator('#expense-form select[name="installments_count"]').selectOption("3");
    await page.locator('#expense-form input[name="first_installment_on"]').fill(today);
    await page.locator('#expense-form button.primary').click();
    await page.getByRole("link", { name: /Kredi Kartlarım/ }).click();
    const cardDetails = page.locator(".rule-row").filter({ hasText: "E2E Kart" }).last();
    await cardDetails.getByText("Ekstreler", { exact: true }).first().click();
    await page.getByText("E2E statement purchase", { exact: true }).waitFor({ state: "visible" });
    await cardDetails.locator("details").last().locator("summary").click();
    const repaymentForm = page.locator('[data-card-repayment]').last();
    await repaymentForm.locator('input[name="amount"]').fill("2.00");
    await repaymentForm.locator('input[name="paid_on"]').fill(today);
    await repaymentForm.locator('select[name="statement_id"]').selectOption({ index: 1 });
    await repaymentForm.locator("button").click();
    await page.getByRole("link", { name: /Genel Bakış/ }).click();
    await page.getByText(/E2E Kart/).waitFor({ state: "visible" });
    await page.getByRole("link", { name: /Harcamalar/ }).click();

    await page.getByRole("link", { name: "Gelirlerim" }).click();
    const incomeForm = page.locator("#income-form");
    await incomeForm.locator('input[name="description"]').fill("E2E beklenen gelir");
    await incomeForm.locator('input[name="amount"]').fill("75.25");
    await incomeForm.locator('input[name="expected_on"]').fill(today);
    await incomeForm.locator('select[name="frequency"]').selectOption("once");
    await incomeForm.locator('button[type="submit"]').click();
    let incomeRecord = page.locator(".planned-row").filter({ hasText: "E2E beklenen gelir" });
    await incomeRecord.waitFor({ state: "visible" });
    await incomeRecord.getByRole("button", { name: "Düzenle" }).click();
    await incomeForm.locator('input[name="amount"]').fill("80.00");
    await incomeForm.locator('button[type="submit"]').click();
    incomeRecord = page.locator(".planned-row").filter({ hasText: "E2E beklenen gelir" });
    await incomeRecord.waitFor({ state: "visible" });
    await incomeRecord.getByRole("button", { name: "Alındı olarak işaretle" }).click();
    await incomeRecord.getByText("Alındı", { exact: true }).waitFor({ state: "visible" });

    await incomeForm.locator('input[name="description"]').fill("E2E aylık maaş");
    await incomeForm.locator('input[name="amount"]').fill("1200.00");
    await incomeForm.locator('input[name="expected_on"]').fill(today);
    await incomeForm.locator('select[name="frequency"]').selectOption("monthly");
    assert.equal(await incomeForm.locator('input[name="end_of_month"]').isChecked(), true);
    await incomeForm.locator('button[type="submit"]').click();
    let incomeRule = page.locator(".rule-row").filter({ hasText: "E2E aylık maaş" });
    await incomeRule.waitFor({ state: "visible" });
    await incomeRule.locator('[data-income-rule-edit]').click();
    await incomeForm.locator('input[name="description"]').fill("E2E güncellenen maaş");
    await incomeForm.locator('select[name="frequency"]').selectOption("weekly");
    await incomeForm.locator('button[type="submit"]').click();
    incomeRule = page.locator(".rule-row").filter({ hasText: "E2E güncellenen maaş" });
    await incomeRule.waitFor({ state: "visible" });
    await incomeRule.locator('[data-income-rule-status][data-status="paused"]').click();
    await incomeRule.locator(".status-paused").waitFor({ state: "visible" });
    await incomeRule.locator('[data-income-rule-status][data-status="active"]').click();
    await incomeRule.locator(".status-active").waitFor({ state: "visible" });
    await incomeRule.locator('[data-income-rule-status][data-status="cancelled"]').click();
    await incomeRule.locator(".status-cancelled").waitFor({ state: "visible" });
    await incomeRecord.waitFor({ state: "visible" });
    const incomeDownloadPromise = page.waitForEvent("download");
    await page.getByRole("link", { name: "Gelirleri CSV olarak indir" }).click();
    const incomeDownload = await incomeDownloadPromise;
    assert.match(await fs.readFile(await incomeDownload.path(), "utf8"), /E2E beklenen gelir/);

    await page.locator("#logout").click();
    await page.getByRole("heading", { name: "Kontrol sizde." }).waitFor({ state: "visible" });
    await register(page, "e2e-second-user@example.invalid");
    await waitForDashboard(page);
    assert.match(await page.locator(".stat.spend strong").innerText(), /0,00/);
    await page.locator("#logout").click();
    await page.getByLabel("E-posta").fill("e2e-owner@example.invalid");
    await page.getByLabel("Parola").fill("wrong-password");
    await page.getByRole("button", { name: "Giriş yap" }).click();
    await page.getByText("E-posta veya parola hatalı.", { exact: true }).waitFor({ state: "visible" });
    await page.getByLabel("Parola").fill("phase-eleven-temporary-password");
    await page.getByRole("button", { name: "Giriş yap" }).click();
    await waitForDashboard(page);
    assert.match(await page.locator(".stat.spend strong").innerText(), /25,50/);

    await page.locator("#open-delete-account").click();
    const deleteAccountDialog = page.locator("#delete-account-dialog");
    await deleteAccountDialog.getByText(/geri alınamaz/).waitFor({ state: "visible" });
    await deleteAccountDialog.locator('input[name="password"]').fill("wrong-password");
    await deleteAccountDialog.locator('input[name="confirmation"]').fill("SİL");
    await deleteAccountDialog.locator('button[type="submit"]').click();
    await page.locator("#delete-account-error").getByText(/kontrol edin/).waitFor({ state: "visible" });
    assert.equal(await page.locator("#delete-account-dialog").evaluate(element => element.open), true);
    await deleteAccountDialog.locator('input[name="password"]').fill("phase-eleven-temporary-password");
    await deleteAccountDialog.locator('button[type="submit"]').click();
    await page.getByRole("status").getByText(/veriler silindi/).waitFor({ state: "visible" });

    await page.getByLabel("E-posta").fill("e2e-second-user@example.invalid");
    await page.getByLabel("Parola").fill("phase-eleven-temporary-password");
    await page.getByRole("button", { name: "Giriş yap" }).click();
    await waitForDashboard(page);
    assert.match(await page.locator(".stat.spend strong").innerText(), /0,00/);

    await page.locator("#logout").click();
    await page.getByLabel("E-posta").fill("e2e-second-user@example.invalid");
    await page.getByLabel("Parola").fill("phase-eleven-temporary-password");
    await page.getByRole("button", { name: "Giriş yap" }).click();
    await waitForDashboard(page);
    databaseCommand("import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); db.execute(\"UPDATE sessions SET expires_at=datetime('now','-1 second') WHERE user_id=(SELECT id FROM users WHERE email=?)\",(sys.argv[2],)); db.commit()", "e2e-second-user@example.invalid");
    await page.reload();
    await page.getByRole("heading", { name: "Kontrol sizde." }).waitFor({ state: "visible" });
  } finally {
    await context.close();
  }
});

test("private-beta signup rejects invalid, expired and reused invitations", async () => {
  const { context, page } = await newPage(betaUrl);
  try {
    assert.equal(await page.locator('input[name="invitation_token"]').count(), 0);
    await page.locator("#toggle-auth").click();
    assert.equal(await page.getByLabel("Davet kodu").count(), 1);

    await page.getByLabel("E-posta").fill("e2e-invalid-invite@example.invalid");
    await page.getByLabel("Parola").fill("phase-eleven-temporary-password");
    await page.getByLabel("Davet kodu").fill("invalid-invitation-token-123456789");
    await page.getByRole("button", { name: "Hesap oluştur" }).click();
    await page.getByText("Davet kodu geçersiz, süresi dolmuş veya kullanılmış.").waitFor({ state: "visible" });

    const expired = issueInvitation(1);
    databaseCommand("import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); db.execute(\"UPDATE invitations SET expires_at=datetime('now','-1 hour') WHERE id=?\",(int(sys.argv[2]),)); db.commit()", String(expired.id));
    await page.getByLabel("E-posta").fill("e2e-expired-invite@example.invalid");
    await page.getByLabel("Davet kodu").fill(expired.token);
    await page.getByRole("button", { name: "Hesap oluştur" }).click();
    await page.getByText("Davet kodu geçersiz, süresi dolmuş veya kullanılmış.").waitFor({ state: "visible" });

    const valid = issueInvitation(24);
    await page.getByLabel("E-posta").fill("e2e-valid-invite@example.invalid");
    await page.getByLabel("Davet kodu").fill(valid.token);
    await page.getByRole("button", { name: "Hesap oluştur" }).click();
    await waitForDashboard(page);
    await page.locator("#logout").click();
    await page.getByRole("heading", { name: "Kontrol sizde." }).waitFor({ state: "visible" });
    await page.locator("#toggle-auth").click();
    await page.getByLabel("E-posta").fill("e2e-reused-invite@example.invalid");
    await page.getByLabel("Parola").fill("phase-eleven-temporary-password");
    await page.getByLabel("Davet kodu").fill(valid.token);
    await page.getByRole("button", { name: "Hesap oluştur" }).click();
    await page.getByText("Davet kodu geçersiz, süresi dolmuş veya kullanılmış.").waitFor({ state: "visible" });
  } finally {
    await context.close();
  }
});

test("responsive layout and labeled controls at requested viewport widths", async () => {
  const { context, page } = await newPage();
  try {
    await register(page, "e2e-responsive@example.invalid");
    await waitForDashboard(page);
    for (const width of [320, 390, 768, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      for (const section of ["overview", "expenses", "cards", "income"]) {
        await page.locator(`[data-section="${section}"]`).click();
        const layout = await page.evaluate(() => ({ document: document.documentElement.scrollWidth, body: document.body.scrollWidth }));
        const overflow = await page.locator("*").evaluateAll(elements => elements
          .map(element => ({ element, bounds: element.getBoundingClientRect() }))
          .filter(({ bounds }) => bounds.right > window.innerWidth + 1 || bounds.left < -1)
          .slice(0, 8)
          .map(({ element, bounds }) => `${element.tagName.toLowerCase()}#${element.id}.${String(element.className).replace(/\s+/g, ".")}[${Math.round(bounds.left)}..${Math.round(bounds.right)}]`));
        assert.ok(layout.document <= width, `${section} document overflow at ${width}px: ${layout.document}; ${overflow.join(", ")}`);
        assert.ok(layout.body <= width, `${section} body overflow at ${width}px: ${layout.body}; ${overflow.join(", ")}`);
        const visibleControl = section === "expenses" ? page.locator("#new-expense") : section === "cards" ? page.locator('#card-form input[name="bank_name"]') : section === "income" ? page.locator('#income-form input[name="description"]') : page.locator(".stat.spend strong");
        const bounds = await visibleControl.boundingBox();
        assert.ok(bounds && bounds.x >= 0 && bounds.x + bounds.width <= width, `${section} control clipped at ${width}px`);
      }
    }
    const unlabeled = await page.locator("input:not([type=hidden])").evaluateAll(elements => elements.filter(element => !element.labels?.length && !element.getAttribute("aria-label")).map(element => element.outerHTML));
    assert.deepEqual(unlabeled, []);
  } finally {
    await context.close();
  }
});
