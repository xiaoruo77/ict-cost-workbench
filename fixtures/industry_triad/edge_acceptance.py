"""Run the synthetic triad through a fresh Microsoft Edge UI session.

No API client, page.evaluate, existing browser profile, storage-state file, HAR
or trace is used. Every mutation is an ordinary visible form/button action.
Exit 0: all checks passed; 1: a check failed; 2: an unavailable dependency
prevented verification. Existing accounts and production port 8765 are unused.

Example (use Windows Python on the machine with Edge):
  python edge_acceptance.py --base-url http://127.0.0.1:18774 \
      --fixtures C:/isolated/fixtures/industry_triad --evidence C:/isolated/edge-qa
The runner never installs dependencies or changes network/security settings.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlsplit
import uuid
import xml.etree.ElementTree as ET
import zipfile


INDUSTRIES = ("pharma", "restaurant", "medical_devices")
EXPORT_BUTTONS = {"json": "下载精确结果JSON", "md": "下载Markdown报告",
                  "docx": "导出Word", "pdf": "导出PDF"}


class Unverified(RuntimeError):
    pass


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def compact(value):
    return re.sub(r"\s+", "", str(value)).replace(",", "")


def decimal(value):
    return Decimal(str(value).replace(",", "").strip())


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def pdf_text(path):
    if importlib.util.find_spec("fitz") is not None:
        import fitz
        with fitz.open(path) as document:
            return "\n".join(page.get_text() for page in document)
    binary = shutil.which("pdftotext")
    if binary:
        completed = subprocess.run([binary, "-layout", str(path), "-"],
            capture_output=True, text=True, encoding="utf-8", timeout=60)
        require(completed.returncode == 0, "PDF text extraction failed")
        return completed.stdout
    raise Unverified("PDF已下载；未安装PyMuPDF或pdftotext，实际PDF内容未验证")


def document_text(path):
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    return "\n".join(node.text or "" for node in root.iter()
                     if node.tag == "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")


def assert_report_text(text, snapshot, expected, industry_label):
    normalized = compact(text)
    for value in (snapshot["analysis_id"], snapshot["revision"], industry_label,
                  expected["scope"]["period"]):
        require(compact(value) in normalized, "Export lost industry, period or exact revision: " + str(value))
    for field in ("total_cost", "asset_total", "pnl_cost", "period_expense"):
        require(compact(expected[field]) in normalized,
                "Export is missing independently expected " + field + "=" + expected[field])


def assert_snapshot(snapshot, expected, rules, identity):
    require((snapshot["analysis_id"], snapshot["revision"]) == identity, "Downloaded JSON identity differs from visible UI")
    require(snapshot["context"]["industry"] == rules["industry"], "Downloaded JSON industry mismatch")
    result = snapshot["result"]
    require(decimal(result["totals"]["total_cost"]) == decimal(expected["total_cost"]), "JSON total differs from independent oracle")
    require({key: decimal(value) for key, value in result["totals"]["cost_structure"].items()}
            == {key: decimal(value) for key, value in expected["cost_groups"].items()}, "JSON cost groups differ from independent oracle")
    for field in ("asset_total", "pnl_cost", "period_expense", "service_cost"):
        require(decimal(result["business_views"][field]) == decimal(expected[field]), "JSON business bridge mismatch: " + field)
    if "settlement" in expected:
        for key, value in expected["settlement"]["totals"].items():
            require(decimal(result["business_views"]["settlement"]["totals"][key]) == decimal(value), "JSON settlement mismatch: " + key)
    require(result["ledger_reconciled"] is True, "JSON ledger did not reconcile")
    originals = [item for item in snapshot["evidence"] if item["source_uri"].startswith("upload://sources/")]
    require(originals and all(item["status"] == "captured" and item["rows"] for item in originals), "Original references were not captured")


class EdgeAcceptance:
    def __init__(self, page, args, evidence, result, expect):
        self.page, self.args, self.evidence, self.result, self.expect = page, args, evidence, result, expect
        self.root = page.locator(".costing-page")

    def step(self, name, **details):
        self.result["steps"].append({"name": name, "status": "passed", **details})
        (self.evidence / "result.json").write_text(json.dumps(self.result, ensure_ascii=False, indent=2), encoding="utf-8")

    def register(self, username, password):
        self.page.goto(self.args.base_url.rstrip("/") + "/#login", wait_until="domcontentloaded")
        self.page.locator(".auth-tabs").get_by_role("button", name="注册", exact=True).click()
        form = self.page.locator(".authform")
        form.get_by_label("用户名", exact=True).fill(username)
        form.get_by_label("密码", exact=True).fill(password)
        form.get_by_label("确认密码", exact=True).fill(password)
        form.get_by_role("button", name="注册并登录", exact=True).click()
        self.expect(self.root).to_be_visible(timeout=self.args.timeout_ms)
        self.expect(self.page.locator(".who-name")).to_have_text(username)

    def logout_and_login(self, username, password):
        self.page.get_by_title("进入个人中心", exact=True).click()
        self.page.get_by_role("button", name="退出登录", exact=True).click()
        form = self.page.locator(".authform")
        self.expect(form).to_be_visible()
        form.get_by_label("用户名", exact=True).fill(username)
        form.get_by_label("密码", exact=True).fill(password)
        form.get_by_role("button", name="登录", exact=True).click()
        self.expect(self.root).to_be_visible(timeout=self.args.timeout_ms)
        self.expect(self.page.locator(".who-name")).to_have_text(username)
        self.step("synthetic_register_logout_login")

    def identity(self):
        paragraph = self.root.locator("p.costing-version").filter(has_text="分析编号")
        self.expect(paragraph).to_be_visible(timeout=self.args.timeout_ms)
        match = re.search(r"分析编号\s+([0-9a-f]{32})\s*·\s*版本\s+([0-9a-f]{64})", paragraph.inner_text())
        require(match is not None, "Visible analysis identity is invalid")
        return match[1], match[2]

    def assert_visible(self, rules, expected, label):
        head = self.root.locator(".costing-result-head")
        self.expect(head).to_contain_text(rules["industry"])
        self.expect(head).to_contain_text(label)
        self.expect(head).to_contain_text(expected["scope"]["period"])
        business = self.root.locator("section.costing-panel").filter(
            has=self.page.get_by_role("heading", name="成本、期间费用与资产桥接", exact=True))
        for title, field in [("本期发生额（含资产）", "total_cost"), ("本期发生额留存的生产资产", "asset_total"),
                             ("本期损益成本费用", "pnl_cost"), ("期间费用", "period_expense"), ("服务成本", "service_cost")]:
            card = business.locator(".costing-summary > div").filter(has_text=title)
            require(decimal(card.locator("strong").inner_text()) == decimal(expected[field]), "Visible amount mismatch: " + field)
        for oid, values in expected["objects"].items():
            card = self.root.locator("article.costing-object").filter(has=self.page.locator("p.muted").filter(
                has_text=re.compile(r"·\s*" + re.escape(oid) + r"$")))
            self.expect(card).to_have_count(1)
            require(decimal(card.locator(".costing-total").inner_text()) == decimal(values["total_cost"]), "Visible object cost mismatch: " + oid)

    def inspect_original(self, folder, output):
        section = self.root.locator("section.costing-panel").filter(
            has=self.page.get_by_role("heading", name=re.compile(r"^来源证据（")))
        row = section.locator("tbody > tr").filter(has_text="upload://sources/").first
        for _ in range(100):
            if row.count():
                break
            next_page = section.get_by_role("button", name="下页证据", exact=True)
            require(next_page.count() and next_page.is_enabled(), "No original source evidence is reachable in UI")
            next_page.click()
        require(row.count(), "Original evidence pagination limit reached")
        cells = row.locator("td")
        uri, selector = cells.nth(0).inner_text(), cells.nth(2).inner_text()
        match = re.fullmatch(r"upload://sources/([A-Za-z0-9_-]{1,40})\.csv", uri)
        require(match is not None, "Unexpected source URI")
        number = int(parse_qs(selector)["row"][0])
        with (folder / (match[1] + ".csv")).open(encoding="utf-8-sig", newline="") as stream:
            records = list(csv.DictReader(stream))
        require(2 <= number <= len(records) + 1, "UI source row outside original file")
        row.get_by_role("button", name="核对原始记录", exact=True).click()
        evidence = section.locator(".costing-evidence")
        self.expect(evidence.get_by_role("heading", level=4)).to_have_text(uri)
        self.expect(evidence).to_contain_text("CSV记录 " + str(number))
        shown = {r.locator("td").nth(0).inner_text(): r.locator("td").nth(1).inner_text()
                 for r in evidence.locator("tbody > tr").all()}
        require(shown == {key: value for key, value in records[number - 2].items()}, "Visible evidence values differ from original CSV row")
        evidence.scroll_into_view_if_needed()
        self.page.screenshot(path=str(output / "original-evidence.png"), full_page=False)
        return {"source_uri": uri, "record": number, "original_sha256": digest(folder / (match[1] + ".csv"))}

    def download_all(self, output, expected, rules, identity):
        output.mkdir(parents=True, exist_ok=True)
        snapshot, downloads = None, []
        for fmt, name in EXPORT_BUTTONS.items():
            button = self.root.get_by_role("button", name=name, exact=True)
            self.expect(button).to_be_enabled(timeout=self.args.timeout_ms)
            with self.page.expect_download(timeout=self.args.timeout_ms) as pending:
                button.click()
            download = pending.value
            require(download.failure() is None, "Browser download failed: " + fmt)
            path = output / ("report." + fmt)
            download.save_as(path)
            require(path.is_file() and path.stat().st_size > 0, "Download is empty: " + fmt)
            record = {"format": fmt, "file": str(path), "sha256": digest(path), "bytes": path.stat().st_size, "status": "passed"}
            if fmt == "json":
                snapshot = json.loads(path.read_text(encoding="utf-8"))
                assert_snapshot(snapshot, expected, rules, identity)
            else:
                try:
                    text = path.read_text(encoding="utf-8") if fmt == "md" else document_text(path) if fmt == "docx" else pdf_text(path)
                    (output / (fmt + "-extracted.txt")).write_text(text, encoding="utf-8")
                    assert_report_text(text, snapshot, expected, rules["industry"])
                except Unverified as exc:
                    record.update(status="unverified", reason=str(exc))
                    self.result["unverified"].append(str(exc))
            downloads.append(record)
        return snapshot, downloads

    def import_industry(self, industry, run):
        folder = self.args.fixtures / industry
        if self.args.variant == "large":
            folder /= "large"
        rules = json.loads((folder / "rules.json").read_text(encoding="utf-8"))
        expected = json.loads((folder / "expected.json").read_text(encoding="utf-8"))
        require(expected["industry"] == industry and "independent" in expected["oracle"], "Use the independent synthetic triad fixtures")
        output = self.evidence / industry
        output.mkdir(parents=True, exist_ok=True)
        label = rules["industry"] + " Edge合成验收 " + run
        panel = self.root.locator("details.costing-panel").filter(has=self.page.locator("summary").filter(has_text="导入自己的成本数据"))
        if panel.get_attribute("open") is None:
            panel.locator("summary").first.click()
        panel.get_by_label("数据集名称", exact=True).fill(label)
        panel.locator('input[type="file"][multiple]').set_input_files([str(path) for path in sorted(folder.glob("*.csv"))])
        panel.locator('input[type="file"][accept*=".json"]').set_input_files(str(folder / "rules.json"))
        self.expect(panel.get_by_text("正在使用规则：rules.json", exact=True)).to_be_visible()
        panel.get_by_role("button", name="校验并导入数据集", exact=True).click()
        imported = self.identity()
        self.assert_visible(rules, expected, label)
        run_button = self.root.get_by_role("button", name="运行成本分析", exact=True)
        self.expect(run_button).to_be_enabled()
        run_button.click()
        self.expect(self.root.locator("p.costing-version").filter(has_text="分析编号")).not_to_contain_text(
            imported[0], timeout=self.args.timeout_ms)
        identity = self.identity()
        require(identity[1] == imported[1], "UI rerun changed the imported calculation revision")
        self.assert_visible(rules, expected, label)
        source = self.inspect_original(folder, output)
        save = self.root.get_by_role("button", name="保存当前报告", exact=True)
        self.expect(save).to_be_enabled()
        save.click()
        self.expect(self.root.get_by_text("报告已保存，版本与当前计算一致，可从「已保存报告」重新打开。", exact=True)).to_be_visible(timeout=self.args.timeout_ms)
        self.expect(self.root.locator(".costing-saved select option[value='" + identity[0] + "']")).to_have_count(1)
        self.page.screenshot(path=str(output / "saved-result.png"), full_page=True)
        snapshot, downloads = self.download_all(output, expected, rules, identity)
        receipt = {"industry": industry, "identity": identity, "imported_identity": imported, "source": source,
                   "expected_total": expected["total_cost"], "downloads": downloads,
                   "input_files": [{"file": str(path), "sha256": digest(path)} for path in sorted(folder.glob("*.csv"))]}
        self.result["industries"].append(receipt)
        self.step("ui_import_run_evidence_save_export", industry=industry, analysis_id=identity[0], revision=identity[1])
        return {"snapshot": snapshot, "rules": rules, "expected": expected, "label": label, "folder": folder, "identity": identity}

    def reopen_first(self, first):
        select = self.root.locator(".costing-saved select")
        self.expect(select).to_be_enabled(timeout=self.args.timeout_ms)
        select.select_option(first["identity"][0])
        self.expect(self.root.locator("p.costing-version").filter(has_text="分析编号")).to_contain_text(first["identity"][0], timeout=self.args.timeout_ms)
        require(self.identity() == first["identity"], "Reopening the first industry returned another revision")
        self.assert_visible(first["rules"], first["expected"], first["label"])
        output = self.evidence / "pharma-reopened"
        output.mkdir(parents=True, exist_ok=True)
        self.inspect_original(first["folder"], output)
        reopened, downloads = self.download_all(output, first["expected"], first["rules"], first["identity"])
        require(reopened == first["snapshot"], "First snapshot changed after cross-industry switching")
        self.page.screenshot(path=str(output / "reopened-result.png"), full_page=True)
        self.result["reopened"] = {"analysis_id": reopened["analysis_id"], "revision": reopened["revision"], "downloads": downloads}
        self.step("same_session_pharma_restaurant_devices_pharma_revision_preserved")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--variant", choices=("small", "large"), default="small")
    parser.add_argument("--headless", action="store_true", help="Use Edge headless; default opens a fresh visible Edge window")
    parser.add_argument("--timeout-ms", type=int, default=240000)
    args = parser.parse_args()
    url = urlsplit(args.base_url)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.port == 8765:
        parser.error("Provide the isolated candidate URL; production port 8765 and URL credentials are refused")
    args.fixtures = args.fixtures.resolve()
    run = uuid.uuid4().hex[:12]
    evidence = args.evidence.resolve() / ("edge-" + run)
    evidence.mkdir(parents=True, exist_ok=False)
    result = {"status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
              "base_url": args.base_url, "browser_channel": "msedge", "variant": args.variant,
              "uses_existing_profile": False, "steps": [], "industries": [], "unverified": [],
              "console_errors": [], "page_errors": [], "failed_requests": []}
    password = secrets.token_urlsafe(24)
    username = "triad_edge_" + run
    started = time.perf_counter()
    exit_code = 1
    page = None
    try:
        try:
            from playwright.sync_api import sync_playwright, expect
        except ImportError as exc:
            raise Unverified("Python Playwright unavailable; Edge UI steps were not run") from exc
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(channel="msedge", headless=args.headless)
            except Exception as exc:
                raise Unverified("Microsoft Edge launch unavailable; no alternative browser was used: " + str(exc)) from exc
            result["browser_version"] = browser.version
            context = browser.new_context(accept_downloads=True, viewport={"width": 1440, "height": 1050})
            try:
                page = context.new_page()
                page.set_default_timeout(args.timeout_ms)
                page.on("console", lambda message: result["console_errors"].append(message.text) if message.type == "error" else None)
                page.on("pageerror", lambda error: result["page_errors"].append(str(error)))
                page.on("requestfailed", lambda request: result["failed_requests"].append({"url": request.url, "failure": request.failure}))
                driver = EdgeAcceptance(page, args, evidence, result, expect)
                driver.register(username, password)
                driver.logout_and_login(username, password)
                retained = [driver.import_industry(industry, run) for industry in INDUSTRIES]
                driver.reopen_first(retained[0])
                # A second fresh UI account must not reopen Alice's snapshot.
                # The original context remains logged in throughout the triad
                # switch sequence above; no cookie/storage state is copied.
                other_context = browser.new_context(accept_downloads=True,
                    viewport={"width": 1440, "height": 1050})
                try:
                    other_page = other_context.new_page()
                    other_page.set_default_timeout(args.timeout_ms)
                    other_page.on("console", lambda message: result["console_errors"].append(message.text)
                                  if message.type == "error" else None)
                    other_page.on("pageerror", lambda error: result["page_errors"].append(str(error)))
                    other = EdgeAcceptance(other_page, args, evidence, result, expect)
                    other.register("triad_other_" + run, password)
                    analysis_id, revision = retained[0]["identity"]
                    endpoint = "/api/platform/costing-snapshots/" + analysis_id
                    with other_page.expect_response(lambda item: item.request.method == "GET" and
                            urlsplit(item.url).path == endpoint, timeout=args.timeout_ms) as pending:
                        other_page.goto(args.base_url.rstrip("/") + "/#costing?analysis=" + analysis_id +
                                        "&revision=" + revision, wait_until="domcontentloaded")
                    denied = pending.value
                    require(denied.status == 404, "Cross-account browser request returned " + str(denied.status))
                    require(parse_qs(urlsplit(denied.url).query).get("revision") == [revision],
                            "Browser request did not pin the attempted snapshot revision")
                    require(set(denied.json()) == {"detail"}, "Denied snapshot response exposed extra data")
                    expect(other.root.locator('[role="alert"]')).to_contain_text(
                        re.compile(r"不存在|不属于当前"), timeout=args.timeout_ms)
                    expect(other.root.locator(".costing-result-head")).to_have_count(0)
                    expect(other.root.locator(".costing-evidence, article.costing-object")).to_have_count(0)
                    other_page.screenshot(path=str(evidence / "cross-account-denied.png"), full_page=True)
                    driver.step("second_synthetic_ui_account_cannot_open_first_snapshot",
                                request_method="GET", request_path=endpoint, revision=revision, http_status=denied.status)
                finally:
                    other_context.close()
                require(not result["page_errors"], "Browser emitted uncaught JavaScript errors; see evidence")
                result["status"] = "unverified" if result["unverified"] else "passed"
                exit_code = 2 if result["unverified"] else 0
            except Exception:
                if page and not page.is_closed():
                    try:
                        page.screenshot(path=str(evidence / "failure.png"), full_page=True)
                        (evidence / "failure-visible-text.txt").write_text(page.locator("body").inner_text(), encoding="utf-8")
                    except Exception as artifact_error:
                        result["failure_capture_error"] = str(artifact_error)
                raise
            finally:
                context.close()
                browser.close()
    except Unverified as exc:
        result.update(status="unverified", error=str(exc))
        exit_code = 2
    except Exception as exc:
        result.update(status="failed", error=type(exc).__name__ + ": " + str(exc))
        exit_code = 1
    finally:
        result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        # Neither passwords, cookies nor authorization headers are persisted.
        serialized = json.dumps(result, ensure_ascii=False, indent=2).replace(password, "[redacted]")
        (evidence / "result.json").write_text(serialized, encoding="utf-8")
        print(json.dumps({"status": result["status"], "evidence": str(evidence), "exit_code": exit_code}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
